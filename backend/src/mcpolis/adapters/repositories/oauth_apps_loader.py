"""Resolve instance-level OAuth app credentials.

Two sources are supported, in priority order:

1. ``MCPOLIS_OAUTH_APPS_JSON`` (inline JSON string) — for Docker and CI
   deployments that ship secrets via env vars.
2. ``MCPOLIS_OAUTH_APPS_PATH`` (JSON file on disk) — for native / bind-
   mount deployments.

If both are set, the inline JSON wins and the file is ignored. Empty or
invalid input returns an empty config; callers treat that as "no
upstream OAuth apps registered".
"""
from __future__ import annotations

import json
from pathlib import Path

import structlog
from pydantic import TypeAdapter

from mcpolis.domain.model.settings import OAuthAppsConfig

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_adapter = TypeAdapter(OAuthAppsConfig)


def load_oauth_apps(
    *, inline_json: str, file_path: Path
) -> OAuthAppsConfig:
    if inline_json.strip():
        try:
            config = _adapter.validate_python(json.loads(inline_json))
            logger.info(
                "oauth_apps.loaded.from_env",
                entries_count=len(config),
            )
            return config
        except Exception:
            logger.warning(
                "oauth_apps.load.from_env.failed",
                exc_info=True,
            )
            return {}

    if file_path.exists():
        try:
            config = _adapter.validate_python(json.loads(file_path.read_text()))
            logger.info(
                "oauth_apps.loaded.from_file",
                entries_count=len(config),
                path=str(file_path),
            )
            return config
        except Exception:
            logger.warning(
                "oauth_apps.load.from_file.failed",
                path=str(file_path),
                exc_info=True,
            )
            return {}

    return {}
