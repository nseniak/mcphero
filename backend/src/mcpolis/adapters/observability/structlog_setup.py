"""Structured logging setup.

Every log record is rendered as a single JSON object whose top-level keys
are searchable in downstream log backends. The first positional arg to
``log.info()``/``log.warning()``/... is the event name (stable, dotted,
snake_case) and becomes the ``event`` field; keyword args become top-level
fields.

The stdlib ``logging`` module is routed through the same processor chain
so records emitted by third-party libraries (uvicorn, httpx, mcp SDK) land
in the same JSON stream.

Per-request context (``request_id``, ``path``, ``method``, ``org_id``,
``user``, ...) is bound via ``structlog.contextvars`` and automatically
merged into every record emitted during the request.
"""
from __future__ import annotations

import logging
import sys

import structlog
from structlog.types import Processor

from mcpolis.adapters.observability.redact_processor import redact_secret_keys


class _HealthProbeFilter(logging.Filter):
    """Drop uvicorn access records for ``GET /health`` / ``/healthz`` probes.

    Applied to the stdlib ``uvicorn.access`` logger so records are
    suppressed *before* reaching structlog's ``ProcessorFormatter`` —
    raising ``structlog.DropEvent`` inside a ``foreign_pre_chain``
    processor propagates out of ``logging.emit()`` as an uncaught
    exception instead of cleanly dropping the record.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        return "GET /health " not in message and "GET /healthz " not in message


def configure_structlog(*, json_logs: bool = True, log_level: str = "INFO") -> None:
    """Configure structlog + stdlib logging to emit structured records.

    Call this once at process start, before any module imports a logger.
    Safe to call multiple times (idempotent at the structlog layer).

    Args:
        json_logs: When True (the default), emit JSON lines — the right
            choice for any deployed environment. When False, use the
            human-readable ``ConsoleRenderer`` (nicer for local dev).
        log_level: Root log level (``DEBUG``/``INFO``/``WARNING``/...).
    """
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    # Shared processors run on *every* record, whether it originated from
    # a structlog ``log.info(...)`` call or from a stdlib ``logger.info(...)``
    # call piped through ``ProcessorFormatter``. ``redact_secret_keys`` is
    # last so it sees the fully merged event dict (contextvars + kwargs +
    # stdlib LogRecord extras) before the renderer turns it into JSON.
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        redact_secret_keys,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            # Prepare the event dict for ProcessorFormatter so stdlib
            # records and structlog records share the same rendering path.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[log_level],
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    renderer: Processor
    if json_logs:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            # Strip the internal ``_record`` key that ProcessorFormatter
            # tacks on before handing off to the renderer.
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # Replace any handlers previously installed by ``logging.basicConfig``
    # or a prior call to this function — we want exactly one JSON handler.
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(log_level)

    # Quiet down chatty third-party loggers. Same list the stdlib
    # ``_configure_logging`` used to maintain.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("mcp.client.streamable_http").setLevel(logging.WARNING)
    # Drop /health probe access logs; keep 4xx/5xx for real requests.
    # Idempotent: remove any existing instance before adding so repeated
    # ``configure_structlog()`` calls don't stack filters.
    access_logger = logging.getLogger("uvicorn.access")
    for existing_filter in list(access_logger.filters):
        if isinstance(existing_filter, _HealthProbeFilter):
            access_logger.removeFilter(existing_filter)
    access_logger.addFilter(_HealthProbeFilter())
