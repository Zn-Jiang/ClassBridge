"""Daemon helpers for the ClassIsland.WSBridge (CIB) companion program.

The classroom client talks to ClassIsland through CIB: a small self-contained
``ClassIsland.WSBridge.exe`` that bridges ClassIsland's IPC events onto a local
WebSocket (``ws://127.0.0.1:6614/status``).  Before using ClassIsland at all we
must therefore make sure that CIB is running and its WebSocket answers.

This module owns that responsibility:

* :func:`get_cib_executable_path` locates the bundled executable;
* :func:`is_process_running` / :func:`check_websocket` / :func:`is_cib_healthy`
  inspect the current state;
* :func:`find_port_holder` reports who occupies port 6614;
* :func:`ensure_cib_running` implements the full "verify → resolve conflicts →
  launch" flow and returns a :class:`CibEnsureResult`.

The interactive part (asking the user whether an occupied port may be freed) is
injected as an async callback, so this module stays completely UI-independent.
When CIB cannot be made available the caller is expected to degrade to the
locally stored static timetable.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable, List, Optional, Tuple

import psutil
import websockets

logger = logging.getLogger("kg.client.cib_daemon")

#: Executable name of the bridge program.
CIB_PROCESS_NAME = "ClassIsland.WSBridge.exe"
#: Port the bridge listens on.
CIB_PORT = 6614
#: WebSocket endpoint exposed by the bridge (it registers the ``/status`` path).
CIB_WS_URL = f"ws://127.0.0.1:{CIB_PORT}/status"

#: Signature of the injected confirmation callback.
ConfirmKillCallback = Callable[[str, int], Awaitable[bool]]

_WS_TIMEOUT_SECONDS = 2.0
_PORT_RELEASE_TIMEOUT_SECONDS = 5.0
_STARTUP_READY_TIMEOUT_SECONDS = 12.0
_POLL_INTERVAL_SECONDS = 0.25


class CibState(str, Enum):
    """Outcome of :func:`ensure_cib_running`."""

    ALREADY_RUNNING = "already_running"
    LAUNCHED = "launched"
    NOT_RESPONDING = "not_responding"
    LAUNCH_TIMEOUT = "launch_timeout"
    LAUNCH_FAILED = "launch_failed"
    PORT_CONFLICT_DECLINED = "port_conflict_declined"
    EXECUTABLE_MISSING = "executable_missing"


@dataclass
class CibEnsureResult:
    """Result of :func:`ensure_cib_running`."""

    ok: bool
    state: CibState
    message: str
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

    The client is deployed as a self-contained folder, so ``client/bin/`` is
    the primary location; the remaining entries keep other layouts working
    (project-root ``bin/``, a frozen build's ``_MEIPASS``, or the executable
    sitting right next to the client code).
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

    # De-duplicate while preserving priority order.
    unique: List[Path] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def get_cib_executable_path(configured_path: Optional[str] = None) -> Path:
    """Locate ``ClassIsland.WSBridge.exe``.

    Resolution order (see :func:`cib_search_paths`):

    1. ``configured_path`` — the ``cib_exe_path`` setting in ``client.toml``.
       An explicit user choice always wins when the file exists.
    2. ``<client>/bin/ClassIsland.WSBridge.exe`` — the self-contained layout
       used by the classroom deployment (and by this repository).
    3. ``<client>/ClassIsland.WSBridge.exe`` — sitting next to the client code.
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
# state inspection
# ---------------------------------------------------------------------------


def is_process_running() -> bool:
    """True when a ``ClassIsland.WSBridge.exe`` process exists."""
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
    return False


async def check_websocket(url: str = CIB_WS_URL, timeout: float = _WS_TIMEOUT_SECONDS) -> bool:
    """Try a short-lived WebSocket connection to the bridge."""
    try:
        async with websockets.connect(
            url,
            open_timeout=timeout,
            close_timeout=1,
            ping_interval=None,
        ):
            return True
    except Exception as exc:
        logger.debug("CIB WebSocket probe failed (%s): %s", url, exc)
        return False


async def is_cib_healthy(url: str = CIB_WS_URL) -> bool:
    """True when the CIB process runs *and* its WebSocket accepts connections."""
    if not is_process_running():
        return False
    return await check_websocket(url)


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


# ---------------------------------------------------------------------------
# launching
# ---------------------------------------------------------------------------


def launch_cib(exe_path: Path) -> Optional[subprocess.Popen]:
    """Start the bridge; returns the process handle or ``None`` on failure.

    ``CREATE_NO_WINDOW`` keeps the classroom desktop tidy — the bridge logs to
    its own file and does not need a console window.
    """
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


async def _wait_for_websocket(timeout: float, url: str = CIB_WS_URL) -> bool:
    """Poll the bridge WebSocket until it answers or *timeout* elapses."""
    deadline = asyncio.get_event_loop().time() + max(0.0, timeout)
    while True:
        if await check_websocket(url):
            return True
        if asyncio.get_event_loop().time() >= deadline:
            return False
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


async def _wait_for_port_release(port: int = CIB_PORT, timeout: float = _PORT_RELEASE_TIMEOUT_SECONDS) -> bool:
    """Wait until nothing is listening on *port* any more."""
    deadline = asyncio.get_event_loop().time() + max(0.0, timeout)
    while True:
        if find_port_holder(port) is None:
            return True
        if asyncio.get_event_loop().time() >= deadline:
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
    wait_ready: bool = True,
    url: str = CIB_WS_URL,
) -> CibEnsureResult:
    """Make sure CIB is running, launching it when necessary.

    Args:
        confirm_kill_callback: async callback ``(process_name, pid) -> bool``
            used when port 6614 is held by another process.  When omitted, a
            conflict is treated as a refusal (nothing is killed).
        exe_path: optional explicit path (``cib_exe_path`` setting).
        port: port to inspect (defaults to 6614).
        wait_ready: wait for the WebSocket to answer after launching.
        url: WebSocket endpoint used for readiness probes.

    Returns:
        A :class:`CibEnsureResult`; check :attr:`CibEnsureResult.should_degrade`
        to decide whether to fall back to the local static timetable.
    """
    # 1) Already running and answering?
    if await is_cib_healthy(url):
        logger.info("CIB already running and reachable")
        return CibEnsureResult(True, CibState.ALREADY_RUNNING, "ClassIsland 桥接器已在运行")

    # 2) Process exists but the WebSocket does not answer — give it a moment
    #    (it may still be starting up).
    if is_process_running():
        if wait_ready and await _wait_for_websocket(_STARTUP_READY_TIMEOUT_SECONDS, url):
            logger.info("CIB became reachable while waiting")
            return CibEnsureResult(True, CibState.ALREADY_RUNNING, "ClassIsland 桥接器已在运行")
        logger.warning("CIB process is running but its WebSocket is unreachable")
        return CibEnsureResult(
            False,
            CibState.NOT_RESPONDING,
            "ClassIsland 桥接器进程存在，但 WebSocket 连接不可用。",
        )

    # 3) Locate the executable before touching anything else.
    try:
        executable = get_cib_executable_path(exe_path)
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return CibEnsureResult(False, CibState.EXECUTABLE_MISSING, str(exc))

    # 4) Port conflict handling.
    killed_pid: Optional[int] = None
    conflict_process: Optional[str] = None
    conflict_pid: Optional[int] = None

    holder = find_port_holder(port)
    if holder is not None:
        conflict_process, conflict_pid = holder
        if conflict_process.lower() == CIB_PROCESS_NAME.lower():
            # Our own bridge holds the port but is not answering: do not kill
            # it blindly — report the unhealthy state instead.
            logger.warning("Port %s is held by CIB itself, which is not responding", port)
            return CibEnsureResult(
                False,
                CibState.NOT_RESPONDING,
                f"端口 {port} 由 ClassIsland 桥接器自身占用，但它没有响应 WebSocket 连接。",
                exe_path=executable,
                conflict_process=conflict_process,
                conflict_pid=conflict_pid,
            )

        if confirm_kill_callback is None:
            logger.warning(
                "Port %s held by %s (PID %s); no confirmation callback available, not killing",
                port,
                conflict_process,
                conflict_pid,
            )
            return CibEnsureResult(
                False,
                CibState.PORT_CONFLICT_DECLINED,
                f"端口 {port} 被 {conflict_process} (PID: {conflict_pid}) 占用，且未提供确认回调。",
                exe_path=executable,
                conflict_process=conflict_process,
                conflict_pid=conflict_pid,
            )

        try:
            approved = bool(await confirm_kill_callback(conflict_process, conflict_pid))
        except Exception as exc:
            logger.exception("Confirmation callback failed: %s", exc)
            approved = False

        if not approved:
            logger.info("User declined to kill %s (PID %s); degrading", conflict_process, conflict_pid)
            return CibEnsureResult(
                False,
                CibState.PORT_CONFLICT_DECLINED,
                f"用户拒绝结束占用端口 {port} 的进程 {conflict_process} (PID: {conflict_pid})。",
                exe_path=executable,
                conflict_process=conflict_process,
                conflict_pid=conflict_pid,
            )

        if not kill_process(conflict_pid):
            return CibEnsureResult(
                False,
                CibState.LAUNCH_FAILED,
                f"无法结束占用进程 {conflict_process} (PID: {conflict_pid})。",
                exe_path=executable,
                conflict_process=conflict_process,
                conflict_pid=conflict_pid,
            )
        killed_pid = conflict_pid

        if not await _wait_for_port_release(port):
            return CibEnsureResult(
                False,
                CibState.LAUNCH_FAILED,
                f"结束 {conflict_process} 后端口 {port} 仍被占用。",
                exe_path=executable,
                conflict_process=conflict_process,
                conflict_pid=conflict_pid,
                killed_pid=killed_pid,
            )
        logger.info("Port %s released after killing PID %s", port, conflict_pid)

    # 5) Start the bridge.
    process = launch_cib(executable)
    if process is None:
        return CibEnsureResult(
            False,
            CibState.LAUNCH_FAILED,
            "启动 ClassIsland.WSBridge.exe 失败。",
            exe_path=executable,
            conflict_process=conflict_process,
            conflict_pid=conflict_pid,
            killed_pid=killed_pid,
        )

    # 6) Optionally wait until it actually answers.
    if wait_ready and not await _wait_for_websocket(_STARTUP_READY_TIMEOUT_SECONDS, url):
        logger.warning("CIB launched (pid=%s) but did not answer within the timeout", process.pid)
        return CibEnsureResult(
            False,
            CibState.LAUNCH_TIMEOUT,
            f"已启动 ClassIsland 桥接器 (PID: {process.pid})，但等待 WebSocket 就绪超时。",
            exe_path=executable,
            process=process,
            conflict_process=conflict_process,
            conflict_pid=conflict_pid,
            killed_pid=killed_pid,
        )

    logger.info("CIB launched and reachable (pid=%s)", process.pid)
    return CibEnsureResult(
        True,
        CibState.LAUNCHED,
        f"已启动 ClassIsland 桥接器 (PID: {process.pid})。",
        exe_path=executable,
        process=process,
        conflict_process=conflict_process,
        conflict_pid=conflict_pid,
        killed_pid=killed_pid,
    )


def describe_degradation(result: CibEnsureResult) -> str:
    """Human-readable explanation for falling back to the local timetable."""
    if result.state == CibState.EXECUTABLE_MISSING:
        return "未找到 ClassIsland 桥接器程序，已降级为本地静态课表模式。"
    if result.state == CibState.PORT_CONFLICT_DECLINED:
        return "端口冲突未解决，已降级为本地静态课表模式。"
    return f"{result.message} 已降级为本地静态课表模式。"
