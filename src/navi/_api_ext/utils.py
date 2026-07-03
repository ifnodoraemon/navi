from pathlib import Path
from fastapi import HTTPException
from ..capabilities import CapabilityContext, CapabilityResult
from ..config import load_config

def local_capability_context(home: Path, *, project_dir: Path) -> CapabilityContext:
    local_surface = load_config(home).runtime.local_surface
    return CapabilityContext(
        home=home,
        peer_id=local_surface,
        sender_id=local_surface,
        source=local_surface,
        permission_ceiling="write",
        workspace=str(project_dir),
    )

def raise_capability_error(result: CapabilityResult, *, not_found_status: int = 409) -> None:
    if result.ok:
        return
    safe_detail = result.message or "capability invocation failed"
    if result.error_reason == "not_found":
        raise HTTPException(status_code=not_found_status, detail=safe_detail)
    raise HTTPException(status_code=409, detail=safe_detail)
