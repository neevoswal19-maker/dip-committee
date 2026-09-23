"""Disk cache and rate limiter - the single chokepoint for every fetch.

Two jobs, deliberately kept together:

1. Don't ask NSE for something we already have. The bhavcopy for 3 March does
   not change, so fetching it twice is pure waste and pure risk.
2. Don't ask NSE too fast. These are unofficial endpoints against a site that
   blocks aggressive clients, and getting blocked mid-scan is the single most
   likely way this system breaks.

Everything that touches the network goes through `cached_fetch`. That means
there is exactly one place to add a retry, tune a delay, or debug a 403.
"""

from __future__ import annotations

import hashlib
import json
import logging
import pickle
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

from src.config import cache_dir, load_config

log = logging.getLogger(__name__)

T = TypeVar("T")


class NotFound(Exception):
    """The source answered, and the answer was that this does not exist.

    Distinct from a failure. A bhavcopy 404 means the session was a holiday or
    the file is not published yet - retrying three times with exponential
    backoff wastes twelve seconds and writes ERROR lines for something that is
    working correctly.
    """


class RateLimiter:
    """Thread-safe minimum-interval limiter with a rolling per-minute cap.

    Two constraints rather than one: a floor on the gap between consecutive
    requests, and a ceiling on requests per rolling minute. The first stops
    bursts, the second stops a sustained grind that would look like scraping.
    """

    def __init__(self, min_interval: float = 0.6, max_per_minute: int = 20):
        self.min_interval = min_interval
        self.max_per_minute = max_per_minute
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._recent: list[float] = []

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()

            gap = now - self._last_call
            if gap < self.min_interval:
                time.sleep(self.min_interval - gap)
                now = time.monotonic()

            self._recent = [t for t in self._recent if now - t < 60.0]
            if len(self._recent) >= self.max_per_minute:
                wait = 60.0 - (now - self._recent[0]) + 0.1
                if wait > 0:
                    log.info("Rate limit reached, pausing %.1fs", wait)
                    time.sleep(wait)
                    now = time.monotonic()
                    self._recent = [t for t in self._recent if now - t < 60.0]

            self._last_call = now
            self._recent.append(now)


class DiskCache:
    """Pickle-backed cache keyed by namespace plus a hash of the parameters.

    Pickle rather than JSON because most of what we cache is a pandas
    DataFrame. The cache is a convenience, never a source of truth: a
    corrupt or unreadable entry is discarded and refetched rather than
    raising, because a stale cache should never be able to stop a scan.
    """

    def __init__(self, root: Path | None = None):
        self.root = root or cache_dir()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, namespace: str, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
        folder = self.root / namespace
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{digest}.pkl"

    def get(self, namespace: str, key: str, ttl_hours: float) -> Any | None:
        path = self._path(namespace, key)
        if not path.exists():
            return None

        try:
            age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
            if age > timedelta(hours=ttl_hours):
                return None
            with open(path, "rb") as fh:
                payload = pickle.load(fh)
            return payload.get("value")
        except (OSError, pickle.PickleError, EOFError, AttributeError) as exc:
            log.debug("Discarding unreadable cache entry %s/%s: %s", namespace, key, exc)
            path.unlink(missing_ok=True)
            return None

    def set(self, namespace: str, key: str, value: Any) -> None:
        path = self._path(namespace, key)
        try:
            # Write to a temp file and replace, so an interrupted write can
            # never leave a half-written entry that looks valid.
            tmp = path.with_suffix(".tmp")
            with open(tmp, "wb") as fh:
                pickle.dump({"key": key, "cached_at": datetime.now(), "value": value}, fh)
            tmp.replace(path)
        except (OSError, pickle.PickleError) as exc:
            log.debug("Could not cache %s/%s: %s", namespace, key, exc)

    def invalidate(self, namespace: str, key: str | None = None) -> int:
        """Drop one entry, or a whole namespace. Returns the count removed."""
        if key is not None:
            path = self._path(namespace, key)
            if path.exists():
                path.unlink()
                return 1
            return 0

        folder = self.root / namespace
        if not folder.exists():
            return 0
        removed = 0
        for path in folder.glob("*.pkl"):
            path.unlink(missing_ok=True)
            removed += 1
        return removed

    def stats(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for folder in sorted(p for p in self.root.iterdir() if p.is_dir()):
            files = list(folder.glob("*.pkl"))
            if not files:
                continue
            out[folder.name] = {
                "entries": len(files),
                "size_mb": round(sum(f.stat().st_size for f in files) / 1e6, 2),
                "newest": datetime.fromtimestamp(max(f.stat().st_mtime for f in files)).isoformat(
                    timespec="seconds"
                ),
            }
        return out


# --- Shared singletons ------------------------------------------------------

_cache: DiskCache | None = None
_limiters: dict[str, RateLimiter] = {}
_limiter_lock = threading.Lock()


def get_cache() -> DiskCache:
    global _cache
    if _cache is None:
        _cache = DiskCache()
    return _cache


def get_limiter(source: str = "nse") -> RateLimiter:
    """One limiter per upstream source, so a slow NSE doesn't throttle Yahoo."""
    with _limiter_lock:
        if source not in _limiters:
            cfg = load_config()
            _limiters[source] = RateLimiter(
                min_interval=float(cfg.get("data.rate_limit.min_interval_seconds", 0.6)),
                max_per_minute=int(cfg.get("data.rate_limit.nse_requests_per_minute", 20)),
            )
        return _limiters[source]


def ttl_for(namespace: str) -> float:
    """TTL in hours from config, defaulting to 12 for unknown namespaces."""
    return float(load_config().get(f"data.cache_ttl_hours.{namespace}", 12))


def make_key(*parts: Any, **kwargs: Any) -> str:
    """Stable cache key. Sorted kwargs so argument order can never split it."""
    pieces = [str(p) for p in parts]
    if kwargs:
        pieces.append(json.dumps(kwargs, sort_keys=True, default=str))
    return "|".join(pieces)


def cached_fetch(
    namespace: str,
    key: str,
    fetcher: Callable[[], T],
    *,
    ttl_hours: float | None = None,
    source: str = "nse",
    retries: int | None = None,
    force_refresh: bool = False,
) -> T | None:
    """Fetch through the cache, the rate limiter and a retry loop.

    Returns None on total failure rather than raising. That is deliberate:
    a scan across 500 stocks must not die because one endpoint is down. The
    caller reports `data_available: false` and the committee records which
    desk was flying blind, which is far more useful than a stack trace.
    """
    cache = get_cache()
    ttl = ttl_hours if ttl_hours is not None else ttl_for(namespace)

    if not force_refresh:
        hit = cache.get(namespace, key, ttl)
        if hit is not None:
            log.debug("cache hit %s/%s", namespace, key)
            return hit

    cfg = load_config()
    attempts = retries if retries is not None else int(cfg.get("data.rate_limit.max_retries", 3))
    backoff = float(cfg.get("data.rate_limit.backoff_base_seconds", 2.0))
    limiter = get_limiter(source)

    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            limiter.acquire()
            value = fetcher()
            if value is not None:
                cache.set(namespace, key, value)
            return value
        except NotFound as exc:
            # A definite absence. Retrying cannot change the answer.
            log.debug("%s/%s: %s", namespace, key, exc)
            return None
        except Exception as exc:  # upstream libraries raise all sorts
            last_error = exc
            if attempt < attempts:
                delay = backoff ** attempt
                log.warning(
                    "%s/%s failed (attempt %d/%d): %s - retrying in %.0fs",
                    namespace, key, attempt, attempts, exc, delay,
                )
                time.sleep(delay)

    log.error("%s/%s failed after %d attempts: %s", namespace, key, attempts, last_error)

    # Last resort: serve a stale entry rather than nothing. A three-day-old
    # shareholding pattern still beats an empty one, and the caller is told
    # how old it is through the DataResult wrapper.
    stale = cache.get(namespace, key, ttl_hours=24 * 365)
    if stale is not None:
        log.warning("Serving stale cache for %s/%s", namespace, key)
        return stale

    return None
