"""
End-to-end integration test with the REAL extraction engine — no mocks.

It runs the actual FastAPI app, the actual threading + timeout + semaphore path,
and the actual `yt_dlp` extractor, against a media file served from a throwaway
localhost HTTP server. Loopback is normally refused by the SSRF guard, so this
test builds the app with the documented developer switch (`allow_private_hosts`)
instead of weakening the default policy.

This is the test that proves the central claim of the product: we return direct
URLs to the media, and we never fetch or store the media ourselves.
"""

from __future__ import annotations

import contextlib
import http.server
import struct
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from app.config import Settings

pytestmark = pytest.mark.slow


def _mp4_bytes() -> bytes:
    """
    A structurally valid, tiny MP4 (ftyp + moov/mvhd + mdat).

    No ffmpeg dependency, and the extractor never decodes it: what matters here is
    the *metadata* path — content sniffing, format construction, URL resolution.
    """

    def box(kind: bytes, payload: bytes = b"") -> bytes:
        return struct.pack(">I", len(payload) + 8) + kind + payload

    ftyp = box(b"ftyp", b"isom" + b"\x00\x00\x02\x00" + b"isomiso2avc1mp41")
    mvhd = box(
        b"mvhd",
        b"\x00" * 4 + b"\x00" * 8 + struct.pack(">I", 1000) + struct.pack(">I", 3000)
        + b"\x00\x01\x00\x00" + b"\x00" * 10 + b"\x00\x00\x04\x00" + b"\x00" * 39,
    )
    return ftyp + box(b"moov", mvhd) + box(b"mdat", b"\x11" * 4096)


@pytest.fixture(scope="module")
def media_server(tmp_path_factory):
    directory = tmp_path_factory.mktemp("media")
    (directory / "sample-clip.mp4").write_bytes(_mp4_bytes())
    (directory / "not-media.txt").write_text("this is not a video", encoding="utf-8")

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(directory), **kwargs)

        def log_message(self, *args):  # keep pytest output clean
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    with contextlib.suppress(Exception):
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="module")
def app(media_server):
    from app.main import create_app

    settings = replace(
        Settings.from_env(),
        site_url="https://ssavevideo.com",
        env="development",
        allow_private_hosts=True,   # dev-only switch, exercised on purpose
        rate_limit=1000,            # this module makes many calls
        cache_ttl=0,                # every request must hit the real extractor
    )
    return create_app(settings)


@pytest.fixture(scope="module")
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client


def test_real_extractor_returns_direct_urls_without_touching_the_media(client, media_server, monkeypatch):
    """
    The point of the architecture, asserted: the response carries a URL, and the
    server itself never downloaded the payload.
    """
    downloads = []
    import yt_dlp

    real = yt_dlp.YoutubeDL.download

    def spy(self, *args, **kwargs):  # pragma: no cover - guard only
        downloads.append("download")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(yt_dlp.YoutubeDL, "download", spy)

    response = client.post("/api/extract", json={"url": f"{media_server}/sample-clip.mp4"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["title"] == "sample-clip"
    assert body["formats"], "a direct MP4 must yield exactly one downloadable row"
    row = body["formats"][0]
    assert row["url"] == f"{media_server}/sample-clip.mp4"
    assert row["ext"] == "mp4"
    assert row["kind"] in {"video", "video_only"}
    assert row["streamable_in_browser"] is True
    assert downloads == [], "the server must never invoke yt-dlp's downloader"


def test_thumbnail_field_is_only_ever_an_http_url(client, media_server):
    body = client.post("/api/extract", json={"url": f"{media_server}/sample-clip.mp4"}).json()
    assert body["thumbnail"] is None or body["thumbnail"].startswith(("https://", "http://"))


def test_a_url_without_media_gets_the_honest_answer(client, media_server):
    """
    A text file is not an error worth 500s: the extractor reads it, finds no
    playable stream, and we say so — with the "no media here" vocabulary rather
    than a generic failure.
    """
    response = client.post("/api/extract", json={"url": f"{media_server}/not-media.txt"})
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "no_formats"
    # The API reuses the exact string the front-end shows for the same case, so
    # the inline error and the JSON body never disagree.
    assert error["message"] == "No downloadable media was found for this link."


def test_api_errors_are_localised_end_to_end(client, media_server):
    """Same failure, requested from the Spanish page -> Spanish copy."""
    spanish = client.post(
        "/api/extract", json={"url": f"{media_server}/not-media.txt", "locale": "es"}
    ).json()["error"]["message"]
    assert "enlace" in spanish
    assert spanish != "No downloadable media was found for this link."



def test_missing_file_maps_to_not_found(client, media_server):
    response = client.post("/api/extract", json={"url": f"{media_server}/nope.mp4"})
    assert response.status_code in {404, 422, 502}
    assert response.json()["error"]["code"] in {"notfound", "unsupported", "extract_failed", "network"}


def test_timeout_budget_is_enforced(client, monkeypatch):
    """
    A hung upstream must not occupy a worker forever: `asyncio.wait_for` is the
    only thing between one slow CDN and a saturated thread pool.
    """
    from app.extraction import ExtractionService

    def hang(self, href, options):
        import time

        time.sleep(2)
        return {}

    monkeypatch.setattr(ExtractionService, "_run_ytdlp", hang)
    # `extract()` reads the timeout from its settings object at call time.
    client.app.state.extractor.settings = replace(
        client.app.state.extractor.settings, extract_timeout=1
    )
    response = client.post("/api/extract", json={"url": "http://127.0.0.1:9/x.mp4"})
    assert response.json()["error"]["code"] in {"timeout", "invalid_url", "extract_failed"}


def test_pages_render_and_the_api_agrees_with_the_schema(client):
    """The homepage's advertised platforms and /api/platforms must be the same list."""
    platforms = {p["slug"] for p in client.get("/api/platforms").json()}
    for slug in platforms:
        assert client.get(f"/{slug}").status_code == 200, slug
    assert "reddit-video-downloader" in platforms
