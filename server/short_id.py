from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Optional

from shared.protocol import MessageRecord, ShortIdMapping


@dataclass
class _ShortIdEntry:
    db_id: int
    sender_id: str
    expires_at: datetime


class ShortIdStore:
    def __init__(self) -> None:
        self._sender_scopes: Dict[str, Dict[str, _ShortIdEntry]] = {}

    def create_scope(
        self,
        *,
        sender_id: str,
        records: list[MessageRecord],
        ttl_seconds: int,
    ) -> list[ShortIdMapping]:
        self.cleanup()

        expires_at = datetime.utcnow() + timedelta(seconds=ttl_seconds)
        scoped_entries: Dict[str, _ShortIdEntry] = {}
        mappings: list[ShortIdMapping] = []

        for index, record in enumerate(records, start=1):
            short_id = str(index)
            if record.db_id is None:
                continue
            scoped_entries[short_id] = _ShortIdEntry(
                db_id=record.db_id,
                sender_id=sender_id,
                expires_at=expires_at,
            )
            mappings.append(
                ShortIdMapping(
                    db_id=record.db_id,
                    short_id=short_id,
                    sender_id=sender_id,
                    msg_type=record.msg_type,
                    content_preview=_preview(record.content),
                    timestamp=record.timestamp,
                )
            )

        self._sender_scopes[str(sender_id)] = scoped_entries
        return mappings

    def resolve(self, *, sender_id: str, short_id: str) -> Optional[int]:
        self.cleanup()
        entry = self._sender_scopes.get(str(sender_id), {}).get(str(short_id))
        if entry is None:
            return None
        return entry.db_id

    def cleanup(self) -> None:
        now = datetime.utcnow()
        expired_senders = []
        for sender_id, scoped_entries in self._sender_scopes.items():
            expired_keys = [
                short_id for short_id, entry in scoped_entries.items() if entry.expires_at <= now
            ]
            for short_id in expired_keys:
                scoped_entries.pop(short_id, None)
            if not scoped_entries:
                expired_senders.append(sender_id)

        for sender_id in expired_senders:
            self._sender_scopes.pop(sender_id, None)


def _preview(content: str, max_length: int = 20) -> str:
    if len(content) <= max_length:
        return content
    return f"{content[: max_length - 3]}..."

