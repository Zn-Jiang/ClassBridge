"""Persistent queue of read receipts that still need to be synced to the server.

The client stores read receipts on disk (a small JSON file in the per-user
appdata directory) instead of only in memory, so that:

- a shutdown / power loss while offline does not lose the receipts;
- after the WebSocket reconnects, the main window can replay them one by one.

The file format is a JSON object::

    {"version": 1, "items": [123, 456, ...]}

where each item is a message ``db_id`` whose read state still has to be
reported to the server.  Writes are atomic (write-temp-then-rename) so a crash
mid-write never corrupts the queue.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import List, Optional

from shared.paths import CLIENT_PENDING_READS_PATH, ensure_client_appdata_dir

logger = logging.getLogger("kg.client.pending_reads")

_VERSION = 1


class PendingReadsStore:
    """Disk-backed queue of ``db_id`` values awaiting read-receipt sync.

    All methods are idempotent and safe to call from the Qt main thread.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or CLIENT_PENDING_READS_PATH
        self._items: List[int] = []
        self._load()

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def add(self, db_id: int) -> None:
        """Queue *db_id* for later sync (no-op if already queued)."""
        if int(db_id) in self._items:
            return
        self._items.append(int(db_id))
        self._save()

    def discard(self, db_id: int) -> None:
        """Remove *db_id* from the queue (no-op if absent)."""
        if int(db_id) not in self._items:
            return
        self._items = [item for item in self._items if item != int(db_id)]
        self._save()

    def all(self) -> List[int]:
        """Return every queued db_id, oldest first."""
        return list(self._items)

    def contains(self, db_id: int) -> bool:
        return int(db_id) in self._items

    def clear(self) -> None:
        if not self._items:
            return
        self._items = []
        self._save()

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
            items = data.get("items", []) if isinstance(data, dict) else []
            self._items = [int(item) for item in items if isinstance(item, (int, float, str))]
        except FileNotFoundError:
            self._items = []
        except (ValueError, TypeError, OSError) as exc:
            # A corrupt queue must not crash the client — start empty but keep
            # the broken file around (renamed) so it can be inspected later.
            logger.warning("Pending-reads file unreadable (%s); ignoring: %s", self._path, exc)
            self._items = []
            self._quarantine_bad_file()

    def _save(self) -> None:
        ensure_client_appdata_dir()
        data = {"version": _VERSION, "items": self._items}
        # Atomic write: write to a temp file in the same directory, then rename.
        fd, tmp_name = tempfile.mkstemp(
            prefix="pending_reads.", suffix=".tmp", dir=str(self._path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)
            os.replace(tmp_name, str(self._path))
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise

    def _quarantine_bad_file(self) -> None:
        try:
            bad = self._path.with_suffix(".json.bad")
            if not bad.exists():
                os.replace(str(self._path), str(bad))
        except OSError:
            pass
