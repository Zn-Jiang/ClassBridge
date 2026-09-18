"""Locally persisted timetable for the desktop client.

The client keeps its own copy of a timetable on disk (a small JSON file in the
per-user appdata directory) so that break-time popups keep working when
ClassIsland is not running.

Structure (``version`` 1)::

    {
      "version": 1,
      "source": "classisland",
      "imported_at": "2026-08-15 21:00:00",
      "profile_file": "8月自习.json",
      "layout_name": "周一至周五课表",
      "entries": [
        {"index": 1, "type": "class", "start": "08:00:00", "end": "08:45:00"},
        {"index": 2, "type": "break", "start": "08:45:00", "end": "09:00:00"}
      ],
      "breaks": [{"name": "课间 1", "start": "08:45", "end": "09:00"}]
    }

``entries`` is the authoritative node list shown in the UI; ``breaks`` mirrors
it in the shape already understood by :mod:`client.schedule_loader`, so the
file can double as a plain schedule file if a user points the client at it.

Writes are atomic (temp file + ``os.replace``) so a crash can never leave a
half-written timetable behind.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from shared.paths import CLIENT_SCHEDULE_PATH, ensure_client_appdata_dir

from .classisland_import import TimeLayoutOption, normalize_clock

logger = logging.getLogger("kg.client.schedule_store")

SCHEDULE_STORE_VERSION = 1

TYPE_CLASS = "class"
TYPE_BREAK = "break"


@dataclass
class StoredEntry:
    """One timetable node as persisted on disk."""

    index: int
    type: str  # "class" | "break"
    start: str  # "HH:MM:SS"
    end: str    # "HH:MM:SS"

    @property
    def is_break(self) -> bool:
        return self.type == TYPE_BREAK

    @property
    def type_label(self) -> str:
        return "课间" if self.is_break else "上课"

    @property
    def start_time(self) -> Optional[time]:
        return _to_time(self.start)

    @property
    def end_time(self) -> Optional[time]:
        return _to_time(self.end)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "type": self.type,
            "start": self.start,
            "end": self.end,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StoredEntry":
        raw_type = str(data.get("type", TYPE_CLASS)).lower()
        entry_type = TYPE_BREAK if raw_type in {"break", "breaking", "1"} else TYPE_CLASS
        return cls(
            index=int(data.get("index", 0) or 0),
            type=entry_type,
            start=normalize_clock(data.get("start")) or "00:00:00",
            end=normalize_clock(data.get("end")) or "00:00:00",
        )


@dataclass
class StoredSchedule:
    """The client's own timetable copy."""

    entries: List[StoredEntry] = field(default_factory=list)
    source: str = "classisland"
    imported_at: str = ""
    profile_file: str = ""
    layout_name: str = ""
    version: int = SCHEDULE_STORE_VERSION

    # ------------------------------------------------------------------
    # derived data
    # ------------------------------------------------------------------

    @property
    def break_entries(self) -> List[StoredEntry]:
        return [entry for entry in self.entries if entry.is_break]

    @property
    def break_count(self) -> int:
        return len(self.break_entries)

    def break_ranges(self) -> List[Tuple[time, time]]:
        """Break windows as ``(start, end)`` pairs, ready for the monitor."""
        ranges: List[Tuple[time, time]] = []
        for entry in self.break_entries:
            start = entry.start_time
            end = entry.end_time
            if start is None or end is None:
                logger.warning("Skipping break with unparsable time: %r", entry)
                continue
            ranges.append((start, end))
        return ranges

    @property
    def is_empty(self) -> bool:
        return not self.entries

    def describe(self) -> str:
        origin = self.layout_name or "本地课表"
        if self.profile_file:
            origin = f"{Path(self.profile_file).stem} - {origin}"
        return f"{origin}（{len(self.entries)} 个节点，{self.break_count} 个课间）"

    # ------------------------------------------------------------------
    # (de)serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "source": self.source,
            "imported_at": self.imported_at,
            "profile_file": self.profile_file,
            "layout_name": self.layout_name,
            "entries": [entry.to_dict() for entry in self.entries],
            # Compatibility mirror for schedule_loader's "breaks" format.
            "breaks": [
                {
                    "name": f"课间 {position}",
                    "start": entry.start[:5],
                    "end": entry.end[:5],
                }
                for position, entry in enumerate(self.break_entries, start=1)
            ],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StoredSchedule":
        raw_entries = data.get("entries")
        entries: List[StoredEntry] = []
        if isinstance(raw_entries, list):
            for item in raw_entries:
                if isinstance(item, dict):
                    entries.append(StoredEntry.from_dict(item))
        if not entries:
            # Older/minimal files may only carry the "breaks" mirror.
            raw_breaks = data.get("breaks")
            if isinstance(raw_breaks, list):
                for position, item in enumerate(raw_breaks, start=1):
                    if not isinstance(item, dict):
                        continue
                    start = normalize_clock(item.get("start"))
                    end = normalize_clock(item.get("end"))
                    if start and end:
                        entries.append(
                            StoredEntry(index=position, type=TYPE_BREAK, start=start, end=end)
                        )
        return cls(
            entries=entries,
            source=str(data.get("source", "classisland")),
            imported_at=str(data.get("imported_at", "")),
            profile_file=str(data.get("profile_file", "")),
            layout_name=str(data.get("layout_name", "")),
            version=int(data.get("version", SCHEDULE_STORE_VERSION) or SCHEDULE_STORE_VERSION),
        )

    @classmethod
    def from_layout_option(cls, option: TimeLayoutOption) -> "StoredSchedule":
        """Build a client-side timetable from a parsed ClassIsland one."""
        entries = [
            StoredEntry(
                index=position,
                type=TYPE_BREAK if entry.is_break else TYPE_CLASS,
                start=entry.start,
                end=entry.end,
            )
            for position, entry in enumerate(option.entries, start=1)
        ]
        return cls(
            entries=entries,
            source="classisland",
            imported_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            profile_file=option.profile_file,
            layout_name=option.name,
        )

    @classmethod
    def from_entries(
        cls,
        entries: List[StoredEntry],
        *,
        profile_file: str = "",
        layout_name: str = "",
        source: str = "classisland",
    ) -> "StoredSchedule":
        return cls(
            entries=list(entries),
            source=source,
            imported_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            profile_file=profile_file,
            layout_name=layout_name,
        )


class ScheduleStore:
    """Disk-backed timetable used by the break monitor thread."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or CLIENT_SCHEDULE_PATH
        self._schedule: Optional[StoredSchedule] = None
        self._loaded = False

    @property
    def path(self) -> Path:
        return self._path

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def load(self, force: bool = False) -> Optional[StoredSchedule]:
        """Load the timetable from disk (cached unless *force* is set)."""
        if self._loaded and not force:
            return self._schedule
        self._loaded = True
        self._schedule = self._read()
        return self._schedule

    def save(self, schedule: StoredSchedule) -> None:
        self._schedule = schedule
        self._loaded = True
        self._write(schedule)

    def clear(self) -> None:
        """Remove the stored timetable (does not touch the config)."""
        self._schedule = None
        self._loaded = True
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("Failed to remove schedule file %s: %s", self._path, exc)

    def current(self) -> Optional[StoredSchedule]:
        return self.load()

    def break_ranges(self) -> List[Tuple[time, time]]:
        """Break windows for the break monitor (empty when nothing stored)."""
        schedule = self.load()
        if schedule is None:
            return []
        return schedule.break_ranges()

    @property
    def has_schedule(self) -> bool:
        schedule = self.load()
        return schedule is not None and not schedule.is_empty

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    def _read(self) -> Optional[StoredSchedule]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning("Failed to read schedule file %s: %s", self._path, exc)
            return None

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("Invalid schedule JSON in %s: %s", self._path, exc)
            self._quarantine()
            return None

        if not isinstance(data, dict):
            logger.warning("Schedule file %s is not a JSON object", self._path)
            self._quarantine()
            return None

        try:
            schedule = StoredSchedule.from_dict(data)
        except (TypeError, ValueError) as exc:
            logger.warning("Malformed schedule file %s: %s", self._path, exc)
            self._quarantine()
            return None

        logger.info("Loaded stored schedule: %s", schedule.describe())
        return schedule

    def _write(self, schedule: StoredSchedule) -> None:
        ensure_client_appdata_dir()
        fd, tmp_name = tempfile.mkstemp(
            prefix="imported_schedule.", suffix=".tmp", dir=str(self._path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(schedule.to_dict(), handle, ensure_ascii=False, indent=2)
            os.replace(tmp_name, str(self._path))
            logger.info("Saved schedule to %s: %s", self._path, schedule.describe())
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise

    def _quarantine(self) -> None:
        try:
            bad = self._path.with_suffix(".json.bad")
            if not bad.exists():
                os.replace(str(self._path), str(bad))
                logger.warning("Quarantined unreadable schedule file as %s", bad)
        except OSError:
            pass


def _to_time(value: str) -> Optional[time]:
    normalized = normalize_clock(value)
    if normalized is None:
        return None
    hour, minute, second = (int(part) for part in normalized.split(":"))
    return time(hour=hour, minute=minute, second=second)
