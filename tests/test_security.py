"""
Guardrail tests: SSRF, rate limiting and the memo cache.

A link-resolving service is a server-side request forgery gadget by
construction, so these are not optional tests — they are the reason the endpoint
can be public at all.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket

import pytest

from app.security import RateLimiter, TTLCache, UnsafeURL, validate_public_url


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------- SSRF

@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/video.mp4",
        "http://127.0.0.1:8080/api/extract",
        "http://localhost:631/",
        "http://[::1]/x.mp4",
        "http://169.254.169.254/latest/meta-data/",          # AWS IMDS
        "http://169.254.170.2/x",                             # ECS task metadata
        "http://10.0.0.5/video",
        "http://192.168.1.1/admin",
        "http://172.16.0.9/internal",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://host.docker.internal/x",
        "http://0.0.0.0/",
        # Not covered by `is_private`: shared-address space clouds use for internal
        # load balancers, and the documentation/benchmarking ranges.
        "http://100.64.1.1/latest/meta-data/",
        "http://198.18.0.1/x",
        "http://192.0.2.1/x",
        "http://203.0.113.5/x",
        "http://[fd00::1]/x",
    ],
)
def test_internal_addresses_and_names_are_refused(url):
    with pytest.raises(UnsafeURL) as exc:
        run(validate_public_url(url))
    assert exc.value.reason in {"invalid_url", "unsupported", "notfound"}


def test_non_http_schemes_are_refused():
    for url in ["file:///etc/passwd", "gopher://x/y", "javascript:alert(1)", "data:text/html,x",
                "ftp://host/v.mp4", "//example.com/x", "https:/example.com/x", ""]:
        with pytest.raises(UnsafeURL):
            run(validate_public_url(url))


def test_credentials_are_stripped_from_an_accepted_url():
    result = run(validate_public_url("https://admin:p@ss@www.youtube.com/watch?v=abc"))
    assert "p@ss" not in result.href and "admin:" not in result.href
    assert result.host == "www.youtube.com"


def test_dangerous_ports_are_refused():
    for url in ["https://example.com:25/x", "http://example.com:6379/", "http://example.com:9200/_search"]:
        with pytest.raises(UnsafeURL):
            run(validate_public_url(url))


def test_response_splitting_and_control_bytes_are_refused():
    for url in ["https://a.com/x\r\nX-Evil: 1", "https://a.com/\x00.mp4", "https://a.com/has space"]:
        with pytest.raises(UnsafeURL):
            run(validate_public_url(url))


def test_length_cap_is_enforced():
    with pytest.raises(UnsafeURL):
        run(validate_public_url("https://a.com/" + "x" * 3000, max_chars=2000))


def test_developer_escape_hatch_allows_loopback_but_still_parses():
    result = run(validate_public_url("http://127.0.0.1:8099/clip.mp4", allow_private_hosts=True))
    assert result.host == "127.0.0.1" and result.port == 8099
    with pytest.raises(UnsafeURL):
        run(validate_public_url("file:///tmp/clip.mp4", allow_private_hosts=True))


def test_public_addresses_still_pass():
    """The guard must not become a blocklist of the whole internet."""
    for url in ["https://93.184.216.34/video.mp4", "https://157.240.7.35/o.mp4"]:
        result = run(validate_public_url(url))
        assert result.href.startswith("https://")


def test_dns_answer_is_what_gets_checked_not_the_string():
    """A hostname that *resolves* to loopback must be blocked just like the IP."""
    def fake_resolver(host, port):
        return [(2, 1, 6, "", ("127.0.0.1", port))]

    with pytest.raises(UnsafeURL):
        run(validate_public_url("https://evil.example/v.mp4", resolver=fake_resolver))

    def fake_public(host, port):
        return [(2, 1, 6, "", ("93.184.216.34", port))]

    ok = run(validate_public_url("https://good.example/v.mp4", resolver=fake_public))
    assert ok.host == "good.example"


def test_unresolvable_host_is_a_notfound_reason():
    def dead(host, port):
        raise socket.gaierror("no answer")

    with pytest.raises(UnsafeURL) as exc:
        run(validate_public_url("https://does-not-exist.invalid/v.mp4", resolver=dead))
    assert exc.value.reason == "notfound"


# -------------------------------------------------------------- rate limiter

def test_fixed_window_allows_n_then_blocks_until_rollover():
    limiter = RateLimiter(limit=2, window_seconds=60)

    async def scenario():
        return [await limiter.acquire("ip") for _ in range(4)]

    results = run(scenario())
    assert [ok for ok, _ in results] == [True, True, False, False]
    assert results[2][1] >= 1
    assert limiter.snapshot()["rejections"] == 2


def test_limiter_tracks_clients_independently():
    limiter = RateLimiter(limit=1, window_seconds=60)

    async def scenario():
        return await limiter.acquire("a"), await limiter.acquire("b"), await limiter.acquire("a")

    a, b, a_again = run(scenario())
    assert a[0] and b[0] and not a_again[0]


def test_limiter_bounds_its_own_memory():
    limiter = RateLimiter(limit=1, window_seconds=60, max_keys=10)

    async def scenario():
        for i in range(50):
            await limiter.acquire(f"ip-{i}")

    run(scenario())
    assert limiter.snapshot()["tracked_clients"] <= 10


# ------------------------------------------------------------------- cache

def test_ttl_cache_expires_and_evicts():
    cache = TTLCache(ttl_seconds=60, max_entries=2)
    cache.set("a", 1)
    cache.set("b", 2)
    assert cache.get("a") == 1
    cache.set("c", 3)  # evicts the least recently used ("b" was touched? no: a was)
    stats = cache.stats()
    assert stats["entries"] <= 2
    assert cache.get("b") is None


def test_ttl_cache_respects_expiry(monkeypatch):
    """An entry older than the TTL must never be handed back."""
    import app.security as sec

    cache = TTLCache(ttl_seconds=10)
    cache.set("k", "v")
    assert cache.get("k") == "v"
    monkeypatch.setattr(sec.time, "monotonic", lambda: 10**9)  # jump past the expiry
    assert cache.get("k") is None


def test_cache_disabled_by_zero_ttl():
    cache = TTLCache(ttl_seconds=0)
    cache.set("a", 1)
    assert cache.get("a") is None


def test_cache_hit_and_miss_counters():
    cache = TTLCache(ttl_seconds=30)
    cache.get("x")
    cache.set("x", 1)
    cache.get("x")
    stats = cache.stats()
    assert stats["misses"] == 1 and stats["hits"] == 1
