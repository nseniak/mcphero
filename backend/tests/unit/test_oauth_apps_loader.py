"""Priority rules for ``load_oauth_apps``: inline env var beats file,
empty input yields empty config, malformed input is swallowed."""
from __future__ import annotations

import json
from pathlib import Path

from mcpolis.adapters.repositories.oauth_apps_loader import load_oauth_apps


VALID_INLINE = '{"githubcopilot.com":{"client_id":"id-from-env","client_secret":"sec-from-env"}}'
VALID_FILE = {
    "githubcopilot.com": {
        "client_id": "id-from-file",
        "client_secret": "sec-from-file",
    }
}


def test_returns_empty_when_nothing_configured(tmp_path: Path) -> None:
    config = load_oauth_apps(
        inline_json="", file_path=tmp_path / "missing.json",
    )
    assert config == {}


def test_loads_from_file_when_inline_empty(tmp_path: Path) -> None:
    path = tmp_path / "oauth_apps.json"
    path.write_text(json.dumps(VALID_FILE))

    config = load_oauth_apps(inline_json="", file_path=path)
    assert config["githubcopilot.com"].client_id == "id-from-file"


def test_inline_json_takes_precedence_over_file(tmp_path: Path) -> None:
    path = tmp_path / "oauth_apps.json"
    path.write_text(json.dumps(VALID_FILE))

    config = load_oauth_apps(inline_json=VALID_INLINE, file_path=path)
    assert config["githubcopilot.com"].client_id == "id-from-env"


def test_whitespace_only_inline_falls_back_to_file(tmp_path: Path) -> None:
    path = tmp_path / "oauth_apps.json"
    path.write_text(json.dumps(VALID_FILE))

    config = load_oauth_apps(inline_json="   \n  ", file_path=path)
    assert config["githubcopilot.com"].client_id == "id-from-file"


def test_malformed_inline_returns_empty(tmp_path: Path) -> None:
    # Bad JSON must not crash app startup — loader swallows and returns {}.
    config = load_oauth_apps(
        inline_json="{not json", file_path=tmp_path / "missing.json",
    )
    assert config == {}


def test_malformed_file_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "oauth_apps.json"
    path.write_text("{broken")
    config = load_oauth_apps(inline_json="", file_path=path)
    assert config == {}


def test_inline_wrong_shape_returns_empty(tmp_path: Path) -> None:
    # JSON parses but doesn't match OAuthAppsConfig schema.
    config = load_oauth_apps(
        inline_json='{"githubcopilot.com": "not-an-object"}',
        file_path=tmp_path / "missing.json",
    )
    assert config == {}
