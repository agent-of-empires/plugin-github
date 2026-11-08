"""Bounded shared cache for optional GitHub CLI destination discovery."""

from __future__ import annotations

import os
import time
import threading

from aoe_github_plugin.utils.gitctx import GhDiscoveryError
from aoe_github_plugin.utils.gitctx import gh_pr_repository

TTL = 300.0
FAILURE_COOLDOWN = 60.0
MAX_ENTRIES = 512
_lock = threading.Lock()
_cache: dict[tuple[str, str], tuple[float, tuple[str, str] | None]] = {}
_failures: dict[tuple[str, str], float] = {}


def _bound(entries: dict) -> None:
    while len(entries) > MAX_ENTRIES:
        entries.pop(next(iter(entries)))


def destination(path: str, branch: str, *, force: bool = False, blocked: bool = False) -> tuple[str, str] | None:
    """Resolve a destination once per TTL, retaining last-good data on failure."""
    key = (os.path.realpath(path), branch)
    now = time.monotonic()
    with _lock:
        cached = _cache.get(key)
        cooling = now < _failures.get(key, 0.0)
    if blocked or (not force and (cooling or (cached is not None and now - cached[0] < TTL))):
        return cached[1] if cached is not None else None
    try:
        result = gh_pr_repository(path)
    except GhDiscoveryError:
        with _lock:
            _failures[key] = time.monotonic() + FAILURE_COOLDOWN
            _bound(_failures)
        return cached[1] if cached is not None else None
    with _lock:
        _cache[key] = (time.monotonic(), result)
        _bound(_cache)
        _failures.pop(key, None)
    return result
