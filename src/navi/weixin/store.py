from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from navi.defaults import DEFAULT_WEIXIN_BASE_URL

from .models import WeixinAccount


class WeixinStore:
    def __init__(self, home: Path):
        self.dir = home / "weixin" / "accounts"
        self.dir.mkdir(parents=True, exist_ok=True)

    def account_path(self, account_id: str) -> Path:
        return self.dir / f"{account_id}.json"

    def save_account(self, account: WeixinAccount) -> None:
        payload = {
            "account_id": account.account_id,
            "token": account.token,
            "base_url": account.base_url,
            "user_id": account.user_id,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        path = self.account_path(account.account_id)
        _atomic_json_write(path, payload)
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def load_account(self, account_id: str) -> WeixinAccount | None:
        path = self.account_path(account_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return WeixinAccount(
            account_id=str(data["account_id"]),
            token=str(data["token"]),
            base_url=str(data.get("base_url") or DEFAULT_WEIXIN_BASE_URL),
            user_id=str(data.get("user_id") or ""),
        )

    def list_accounts(self) -> list[str]:
        return [path.stem for path in sorted(self.dir.glob("*.json")) if not path.name.endswith(".context-tokens.json")]

    def sync_path(self, account_id: str) -> Path:
        return self.dir / f"{account_id}.sync.json"

    def load_sync_buf(self, account_id: str) -> str:
        path = self.sync_path(account_id)
        if not path.exists():
            return ""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return ""
        return str(data.get("get_updates_buf") or "")

    def save_sync_buf(self, account_id: str, sync_buf: str) -> None:
        _atomic_json_write(self.sync_path(account_id), {"get_updates_buf": sync_buf})


class ContextTokenStore:
    def __init__(self, home: Path):
        self.path = home / "weixin" / "context-tokens.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._tokens = self._load()

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return {str(key): str(value) for key, value in data.items()}

    def get(self, account_id: str, peer_id: str) -> str:
        return self._tokens.get(f"{account_id}:{peer_id}", "")

    def put(self, account_id: str, peer_id: str, token: str) -> None:
        if not token:
            return
        self._tokens[f"{account_id}:{peer_id}"] = token
        _atomic_json_write(self.path, self._tokens)


class MessageDeduplicator:
    def __init__(self, ttl_seconds: int = 300):
        self.ttl_seconds = ttl_seconds
        self._seen: dict[str, float] = {}

    def seen(self, key: str) -> bool:
        now = time.time()
        self._seen = {
            existing: expires_at for existing, expires_at in self._seen.items() if expires_at > now
        }
        if key in self._seen:
            return True
        self._seen[key] = now + self.ttl_seconds
        return False


def extract_text(payload: dict[str, Any]) -> str:
    item_list = payload.get("item_list")
    if isinstance(item_list, list):
        text = _extract_text_items(item_list)
        if text:
            return text
    for key in ("text", "content", "message", "body"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    items = payload.get("items")
    if isinstance(items, list):
        parts = []
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts).strip()
    return ""


def _extract_text_items(items: list[Any]) -> str:
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == 1:
            text = str((item.get("text_item") or {}).get("text") or "")
            ref = item.get("ref_msg") or {}
            ref_item = ref.get("message_item") or {}
            if isinstance(ref_item, dict):
                ref_text = _extract_text_items([ref_item])
                title = str(ref.get("title") or "")
                if ref_text or title:
                    prefix = " | ".join(part for part in (title, ref_text) if part)
                    return f"[引用: {prefix}]\n{text}".strip()
            return text.strip()
    for item in items:
        if isinstance(item, dict) and item.get("type") == 3:
            voice_text = str((item.get("voice_item") or {}).get("text") or "")
            if voice_text.strip():
                return voice_text.strip()
    return ""


def split_text_for_weixin(content: str, max_length: int = 2000) -> list[str]:
    if not content or not content.strip():
        return []
    content = content.strip()
    if len(content) <= max_length:
        lines = content.splitlines()
        if 1 < len(lines) <= 3 and all(line.strip() and not line.startswith(("#", "-", "|", "```")) for line in lines):
            return [line.strip() for line in lines]
        return [content]
    chunks: list[str] = []
    current = ""
    for block in content.split("\n\n"):
        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) <= max_length:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = ""
        while len(block) > max_length:
            chunks.append(block[:max_length])
            block = block[max_length:]
        current = block
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk.strip()]


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
