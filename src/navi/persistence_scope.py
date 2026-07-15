from __future__ import annotations

from typing import Any


def append_actor_scope(
    clauses: list[str],
    params: list[Any],
    *,
    source: str = "",
    peer_id: str = "",
    sender_id: str = "",
    workspace: str = "",
) -> None:
    """Append the durable actor/workspace visibility contract to a SQL query.

    Empty values stored on a record are unscoped. A non-empty record value must
    match the corresponding non-empty caller value.
    """
    for column, value in (
        ("source", source),
        ("peer_id", peer_id),
        ("sender_id", sender_id),
        ("workspace", workspace),
    ):
        normalized = str(value or "")
        if not normalized:
            continue
        clauses.append(f"({column} = ? OR {column} = '')")
        params.append(normalized)
