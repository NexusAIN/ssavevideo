"""
Input hardening and abuse control for the extraction endpoint.

Three independent guards:

1. `validate_public_url` — a real SSRF gate. A video downloader is a textbook
   SSRF gadget: give a user a "fetch this URL" button and someone will point it
   at 169.254.169.254 to steal cloud credentials. We therefore refuse any URL
   whose host resolves to a loopback, private, link-local, reserved or
   multicast address, and we only ever allow http/https.

2. `RateLimiter` — fixed-window per-IP budget on the *lookup* endpoint, which
   is the only expensive thing the service does. It protects the shared IP
   reputation of the box (platforms rate-limit by source address, so hammering
   them gets the whole server blocked).

3. `TTLCache` — successful extractions are memoised briefly. Video links
   explode in volume around the same handful of URLs during traffic spikes;
   absorbing those keeps both our CPU and the upstream platform happy.

All three are dependency-free and single-process safe; in front of N replicas
you would swap RateLimiter for Redis, and the interface is deliberately small
enough for that to be a 20-line change.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional
from urllib.parse import unquote, urlparse


class UnsafeURL(ValueError):
    """Raised for a URL that must not be fetched. `.reason` maps to a UI message."""

    def __init__(self, message: str, reason: str = "invalid_url") -> None:
        super().__init__(message)
        self.reason = reason


SCHEME_ALLOWLIST = frozenset({"http", "https"})

# Hostnames that are never legitimate media sources but show up in SSRF payloads.
BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata",
        "metadata.google.internal",
        "instance-data",
        "wpad",
        "docker.internal",
        "host.docker.internal",
    }
)

@dataclass(frozen=True)
class SafeURL:
    """A validated URL plus the normalised string we hand to the extractor."""

    href: str
    scheme: str
    host: str
    port: Optional[int]


def _strip_credentials(href: str) -> str:
    """Remove any user:pass@ the visitor embedded — a downloader needs none."""
    parsed = urlparse(href)
    if parsed.username is not None or parsed.password is not None:
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        href = parsed._replace(netloc=netloc).geturl()
    return href


def _is_public_ip(candidate: str) -> bool:
    """
    Decide whether an address may be connected to *at all*.

    Two conditions, not one. `is_private` alone is not enough: the carrier-grade
    NAT range 100.64.0.0/10 (used by several clouds for internal load balancers
    and metadata proxies) is neither private nor reserved in `ipaddress`, so it
    slips through — and requiring `is_global` catches it plus the documentation
    and benchmarking ranges in one stroke.
    """
    try:
        ip = ipaddress.ip_address(candidate.strip("[]"))
    except ValueError:
        return False  # not an IP literal at all -> decided by DNS below
    if not ip.is_global:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


async def validate_public_url(
    raw_url: str,
    *,
    max_chars: int = 2000,
    allow_private_hosts: bool = False,
    resolver: Optional[object] = None,
) -> SafeURL:
    """
    Validate a user-supplied media URL.

    Runs the CPU/DNS part in a worker thread so the event loop is never blocked
    by a slow (or deliberately hanging) DNS answer.
    """
    return await asyncio.get_running_loop().run_in_executor(
        None,
        _validate_public_url_sync,
        raw_url,
        max_chars,
        allow_private_hosts,
        resolver or socket.getaddrinfo,
    )


def _validate_public_url_sync(
    raw_url: str,
    max_chars: int,
    allow_private_hosts: bool,
    getaddrinfo,
) -> SafeURL:
    if not isinstance(raw_url, str):
        raise UnsafeURL("The link must be text.")

    href = raw_url.strip().strip("<>").replace("\u200b", "")
    href = unquote(href) if href.lower().startswith(("http://", "https://")) else href

    if not href:
        raise UnsafeURL("No link supplied.", reason="empty")
    if len(href) > max_chars:
        raise UnsafeURL(f"Link is longer than {max_chars} characters.")
    if any(ch.isspace() for ch in href):
        raise UnsafeURL("The link contains whitespace; copy the full URL.")
    if "\r" in href or "\n" in href:
        raise UnsafeURL("Header injection attempt blocked.", reason="invalid_url")

    parsed = urlparse(href)
    if parsed.scheme.lower() not in SCHEME_ALLOWLIST:
        raise UnsafeURL(f"Only http(s) links are accepted, got '{parsed.scheme or 'none'}'.")
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host:
        raise UnsafeURL("The link has no readable hostname.")
    if host in BLOCKED_HOSTNAMES or host.endswith((".localhost", ".internal", ".local")):
        raise UnsafeURL("That hostname is not allowed.", reason="unsupported")

    try:
        port = parsed.port
    except ValueError as exc:  # e.g. "https://host:99999/"
        raise UnsafeURL("The link contains an invalid port number.") from exc
    if port in {25, 465, 587, 135, 139, 445, 3389, 3306, 5432, 6379, 9200, 11211, 27017}:
        raise UnsafeURL("That port is not allowed.", reason="invalid_url")

    # --- SSRF: resolve and reject non-routable answers -----------------------
    try:
        infos = getaddrinfo(host, port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise UnsafeURL(f"Hostname does not resolve: {exc}", reason="notfound") from exc

    addresses = {info[4][0] for info in infos}
    if not addresses:
        raise UnsafeURL("Hostname returned no address.", reason="notfound")
    if not allow_private_hosts and not all(_is_public_ip(a) for a in addresses):
        raise UnsafeURL(
            "The link points at an internal or reserved network address.",
            reason="invalid_url",
        )

    netloc = host if not port else f"{host}:{port}"
    normalised = parsed._replace(netloc=netloc).geturl()
    return SafeURL(
        href=_strip_credentials(normalised),
        scheme=parsed.scheme.lower(),
        host=host,
        port=port,
    )


class RateLimiter:
    """
    Fixed-window counter per client key.

    Fixed window (not token bucket) because the failure mode we care about is a
    script firing hundreds of lookups per minute; a window is one dict lookup
    and self-heals at the boundary. Returns `(allowed, retry_after_seconds)`.
    """

    def __init__(self, limit: int, window_seconds: int, max_keys: int = 20000) -> None:
        self.limit = max(1, limit)
        self.window = max(1, window_seconds)
        self.max_keys = max_keys
        self._lock = asyncio.Lock()
        self._buckets: "OrderedDict[str, tuple[int, int]]" = OrderedDict()
        self.rejections = 0  # exposed on /metrics for alerting

    async def acquire(self, key: str) -> tuple[bool, int]:
        now = time.time()
        slot = int(now // self.window)
        async with self._lock:
            count_slot, count = self._buckets.get(key, (slot, 0))
            if count_slot != slot:  # window rolled over
                count_slot, count = slot, 0
            if count >= self.limit:
                self.rejections += 1
                self._buckets[key] = (count_slot, count)
                retry_after = max(1, int((count_slot + 1) * self.window - now) + 1)
                return False, retry_after
            self._buckets[key] = (count_slot, count + 1)
            self._buckets.move_to_end(key)
            while len(self._buckets) > self.max_keys:  # bound memory usage
                self._buckets.popitem(last=False)
            return True, 0

    def snapshot(self) -> dict:
        return {
            "limit": self.limit,
            "window_seconds": self.window,
            "tracked_clients": len(self._buckets),
            "rejections": self.rejections,
        }


class TTLCache:
    """Small thread-safe TTL LRU. Memoises successful extractions only."""

    def __init__(self, ttl_seconds: int, max_entries: int = 512) -> None:
        self.ttl = max(0, ttl_seconds)
        self.max_entries = max(1, max_entries)
        self._data: "OrderedDict[str, tuple[float, object]]" = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str):
        if self.ttl <= 0:
            return None
        with self._lock:
            item = self._data.get(key)
            if not item:
                self.misses += 1
                return None
            expires_at, value = item
            if expires_at < time.monotonic():
                self._data.pop(key, None)
                self.misses += 1
                return None
            self._data.move_to_end(key)
            self.hits += 1
            return value

    def set(self, key: str, value) -> None:
        if self.ttl <= 0:
            return
        with self._lock:
            self._data[key] = (time.monotonic() + self.ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)

    def stats(self) -> dict:
        with self._lock:
            return {
                "entries": len(self._data),
                "hits": self.hits,
                "misses": self.misses,
                "ttl_seconds": self.ttl,
            }
