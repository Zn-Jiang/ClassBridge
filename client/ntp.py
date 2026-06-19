from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import time

import ntplib


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

