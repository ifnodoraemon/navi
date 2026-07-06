from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

import yaml

from navi.connector_registry import ConnectorAdapter, ConnectorSpec

from .config import load_weixin_config
from .store import WeixinStore


def _load_spec() -> ConnectorSpec:
    raw = yaml.safe_load(
        (Path(__file__).with_name("specs") / "connector.yaml").read_text(encoding="utf-8")
    )
    approval_commands = raw.get("approval_commands") or {}
    return ConnectorSpec(
        name=str(raw["name"]),
        surface=str(raw["surface"]),
        status_tool=str(raw["status_tool"]),
        status_description=str(raw["status_description"]),
        session_alias_prefix=str(raw["session_alias_prefix"]),
        local_source=str(raw["local_source"]),
        approval_approve_commands=tuple(
            str(item) for item in approval_commands.get("approve") or ()
        ),
        approval_reject_commands=tuple(
            str(item) for item in approval_commands.get("reject") or ()
        ),
        approval_template=str(raw.get("approval_template") or ""),
    )


SPEC = _load_spec()


def create_adapter() -> ConnectorAdapter:
    from .evals import load_journey_eval_dataset, run_journey_eval_dataset

    return ConnectorAdapter(
        spec=SPEC,
        enabled=_enabled,
        status=_status,
        diagnostics=_diagnostics,
        register_tools=lambda registry, home: _register_tools(registry, home, SPEC),
        setup=_setup,
        run=_run,
        load_journey_eval_dataset=load_journey_eval_dataset,
        run_journey_eval_dataset=run_journey_eval_dataset,
    )


def _enabled(home: Path) -> bool:
    return load_weixin_config(home).enabled


def _status(home: Path) -> dict[str, Any]:
    config = load_weixin_config(home)
    store = WeixinStore(home)
    facts = {
        "configured": bool(config.account_id or store.list_accounts()),
        "account_id": config.account_id,
        "saved_accounts": store.list_accounts(),
        "dm_policy": config.dm_policy,
        "group_policy": config.group_policy,
        "status": "unknown",
        "error": "",
        "last_update": 0.0,
    }
    status_file = home / "weixin" / "status.json"
    if status_file.exists():
        try:
            data = json.loads(status_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                logging.getLogger("navi.weixin").warning(
                    "status.json must be a JSON object, got %s", type(data).__name__
                )
            else:
                facts.update(
                    {
                        "status": data.get("status", "unknown"),
                        "error": data.get("error", ""),
                        "last_update": data.get("last_update", 0.0),
                    }
                )
        except json.JSONDecodeError as e:
            logging.getLogger("navi.weixin").warning("Corrupted status.json: %s", e)
        except OSError as e:
            logging.getLogger("navi.weixin").warning("Failed to read status file: %s", e)
    return facts


def _diagnostics(home: Path) -> list[dict[str, str]]:
    config = load_weixin_config(home)
    saved_account = WeixinStore(home).load_account(config.account_id) if config.account_id else None
    token_present = bool(config.token or (saved_account and saved_account.token))
    ready = config.enabled and config.account_id and token_present
    return [
        {
            "name": f"connector.{SPEC.name}.config",
            "status": "ok" if ready else "missing",
            "detail": (
                f"enabled={config.enabled} "
                f"account_present={bool(config.account_id)} "
                f"token_present={token_present}"
            ),
        }
    ]


def _register_tools(registry: Any, home: Path, spec: ConnectorSpec) -> None:
    from navi.tools import ALL_EXECUTION_CONTEXTS, ToolResult, ToolSpec

    registry.register(
        ToolSpec(
            name=spec.status_tool,
            capability_class="connector",
            execution_contexts=ALL_EXECUTION_CONTEXTS,
            description=spec.status_description,
            input_schema={"type": "object", "properties": {}},
            output_schema={
                "type": "object",
                "properties": {
                    "configured": {"type": "boolean"},
                    "account_id": {"type": "string"},
                    "saved_accounts": {"type": "array", "items": {"type": "string"}},
                    "dm_policy": {"type": "string"},
                    "group_policy": {"type": "string"},
                    "status": {"type": "string"},
                    "error": {"type": "string"},
                    "last_update": {"type": "number"},
                },
            },
            source=spec.surface,
        ),
        lambda args: ToolResult(tool=spec.status_tool, ok=True, facts=_status(home)),
    )
    registry.register(
        ToolSpec(
            name="connector.weixin.send_file",
            capability_class="connector.outbound_media",
            execution_contexts=ALL_EXECUTION_CONTEXTS,
            description=(
                "Send a file (and optional text message) to the user via Weixin. "
                "This is a terminal action \u2014 no further tools can be called in this turn after sending."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "text": {"type": "string", "description": "Optional text caption or message to send alongside the file."},
                },
                "required": ["path"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "entity_type": {"type": "string"},
                    "entity_id": {"type": "string"},
                    "state_transition": {"type": "string"},
                    "turn_scope": {"type": "string"},
                    "source_path": {"type": "string"},
                    "outbound_path": {"type": "string"},
                    "size": {"type": "integer"},
                },
            },
            facts_only=True,
            mutates=True,
            permission="write",
            source=f"connector.{spec.name}",
        ),
        lambda args: _send_file_handler(home, args),
    )


async def _setup(home: Path, project_dir: Path, timeout_seconds: int, on_qr: Any | None) -> str:
    service = _service(home, project_dir)
    return await service.setup(timeout_seconds=timeout_seconds, on_qr=on_qr)


async def _run(home: Path, project_dir: Path, once: bool) -> None:
    await _service(home, project_dir).run(once=once)


def _send_file_handler(home: Path, args: dict[str, Any]):
    from navi.tools import ToolResult
    import json
    import shutil
    
    text = args.get("text") or ""
    raw_path = str(args.get("path") or "").strip()
    if not raw_path:
        return ToolResult(tool="connector.weixin.send_file", ok=False, error="path is required")
    try:
        source = Path(raw_path).expanduser().resolve()
    except OSError as exc:
        return ToolResult(tool="connector.weixin.send_file", ok=False, error=str(exc))
    if not source.exists():
        return ToolResult(
            tool="connector.weixin.send_file",
            ok=False,
            error=f"file not found: {source}",
            facts={"source_path": str(source)},
        )
    if not source.is_file():
        return ToolResult(
            tool="connector.weixin.send_file",
            ok=False,
            error=f"path is not a file: {source}",
            facts={"source_path": str(source)},
        )

    outbox = home / "weixin" / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    target = _unique_outbound_path(
        outbox / _safe_outbound_name(str(args.get("file_name") or "") or source.name)
    )
    try:
        shutil.copy2(source, target)
    except OSError as exc:
        return ToolResult(
            tool="connector.weixin.send_file",
            ok=False,
            error=str(exc),
            facts={"source_path": str(source), "outbound_path": str(target)},
        )

    return ToolResult(
        tool="connector.weixin.send_file",
        ok=True,
        terminal=True,
        action="connector_outbound",
        message=text,
        facts={
            "entity_type": "outbound_media",
            "entity_id": str(target),
            "state_transition": "staged",
            "turn_scope": "current",
            "source_path": str(source),
            "outbound_path": str(target),
            "size": target.stat().st_size,
        },
    )


def _safe_outbound_name(value: str) -> str:
    candidate = Path(value).name.strip()
    return candidate or "outbound-file"


def _unique_outbound_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem or "outbound-file"
    suffix = path.suffix
    for index in range(1, 1000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise OSError(f"could not allocate outbound file path under {path.parent}")


def _service(home: Path, project_dir: Path):
    from .service import WeixinService

    return WeixinService(
        home=home,
        config=load_weixin_config(home),
        runtime=build_runtime_for_connector(home),
        project_dir=project_dir,
        local_source=SPEC.local_source,
        session_alias_prefix=SPEC.session_alias_prefix,
    )


def build_runtime_for_connector(home: Path):
    from navi.app_factory import build_runtime

    return build_runtime(home)
