"""Background watchdog for the ClassIsland desktop process.

The client prefers live ClassIsland events for break-time popups.  When
ClassIsland is not running (crashed, not started, being updated, …) those
events never arrive, so this thread polls for ``ClassIsland.Desktop.exe``
and tells the UI when the situation changes.  The UI then falls back to the
locally stored timetable.

Detection is deliberately tri-state: :meth:`check_alive` returns ``None`` when
the process table cannot be read (e.g. psutil is missing) so that an
inconclusive check never triggers a spurious fallback.
"""

from __future__ import annotations

import logging
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal

from .classisland_import import ClassIslandConfigParser

logger = logging.getLogger("kg.client.ci_watchdog")

#: How often to look for the ClassIsland process.
DEFAULT_CHECK_INTERVAL_SECONDS = 5.0
#: Sleep granularity, so stop() is honoured quickly.
_SLEEP_STEP_MS = 200


class CiProcessWatcher(QThread):
    """Poll for ``ClassIsland.Desktop.exe`` and emit on state changes.

    Signals
    -------
    alive_changed : bool
        Emitted only when the detected state *changes* (True = running).
        Never emitted for inconclusive checks.
    """

    alive_changed = pyqtSignal(bool)

    def __init__(
        self,
        interval_seconds: float = DEFAULT_CHECK_INTERVAL_SECONDS,
        parent=None,
    ) -> None:
        super().__init__(parent)
        try:
            interval = float(interval_seconds)
        except (TypeError, ValueError):
            interval = DEFAULT_CHECK_INTERVAL_SECONDS
        self._interval = max(1.0, interval)
        self._running = True
        self._last_state: Optional[bool] = None

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Ask the thread to finish (returns promptly from ``run``)."""
        self._running = False

    @property
    def last_state(self) -> Optional[bool]:
        """Last conclusive result, or ``None`` if never determined."""
        return self._last_state

    def check_alive(self) -> Optional[bool]:
        """One-shot detection (also usable without starting the thread)."""
        return ClassIslandConfigParser.is_process_running()

    # ------------------------------------------------------------------
    # QThread
    # ------------------------------------------------------------------

    def run(self) -> None:
        while self._running:
            try:
                state = self.check_alive()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("ClassIsland watchdog check failed: %s", exc)
                state = None

            if state is not None and state != self._last_state:
                self._last_state = state
                logger.info(
                    "ClassIsland process state changed: %s",
                    "running" if state else "not running",
                )
                self.alive_changed.emit(state)

            self._sleep_stepwise()

    def _sleep_stepwise(self) -> None:
        """Sleep in small steps so ``stop()`` takes effect quickly."""
        elapsed = 0.0
        while self._running and elapsed < self._interval:
            self.msleep(_SLEEP_STEP_MS)
            elapsed += _SLEEP_STEP_MS / 1000.0
