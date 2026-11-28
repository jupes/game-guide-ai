"""Remember which budget pays for a screen lookup, without caching its authorization."""

import threading
import time
from collections import OrderedDict


class ScreenLookupBudget:
    def __init__(self, *, limit: int = 256, lifetime_s: float = 300) -> None:
        self._limit = limit
        self._lifetime_s = lifetime_s
        self._entries: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._lock = threading.Lock()

    def grant_id(self, digest: str) -> str | None:
        with self._lock:
            entry = self._entries.get(digest)
            if entry is None:
                return None
            grant, expires = entry
            if expires <= time.monotonic():
                del self._entries[digest]
                return None
            return grant

    def remember(self, digest: str, grant: str) -> None:
        with self._lock:
            self._entries[digest] = (grant, time.monotonic() + self._lifetime_s)
            self._entries.move_to_end(digest)
            if len(self._entries) > self._limit:
                self._entries.popitem(last=False)

    def forget(self, digest: str) -> None:
        with self._lock:
            self._entries.pop(digest, None)
