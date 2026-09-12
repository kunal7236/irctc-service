"""
Tiny in-process TTL cache.

Schedule data (train details, routes, coach composition) changes rarely and is
cached for hours; availability, fares and PNR are time-sensitive and are never
cached. This is what keeps a chatty agent from hammering erail and getting the
Pi's IP rate-limited.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

# Seconds. Anything not listed is not cached.
TTL = {
    "train_details": 6 * 3600,
    "train_route": 24 * 3600,
    "trains_between": 3600,
    # Deliberately absent: availability, pnr, refund.
}


class TTLCache:
    def __init__(self, max_entries: int = 512):
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._max = max_entries

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._data.get(key)
            if not entry:
                return None
            expires_at, value = entry
            if time.time() >= expires_at:
                self._data.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        if ttl <= 0:
            return
        with self._lock:
            if len(self._data) >= self._max:
                # Evict whatever expires soonest.
                oldest = min(self._data, key=lambda k: self._data[k][0])
                self._data.pop(oldest, None)
            self._data[key] = (time.time() + ttl, value)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def stats(self) -> dict[str, int]:
        with self._lock:
            now = time.time()
            return {
                "entries": len(self._data),
                "live": sum(1 for exp, _ in self._data.values() if exp > now),
            }


_cache = TTLCache()


def cached(kind: str, key: str, produce: Callable[[], Any]) -> Any:
    """Return a cached value for `kind`, or produce and store it.

    A `kind` with no TTL entry bypasses the cache entirely.
    """
    ttl = TTL.get(kind, 0)
    if ttl <= 0:
        return produce()
    full_key = f"{kind}:{key}"
    hit = _cache.get(full_key)
    if hit is not None:
        return hit
    value = produce()
    _cache.set(full_key, value, ttl)
    return value


def clear() -> None:
    _cache.clear()


def stats() -> dict[str, int]:
    return _cache.stats()
