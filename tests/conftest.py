"""
Shared fixtures.

The app is built per test through `create_app(settings)` rather than importing the
module-level `app`, so a test can construct a Settings object with a different
policy (e.g. `allow_private_hosts=True` for the local-file integration test) and
get a fresh, isolated instance.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent


def _fresh_settings(**overrides):
    from app.config import Settings

    base = Settings.from_env()
    # Force the "as if deployed on the real domain" view so canonical URL tests
    # never depend on the CI environment's env vars.
    base = replace(
        base,
        site_url="https://ssavevideo.com",
        brand="SsaveVideo",
        env="production",
        cache_ttl=60,
        rate_limit=20,
        rate_window=60,
        allow_private_hosts=False,
    )
    return replace(base, **overrides) if overrides else base


@pytest.fixture(scope="session")
def settings():
    return _fresh_settings()


@pytest.fixture(scope="session")
def store(settings):
    from app.content import ContentStore

    return ContentStore(settings=settings)


@pytest.fixture(scope="session")
def schema():
    return json.loads((ROOT / "data" / "seo_platforms.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def catalog():
    return json.loads((ROOT / "data" / "locales.json").read_text(encoding="utf-8"))


@pytest.fixture()
def app(settings):
    """A fresh app instance: no module-level global state shared between tests."""
    from app.main import create_app

    return create_app(settings)


@pytest.fixture()
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def fault_client(app):
    """
    Client that returns the 500 response instead of re-raising it.

    TestClient re-raises server exceptions by default (useful everywhere else), so
    tests that assert on the *shape* of an unhandled failure need this variant.
    """
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture()
def private_client():
    """Client whose SSRF guard permits loopback, for the local-media integration test."""
    from app.main import create_app

    with TestClient(create_app(_fresh_settings(allow_private_hosts=True, rate_limit=500))) as test_client:
        yield test_client
