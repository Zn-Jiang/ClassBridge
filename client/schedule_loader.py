from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Any, List, Optional, Tuple


CLIENT_DIR = Path(__file__).resolve().parent
LEGACY_SCHEDULE_PATH = CLIENT_DIR / "schedule.json"
logger = logging.getLogger("kg.client.schedule")


@dataclass(frozen=True)
class ScheduleSource:
    key: str
    label: str
    path: Path


def runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def runtime_schedule_dir() -> Path:
    return runtime_root() / "schedule"


def client_schedule_dir() -> Path:
    return CLIENT_DIR / "schedule"


def list_schedule_sources() -> List[ScheduleSource]:
    sources: List[ScheduleSource] = []
    seen_paths: set[Path] = set()

    for schedule_dir, key_prefix in (
        (runtime_schedule_dir(), "schedule"),
        (client_schedule_dir(), "client/schedule"),
    ):
        if not schedule_dir.exists():
            continue
        for path in sorted(schedule_dir.glob("*.json")):
            resolved = path.resolve()
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            sources.append(
                ScheduleSource(
                    key=f"{key_prefix}/{path.name}",
                    label=path.stem,
                    path=path,
                )
            )

    if LEGACY_SCHEDULE_PATH.exists():
        resolved = LEGACY_SCHEDULE_PATH.resolve()
        if resolved in seen_paths:
            return sources
        sources.append(
            ScheduleSource(
                key="client/schedule.json",
                label="schedule",
                path=LEGACY_SCHEDULE_PATH,
            )
        )

    return sources


def resolve_schedule_source(selected_key: Optional[str]) -> Optional[ScheduleSource]:
    sources = list_schedule_sources()
    if not sources:
        return None
    if selected_key:
        for item in sources:
            if item.key == selected_key:
                return item
    return sources[0]


def load_schedule_break_ranges(path: Optional[Path]) -> List[Tuple[time, time]]:
    ranges, _ = validate_schedule_file(path)
    return ranges


def validate_schedule_file(path: Optional[Path]) -> Tuple[List[Tuple[time, time]], Optional[str]]:
    if path is None or not path.exists():
        return _warn_invalid(path, "时间表文件不存在。")

    data, load_error = _load_json_file(path)
    if load_error:
        return _warn_invalid(path, load_error)
    if not isinstance(data, dict):
        return _warn_invalid(path, "时间表不是合法的 JSON 对象。")

    ranges = []
    breaks = data.get("breaks", [])
    if not isinstance(breaks, list):
        return _warn_invalid(path, "时间表中的 breaks 字段不是数组。")

    item_errors: List[str] = []
    for index, item in enumerate(breaks, start=1):
        if not isinstance(item, dict):
            logger.warning("Skipping invalid break item in %s: %r", path, item)
            item_errors.append(f"第 {index} 项不是对象")
            continue
        start_text = item.get("start")
        end_text = item.get("end")
        if not isinstance(start_text, str) or not isinstance(end_text, str):
            logger.warning("Skipping break with missing start/end in %s: %r", path, item)
            item_errors.append(f"第 {index} 项缺少 start 或 end")
            continue
        try:
            start = _parse_clock(start_text)
            end = _parse_clock(end_text)
        except ValueError:
            logger.warning("Skipping break with invalid time format in %s: %r", path, item)
            item_errors.append(f"第 {index} 项时间格式错误，应为 HH:MM")
            continue
        ranges.append((start, end))
    if not ranges:
        if item_errors:
            return _warn_invalid(path, "时间表中没有可用的 break 配置：" + "；".join(item_errors[:3]))
        return _warn_invalid(path, "时间表中没有可用的 break 配置。")
    return ranges, None


def _load_json_file(path: Path) -> Tuple[Optional[Any], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "时间表文件不存在。"
    except OSError as exc:
        logger.warning("Failed to read schedule file %s: %s", path, exc)
        return None, f"读取时间表文件失败：{exc}"
    except json.JSONDecodeError as exc:
        logger.warning("Invalid schedule JSON %s: %s", path, exc)
        return None, f"JSON 不合法：第 {exc.lineno} 行第 {exc.colno} 列附近有语法错误。"


def _warn_invalid(path: Optional[Path], message: str) -> Tuple[List[Tuple[time, time]], str]:
    logger.warning("Schedule validation failed for %s: %s", path, message)
    return [], message


def _parse_clock(value: str) -> time:
    hour_text, minute_text = value.split(":", 1)
    return time(hour=int(hour_text), minute=int(minute_text))
