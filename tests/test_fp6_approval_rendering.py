"""FP-6 regression: the connector-agnostic core must not hardcode
channel-specific approval verbs (``批准`` / ``拒绝``). Approval reply text is
rendered through the connector affordance (``approval_template`` +
``approval_commands``) sourced from each connector's spec.
"""
from __future__ import annotations

from pathlib import Path

from navi.connector_registry import (
    approval_surface_affordance,
    render_approval_reply,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_render_approval_reply_uses_connector_template() -> None:
    # The weixin connector declares an approval_template with approve/reject
    # commands. The rendered reply must source its verbs from that spec, not
    # from a hardcoded core string.
    rendered = render_approval_reply(
        "connector.weixin", code="ABC123", run_id="run-1", action="execute:file.write"
    )
    # Template-sourced content (from weixin/specs/connector.yaml).
    assert "ABC123" in rendered
    assert "run-1" in rendered
    # The approve command "批准" comes from the connector spec, not the core.
    assert "批准" in rendered


def test_render_approval_reply_cli_source_is_connector_agnostic() -> None:
    # CLI / local API sources have no matching connector affordance.
    rendered = render_approval_reply(
        "cli", code="ZZZ999", run_id="run-9", action="execute:shell.run"
    )
    assert "ZZZ999" in rendered
    # No hardcoded Chinese verbs leak in for the connector-less path.
    assert "批准" not in rendered
    assert "拒绝" not in rendered


def test_core_control_files_do_not_hardcode_approval_verbs() -> None:
    """FP-6: control.py and capabilities.py must not embed the ``批准`` /
    ``拒绝`` channel verbs. Those live only in connector specs."""
    for rel in ("src/navi/control.py", "src/navi/capabilities.py"):
        text = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
        assert "批准" not in text, f"{rel} must not hardcode 批准"
        assert "拒绝" not in text, f"{rel} must not hardcode 拒绝"


def test_no_connector_affordance_for_unknown_source() -> None:
    # Principle 4: no core default. A source with no matching connector has no
    # approval affordance.
    assert approval_surface_affordance("source-with-no-connector") == {}
