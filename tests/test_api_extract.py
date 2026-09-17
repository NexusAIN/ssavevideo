"""
`POST /api/extract` contract tests: validation, abuse control, error vocabulary
and the data-driven media profile.

yt-dlp itself is faked here (`_extract_sync` is the only place that touches the
network); what is under test is our behaviour around it — which formats we
surface, which we hide, and what a caller learns when something goes wrong.
"""

from __future__ import annotations

import copy

import pytest

YOUTUBE_LIKE_INFO = {
    "id": "dQw4w9WgXcQ",
    "title": "Never  Gonna   Give  You  Up",
    "extractor_key": "Youtube",
    "webpage_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "thumbnail": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
    "uploader": "Rick Astley",
    "duration": 212,
    "upload_date": "19871025",
    "availability": "public",
    "is_live": False,
    "formats": [
        {"format_id": "271", "ext": "webm", "vcodec": "vp9", "acodec": "none", "height": 1080,
         "fps": 30, "tbr": 3000, "url": "https://video.example/271.webm", "protocol": "https",
         "filesize": 80_000_000},
        {"format_id": "251", "ext": "webm", "vcodec": "none", "acodec": "opus", "abr": 160,
         "url": "https://video.example/251.webm", "protocol": "https", "filesize": 4_200_000},
        {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 129,
         "url": "https://video.example/140.m4a", "protocol": "https", "filesize": 3_400_000},
        {"format_id": "137", "ext": "mp4", "vcodec": "avc1.640028", "acodec": "none", "height": 1080,
         "tbr": 4300, "url": "https://video.example/137.mp4", "protocol": "https"},
        {"format_id": "248", "ext": "webm", "vcodec": "vp9", "acodec": "none", "height": 720,
         "tbr": 1800, "url": "https://video.example/248.webm", "protocol": "https"},
        {"format_id": "299", "ext": "mp4", "vcodec": "avc1.64002A", "acodec": "mp4a.40.2",
         "height": 1080, "fps": 60, "tbr": 5200, "url": "https://video.example/299.mp4",
         "protocol": "https", "filesize": 120_000_000, "format_note": "HD"},
        {"format_id": "18", "ext": "mp4", "vcodec": "avc1.42001E", "acodec": "mp4a.40.2",
         "height": 360, "tbr": 700, "url": "https://video.example/18.mp4", "protocol": "https",
         "filesize": 19_000_000},
        {"format_id": "22", "ext": "mp4", "vcodec": "avc1.42001E", "acodec": "mp4a.40.2",
         "height": 720, "tbr": 1500, "url": "https://video.example/22.mp4", "protocol": "https",
         "filesize": 36_000_000},
        {"format_id": "sb2", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a", "height": 2160,
         "tbr": 9000, "url": "https://video.example/sb2.mp4", "protocol": "https"},
        {"format_id": "drm-1", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a", "height": 720,
         "drm": ["com.widevine.alpha"], "url": "https://video.example/drm", "protocol": "https"},
        {"format_id": "hls-0", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a", "height": 720,
         "url": "https://video.example/master.m3u8", "protocol": "m3u8"},
        {"format_id": "no-url", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a", "height": 480},
    ],
}

REDDIT_LIKE_INFO = {
    "id": "abc123",
    "title": "cat vs vacuum",
    "extractor_key": "Reddit",
    "webpage_url": "https://www.reddit.com/r/cats/comments/abc123/",
    "duration": 12,
    "formats": [
        {"format_id": "dash_video", "ext": "mp4", "vcodec": "avc1", "acodec": "none", "height": 1080,
         "tbr": 5000, "url": "https://v.redd.it/video.mp4", "protocol": "https"},
        {"format_id": "dash_audio", "ext": "m4a", "vcodec": "none", "acodec": "mp4a", "abr": 128,
         "url": "https://v.redd.it/audio.m4a", "protocol": "https"},
        {"format_id": "public", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a", "height": 540,
         "tbr": 1200, "url": "https://v.redd.it/merged.mp4", "protocol": "https", "filesize": 2_000_000},
    ],
}


@pytest.fixture()
def fake_ytdlp(monkeypatch):
    """Replace the thread-bound yt-dlp call; everything above it still runs."""

    def _install(info: dict):
        """Patch the one function that touches the network; everything else is real."""
        from app.extraction import ExtractionService

        monkeypatch.setattr(
            ExtractionService, "_run_ytdlp", lambda self, href, options: copy.deepcopy(info)
        )
        return info

    return _install


def extract(client, url="https://www.youtube.com/watch?v=dQw4w9WgXcQ", **extra):
    return client.post("/api/extract", json={"url": url, **extra})


# ----------------------------------------------------------------- happy path

def test_returns_normalised_payload_and_direct_links(client, fake_ytdlp):
    fake_ytdlp(YOUTUBE_LIKE_INFO)
    response = extract(client)
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["title"] == "Never Gonna Give You Up"          # whitespace collapsed
    assert data["duration_text"] == "3:32"
    assert data["upload_date"] == "1987-10-25"
    assert data["platform"] == "Youtube"
    assert response.headers["x-extract-cache"] == "MISS"
    urls = {f["url"] for f in data["formats"]}
    assert all(u.startswith("https://") for u in urls)
    assert "https://video.example/drm" not in urls, "DRM rows must never be offered"
    assert not any("no-url" == f["id"] for f in data["formats"])


def test_default_button_is_the_highest_merged_stream_at_or_below_1080p(client, fake_ytdlp):
    fake_ytdlp(YOUTUBE_LIKE_INFO)
    formats = extract(client).json()["formats"]
    defaults = [f for f in formats if f["is_default"]]
    assert len(defaults) == 1
    best = defaults[0]
    assert best["kind"] == "video" and best["height"] == 1080 and best["ext"] == "mp4"
    assert best["has_merged_audio"] is True


def test_video_only_rows_are_labelled_and_sorted_last(client, fake_ytdlp):
    fake_ytdlp(YOUTUBE_LIKE_INFO)
    formats = extract(client).json()["formats"]
    kinds = [f["kind"] for f in formats]
    assert kinds == sorted(kinds, key={"video": 0, "audio": 1, "video_only": 2}.get)
    video_only = next(f for f in formats if f["kind"] == "video_only")
    assert video_only["has_merged_audio"] is False


def test_fragmented_streams_are_not_offered_as_download_buttons(client, fake_ytdlp):
    """A manifest URL is not a file: it must be labelled, and never the default."""
    info = copy.deepcopy(YOUTUBE_LIKE_INFO)
    info["formats"] = [
        {"format_id": "hls-0", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a", "height": 720,
         "tbr": 1500, "url": "https://video.example/master.m3u8", "protocol": "m3u8"},
    ]
    fake_ytdlp(info)
    formats = extract(client).json()["formats"]
    row = next(f for f in formats if f["url"].endswith(".m3u8"))
    assert row["streamable_in_browser"] is False
    # …and when a real file exists, the file wins the default slot.
    fake_ytdlp(YOUTUBE_LIKE_INFO)
    mixed = extract(client, "https://www.youtube.com/watch?v=other").json()["formats"]
    assert not any(f["url"].endswith(".m3u8") and f["is_default"] for f in mixed)


def test_result_is_cached_and_flagged(client, fake_ytdlp):
    fake_ytdlp(YOUTUBE_LIKE_INFO)
    first = extract(client)
    second = extract(client)
    assert first.headers["x-extract-cache"] == "MISS"
    assert second.headers["x-extract-cache"] == "HIT"
    assert second.json()["cached"] is True
    assert second.json()["resolve_ms"] == 0


def test_reddit_profile_promises_one_merged_file(client, fake_ytdlp):
    """`require_progressive` is the reason Reddit does not download silent."""
    fake_ytdlp(REDDIT_LIKE_INFO)
    formats = extract(client, "https://www.reddit.com/r/cats/comments/abc123/",
                      platform="reddit-video-downloader").json()["formats"]
    assert [f["kind"] for f in formats] == ["video", "audio"]
    assert formats[0]["url"].endswith("merged.mp4")
    assert all(f["kind"] != "video_only" for f in formats)


def test_mp3_profile_hides_video_rows(client, fake_ytdlp):
    fake_ytdlp(YOUTUBE_LIKE_INFO)
    data = extract(client, "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
                   platform="youtube-mp3-downloader").json()
    assert data["formats"]
    assert {f["kind"] for f in data["formats"]} == {"audio"}
    assert {f["ext"] for f in data["formats"]} <= {"m4a", "webm", "opus", "mp3"}


def test_tiktok_profile_drops_watermarked_renditions(client, fake_ytdlp):
    info = copy.deepcopy(YOUTUBE_LIKE_INFO)
    info["formats"].insert(0, {
        "format_id": "watermark_1", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a", "height": 1080,
        "tbr": 5500, "url": "https://cdn.example/watermark.mp4", "protocol": "https",
        "format_note": "with watermark",
    })
    fake_ytdlp(info)
    formats = extract(client, "https://www.tiktok.com/@a/video/1", platform="tiktok").json()["formats"]
    assert not any("watermark" in (f["quality_note"] or "").lower() for f in formats)


def test_max_height_is_enforced_per_platform(client, fake_ytdlp):
    fake_ytdlp(YOUTUBE_LIKE_INFO)
    shorts = extract(client, "https://www.youtube.com/shorts/abc", platform="youtube-shorts-downloader")
    heights = [f["height"] for f in shorts.json()["formats"] if f["height"]]
    assert max(heights) <= 1920


def test_openapi_shape_is_stable(client, fake_ytdlp):
    fake_ytdlp(YOUTUBE_LIKE_INFO)
    data = extract(client).json()
    for field in ("ok", "platform", "id", "title", "formats", "resolve_ms", "warnings"):
        assert field in data
    for fmt in data["formats"]:
        assert {"id", "label", "kind", "ext", "url", "streamable_in_browser", "is_default"} <= set(fmt)


# ---------------------------------------------------------------- error paths

@pytest.mark.parametrize(
    "body,code,status",
    [
        ({"url": "short"}, "bad_request", 422),
        ({"url": "ftp://host/video.mp4"}, "invalid_url", 422),
        ({"url": "http://169.254.169.254/latest/meta-data/"}, "invalid_url", 422),
        ({"url": "http://127.0.0.1:8080/x.mp4"}, "invalid_url", 422),
        ({"url": "https://localhost/x.mp4"}, "unsupported", 422),
        ({"url": "https://example.com/", "extra": 1}, "bad_request", 422),
    ],
)
def test_input_is_rejected_before_any_network_work(client, body, code, status):
    response = client.post("/api/extract", json=body)
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    assert response.json()["ok"] is False


def test_control_characters_and_newlines_are_refused(client):
    assert client.post("/api/extract", json={"url": "https://a.com/x\nHost: evil"}).status_code == 422
    assert client.post("/api/extract", json={"url": "https://a.com/\x00x"}).status_code == 422


def test_overlong_url_is_refused(client):
    response = client.post("/api/extract", json={"url": "https://example.com/" + "a" * 3000})
    assert response.status_code == 422


@pytest.mark.parametrize(
    "message,expected_code",
    [
        ("ERROR: [youtube] Sign in to confirm you're not a bot", "upstream_rate_limit"),
        ("ERROR: [instagram] This page requires you to log in", "private"),
        ("ERROR: [facebook] Video unavailable (404)", "notfound"),
        ("ERROR: [tiktok] Unsupported URL: https://x", "unsupported"),
        ("ERROR: unable to connect: timed out", "timeout"),
        ("ERROR: something exotic happened", "extract_failed"),
    ],
)
def test_upstream_failures_map_to_the_error_vocabulary(client, monkeypatch, message, expected_code):
    from app.extraction import ExtractionService

    def boom(self, href, options):
        raise RuntimeError(message)

    monkeypatch.setattr(ExtractionService, "_run_ytdlp", boom)
    response = extract(client)
    assert response.json()["error"]["code"] == expected_code, response.text


def test_private_video_returns_localised_403(client, fake_ytdlp, monkeypatch):
    from app.extraction import ExtractError, ExtractionService

    def private(self, href, options):
        raise ExtractError("Login required", code="private", status=403)

    monkeypatch.setattr(ExtractionService, "_run_ytdlp", private)
    response = extract(client)
    assert response.status_code == 403
    body = response.json()
    assert body["error"]["code"] == "private"
    # Spanish page -> Spanish message: the error contract is locale-aware.
    spanish = client.post("/api/extract", json={"url": "https://www.youtube.com/watch?v=x", "locale": "es"})
    assert "SsaveVideo" not in spanish.json()["error"]["message"]
    assert len(spanish.json()["error"]["message"]) > 20


def test_video_without_media_is_a_422_not_an_empty_200(client, fake_ytdlp):
    fake_ytdlp({"id": "x", "title": "silent", "extractor_key": "Generic",
                "webpage_url": "https://www.youtube.com/watch?v=x", "formats": []})
    response = extract(client)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "no_formats"


def test_playlist_links_resolve_to_the_first_entry_only(client, fake_ytdlp):
    """A playlist URL must cost one extraction, not N — and must not leak `entries`."""
    fake_ytdlp({
        "id": "PL1", "title": "Mix", "extractor_key": "YoutubeTab", "_type": "playlist",
        "entries": [YOUTUBE_LIKE_INFO, {"id": "second", "title": "next", "formats": []}],
        "formats": [],
    })
    data = extract(client).json()
    assert data["id"] == "dQw4w9WgXcQ"
    assert data["title"] == "Never Gonna Give You Up"


# ------------------------------------------------------------ abuse control

def test_rate_limit_returns_429_with_retry_after(client):
    from app.main import create_app  # noqa: F401  (documents where the limiter lives)

    limiter = client.app.state.limiter
    limiter.limit = 3
    blocked_locally = {"url": "http://127.0.0.1:9/video.mp4"}
    for _ in range(3):
        client.post("/api/extract", json=blocked_locally)
    response = client.post("/api/extract", json=blocked_locally)
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) >= 1
    assert response.json()["error"]["code"] == "rate_limited"
    assert limiter.snapshot()["rejections"] >= 1


def test_rate_limit_key_follows_forwarded_for(client):
    limiter = client.app.state.limiter
    limiter.limit = 1
    body = {"url": "http://127.0.0.1:9/video.mp4"}
    first = client.post("/api/extract", json=body, headers={"X-Forwarded-For": "9.9.9.9"})
    assert first.status_code == 422, "blocked by the SSRF guard, not by the limiter"
    blocked = client.post("/api/extract", json=body, headers={"X-Forwarded-For": "9.9.9.9"})
    assert blocked.status_code == 429, "same visitor, second hit, must be throttled"
    other = client.post("/api/extract", json=body, headers={"X-Forwarded-For": "8.8.8.8"})
    assert other.status_code != 429, "a different visitor must not inherit the block"


def test_concurrency_gate_is_bounded(client):
    settings = client.app.state.settings
    assert 1 <= settings.max_concurrency <= 32
    assert client.app.state.extractor._gate._value <= settings.max_concurrency


def test_api_metadata_endpoints(client):
    platforms = client.get("/api/platforms").json()
    assert len(platforms) == 10
    reddit = next(p for p in platforms if p["slug"] == "reddit-video-downloader")
    assert reddit["extraction"]["require_progressive"] is True
    assert all(p["domains"] for p in platforms)


def test_an_unexpected_engine_failure_is_never_a_500(fault_client, monkeypatch):
    """
    Design guarantee: anything the engine throws — even a bug we did not anticipate —
    is classified into the documented error vocabulary (502/504/429/…), so the UI
    always has a real message to show and an API caller never has to parse a 500.
    """
    from app.extraction import ExtractionService

    def explode(self, href, options):
        raise RuntimeError("unanticipated engine bug")

    monkeypatch.setattr(ExtractionService, "_run_ytdlp", explode)
    response = fault_client.post("/api/extract", json={"url": "https://www.youtube.com/watch?v=abc"})
    assert response.status_code in {502, 504}
    body = response.json()
    assert body["error"]["code"] in {"extract_failed", "network", "timeout"}
    assert body["error"]["message"] and "unanticipated" not in response.text


def test_api_never_echoes_the_visitors_url_back_unescaped(client):
    """An error body is a reflection point; keep it free of raw input."""
    evil = 'https://www.youtube.com/watch?v=1"><script>alert(1)</script>'
    response = client.post("/api/extract", json={"url": evil})
    assert "<script>alert(1)</script>" not in response.text
