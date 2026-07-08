from __future__ import annotations

import sys

from navi.control import CurrentStateBuilder, SurfaceContext, current_state_facts
from navi.harness import Harness, HarnessCommand
from navi.loop_contracts import TimeoutPolicy, VaultHandle
from navi.vault import VaultStore


def test_vault_store_persists_handles_without_prompt_secret_values(tmp_path) -> None:
    handle = VaultHandle(
        uri="secret://github/default-token",
        purpose="github api",
        env_var="GITHUB_TOKEN",
    )
    VaultStore(tmp_path).put(handle, "secret-token-value")

    handles = VaultStore(tmp_path).list_handles()

    assert handles[0].to_prompt_dict() == {
        "handle": "secret://github/default-token",
        "purpose": "github api",
        "env_var": "GITHUB_TOKEN",
    }
    assert "secret-token-value" not in repr(handles)


def test_harness_home_uses_persistent_vault_and_redacts_secret_output(tmp_path) -> None:
    handle = VaultHandle(
        uri="secret://test/api-token",
        purpose="test token",
        env_var="NAVI_TEST_SECRET",
    )
    VaultStore(tmp_path).put(handle, "persistent-secret")
    harness = Harness(home=tmp_path)

    result = harness.run_command(
        HarnessCommand(
            command=(sys.executable, "-c", "import os; print(os.environ['NAVI_TEST_SECRET'])"),
            cwd=tmp_path,
            timeout=TimeoutPolicy(seconds=5),
            vault_handles=(handle,),
        )
    )

    assert result.ok is True
    assert "persistent-secret" not in result.stdout
    assert "[REDACTED]" in result.stdout
    assert "persistent-secret" not in repr(result.to_facts())


def test_current_state_exposes_vault_handles_only(tmp_path) -> None:
    handle = VaultHandle(
        uri="secret://deploy/prod-key",
        purpose="deployment key",
        env_var="DEPLOY_KEY",
    )
    VaultStore(tmp_path).put(handle, "deploy-secret-value")

    facts = current_state_facts(
        CurrentStateBuilder(tmp_path).build(
            SurfaceContext(
                home=tmp_path,
                source="cli",
                peer_id="cli",
                sender_id="tester",
                workspace=str(tmp_path),
            )
        )
    )

    assert facts["vault_handle_state"] == [
        {
            "handle": "secret://deploy/prod-key",
            "purpose": "deployment key",
            "env_var": "DEPLOY_KEY",
        }
    ]
    assert "deploy-secret-value" not in repr(facts)
