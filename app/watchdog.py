"""Track event loop stalls for logging and health snapshots."""
from __future__ import annotations

import asyncio
import logging
import time

from . import config


log = logging.getLogger("aml.watchdog")
_max_lag_s = 0.0
_lag_events = 0


async def run() -> None:
    """Measure one-second sleeps until the owning task is cancelled."""
    global _max_lag_s, _lag_events

    while True:
        started = time.monotonic()
        await asyncio.sleep(1.0)
        lag = max(0.0, time.monotonic() - started - 1.0)
        _max_lag_s = max(_max_lag_s, lag)
        if lag > config.LOOP_LAG_WARN_S:
            _lag_events += 1
            log.warning("event loop lagged %.2fs", lag)


def snapshot() -> dict:
    """Return a fresh snapshot without exposing mutable module state."""
    return {"max_lag_s": _max_lag_s, "lag_events": _lag_events}
