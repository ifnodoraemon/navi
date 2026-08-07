from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

# Adapters that fabricate a message id because the upstream payload carries no
# native one must prefix it with this marker. Native ids are authoritative
# transport idempotency keys; synthetic ids fall back to content-key dedupe.
SYNTHETIC_MESSAGE_ID_PREFIX = "synthetic:"


def is_synthetic_message_id(message_id: str) -> bool:
    return message_id.startswith(SYNTHETIC_MESSAGE_ID_PREFIX)


@dataclass(frozen=True)
class ConnectorMessage:
    """Transport-neutral connector ingress contract."""

    message_id: str
    peer_id: str
    sender_id: str
    text: str
    source: str
    session_alias_prefix: str
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def session_alias(self) -> str:
        peer_id = self.peer_id.strip() or "unknown"
        sender_id = self.sender_id.strip() or "unknown"
        return f"{self.session_alias_prefix}:{peer_id}:{sender_id}"

    @property
    def content_key(self) -> str:
        payload = json.dumps(
            {
                "source": self.source,
                "peer_id": self.peer_id,
                "sender_id": self.sender_id,
                "text": self.text,
                "facts": self.facts,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        digest = hashlib.md5(payload.encode()).hexdigest()
        return f"content:{self.source}:{self.peer_id}:{self.sender_id}:{digest}"
