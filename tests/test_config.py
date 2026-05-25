from __future__ import annotations

from navi.config import load_config, write_default_config
from navi.telegram.config import load_telegram_config
from navi.weixin.config import load_weixin_config


def test_default_config_round_trip(tmp_path):
    path = write_default_config(tmp_path)

    config = load_config(tmp_path)

    assert path.exists()
    assert config.model.provider == "mock"
    assert not hasattr(config, "weixin")
    assert config.runtime.service_name == "navi.service"
    assert config.runtime.agent_step_budget == 8
    assert config.execution.provider == "navi"


def test_connector_env_file_overrides_weixin(tmp_path):
    write_default_config(tmp_path)
    (tmp_path / "env").write_text(
        "\n".join(
            [
                "WEIXIN_ACCOUNT_ID=acct",
                "WEIXIN_TOKEN=token",
                "WEIXIN_ALLOWED_USERS=user_a,user_b",
                "WEIXIN_DM_POLICY=allowlist",
            ]
        ),
        encoding="utf-8",
    )

    config = load_weixin_config(tmp_path)

    assert config.account_id == "acct"
    assert config.token == "token"
    assert config.dm_policy == "allowlist"
    assert config.allowed_users == ["user_a", "user_b"]


def test_connector_env_file_overrides_telegram(tmp_path):
    write_default_config(tmp_path)
    (tmp_path / "env").write_text(
        "\n".join(
            [
                "NAVI_TELEGRAM_ENABLED=true",
                "TELEGRAM_BOT_TOKEN=token",
                "TELEGRAM_ALLOWED_USERS=123,456",
                "TELEGRAM_DM_POLICY=allowlist",
                "TELEGRAM_HOME_CHAT_ID=123",
            ]
        ),
        encoding="utf-8",
    )

    config = load_telegram_config(tmp_path)

    assert config.enabled is True
    assert config.bot_token == "token"
    assert config.dm_policy == "allowlist"
    assert config.allowed_users == ["123", "456"]
    assert config.home_chat_id == "123"


def test_env_file_overrides_runtime_facts(tmp_path):
    write_default_config(tmp_path)
    (tmp_path / "env").write_text(
        "\n".join(
            [
                "NAVI_SERVICE_NAME=custom.service",
                "NAVI_WEB_URL=http://navi.example",
                "NAVI_AGENT_STEP_BUDGET=12",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert config.runtime.service_name == "custom.service"
    assert config.runtime.web_url == "http://navi.example"
    assert config.runtime.agent_step_budget == 12


def test_env_file_overrides_execution_facts(tmp_path):
    write_default_config(tmp_path)
    (tmp_path / "env").write_text(
        "\n".join(
            [
                "NAVI_EXECUTION_PROVIDER=codex",
                "NAVI_EXECUTION_TIMEOUT_SECONDS=9",
                "NAVI_EXECUTION_MOCK=true",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert config.execution.provider == "navi"
    assert config.execution.timeout_seconds == 9
    assert config.execution.mock is True


def test_deepseek_env_defaults(tmp_path):
    write_default_config(tmp_path)
    (tmp_path / "env").write_text(
        "\n".join(
            [
                "NAVI_MODEL_PROVIDER=deepseek",
                "DEEPSEEK_API_KEY=sk-test",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert config.model.provider == "deepseek"
    assert config.model.model == "deepseek-v4-pro"
    assert config.model.api_base_url == "https://api.deepseek.com"
    assert config.model.api_key == "sk-test"


def test_custom_model_provider_can_be_declared_without_package_spec(tmp_path):
    write_default_config(tmp_path)
    (tmp_path / "config.yaml").write_text(
        "\n".join(
            [
                "model:",
                "  provider: private-gateway",
                "  kind: openai-compatible",
                "  model: local-agent-model",
                "  api_base_url: http://localhost:11434/v1",
                "  api_key_env:",
                "    - PRIVATE_GATEWAY_API_KEY",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "env").write_text("PRIVATE_GATEWAY_API_KEY=sk-local", encoding="utf-8")

    config = load_config(tmp_path)

    assert config.model.provider == "private-gateway"
    assert config.model.kind == "openai-compatible"
    assert config.model.model == "local-agent-model"
    assert config.model.api_base_url == "http://localhost:11434/v1"
    assert config.model.api_key == "sk-local"


def test_model_fallbacks_are_loaded_from_config(tmp_path):
    write_default_config(tmp_path)
    (tmp_path / "config.yaml").write_text(
        "\n".join(
            [
                "model:",
                "  provider: private-primary",
                "  kind: openai-compatible",
                "  model: primary-model",
                "  api_base_url: http://primary.local/v1",
                "  fallbacks:",
                "    - provider: mock",
                "      model: mock",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert config.model.provider == "private-primary"
    assert config.model.fallbacks[0].provider == "mock"


def test_model_routes_are_loaded_from_config(tmp_path):
    write_default_config(tmp_path)
    (tmp_path / "config.yaml").write_text(
        "\n".join(
            [
                "model:",
                "  provider: mock",
                "  model: mock",
                "  routes:",
                "    planner:",
                "      provider: private-planner",
                "      kind: openai-compatible",
                "      model: planner-model",
                "      api_base_url: http://planner.local/v1",
                "      fallbacks:",
                "        - provider: mock",
                "          model: mock",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert config.model.routes["planner"].provider == "private-planner"
    assert config.model.routes["planner"].fallbacks[0].provider == "mock"
