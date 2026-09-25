"""Qt integration layer for the CIB daemon.

:mod:`client.cib_daemon` is deliberately UI-free, so this small module bridges
it to the Qt world:

* it runs the (async) ``ensure_cib_running`` flow inside a :class:`QThread`,
  keeping the GUI responsive while a possibly slow launch is in progress;
* it forwards the "may I kill the process holding port 6614?" question to the
  main thread as a signal and waits for the answer, which lets the window own
  the dialog while the daemon stays decoupled.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal

from . import cib_daemon

logger = logging.getLogger("kg.client.cib_supervisor")

#: How long to wait for the user's answer before assuming "no".
CONFIRM_TIMEOUT_SECONDS = 120.0


class CibSupervisor(QThread):
    """Verify/launch CIB off the GUI thread.

    Signals
    -------
    confirm_kill_requested : str, int
        Emitted when port 6614 is held by another process.  The main thread
        must show a dialog and then call :meth:`provide_confirmation`.
    completed : object
        Emitted once with the resulting :class:`cib_daemon.CibEnsureResult`.
    """

    confirm_kill_requested = pyqtSignal(str, int)
    completed = pyqtSignal(object)

    def __init__(
        self,
        *,
        exe_path: Optional[str] = None,
        port: int = cib_daemon.CIB_PORT,
        url: str = cib_daemon.CIB_WS_URL,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._exe_path = exe_path
        self._port = port
        self._url = url
        self._confirm_event = threading.Event()
        self._confirm_result = False

    # ------------------------------------------------------------------
    # main-thread API
    # ------------------------------------------------------------------

    def provide_confirmation(self, approved: bool) -> None:
        """Answer a pending :attr:`confirm_kill_requested` (main thread)."""
        self._confirm_result = bool(approved)
        self._confirm_event.set()

    # ------------------------------------------------------------------
    # QThread
    # ------------------------------------------------------------------

    def run(self) -> None:
        try:
            result = asyncio.run(self._ensure())
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("CIB supervisor failed: %s", exc)
            result = cib_daemon.CibEnsureResult(
                False,
                cib_daemon.CibState.LAUNCH_FAILED,
                f"ClassIsland 桥接器守护线程异常：{exc}",
            )
        self.completed.emit(result)

    async def _ensure(self) -> cib_daemon.CibEnsureResult:
        return await cib_daemon.ensure_cib_running(
            confirm_kill_callback=self._request_confirmation,
            exe_path=self._exe_path,
            port=self._port,
            url=self._url,
        )

    async def _request_confirmation(self, process_name: str, pid: int) -> bool:
        """Ask the main thread whether the port holder may be killed."""
        self._confirm_event.clear()
        self._confirm_result = False
        self.confirm_kill_requested.emit(process_name, pid)

        loop = asyncio.get_event_loop()
        # Block in a worker thread so the event loop keeps running.
        answered = await loop.run_in_executor(
            None, self._confirm_event.wait, CONFIRM_TIMEOUT_SECONDS
        )
        if not answered:
            logger.warning(
                "Timed out after %.0fs waiting for the port-conflict confirmation; "
                "treating as refusal",
                CONFIRM_TIMEOUT_SECONDS,
            )
            return False
        return self._confirm_result
