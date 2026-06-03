from __future__ import annotations

import asyncio
from typing import Any

import httpx
import structlog
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from mcpolis.adapters.upstream_clients.connection_task_base import (
    ConnectionTaskBase,
)
from mcpolis.adapters.upstream_clients.notification_handler import (
    OnPromptListChanged,
    OnResourceListChanged,
    OnToolListChanged,
    build_tool_change_message_handler,
)
from mcpolis.adapters.upstream_clients.safe_http_transport import (
    SafeAsyncHTTPTransport,
)
from mcpolis.domain.model.upstream import (
    HttpTransportConfig,
    ServerInfo,
    UpstreamDefinition,
    UpstreamSelfDescription,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

INIT_TIMEOUT = 30.0
CLOSE_TIMEOUT = 10.0


class HttpConnectionTask(ConnectionTaskBase):
    """Manages an HTTP MCP connection in a dedicated asyncio.Task.

    The background task owns the full context manager stack
    (streamablehttp_client, ClientSession), so teardown always happens
    in the same task that created it.
    """

    def __init__(
        self,
        upstream: UpstreamDefinition,
        user_id: str,
        bearer_token: str | None = None,
        auth: httpx.Auth | None = None,
        on_tool_list_changed: OnToolListChanged | None = None,
        on_resource_list_changed: OnResourceListChanged | None = None,
        on_prompt_list_changed: OnPromptListChanged | None = None,
    ) -> None:
        super().__init__(
            upstream,
            user_id,
            bearer_token=bearer_token,
            on_tool_list_changed=on_tool_list_changed,
            on_resource_list_changed=on_resource_list_changed,
            on_prompt_list_changed=on_prompt_list_changed,
        )
        # http-only: optional ``httpx.Auth`` instance (e.g. an
        # OAuth2-aware adapter that refreshes tokens on 401).
        self._auth = auth

    def _log_extras(self) -> dict[str, str]:
        return {"transport": "streamable_http"}

    def _close_timeout_event_name(self) -> str:
        return "upstream.http.close.timeout_cancelling"

    def _close_timeout_seconds(self) -> float:
        return CLOSE_TIMEOUT

    async def _run(self) -> None:
        """Background task that owns the HTTP connection lifecycle."""
        assert self._upstream.http is not None
        cfg: HttpTransportConfig = self._upstream.http

        headers = dict(cfg.headers)
        if self._auth is None:
            token = self._bearer_token or self._upstream.auth.token
            if token:
                headers["Authorization"] = f"Bearer {token}"

        try:
            http_client = httpx.AsyncClient(
                headers=headers, auth=self._auth, timeout=INIT_TIMEOUT,
                transport=SafeAsyncHTTPTransport(),
            )
            async with streamable_http_client(
                url=cfg.url, http_client=http_client,
            ) as (
                read_stream,
                write_stream,
                _get_session_id,
            ):
                session = ClientSession(
                    read_stream,
                    write_stream,
                    message_handler=build_tool_change_message_handler(
                        self._upstream.id,
                        self._on_tool_list_changed,
                        self._on_resource_list_changed,
                        self._on_prompt_list_changed,
                    ),
                )
                async with session:
                    try:
                        init_result = await asyncio.wait_for(
                            session.initialize(), timeout=INIT_TIMEOUT
                        )
                    except Exception as exc:
                        self._session_future.set_exception(exc)
                        return

                    si = init_result.serverInfo
                    self.server_info = ServerInfo(
                        name=si.name, version=si.version, title=si.title
                    )
                    # ``Implementation`` allows extras (extra="allow"),
                    # so a server-attached ``description`` field — Notion
                    # fills one in — surfaces here via getattr.
                    # ``ServerCapabilities`` likewise allows extras, which
                    # is how MCP-Apps servers advertise the
                    # ``io.modelcontextprotocol/ui`` extension. Pull it
                    # off via getattr so the gateway can re-advertise.
                    raw_ext: object = getattr(
                        init_result.capabilities, "extensions", None,
                    )
                    capabilities_extensions: dict[str, dict[str, Any]] = {}
                    if isinstance(raw_ext, dict):
                        for ext_key, ext_val in raw_ext.items():  # type: ignore[reportUnknownVariableType]
                            if isinstance(ext_key, str) and isinstance(ext_val, dict):
                                capabilities_extensions[ext_key] = dict(ext_val)  # type: ignore[reportUnknownArgumentType]
                    self.self_description = UpstreamSelfDescription(
                        name=si.name,
                        version=si.version,
                        instructions=init_result.instructions,
                        description=getattr(si, "description", None),
                        website_url=si.websiteUrl,
                        capabilities_extensions=capabilities_extensions,
                    )
                    logger.info(
                        "upstream.http.connection.established",
                        upstream_id=self._upstream.id,
                    )
                    self._session_future.set_result(session)

                    # Block until shutdown is requested
                    await self._shutdown_event.wait()

        except Exception as exc:
            if not self._session_future.done():
                self._session_future.set_exception(exc)
