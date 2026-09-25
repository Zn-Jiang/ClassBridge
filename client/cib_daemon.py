"""Daemon helpers for the ClassIsland.WSBridge (CIB) companion program.

The classroom client talks to ClassIsland through CIB: a small self-contained
executable that bridges ClassIsland's IPC events onto a local WebSocket
(``ws://localhost:6614/``).

**CIB 2.0 capabilities protocol** (see ``classisland-ws-bridge/Release/v2.0/``):
after connecting, a client may send::

    {"action": "capabilities"}      # or the plain string "capabilities"

and receives::

    {"type": "capabilities",
     "data": {"课程服务": [...], "订阅": ["OnClassNotifyId", "OnBreakingTimeNotifyId"]}}

Events are then pushed as ``{"type": "event", "eventName": "..."}``.
``{"action": "get_properties", "keys": [...]}`` returns live lesson properties.

Because of that, this module never decides "is the bridge available?" from the
*process name* — a renamed or upgraded binary would break such a check.  The
decision is made by **probing the port and asking for its capabilities**:

1. Is ClassIsland itself running?  (without it, a bridge can never connect)
2. Can we reach port 6614 over WebSocket?
3. Does ``capabilities`` advertise ``OnBreakingTimeNotifyId``?

Only if all three hold is the bridge usable; otherwise the caller is told to
start CIB (or to start ClassIsland first) and degrades to the local timetable.

The interactive part (asking whether an occupied port may be freed) is injected
as an async callback, so the module stays completely UI-independent.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import psutil
import websockets

from .classisland_import import ClassIslandConfigParser

logger = logging.getLogger("kg.client.cib_daemon")

#: Executable name of the bridge program (used for launching and diagnostics,
#: **never** as the sole availability check).
CIB_PROCESS_NAME = "ClassIsland.WSBridge.exe"
#: Port the bridge listens on.
CIB_PORT = 6614
#: WebSocket endpoint exposed by CIB 2.0 (the listener serves the root path).
CIB_WS_URL = f"ws://localhost:{CIB_PORT}/"
#: Capability / subscription that proves the bridge can report break times.
BREAK_EVENT_CAPABILITY = "OnBreakingTimeNotifyId"
#: Subscription for "class started" events.
CLASS_EVENT_CAPABILITY = "OnClassNotifyId"

#: Signature of the injected confirmation callback.
ConfirmKillCallback = Callable[[str, int], Awaitable[bool]]

_WS_TIMEOUT_SECONDS = 3.0
_PORT_RELEASE_TIMEOUT_SECONDS = 5.0
_STARTUP_READY_TIMEOUT_SECONDS = 15.0
_POLL_INTERVAL_SECONDS = 0.5


class CibState(str, Enum):
    """Outcome of :func:`ensure_cib_running`."""

    #: A usable bridge answered (capabilities probe passed).
    READY = "ready"
    #: The bridge was started by us and passed the capabilities probe.
    LAUNCHED = "launched"
    #: ClassIsland itself is not running — a bridge could never connect.
    CLASSISLAND_NOT_RUNNING = "classisland_not_running"
    #: Something answers on 6614 but does not offer the break-time event.
    BRIDGE_UNRESPONSIVE = "bridge_unresponsive"
    #: Nothing is listening on 6614 and the bridge could not be started.
    NOT_RUNNING = "not_running"
    #: The bridge was launched but never became ready in time.
    LAUNCH_TIMEOUT = "launch_timeout"
    #: Launching the executable failed.
    LAUNCH_FAILED = "launch_failed"
    #: Port 6614 is held by another process and the user declined to free it.
    PORT_CONFLICT_DECLINED = "port_conflict_declined"
    #: ``ClassIsland.WSBridge.exe`` was not found on disk.
    EXECUTABLE_MISSING = "executable_missing"


@dataclass
class BridgeProbe:
    """Result of one capabilities probe against the bridge port."""

    reachable: bool = False
    capabilities: Dict[str, List[str]] = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def subscriptions(self) -> List[str]:
        """Capabilities advertised under the subscription group."""
        return list(self.capabilities.get("订阅", []))

    @property
    def services(self) -> List[str]:
        """Capabilities advertised under the lesson-service group."""
        return list(self.capabilities.get("课程服务", []))

    def has_capability(self, name: str) -> bool:
        """True when *name* appears in any advertised group."""
        lowered = name.lower()
        for values in self.capabilities.values():
            for value in values:
                if str(value).lower() == lowered:
                    return True
        return False

    @property
    def has_break_event(self) -> bool:
        """True when the bridge can report break-time events."""
        return self.has_capability(BREAK_EVENT_CAPABILITY)

    @property
    def ok(self) -> bool:
        """The bridge is reachable *and* offers the break-time event."""
        return self.reachable and self.has_break_event

    @property
    def all_capabilities(self) -> List[str]:
        seen: List[str] = []
        for values in self.capabilities.values():
            for value in values:
                if value not in seen:
                    seen.append(value)
        return seen

    def describe(self) -> str:
        if not self.reachable:
            return f"端口 {CIB_PORT} 无法连接（{self.error or '无响应'}）"
        if not self.has_break_event:
            return (
                f"端口 {CIB_PORT} 有服务响应，但其 capabilities 未提供 "
                f"{BREAK_EVENT_CAPABILITY}（能力：{self.all_capabilities or '无'}）"
            )
        return f"桥接器能力探测通过（{len(self.all_capabilities)} 项能力）"


@dataclass
class CibEnsureResult:
    """Result of :func:`ensure_cib_running`."""

    ok: bool
    state: CibState
    message: str
    probe: Optional[BridgeProbe] = None
    exe_path: Optional[Path] = None
    process: Optional[subprocess.Popen] = None
    conflict_process: Optional[str] = None
    conflict_pid: Optional[int] = None
    killed_pid: Optional[int] = None

    @property
    def should_degrade(self) -> bool:
        """True when the caller must fall back to the local static timetable."""
        return not self.ok

    def describe(self) -> str:
        return f"[{self.state.value}] {self.message}"


# ---------------------------------------------------------------------------
# locating the executable
# ---------------------------------------------------------------------------


def get_project_root() -> Path:
    """Return the application root directory.

    Frozen (PyInstaller) builds live next to ``sys.executable``; in a source
    checkout this is the directory containing ``client/``.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def client_directory() -> Path:
    """Return the directory holding the client package (``client/``)."""
    return Path(__file__).resolve().parent


def cib_search_paths(configured_path: Optional[str] = None) -> List[Path]:
    """Every location consulted for ``ClassIsland.WSBridge.exe``, in order.

    Used only when the bridge has to be *launched*; availability itself is
    decided by :func:`probe_bridge`.
    """
    candidates: List[Path] = []

    if configured_path:
        candidates.append(Path(configured_path))

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "bin" / CIB_PROCESS_NAME)
        candidates.append(Path(meipass) / CIB_PROCESS_NAME)

    client_dir = client_directory()
    # Preferred layout: <client>/bin/ClassIsland.WSBridge.exe
    candidates.append(client_dir / "bin" / CIB_PROCESS_NAME)
    candidates.append(client_dir / CIB_PROCESS_NAME)

    project_root = get_project_root()
    candidates.append(project_root / "bin" / CIB_PROCESS_NAME)
    candidates.append(project_root / CIB_PROCESS_NAME)

    unique: List[Path] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def get_cib_executable_path(configured_path: Optional[str] = None) -> Path:
    """Locate ``ClassIsland.WSBridge.exe`` (needed to *start* the bridge).

    Resolution order (see :func:`cib_search_paths`):

    1. ``configured_path`` — the ``cib_exe_path`` setting in ``client.toml``.
    2. ``<client>/bin/ClassIsland.WSBridge.exe`` — the self-contained layout.
    3. ``<client>/ClassIsland.WSBridge.exe`` — next to the client code.
    4. ``<project root|exe dir>/bin/`` and ``<project root|exe dir>/``.
    5. ``sys._MEIPASS/…`` for frozen single-file builds.

    Raises:
        FileNotFoundError: when none of the candidates exists.
    """
    for candidate in cib_search_paths(configured_path):
        if candidate.is_file():
            logger.debug("Using CIB at %s", candidate)
            return candidate

    if configured_path:
        logger.warning("Configured cib_exe_path does not exist: %s", configured_path)
    logger.error(
        "ClassIsland.WSBridge.exe not found. Searched: %s",
        ", ".join(str(path) for path in cib_search_paths(configured_path)),
    )
    raise FileNotFoundError("未找到 ClassIsland.WSBridge.exe")


# ---------------------------------------------------------------------------
# ClassIsland / bridge state inspection
# ---------------------------------------------------------------------------


def is_classisland_running() -> Optional[bool]:
    """Whether ClassIsland itself is running.

    ``None`` means "could not be determined" (psutil missing or the process
    table unreadable) — callers should keep their current behaviour then.
    """
    return ClassIslandConfigParser.is_process_running()


def is_bridge_process_running() -> bool:
    """Whether a process named ``ClassIsland.WSBridge.exe`` exists.

    Diagnostics only — a renamed build would make this return ``False`` even
    though the bridge works, so never base availability on it.
    """
    try:
        for process in psutil.process_iter(["name"]):
            try:
                name = str(process.info.get("name") or "")
                if name.lower() == CIB_PROCESS_NAME.lower():
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Failed to enumerate processes while looking for CIB: %s", exc)
    return False


def find_port_holder(port: int = CIB_PORT) -> Optional[Tuple[str, int]]:
    """Return ``(process_name, pid)`` of the listener on *port*, if any."""
    try:
        connections = psutil.net_connections(kind="tcp")
    except (psutil.AccessDenied, OSError) as exc:
        logger.warning("Cannot inspect TCP connections: %s", exc)
        return None
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Unexpected error inspecting TCP connections: %s", exc)
        return None

    for conn in connections:
        try:
            if conn.status != psutil.CONN_LISTEN:
                continue
            local = conn.laddr
            if not local or local.port != port:
                continue
            pid = conn.pid
            if pid is None:
                continue
            try:
                name = psutil.Process(pid).name()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                name = f"未知进程 (PID {pid})"
            return name, int(pid)
        except Exception:  # pragma: no cover - defensive
            continue
    return None


# ---------------------------------------------------------------------------
# capability probing (the authoritative availability check)
# ---------------------------------------------------------------------------


async def query_capabilities(
    url: str = CIB_WS_URL,
    timeout: float = _WS_TIMEOUT_SECONDS,
) -> BridgeProbe:
    """Connect to the bridge and ask for its capability list.

    Never raises: a failure is reported through :class:`BridgeProbe`.
    """
    try:
        async with websockets.connect(
            url,
            open_timeout=timeout,
            close_timeout=1,
            ping_interval=None,
            max_size=2**20,
        ) as websocket:
            await websocket.send(json.dumps({"action": "capabilities"}))
            raw = await asyncio.wait_for(websocket.recv(), timeout=timeout)
    except Exception as exc:
        logger.debug("Bridge capabilities probe failed (%s): %s", url, exc)
        return BridgeProbe(reachable=False, error=str(exc))

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("Bridge returned non-JSON to capabilities: %r (%s)", raw[:120], exc)
        return BridgeProbe(reachable=True, error=f"capabilities 响应不是 JSON：{exc}")

    if not isinstance(payload, dict):
        return BridgeProbe(reachable=True, error="capabilities 响应不是 JSON 对象")

    data = payload.get("data")
    if not isinstance(data, dict):
        # Reached something, but it does not speak the CIB protocol.
        return BridgeProbe(
            reachable=True,
            error=f"capabilities 响应缺少 data 字段：{str(payload)[:120]}",
        )

    capabilities: Dict[str, List[str]] = {}
    for group, values in data.items():
        if isinstance(values, list):
            capabilities[str(group)] = [str(item) for item in values]

    probe = BridgeProbe(reachable=True, capabilities=capabilities)
    logger.info("Bridge probe: %s", probe.describe())
    return probe


async def probe_bridge(
    url: str = CIB_WS_URL,
    timeout: float = _WS_TIMEOUT_SECONDS,
) -> BridgeProbe:
    """Probe the bridge port and report whether break events are available."""
    return await query_capabilities(url, timeout)


async def is_bridge_ready(url: str = CIB_WS_URL) -> bool:
    """True when a bridge answers *and* advertises the break-time event."""
    return (await probe_bridge(url)).ok


async def wait_for_bridge(
    timeout: float,
    url: str = CIB_WS_URL,
) -> BridgeProbe:
    """Poll until the bridge is ready or *timeout* elapses."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + max(0.0, timeout)
    probe = BridgeProbe(reachable=False, error="未探测")
    while True:
        probe = await probe_bridge(url)
        if probe.ok:
            return probe
        if loop.time() >= deadline:
            return probe
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# process control
# ---------------------------------------------------------------------------


def kill_process(pid: int, timeout: float = 3.0) -> bool:
    """Kill *pid* and wait until it is gone.  Returns True on success."""
    try:
        process = psutil.Process(pid)
    except psutil.NoSuchProcess:
        logger.info("Process %s already gone", pid)
        return True
    except psutil.AccessDenied as exc:
        logger.error("Access denied while opening process %s: %s", pid, exc)
        return False

    try:
        process.kill()
        process.wait(timeout=timeout)
        logger.info("Killed process %s (%s)", pid, process.name())
        return True
    except psutil.NoSuchProcess:
        return True
    except psutil.TimeoutExpired:
        logger.error("Process %s did not exit within %.1fs", pid, timeout)
        return False
    except psutil.AccessDenied as exc:
        logger.error("Access denied while killing process %s: %s", pid, exc)
        return False
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("Failed to kill process %s: %s", pid, exc)
        return False


def launch_cib(exe_path: Path) -> Optional[subprocess.Popen]:
    """Start the bridge; returns the process handle or ``None`` on failure."""
    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        process = subprocess.Popen(
            [str(exe_path)],
            cwd=str(exe_path.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except OSError as exc:
        logger.error("Failed to launch CIB (%s): %s", exe_path, exc)
        return None
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("Unexpected error launching CIB: %s", exc)
        return None

    logger.info("Launched CIB: %s (pid=%s)", exe_path, process.pid)
    return process


async def _wait_for_port_release(port: int = CIB_PORT, timeout: float = _PORT_RELEASE_TIMEOUT_SECONDS) -> bool:
    """Wait until nothing is listening on *port* any more."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + max(0.0, timeout)
    while True:
        if find_port_holder(port) is None:
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------


async def ensure_cib_running(
    *,
    confirm_kill_callback: Optional[ConfirmKillCallback] = None,
    exe_path: Optional[str] = None,
    port: int = CIB_PORT,
    url: str = CIB_WS_URL,
    wait_ready: bool = True,
) -> CibEnsureResult:
    """Make sure a *working* ClassIsland bridge is available.

    The flow follows the capability-based rule set:

    1. **Is ClassIsland running?**  Without it the bridge can never connect, so
       there is no point launching anything — report and degrade.
    2. **Probe port 6614 and ask for ``capabilities``.**  If the answer
       advertises ``OnBreakingTimeNotifyId`` the bridge is usable, regardless of
       what the executable is called or which version it is.
    3. Otherwise (nothing there, or something that is not a usable bridge)
       resolve port conflicts — if the port is held by a foreign process, ask
       via *confirm_kill_callback* before freeing it.
    4. Launch ``ClassIsland.WSBridge.exe`` and probe again.

    Args:
        confirm_kill_callback: async ``(process_name, pid) -> bool``; when
            omitted, a port conflict is treated as a refusal.
        exe_path: optional explicit executable path (``cib_exe_path``).
        port: port to inspect (defaults to 6614).
        url: WebSocket endpoint used for the capabilities probe.
        wait_ready: wait for the bridge to become ready after launching.

    Returns:
        A :class:`CibEnsureResult`; check :attr:`CibEnsureResult.should_degrade`.
    """
    # --- 1) ClassIsland must be running -------------------------------------
    classisland_alive = is_classisland_running()
    if classisland_alive is False:
        logger.warning("ClassIsland is not running; the bridge cannot be used")
        return CibEnsureResult(
            False,
            CibState.CLASSISLAND_NOT_RUNNING,
            "未检测到 ClassIsland 进程，启动桥接器也无法连接，请先启动 ClassIsland。",
        )
    if classisland_alive is None:
        logger.info("ClassIsland process state unknown; continuing with bridge probe")

    # --- 2) Capability probe (name/version independent) ---------------------
    probe = await probe_bridge(url)
    if probe.ok:
        logger.info("Bridge already usable: %s", probe.describe())
        return CibEnsureResult(True, CibState.READY, "ClassIsland 桥接器已就绪。", probe=probe)

    logger.info("Bridge not usable yet: %s", probe.describe())

    # --- 3) Something is listening but it is not a usable bridge ------------
    holder = find_port_holder(port)
    killed_pid: Optional[int] = None
    conflict_process: Optional[str] = None
    conflict_pid: Optional[int] = None

    if holder is not None and not probe.reachable:
        # A foreign listener owns the port: ask before touching it.
        conflict_process, conflict_pid = holder
        if confirm_kill_callback is None:
            logger.warning(
                "Port %s held by %s (PID %s); no confirmation callback, not killing",
                port,
                conflict_process,
                conflict_pid,
            )
            return CibEnsureResult(
                False,
                CibState.PORT_CONFLICT_DECLINED,
                f"端口 {port} 被 {conflict_process} (PID: {conflict_pid}) 占用，且未提供确认回调。",
                probe=probe,
                conflict_process=conflict_process,
                conflict_pid=conflict_pid,
            )

        try:
            approved = bool(await confirm_kill_callback(conflict_process, conflict_pid))
        except Exception as exc:
            logger.exception("Confirmation callback failed: %s", exc)
            approved = False

        if not approved:
            logger.info("User declined to free port %s from %s", port, conflict_process)
            return CibEnsureResult(
                False,
                CibState.PORT_CONFLICT_DECLINED,
                f"用户拒绝结束占用端口 {port} 的进程 {conflict_process} (PID: {conflict_pid})。",
                probe=probe,
                conflict_process=conflict_process,
                conflict_pid=conflict_pid,
            )

        if not kill_process(conflict_pid):
            return CibEnsureResult(
                False,
                CibState.LAUNCH_FAILED,
                f"无法结束占用进程 {conflict_process} (PID: {conflict_pid})。",
                probe=probe,
                conflict_process=conflict_process,
                conflict_pid=conflict_pid,
            )
        killed_pid = conflict_pid

        if not await _wait_for_port_release(port):
            return CibEnsureResult(
                False,
                CibState.LAUNCH_FAILED,
                f"结束 {conflict_process} 后端口 {port} 仍被占用。",
                probe=probe,
                conflict_process=conflict_process,
                conflict_pid=conflict_pid,
                killed_pid=killed_pid,
            )
        logger.info("Port %s released after killing PID %s", port, conflict_pid)

    # --- 4) Launch the bridge ----------------------------------------------
    try:
        executable = get_cib_executable_path(exe_path)
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        state = CibState.BRIDGE_UNRESPONSIVE if probe.reachable else CibState.EXECUTABLE_MISSING
        return CibEnsureResult(
            False,
            state,
            f"{exc} 请手动启动 ClassIsland 桥接器（或配置 cib_exe_path）。",
            probe=probe,
            conflict_process=conflict_process,
            conflict_pid=conflict_pid,
            killed_pid=killed_pid,
        )

    process = launch_cib(executable)
    if process is None:
        return CibEnsureResult(
            False,
            CibState.LAUNCH_FAILED,
            "启动 ClassIsland.WSBridge.exe 失败。",
            probe=probe,
            exe_path=executable,
            conflict_process=conflict_process,
            conflict_pid=conflict_pid,
            killed_pid=killed_pid,
        )

    # --- 5) Probe again ----------------------------------------------------
    if wait_ready:
        probe = await wait_for_bridge(_STARTUP_READY_TIMEOUT_SECONDS, url)
        if not probe.ok:
            logger.warning("CIB launched (pid=%s) but is still not ready: %s", process.pid, probe.describe())
            return CibEnsureResult(
                False,
                CibState.LAUNCH_TIMEOUT,
                f"已启动 ClassIsland 桥接器 (PID: {process.pid})，但等待其就绪超时：{probe.describe()}",
                probe=probe,
                exe_path=executable,
                process=process,
                conflict_process=conflict_process,
                conflict_pid=conflict_pid,
                killed_pid=killed_pid,
            )

    logger.info("Bridge launched and ready (pid=%s)", process.pid)
    return CibEnsureResult(
        True,
        CibState.LAUNCHED,
        f"已启动 ClassIsland 桥接器 (PID: {process.pid}) 并通过能力探测。",
        probe=probe,
        exe_path=executable,
        process=process,
        conflict_process=conflict_process,
        conflict_pid=conflict_pid,
        killed_pid=killed_pid,
    )


def describe_degradation(result: CibEnsureResult) -> str:
    """Human-readable explanation for falling back to the local timetable."""
    if result.state == CibState.CLASSISLAND_NOT_RUNNING:
        return "未检测到 ClassIsland，已降级为本地静态课表模式（请先启动 ClassIsland）。"
    if result.state == CibState.EXECUTABLE_MISSING:
        return "未找到 ClassIsland 桥接器程序，请手动启动 ClassIsland.WSBridge.exe；已降级为本地静态课表模式。"
    if result.state == CibState.BRIDGE_UNRESPONSIVE:
        return "端口 6614 上的服务未提供课间事件能力，请确认桥接器版本；已降级为本地静态课表模式。"
    if result.state == CibState.PORT_CONFLICT_DECLINED:
        return "端口冲突未解决，已降级为本地静态课表模式。"
    return f"{result.message} 已降级为本地静态课表模式。"
