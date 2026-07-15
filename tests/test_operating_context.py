from __future__ import annotations

import pytest

from navi.operating_context import OperatingContext, max_permission, permission_allows
from navi.tools import ToolSpec


def test_unknown_permissions_fail_closed(tmp_path):
    assert permission_allows("undeclared", "write") is False
    assert permission_allows("read", "undeclared") is False

    with pytest.raises(ValueError, match="unsupported permission"):
        max_permission("read", "undeclared")
    with pytest.raises(ValueError, match="unsupported permission"):
        OperatingContext(home=tmp_path, permission_ceiling="undeclared")


def test_tool_spec_rejects_unknown_permission():
    with pytest.raises(ValueError, match="unsupported permission"):
        ToolSpec(
            name="invalid.permission",
            capability_class="test",
            execution_contexts=("turn",),
            description="Invalid test capability.",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            permission="undeclared",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("permission_policy", "tool_name_switch"),
        ("risk_policy", "incident_override"),
        ("context_policy", "implicit_actor_guess"),
        ("runtime_policy", "capability_name_switch"),
    ],
)
def test_tool_spec_rejects_undeclared_governance_policies(field, value):
    kwargs = {field: value}
    with pytest.raises(ValueError, match=f"unsupported {field}"):
        ToolSpec(
            name="invalid.policy",
            capability_class="test",
            execution_contexts=("turn",),
            description="Invalid test capability.",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            **kwargs,
        )
