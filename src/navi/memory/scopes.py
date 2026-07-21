from __future__ import annotations

import hashlib
from pathlib import Path


def memory_scopes_for_context(
    *,
    source: str = "",
    peer_id: str = "",
    sender_id: str = "",
    session_id: str = "",
    workspace: str = "",
    home: Path | None = None,
) -> tuple[str, ...]:
    """Return the deterministic memory scopes visible to one execution actor."""
    scopes = ["global"]
    if home is not None and (source or peer_id or sender_id):
        from ..identity import IdentityStore, identity_memory_scope

        identity_id = IdentityStore(home).resolve(
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
        )
        if identity_id:
            scopes.append(identity_memory_scope(identity_id))
    if source or peer_id or sender_id:
        scopes.append(_hashed_scope("actor", source, peer_id, sender_id))
    if session_id:
        scopes.append(_hashed_scope("session", session_id))
    if workspace:
        resolved = str(Path(workspace).expanduser().resolve())
        scopes.append(_hashed_scope("workspace", resolved))
    return tuple(dict.fromkeys(scopes))


def default_memory_scope(
    *,
    source: str = "",
    peer_id: str = "",
    sender_id: str = "",
    session_id: str = "",
    workspace: str = "",
    home: Path | None = None,
) -> str:
    scopes = memory_scopes_for_context(
        source=source,
        peer_id=peer_id,
        sender_id=sender_id,
        session_id=session_id,
        workspace=workspace,
        home=home,
    )
    return next(
        (
            scope
            for scope in scopes
            if scope.startswith("person:") or scope.startswith("actor:")
        ),
        scopes[-1],
    )


def resolve_memory_scope(
    requested: str,
    *,
    source: str = "",
    peer_id: str = "",
    sender_id: str = "",
    session_id: str = "",
    workspace: str = "",
    home: Path | None = None,
) -> str:
    """Resolve a model-friendly scope kind without widening the caller envelope."""
    scopes = memory_scopes_for_context(
        source=source,
        peer_id=peer_id,
        sender_id=sender_id,
        session_id=session_id,
        workspace=workspace,
        home=home,
    )
    by_kind = {scope.split(":", 1)[0]: scope for scope in scopes}
    normalized = requested.strip().lower()
    return by_kind.get(normalized, requested.strip())


def writable_memory_scopes_for_context(
    *,
    source: str = "",
    peer_id: str = "",
    sender_id: str = "",
    session_id: str = "",
    workspace: str = "",
    allow_global: bool = False,
    home: Path | None = None,
) -> tuple[str, ...]:
    """Return write scopes, reserving global writes for the trusted local surface."""
    scopes = list(
        memory_scopes_for_context(
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            session_id=session_id,
            workspace=workspace,
            home=home,
        )
    )
    return tuple(scope for scope in scopes if scope != "global" or allow_global)


def _hashed_scope(kind: str, *parts: str) -> str:
    raw = "\x00".join(str(part or "") for part in parts)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"{kind}:{digest}"
