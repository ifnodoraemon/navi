from __future__ import annotations


PERMISSION_ORDER = {
    "read": 0,
    "network": 1,
    "prepare": 2,
    "write": 3,
}


def normalize_permission(value: object, *, default: str = "read") -> str:
    """Return a declared permission or reject an unknown contract value."""
    permission = str(value or "").strip().lower()
    if not permission:
        permission = str(default or "").strip().lower()
    if permission not in PERMISSION_ORDER:
        raise ValueError(f"unsupported permission: {value}")
    return permission
