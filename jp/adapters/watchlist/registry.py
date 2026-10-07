from __future__ import annotations
from typing import Any, Callable, Dict, List

from jp.models import RawJob

# Fetcher(entry, http, limits, limiter, log) -> List[RawJob]
Fetcher = Callable[..., List[RawJob]]
FETCHERS: Dict[str, Fetcher] = {}


def register(ats: str) -> Callable[[Fetcher], Fetcher]:
    def deco(fn: Fetcher) -> Fetcher:
        FETCHERS[ats] = fn
        return fn
    return deco


def limit(entry: Any, limits: Dict[str, Any], key: str, default: int) -> int:
    """每家的 params 可覆盖全局 limits。"""
    return int((entry.params or {}).get(key, limits.get(key, default)))
