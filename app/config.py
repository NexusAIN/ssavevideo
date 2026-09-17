"""
Runtime configuration for ssavevideo.com.

Everything is environment-driven so the same image can run in dev, staging and
production without code changes. Defaults are chosen for a single small VPS
behind a reverse proxy, which is the deployment this app is designed for:
1 vCPU, no disk writes of media, no outbound bandwidth beyond metadata.

Env vars (all optional):
    SSAVE_SITE_URL            canonical origin, e.g. https://ssavevideo.com
    SSAVE_BRAND               brand name used in copy/schema
    SSAVE_ENV                 development | production (affects cache headers, errors)
    SSAVE_MAX_CONCURRENCY     simultaneous upstream extractions allowed
    SSAVE_RATE_LIMIT          lookups per SSAVE_RATE_WINDOW per client IP
    SSAVE_RATE_WINDOW         seconds
    SSAVE_CACHE_TTL           seconds a successful extraction is memoised
    SSAVE_EXTRACT_TIMEOUT     wall-clock budget for one upstream extraction
    SSAVE_TRUST_PROXY_XFF     trust X-Forwarded-For / X-Forwarded-Proto (default true)
    SSAVE_ALLOW_PRIVATE_HOSTS allow RFC1918/loopback media hosts (DEV/TESTS ONLY)
    SSAVE_YTDLP_OPTIONS       extra JSON merged into the yt-dlp option dict (ops escape hatch)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

# --- filesystem layout -------------------------------------------------------
ROOT_DIR: Path = Path(__file__).resolve().parent.parent
APP_DIR: Path = ROOT_DIR / "app"
TEMPLATE_DIR: Path = APP_DIR / "templates"
STATIC_DIR: Path = APP_DIR / "static"
DATA_DIR: Path = ROOT_DIR / "data"

SEO_SCHEMA_FILE: Path = DATA_DIR / "seo_platforms.json"
LOCALES_FILE: Path = DATA_DIR / "locales.json"
LEGAL_FILE: Path = DATA_DIR / "legal.json"


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value.strip() == "" else value.strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:  # a human typed something odd in the .env file
        return default


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Immutable, validated view of the process environment."""

    site_url: str
    brand: str
    env: str
    max_concurrency: int
    rate_limit: int
    rate_window: int
    cache_ttl: int
    cache_max_entries: int
    extract_timeout: int
    socket_timeout: int
    retries: int
    trust_proxy_xff: bool
    allow_private_hosts: bool
    max_formats_returned: int
    max_url_chars: int
    extra_ytdlp_options: dict = field(default_factory=dict)

    # ---- derived ----
    @property
    def is_production(self) -> bool:
        return self.env.lower() in {"prod", "production"}

    @property
    def origin(self) -> str:
        """Canonical origin without a trailing slash (never trust a trailing '/')."""
        return self.site_url.rstrip("/")

    @property
    def html_cache_control(self) -> str:
        """
        HTML is served with a short freshness window so Googlebot always sees a
        renderable page while a CDN absorbs the flood. Static assets are
        fingerprint-free but content-hashed by the Tailwind build, so a day of
        immutability is safe and keeps the pages cacheable by shared caches.
        """
        return "public, max-age=60, s-maxage=3600, stale-while-revalidate=86400"

    @property
    def static_cache_control(self) -> str:
        return "public, max-age=86400, s-maxage=604800, immutable" if self.is_production \
            else "no-cache"

    @staticmethod
    def from_env() -> "Settings":
        raw_extra = _env("SSAVE_YTDLP_OPTIONS", "")
        try:
            extra = json.loads(raw_extra) if raw_extra else {}
            if not isinstance(extra, dict):
                raise ValueError("SSAVE_YTDLP_OPTIONS must be a JSON object")
        except json.JSONDecodeError as exc:  # fail loudly at boot, not per request
            raise RuntimeError(f"Invalid SSAVE_YTDLP_OPTIONS: {exc}") from exc

        return Settings(
            site_url=_env("SSAVE_SITE_URL", "https://ssavevideo.com"),
            brand=_env("SSAVE_BRAND", "SsaveVideo"),
            env=_env("SSAVE_ENV", "development"),
            max_concurrency=max(1, _env_int("SSAVE_MAX_CONCURRENCY", 6)),
            rate_limit=max(1, _env_int("SSAVE_RATE_LIMIT", 20)),
            rate_window=max(1, _env_int("SSAVE_RATE_WINDOW", 60)),
            cache_ttl=max(0, _env_int("SSAVE_CACHE_TTL", 900)),
            cache_max_entries=max(16, _env_int("SSAVE_CACHE_MAX_ENTRIES", 512)),
            extract_timeout=max(5, _env_int("SSAVE_EXTRACT_TIMEOUT", 25)),
            socket_timeout=max(2, _env_int("SSAVE_SOCKET_TIMEOUT", 12)),
            retries=max(0, _env_int("SSAVE_RETRIES", 1)),
            trust_proxy_xff=_env_bool("SSAVE_TRUST_PROXY_XFF", True),
            allow_private_hosts=_env_bool("SSAVE_ALLOW_PRIVATE_HOSTS", False),
            max_formats_returned=max(1, _env_int("SSAVE_MAX_FORMATS", 10)),
            max_url_chars=_env_int("SSAVE_MAX_URL_CHARS", 2000),
            extra_ytdlp_options=extra,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide singleton (lru_cache doubles as the memo)."""
    return Settings.from_env()
