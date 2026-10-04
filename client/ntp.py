from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
import time
from typing import Optional

import ntplib

from PyQt6.QtCore import QThread, pyqtSignal

logger = logging.getLogger("kg.client.ntp")


@dataclass
class TimeSyncResult:
    source: str
    current_time: datetime


def get_network_time(server: str) -> TimeSyncResult:
    try:
        client = ntplib.NTPClient()
        response = client.request(server, version=3, timeout=2)
        return TimeSyncResult(source="ntp", current_time=datetime.fromtimestamp(response.tx_time))
    except Exception:
        return TimeSyncResult(source="system", current_time=datetime.fromtimestamp(time.time()))


class NtpSyncThread(QThread):
    """Run the NTP request off the GUI thread.

    ``get_network_time`` blocks for ~100ms (up to the 2s timeout on a bad
    network); doing that inline froze the start-up splash mid-animation.

    Signals
    -------
    synced : object
        Emitted once with the resulting :class:`TimeSyncResult`.
    """

    synced = pyqtSignal(object)

    def __init__(self, server: str, parent=None) -> None:
        super().__init__(parent)
        self._server = server
        self._cancelled = False

    @property
    def server(self) -> str:
        return self._server

    def set_server(self, server: str) -> None:
        self._server = server

    def cancel(self) -> None:
        """Ignore the result (the window is shutting down)."""
        self._cancelled = True

    def run(self) -> None:
        try:
            result: Optional[TimeSyncResult] = get_network_time(self._server)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("NTP sync failed: %s", exc)
            result = TimeSyncResult(source="system", current_time=datetime.fromtimestamp(time.time()))
        if not self._cancelled:
            self.synced.emit(result)


