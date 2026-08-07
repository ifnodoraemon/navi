from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from navi.app_factory import build_runtime
from navi.cli import app
from navi.config import load_config, validate_config, write_default_config
from navi.telegram.config import load_telegram_config


def _write_config(home: Path, payload: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )


def test_default_config_contains_every_global_section_and_is_private(tmp_path: Path) -> None:
    path = write_default_config(tmp_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert list(raw) == [
        "model",
        "runtime",
        "execution",
        "api",
        "search",
        "connectors",
        "mcp",
    ]
    assert raw["api"]["api_key"]
    assert raw["mcp"]["servers"]["exa"]["url"] == "https://mcp.exa.ai/mcp"
    assert raw["search"]["providers"]["exa"]["kind"] == "exa_mcp"
    assert raw["search"]["providers"]["searxng"]["enabled"] is False
    assert raw["search"]["providers"]["searxng"]["allow_private_network"] is False
    assert raw["search"]["providers"]["x"] == {
        "kind": "x_api",
        "enabled": False,
        "endpoint": "https://api.x.com",
        "bearer_token": "",
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_runtime_construction_fails_closed_on_invalid_config(tmp_path: Path) -> None:
    write_default_config(tmp_path)

    with pytest.raises(
        ValueError,
        match=r"invalid Navi configuration: .*model\.api_key is required",
    ):
        build_runtime(tmp_path)


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        (
            {"api_key": "model-key", "response_transport": "automatic"},
            "response_transport 'automatic' is unsupported",
        ),
        (
            {
                "provider": "anthropic",
                "api_key": "model-key",
                "response_transport": "sse",
            },
            "response_transport 'sse' requires kind 'openai-compatible'",
        ),
        (
            {
                "api_key": "model-key",
                "request_options": {"stream": True},
            },
            "request_options cannot override runtime fields: stream",
        ),
    ],
)
def test_model_response_transport_is_explicit_and_validated(
    tmp_path: Path, model: dict, expected: str
) -> None:
    _write_config(tmp_path, {"model": model, "api": {"api_key": "api-key"}})

    config = load_config(tmp_path)

    assert any(expected in error for error in validate_config(config, tmp_path))


def test_search_private_network_opt_in_must_be_boolean(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {
            "search": {
                "providers": {
                    "local": {
                        "kind": "searxng",
                        "endpoint": "http://127.0.0.1:8888",
                        "allow_private_network": "yes",
                    }
                }
            }
        },
    )

    with pytest.raises(ValueError, match="allow_private_network must be a boolean"):
        load_config(tmp_path)


def test_process_environment_does_not_override_global_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config(
        tmp_path,
        {
            "model": {
                "provider": "openai-compatible",
                "model": "configured-model",
                "api_key": "configured-model-key",
            },
            "api": {"api_key": "configured-api-key"},
            "connectors": {"telegram": {"enabled": True, "bot_token": "configured-bot-token"}},
        },
    )
    monkeypatch.setenv("NAVI_MODEL_API_KEY", "environment-model-key")
    monkeypatch.setenv("NAVI_API_KEY", "environment-api-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "environment-bot-token")

    config = load_config(tmp_path)
    telegram = load_telegram_config(tmp_path)

    assert config.model.api_key == "configured-model-key"
    assert config.api.api_key == "configured-api-key"
    assert telegram.bot_token == "configured-bot-token"


@pytest.mark.parametrize("legacy_name", ["env", "mcp.json", "api_key"])
def test_legacy_config_files_are_rejected(tmp_path: Path, legacy_name: str) -> None:
    write_default_config(tmp_path)
    (tmp_path / legacy_name).write_text("legacy", encoding="utf-8")

    with pytest.raises(ValueError, match="legacy configuration files are unsupported"):
        load_config(tmp_path)


def test_invalid_search_reference_fails_validation(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {
            "model": {"api_key": "model-key"},
            "api": {"api_key": "api-key"},
            "search": {
                "providers": {
                    "exa": {
                        "kind": "exa_mcp",
                        "mcp_server": "missing",
                    }
                }
            },
            "mcp": {"servers": {}},
        },
    )
    config = load_config(tmp_path)

    assert (
        "search.providers.exa.mcp_server 'missing' is not an enabled MCP server"
        in validate_config(config, tmp_path)
    )


def test_legacy_flat_search_config_is_rejected(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {
            "search": {
                "provider": "exa_mcp",
                "mcp_server": "exa",
            }
        },
    )

    with pytest.raises(ValueError, match="search has unsupported fields"):
        load_config(tmp_path)


def test_enabled_x_provider_requires_bearer_token(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {
            "search": {
                "providers": {
                    "x": {
                        "kind": "x_api",
                        "enabled": True,
                        "endpoint": "https://api.x.com",
                    }
                }
            }
        },
    )
    config = load_config(tmp_path)

    assert (
        "search.providers.x.bearer_token is required when enabled"
        in validate_config(config, tmp_path)
    )


def test_config_command_redacts_all_secrets(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {
            "model": {"api_key": "model-secret"},
            "api": {"api_key": "api-secret"},
            "connectors": {"telegram": {"bot_token": "telegram-secret"}},
            "search": {
                "providers": {
                    "x": {
                        "kind": "x_api",
                        "enabled": False,
                        "endpoint": "https://api.x.com",
                        "bearer_token": "x-secret",
                    }
                }
            },
            "mcp": {
                "servers": {
                    "exa": {
                        "url": "https://mcp.exa.ai/mcp",
                        "headers": {"authorization": "mcp-secret"},
                        "tool_permissions": {"web_search_exa": "network"},
                    }
                }
            },
        },
    )

    result = CliRunner().invoke(app, ["config"], env={"NAVI_HOME": str(tmp_path)})

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["path"] == str(tmp_path / "config.yaml")
    assert payload["bootstrap_environment"] == ["NAVI_HOME"]
    assert "model-secret" not in result.output
    assert "api-secret" not in result.output
    assert "telegram-secret" not in result.output
    assert "x-secret" not in result.output
    assert "mcp-secret" not in result.output
    assert result.output.count("[REDACTED]") >= 5
