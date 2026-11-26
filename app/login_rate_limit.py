"""In-process login limits shared by the browser and mobile endpoints."""

from collections import deque
from hashlib import sha256
from math import ceil
from time import monotonic


class LoginRateLimiter:
    WINDOW_SECONDS = 300
    IP_LIMIT = 30
    ACCOUNT_LIMIT = 8
    MAX_KEYS = 4096

    def __init__(self):
        self._attempts: dict[tuple[str, str], deque[float]] = {}
        self._next_cleanup = 0.0

    def retry_after(self, ip: str, email: str) -> int:
        now = monotonic()
        cutoff = now - self.WINDOW_SECONDS
        if now >= self._next_cleanup:
            self._attempts = {
                key: times for key, times in self._attempts.items() if times[-1] > cutoff
            }
            self._next_cleanup = now + 30

        account = sha256(email.strip().lower().encode()).hexdigest()
        rules = ((("ip", ip), self.IP_LIMIT), (("account", account), self.ACCOUNT_LIMIT))
        wait = 0
        for key, limit in rules:
            times = self._attempts.get(key)
            if not times:
                continue
            while times and times[0] <= cutoff:
                times.popleft()
            if not times:
                del self._attempts[key]
            elif len(times) >= limit:
                wait = max(wait, ceil(times[0] + self.WINDOW_SECONDS - now))
        if wait:
            return wait
        missing = sum(key not in self._attempts for key, _ in rules)
        if len(self._attempts) + missing > self.MAX_KEYS:
            return max(1, ceil(self._next_cleanup - now))
        for key, _ in rules:
            self._attempts.setdefault(key, deque()).append(now)
        return 0
