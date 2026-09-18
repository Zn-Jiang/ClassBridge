"""ClassIsland configuration import.

Parses a local ClassIsland installation's own JSON files so the client can
import a timetable without talking to ClassIsland at runtime.

This module is **pure Python and intentionally free of any Qt dependency** —
the UI layer (``client/import_wizard.py``) only consumes the dataclasses
defined here.

Discovery order (see the project spec):

1. Locate ``ClassIsland.Desktop.exe`` via :mod:`psutil` and derive its
   ``data`` directory (``<root>/data``).
2. Read ``data/Settings.json`` to learn the active profile
   (``SelectedProfile``), then load ``data/Profiles/<profile>``.
3. Fall back to ``<profile>.json.bak`` when the main file is missing or
   malformed, reporting ``used_backup`` so the UI can warn the user.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("kg.client.ci_import")

#: Executable name used to auto-detect a running ClassIsland installation.
CLASSISLAND_PROCESS_NAME = "ClassIsland.Desktop.exe"

#: ClassIsland ``TimeType`` values.
TIME_TYPE_CLASS = 0
TIME_TYPE_BREAK = 1

_SETTINGS_FILENAME = "Settings.json"
_PROFILES_DIRNAME = "Profiles"
_DATA_DIRNAME = "data"

_CLOCK_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*$")
# Guard against pathological nesting while searching for ``TimeLayouts``.
_MAX_SEARCH_DEPTH = 6


# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------


@dataclass
class TimeLayoutEntry:
    """One node of a ClassIsland timetable (a lesson or a break)."""

    index: int
    time_type: int
    start: str  # normalized "HH:MM:SS"
    end: str    # normalized "HH:MM:SS"

    @property
    def is_break(self) -> bool:
        return self.time_type == TIME_TYPE_BREAK

    @property
    def type_label(self) -> str:
        return "课间" if self.is_break else "上课"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "type": "break" if self.is_break else "class",
            "time_type": self.time_type,
            "start": self.start,
            "end": self.end,
        }


@dataclass
class TimeLayoutOption:
    """A single timetable (``TimeLayout``) found inside a profile file."""

    profile_file: str          # e.g. "8月自习.json"
    layout_id: str             # internal uuid / key inside TimeLayouts
    name: str                  # e.g. "周一至周五课表"
    entries: List[TimeLayoutEntry] = field(default_factory=list)

    @property
    def profile_stem(self) -> str:
        return Path(self.profile_file).stem

    @property
    def break_count(self) -> int:
        return sum(1 for entry in self.entries if entry.is_break)

    @property
    def class_count(self) -> int:
        return sum(1 for entry in self.entries if not entry.is_break)

    @property
    def label(self) -> str:
        """Dropdown label, e.g. ``[8月自习] - 周一至周五课表 (包含 3 个课间)``."""
        return f"[{self.profile_stem}] - {self.name} (包含 {self.break_count} 个课间)"


@dataclass
class CiPaths:
    """Resolved filesystem locations of a ClassIsland installation."""

    root_dir: Optional[Path] = None
    data_dir: Optional[Path] = None
    settings_path: Optional[Path] = None
    profiles_dir: Optional[Path] = None

    @property
    def is_complete(self) -> bool:
        return bool(self.data_dir and self.settings_path and self.profiles_dir)


@dataclass
class ProfileLoadResult:
    """Outcome of loading one profile file."""

    profile_file: str
    layouts: List[TimeLayoutOption] = field(default_factory=list)
    used_backup: bool = False
    warning: Optional[str] = None
    error: Optional[str] = None


@dataclass
class ScanResult:
    """Outcome of scanning every profile file."""

    options: List[TimeLayoutOption] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def has_data(self) -> bool:
        return bool(self.options)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def normalize_clock(value: Any) -> Optional[str]:
    """Normalize ``"8:5"``/``"08:05"``/``"08:05:00"`` to ``"HH:MM:SS"``."""
    if not isinstance(value, str):
        return None
    match = _CLOCK_RE.match(value)
    if match is None:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    second = int(match.group(3) or 0)
    if not (0 <= hour < 24 and 0 <= minute < 60 and 0 <= second < 60):
        return None
    return f"{hour:02d}:{minute:02d}:{second:02d}"


def parse_time_type(value: Any) -> Optional[int]:
    """Coerce a ClassIsland ``TimeType`` to an int (accepts numeric strings)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
    return None


def _find_key(mapping: Dict[str, Any], *names: str) -> Any:
    """Case-insensitive lookup of the first matching key."""
    if not isinstance(mapping, dict):
        return None
    lowered = {str(key).lower(): key for key in mapping}
    for name in names:
        key = lowered.get(name.lower())
        if key is not None:
            return mapping[key]
    return None


def _find_layouts_container(data: Any, depth: int = 0) -> Optional[Dict[str, Any]]:
    """Locate the ``TimeLayouts`` mapping, preferring the top level.

    Falls back to a bounded depth-first search so that profile files with a
    slightly different envelope still work.
    """
    if not isinstance(data, dict) or depth > _MAX_SEARCH_DEPTH:
        return None
    direct = _find_key(data, "TimeLayouts", "TimeLayout")
    if isinstance(direct, dict):
        return direct
    for value in data.values():
        if isinstance(value, dict):
            found = _find_layouts_container(value, depth + 1)
            if found is not None:
                return found
    return None


def _iter_profile_files(profiles_dir: Path) -> List[Path]:
    """Return profile ``*.json`` files, excluding ``.bak`` backups."""
    if not profiles_dir.is_dir():
        return []
    files = [
        path
        for path in profiles_dir.glob("*.json")
        if not path.name.lower().endswith((".bak",))
    ]
    return sorted(files, key=lambda item: item.name)


def backup_candidates(path: Path) -> List[Path]:
    """Possible backup files for *path*.

    For ``8月自习.json`` this yields ``8月自习.json.bak`` (the ClassIsland
    convention) followed by the shorter ``8月自习.bak`` variant.
    """
    candidates: List[Path] = [path.with_name(path.name + ".bak")]
    if path.suffix:
        candidates.append(path.with_suffix(".bak"))
    # De-duplicate while preserving order.
    unique: List[Path] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


class ClassIslandConfigParser:
    """Reads timetables out of a local ClassIsland installation.

    The class performs no UI work and never raises for missing/malformed
    files: failures are reported through return values and log messages.
    """

    def __init__(
        self,
        root_dir: Optional[Path] = None,
        data_dir: Optional[Path] = None,
    ) -> None:
        self._root_dir: Optional[Path] = Path(root_dir) if root_dir else None
        self._data_dir: Optional[Path] = Path(data_dir) if data_dir else None

    # ------------------------------------------------------------------
    # path detection
    # ------------------------------------------------------------------

    @staticmethod
    def find_process_exe() -> Optional[Path]:
        """Return the executable path of a running ClassIsland process."""
        try:
            import psutil  # imported lazily: optional at import time
        except ImportError:
            logger.warning("psutil is not installed; cannot auto-detect ClassIsland.")
            return None

        try:
            for process in psutil.process_iter(["name", "exe"]):
                try:
                    info = process.info
                    name = str(info.get("name") or "")
                    if name.lower() != CLASSISLAND_PROCESS_NAME.lower():
                        continue
                    exe = info.get("exe")
                    if exe:
                        logger.info("Detected ClassIsland process: %s", exe)
                        return Path(exe)
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Failed to enumerate processes: %s", exc)
        return None

    @staticmethod
    def is_process_running() -> Optional[bool]:
        """Return whether ClassIsland is running.

        ``None`` means "could not be determined" (psutil unavailable or the
        process table could not be read) — callers should then keep their
        current behaviour instead of assuming ClassIsland is gone.
        """
        try:
            import psutil  # imported lazily: optional at import time
        except ImportError:
            logger.warning("psutil is not installed; cannot detect ClassIsland.")
            return None

        try:
            for process in psutil.process_iter(["name"]):
                try:
                    name = str(process.info.get("name") or "")
                    if name.lower() == CLASSISLAND_PROCESS_NAME.lower():
                        return True
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Failed to enumerate processes: %s", exc)
            return None
        return False

    @staticmethod
    def data_dir_from_exe(exe_path: Path) -> Optional[Path]:
        """Derive ``<root>/data`` from an executable path.

        Checks both the executable's own directory and its parent, because
        ClassIsland may be installed either flat or inside a subdirectory.
        """
        try:
            exe_path = Path(exe_path)
        except TypeError:
            return None
        for candidate_root in (exe_path.parent, exe_path.parent.parent):
            candidate = candidate_root / _DATA_DIRNAME
            if candidate.is_dir():
                return candidate
        return None

    @staticmethod
    def data_dir_from_root(root_dir: Path) -> Optional[Path]:
        """Derive a ``data`` directory from a user-selected root directory.

        Accepts the installation root, the ``data`` directory itself, or a
        directory that contains ``Settings.json``/``Profiles``.
        """
        if root_dir is None:
            return None
        root = Path(root_dir)
        candidates = [
            root / _DATA_DIRNAME,
            root,
            root / "ClassIsland" / _DATA_DIRNAME,
        ]
        for candidate in candidates:
            if (candidate / _PROFILES_DIRNAME).is_dir() or (candidate / _SETTINGS_FILENAME).is_file():
                return candidate
        return None

    @classmethod
    def auto_detect(cls) -> "ClassIslandConfigParser":
        """Best-effort detection of the running ClassIsland installation."""
        parser = cls()
        exe = cls.find_process_exe()
        if exe is not None:
            parser._root_dir = exe.parent
            data_dir = cls.data_dir_from_exe(exe)
            if data_dir is not None:
                parser._data_dir = data_dir
                logger.info("ClassIsland data directory: %s", data_dir)
            else:
                logger.warning("ClassIsland found at %s but no data directory nearby.", exe)
        return parser

    # -- setters used by the wizard when the user corrects the paths -----

    def set_root_dir(self, root_dir: Optional[Path]) -> None:
        self._root_dir = Path(root_dir) if root_dir else None

    def set_data_dir(self, data_dir: Optional[Path]) -> None:
        self._data_dir = Path(data_dir) if data_dir else None

    @property
    def root_dir(self) -> Optional[Path]:
        return self._root_dir

    @property
    def data_dir(self) -> Optional[Path]:
        """Resolve the data directory, falling back to root-derived guesses."""
        if self._data_dir is not None and self._data_dir.is_dir():
            return self._data_dir
        if self._root_dir is not None:
            derived = self.data_dir_from_root(self._root_dir)
            if derived is not None:
                return derived
        return self._data_dir

    @property
    def settings_path(self) -> Optional[Path]:
        data_dir = self.data_dir
        if data_dir is None:
            return None
        return data_dir / _SETTINGS_FILENAME

    @property
    def profiles_dir(self) -> Optional[Path]:
        data_dir = self.data_dir
        if data_dir is None:
            return None
        return data_dir / _PROFILES_DIRNAME

    def resolved_paths(self) -> CiPaths:
        return CiPaths(
            root_dir=self.root_dir,
            data_dir=self.data_dir,
            settings_path=self.settings_path,
            profiles_dir=self.profiles_dir,
        )

    # ------------------------------------------------------------------
    # settings.json
    # ------------------------------------------------------------------

    def read_selected_profile(self) -> Optional[str]:
        """Return ``SelectedProfile`` from ``data/Settings.json``."""
        settings_path = self.settings_path
        data, error = load_json_file(settings_path)
        if error:
            logger.warning("Cannot read ClassIsland settings: %s", error)
            return None
        if not isinstance(data, dict):
            logger.warning("ClassIsland settings is not a JSON object: %s", settings_path)
            return None

        selected = _find_key(data, "SelectedProfile")
        if not isinstance(selected, str) or not selected.strip():
            logger.warning("SelectedProfile missing/empty in %s", settings_path)
            return None
        return selected.strip()

    # ------------------------------------------------------------------
    # profiles
    # ------------------------------------------------------------------

    def load_json_with_backup(self, path: Path) -> tuple:
        """Load *path*, falling back to ``<path>.bak`` variants.

        Returns ``(data, used_backup, warning, error)``.
        """
        data, error = load_json_file(path)
        if error is None:
            return data, False, None, None

        # Try the backups before giving up.
        for backup in backup_candidates(path):
            if not backup.is_file():
                continue
            backup_data, backup_error = load_json_file(backup)
            if backup_error is None:
                warning = "主配置文件损坏，已自动加载备份配置文件"
                logger.warning("%s (main=%s, backup=%s)", warning, path, backup)
                return backup_data, True, warning, None
            logger.warning("Backup %s is also unreadable: %s", backup, backup_error)

        return None, False, None, error

    def layouts_from_data(
        self,
        profile_file: str,
        data: Any,
    ) -> List[TimeLayoutOption]:
        """Extract every ``TimeLayout`` from a decoded profile document."""
        container = _find_layouts_container(data)
        if not isinstance(container, dict):
            logger.warning("No TimeLayouts found in %s", profile_file)
            return []

        options: List[TimeLayoutOption] = []
        for layout_id, raw_layout in container.items():
            option = self._parse_layout(profile_file, str(layout_id), raw_layout)
            if option is not None:
                options.append(option)
        return options

    def _parse_layout(
        self,
        profile_file: str,
        layout_id: str,
        raw_layout: Any,
    ) -> Optional[TimeLayoutOption]:
        if not isinstance(raw_layout, dict):
            return None

        name = _find_key(raw_layout, "Name", "name")
        layout_name = str(name).strip() if isinstance(name, str) and name.strip() else f"时间表 {layout_id[:8]}"

        nodes = _find_key(raw_layout, "Layouts", "Layout", "layouts", "layout")
        if not isinstance(nodes, list):
            logger.warning("Layout %s in %s has no Layouts list", layout_id, profile_file)
            return None

        entries: List[TimeLayoutEntry] = []
        for index, node in enumerate(nodes, start=1):
            entry = self._parse_entry(index, node)
            if entry is not None:
                entries.append(entry)

        if not entries:
            return None

        return TimeLayoutOption(
            profile_file=profile_file,
            layout_id=layout_id,
            name=layout_name,
            entries=entries,
        )

    def _parse_entry(self, index: int, node: Any) -> Optional[TimeLayoutEntry]:
        if not isinstance(node, dict):
            return None

        start = normalize_clock(_find_key(node, "StartTime", "start"))
        end = normalize_clock(_find_key(node, "EndTime", "end"))
        if start is None or end is None:
            logger.debug("Skipping node %s with unusable times: %r", index, node)
            return None

        time_type = parse_time_type(_find_key(node, "TimeType", "time_type"))
        if time_type is None:
            time_type = TIME_TYPE_CLASS

        return TimeLayoutEntry(index=index, time_type=time_type, start=start, end=end)

    def load_profile(self, profile_file: str) -> ProfileLoadResult:
        """Load one profile file (with ``.bak`` fallback) and its layouts."""
        result = ProfileLoadResult(profile_file=profile_file)
        profiles_dir = self.profiles_dir
        if profiles_dir is None:
            result.error = "未找到 ClassIsland 数据目录。"
            return result

        path = profiles_dir / profile_file
        if not path.is_file():
            # The user may have typed just a stem; try to resolve it.
            stem_path = profiles_dir / f"{Path(profile_file).stem}.json"
            if stem_path.is_file():
                path = stem_path
                result.profile_file = path.name
            else:
                result.error = f"配置文件不存在：{path}"
                logger.warning("%s", result.error)
                return result

        data, used_backup, warning, error = self.load_json_with_backup(path)
        if error is not None:
            result.error = f"配置文件解析失败：{error}"
            return result

        result.used_backup = used_backup
        result.warning = warning
        result.layouts = self.layouts_from_data(result.profile_file, data)
        if not result.layouts:
            result.error = f"{result.profile_file} 中没有找到可用的时间表。"
        return result

    def scan_all_layouts(self) -> List[TimeLayoutOption]:
        """Scan **every** ``Profiles/*.json`` and return all timetables.

        The active profile is listed first so the UI can preselect it, the
        remaining files follow in alphabetical order.
        """
        return self.scan_result().options

    def scan_result(self) -> "ScanResult":
        """Like :meth:`scan_all_layouts` but also reports warnings/errors."""
        result = ScanResult()
        profiles_dir = self.profiles_dir
        if profiles_dir is None or not profiles_dir.is_dir():
            result.errors.append("未找到 Profiles 文件夹，请检查 ClassIsland 数据目录。")
            return result

        selected = self.read_selected_profile()
        if selected is None:
            result.warnings.append("未能读取 Settings.json 中的 SelectedProfile，将按文件名顺序列出。")

        files = _iter_profile_files(profiles_dir)
        ordered: List[Path] = []
        if selected:
            for path in files:
                if path.name == selected or path.stem == Path(selected).stem:
                    ordered.append(path)
        for path in files:
            if path not in ordered:
                ordered.append(path)

        for path in ordered:
            load_result = self.load_profile(path.name)
            if load_result.error:
                logger.warning("Skipping profile %s: %s", path.name, load_result.error)
                result.errors.append(f"{path.name}：{load_result.error}")
                continue
            if load_result.warning:
                result.warnings.append(f"{path.name}：{load_result.warning}")
            result.options.extend(load_result.layouts)

        if not result.options and not result.errors:
            result.errors.append("没有从 ClassIsland 配置中找到任何时间表。")
        return result

    def default_layout(self, options: List[TimeLayoutOption]) -> Optional[TimeLayoutOption]:
        """Pick the timetable belonging to ``SelectedProfile`` when possible."""
        if not options:
            return None
        selected = self.read_selected_profile()
        if selected:
            stem = Path(selected).stem
            for option in options:
                if option.profile_stem == stem:
                    return option
        return options[0]


# ---------------------------------------------------------------------------
# module-level helpers (also used by other modules)
# ---------------------------------------------------------------------------


def load_json_file(path: Optional[Path]) -> tuple:
    """Read a UTF-8 JSON file, returning ``(data, error_message)``."""
    if path is None:
        return None, "路径为空。"
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, f"文件不存在：{path}"
    except OSError as exc:
        logger.warning("Failed to read %s: %s", path, exc)
        return None, f"读取文件失败：{exc}"
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        logger.warning("Invalid JSON in %s: %s", path, exc)
        return None, f"JSON 不合法（第 {exc.lineno} 行第 {exc.colno} 列）：{exc.msg}"
