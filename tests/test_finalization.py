from __future__ import annotations

import pytest

from navi.finalization import synthesize_user_reply_from_facts


class _FailingRuntime:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, *, role: str) -> str:
        del messages
        self.calls += 1
        assert role == "responder"
        raise RuntimeError("responder unavailable")


@pytest.mark.asyncio
async def test_fact_response_propagates_model_failure_without_empty_text_substitution():
    runtime = _FailingRuntime()

    with pytest.raises(RuntimeError, match="responder unavailable"):
        await synthesize_user_reply_from_facts(
            runtime,
            user_text="what happened",
            facts={"state_transition": "failed"},
        )

    assert runtime.calls == 1
