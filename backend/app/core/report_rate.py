"""In-memory per-address limit for public project reports."""
from __future__ import annotations

import threading
import time
from collections import deque

# A human report is rare. Ten per minute stops a burst without a shared store.
LIMIT = 10
WINDOW_S = 60.0

_hits: dict[str, deque[float]] = {}
_lock = threading.Lock()


def reset() -> None:
    with _lock:
        _hits.clear()


def allow(key: str, *, limit: int = LIMIT, window_s: float = WINDOW_S) -> bool:
    now = time.monotonic()
    with _lock:
        bucket = _hits.setdefault(key or "unknown", deque())
        cutoff = now - window_s
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True
