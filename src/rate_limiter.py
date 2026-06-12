# src/rate_limiter.py
"""Generic in-memory rate limiter — sliding window, keyed by IP."""

import ipaddress
import os
import threading
import time
from typing import Dict, List

# Direct peers inside these networks are trusted to report the real client IP
# via forwarded headers. Defaults cover loopback and the docker bridge range —
# the deployment this repo targets fronts the app with a reverse proxy /
# Cloudflare tunnel whose container connects from the compose network.
_DEFAULT_TRUSTED_CIDRS = "127.0.0.0/8,::1/128,172.16.0.0/12"
_trusted_cache: tuple = ("", [])


def _trusted_networks():
    global _trusted_cache
    raw = os.getenv("TRUSTED_PROXY_CIDRS", _DEFAULT_TRUSTED_CIDRS)
    if raw != _trusted_cache[0]:
        nets = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                nets.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                pass
        _trusted_cache = (raw, nets)
    return _trusted_cache[1]


def client_key(request) -> str:
    """Best client identity for rate-limit bucketing.

    Behind the SECURITY.md-recommended reverse proxy every connection arrives
    from the proxy's IP, so keying on request.client.host collapses all users
    into ONE bucket — a single abuser locks every legitimate login out. When
    the direct peer is a trusted proxy (TRUSTED_PROXY_CIDRS, defaults to
    loopback + the docker bridge range), use the client IP it reports:
    CF-Connecting-IP, else the LAST entry of X-Forwarded-For — the hop the
    trusted proxy itself appended (earlier entries are caller-supplied).
    Forwarded headers from untrusted peers are ignored entirely: honoring
    them would hand an attacker unlimited fresh buckets.
    """
    peer = getattr(getattr(request, "client", None), "host", None) or "unknown"
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return peer  # e.g. "testclient" in tests — no proxy semantics
    if not any(addr in net for net in _trusted_networks()):
        return peer
    headers = getattr(request, "headers", None) or {}
    cf = (headers.get("cf-connecting-ip") or "").strip()
    if cf:
        return cf
    xff = (headers.get("x-forwarded-for") or "").strip()
    if xff:
        last_hop = xff.split(",")[-1].strip()
        if last_hop:
            return last_hop
    return peer


class RateLimiter:
    """Sliding-window rate limiter.

    Usage:
        limiter = RateLimiter(max_requests=5, window_seconds=60)
        if not limiter.check(ip):
            raise HTTPException(429, "Too many requests")
    """

    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window = window_seconds
        self._log: Dict[str, List[float]] = {}
        self._lock = threading.Lock()
        self._last_cleanup = time.monotonic()
        self._cleanup_interval = max(window_seconds * 2, 120)

    def check(self, key: str) -> bool:
        """Return True if the request is allowed, False if rate-limited."""
        now = time.monotonic()
        with self._lock:
            self._maybe_cleanup(now)
            timestamps = self._log.get(key, [])
            cutoff = now - self.window
            timestamps = [t for t in timestamps if t > cutoff]
            if len(timestamps) >= self.max_requests:
                self._log[key] = timestamps
                return False
            timestamps.append(now)
            self._log[key] = timestamps
            return True

    def _maybe_cleanup(self, now: float) -> None:
        """Periodically purge stale entries."""
        if now - self._last_cleanup < self._cleanup_interval:
            return
        self._last_cleanup = now
        cutoff = now - self.window
        stale = [k for k, v in self._log.items() if not v or v[-1] <= cutoff]
        for k in stale:
            del self._log[k]
