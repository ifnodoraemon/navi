from __future__ import annotations

from navi.config import load_config, write_default_config


def test_default_config_round_trip(tmp_path):
    path = write_default_config(tmp_path)

    config = load_config(tmp_path)

    assert path.exists()
    assert config.model.provider == "mock"
    assert config.weixin.group_policy == "disabled"


def test_env_file_overrides_weixin(tmp_path):
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

    config = load_config(tmp_path)

    assert config.weixin.account_id == "acct"
    assert config.weixin.token == "token"
    assert config.weixin.dm_policy == "allowlist"
    assert config.weixin.allowed_users == ["user_a", "user_b"]


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
