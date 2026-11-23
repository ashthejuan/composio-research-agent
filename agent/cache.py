"""Disk cache for tool calls (FR-02 cost control). Key = sha256(json({tool, args}))."""
from __future__ import annotations

import functools
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from agent.config import get_settings

_MISS = object()


def make_key(tool: str, args: Any) -> str:
    blob = json.dumps({"tool": tool, "args": args}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


class DiskCache:
    def __init__(self, directory: Optional[Path] = None, refresh: Optional[bool] = None) -> None:
        self.directory = Path(directory) if directory else get_settings().CACHE_DIR
        self.refresh = refresh if refresh is not None else os.environ.get("REFRESH", "") in ("1", "true", "True")
        self.hits = 0
        self.misses = 0

    def _path(self, tool: str, args: Any) -> Path:
        return self.directory / f"{make_key(tool, args)}.json"

    def get(self, tool: str, args: Any, default: Any = None) -> Any:
        """Return cached value, or `default` on miss / refresh / unreadable file. Counts hits/misses."""
        p = self._path(tool, args)
        if not self.refresh and p.exists():
            try:
                value = json.loads(p.read_text())["value"]
                self.hits += 1
                return value
            except (OSError, ValueError, KeyError):
                pass
        self.misses += 1
        return default

    def set(self, tool: str, args: Any, value: Any) -> None:
        """Store `value`. It must be strict JSON: a value that only survives via str() would come back
        as a different type on the next hit, so raise TypeError/ValueError instead of coercing it."""
        json.dumps(value, allow_nan=False)
        self.directory.mkdir(parents=True, exist_ok=True)
        p = self._path(tool, args)
        fd, tmp = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"tool": tool, "args": args, "value": value}, f, default=str)
            os.replace(tmp, p)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def stats(self) -> dict[str, int]:
        return {"hits": self.hits, "misses": self.misses}

    def cached(self, tool: str) -> Callable:
        """Decorator for async *functions* returning JSON-serialisable values; key = (args, kwargs).
        Not for methods: `self` would be str()-ed into the key (memory address -> unstable)."""

        def deco(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
            @functools.wraps(fn)
            async def wrapper(*a: Any, **kw: Any) -> Any:
                key_args = {"a": list(a), "kw": kw}
                hit = self.get(tool, key_args, _MISS)
                if hit is not _MISS:
                    return hit
                value = await fn(*a, **kw)
                self.set(tool, key_args, value)
                return value

            return wrapper

        return deco


_default: Optional[DiskCache] = None


def get_cache() -> DiskCache:
    global _default
    if _default is None:
        _default = DiskCache()
    return _default


def reset_cache() -> None:
    global _default
    _default = None
