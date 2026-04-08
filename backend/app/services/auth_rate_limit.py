from __future__ import annotations

from collections import deque
from threading import Lock
import time


_FAILURES: dict[str, deque[float]] = {}
_LOCK = Lock()


def _prune(now: float, attempts: deque[float], window_seconds: int) -> None:
    cutoff = now - max(1, window_seconds)
    while attempts and attempts[0] <= cutoff:
        attempts.popleft()


def is_limited(key: str, *, limit: int, window_seconds: int) -> tuple[bool, int]:
    now = time.monotonic()
    with _LOCK:
        attempts = _FAILURES.get(key)
        if not attempts:
            return False, 0
        _prune(now, attempts, window_seconds)
        if len(attempts) < limit:
            if not attempts:
                _FAILURES.pop(key, None)
            return False, 0
        retry_after = max(1, int(window_seconds - (now - attempts[0])))
        return True, retry_after


def register_failure(key: str, *, window_seconds: int) -> None:
    now = time.monotonic()
    with _LOCK:
        attempts = _FAILURES.setdefault(key, deque())
        _prune(now, attempts, window_seconds)
        attempts.append(now)


def clear_failures(key: str) -> None:
    with _LOCK:
        _FAILURES.pop(key, None)


def clear_all_failures() -> None:
    with _LOCK:
        _FAILURES.clear()
