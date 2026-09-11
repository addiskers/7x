"""
In-process gauge of currently-connected live calls.

Shared by both dial loops (campaign_runner + scheduler) so campaign dials and
callbacks TOGETHER never exceed MAX_LIVE_CALLS simultaneous calls. The Plivo /
browser media-stream handlers inc() on connect and dec() on disconnect.

Counts *connected* calls (the WS is up). A ringing-but-unanswered dial isn't
counted yet, but the per-tick pacing in each loop bounds the burst, so this is an
effective safety ceiling, not a hard real-time semaphore.
"""

import logging
import os
import threading

logger = logging.getLogger(__name__)

_count = 0
_lock = threading.Lock()


def inc() -> int:
    global _count
    with _lock:
        _count += 1
        n = _count
    logger.debug("live calls +1 -> %d", n)
    return n


def dec() -> int:
    global _count
    with _lock:
        _count = max(0, _count - 1)
        n = _count
    logger.debug("live calls -1 -> %d", n)
    return n


def count() -> int:
    with _lock:
        return _count


def max_live() -> int:
    """Global cap on simultaneous live calls (campaign + callbacks). Read at dial
    time, so setting MAX_LIVE_CALLS in .env works (unlike DATA_DIR). Default 10 —
    a wedding-day reminder burst has to clear hundreds of guests inside an hour.
    Turn it up on the day once a few live calls look right."""
    try:
        return max(1, int(os.getenv("MAX_LIVE_CALLS", "10")))
    except (TypeError, ValueError):
        return 10


def room() -> int:
    """How many more calls can start right now (>= 0)."""
    return max(0, max_live() - count())
