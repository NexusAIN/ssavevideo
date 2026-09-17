"""
Unit tests for the format picker and the media policy it enforces.

The picker is where a downloader either earns trust or loses it: the labels must
match what the file actually contains (a "1080p" that is really a padded 720p,
or a "video only" row served as the default button, both cost users time). These
tests pin that behaviour against synthetic manifests shaped like real YouTube /
Reddit / TikTok output.
"""

from __future__ import annotations

from dataclasses import asdict

import pytest

from app.extraction import (
    ExtractionService,
    classify,
    flatten_playlist,
    human_duration,
    human_filesize,
)
from app.config import Settings
from dataclasses import replace


@pytest.fixture()
def service():
    return ExtractionService(replace(Settings.from_env(), cache_ttl=0, max_formats_returned=8))


def fmt(**kwargs) -> dict:
    base = {
        "format_id": kwargs.pop("id", "x"),
        "url": kwargs.pop("url", f"https://cdn.example/{kwargs.get('ext', 'mp4')}"),
        "ext": "mp4",
        "protocol": "https",
    }
    base.update(kwargs)
    return base


# ------------------------------------------------------------------ classify

@pytest.mark.parametrize(
    "stream,expected",
    [
        ({"vcodec": "avc1", "acodec": "mp4a", "ext": "mp4"}, ("video", True, True)),
        ({"vcodec": "avc1", "acodec": "none", "ext": "mp4"}, ("video_only", False, True)),
        ({"vcodec": "none", "acodec": "opus", "ext": "webm"}, ("audio", False, True)),
        ({"vcodec": None, "ext": "mp4"}, ("video", True, False)),        # bare file, muxed container
        ({"vcodec": None, "ext": "mp3"}, ("audio", False, False)),       # bare audio file
        ({"vcodec": None, "ext": "webm"}, ("video", True, False)),
        ({"vcodec": "none", "ext": "mp4"}, ("audio", False, False)),      # explicit no-video
    ],
)
def test_classify_reads_codecs_and_guesses_honestly_when_unknown(stream, expected):
    assert classify(stream) == expected


# -------------------------------------------------------------- selection

def test_progressive_rows_win_and_are_bucketed_by_resolution(service):
    formats = [
        fmt(id="271", vcodec="vp9", acodec="none", height=1080, tbr=3000, ext="webm"),
        fmt(id="137", vcodec="avc1", acodec="none", height=1080, tbr=4300, ext="mp4"),
        fmt(id="299", vcodec="avc1", acodec="mp4a", height=1080, tbr=5200, ext="mp4"),
        fmt(id="22", vcodec="avc1", acodec="mp4a", height=720, tbr=1500, ext="mp4"),
        fmt(id="18", vcodec="avc1", acodec="mp4a", height=360, tbr=700, ext="mp4"),
        fmt(id="140", vcodec="none", acodec="mp4a", abr=129, ext="m4a"),
    ]
    options = service._select_formats(
        formats, audio_only=False, require_progressive=False, prefer_clean=False,
        ext_priority=["mp4", "webm"], max_height=2160, ladder=[2160, 1080, 720, 360],
    )
    by_id = {o.id: o for o in options}
    assert "299" in by_id, "best merged 1080p must survive"
    assert "22" in by_id and "18" in by_id, "one row per resolution bucket"
    assert by_id["299"].is_default, "the default is the highest merged row <= 1080p"
    assert [o.kind for o in options] == sorted(
        [o.kind for o in options], key=lambda k: {"video": 0, "audio": 1, "video_only": 2}[k]
    )
    # Separate video+audio streams are not merged server-side, so they must be
    # labelled instead of silently offered as if they were files that play.
    assert by_id["137"].kind == "video_only" and by_id["137"].has_merged_audio is False
    assert by_id["140"].kind == "audio"


def test_require_progressive_drops_silent_streams(service):
    formats = [
        fmt(id="dash_video", vcodec="avc1", acodec="none", height=1080, tbr=5000),
        fmt(id="dash_audio", vcodec="none", acodec="mp4a", abr=128, ext="m4a"),
        fmt(id="public", vcodec="avc1", acodec="mp4a", height=540, tbr=1200),
    ]
    options = service._select_formats(
        formats, audio_only=False, require_progressive=True, prefer_clean=False,
        ext_priority=["mp4"], max_height=1080, ladder=[1080, 720, 540],
    )
    assert [o.id for o in options] == ["public", "dash_audio"]
    assert options[0].kind == "video" and options[0].is_default


def test_audio_only_profile_never_returns_video(service):
    formats = [
        fmt(id="140", vcodec="none", acodec="mp4a", abr=129, ext="m4a"),
        fmt(id="251", vcodec="none", acodec="opus", abr=160, ext="webm"),
        fmt(id="18", vcodec="avc1", acodec="mp4a", height=360, tbr=700),
    ]
    options = service._select_formats(
        formats, audio_only=True, require_progressive=False, prefer_clean=False,
        ext_priority=["mp4"], max_height=0, ladder=[],
    )
    assert {o.kind for o in options} == {"audio"}
    assert options[0].id == "251", "the 160 kbps stream beats the 129 kbps one"
    assert "kbps" in options[0].label


def test_max_height_and_watermark_filters(service):
    formats = [
        fmt(id="4k", vcodec="avc1", acodec="mp4a", height=2160, tbr=9000),
        fmt(id="1080", vcodec="avc1", acodec="mp4a", height=1080, tbr=4000),
        fmt(id="wm", vcodec="avc1", acodec="mp4a", height=1080, tbr=4200, format_note="with watermark"),
    ]
    capped = service._select_formats(
        formats, audio_only=False, require_progressive=False, prefer_clean=False,
        ext_priority=["mp4"], max_height=1080, ladder=[1080, 720],
    )
    assert "4k" not in {o.id for o in capped}
    clean = service._select_formats(
        formats, audio_only=False, require_progressive=False, prefer_clean=True,
        ext_priority=["mp4"], max_height=1080, ladder=[1080, 720],
    )
    assert "wm" not in {o.id for o in clean}


def test_drm_and_urlless_rows_are_never_offered(service):
    formats = [
        {"format_id": "drm", "url": "https://x/drm", "ext": "mp4", "drm": ["com.widevine.alpha"]},
        {"format_id": "nostream", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a"},
        {"format_id": "file", "url": "file:///etc/passwd", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a"},
        fmt(id="ok", vcodec="avc1", acodec="mp4a", height=720, tbr=1000),
    ]
    options = service._select_formats(
        formats, audio_only=False, require_progressive=False, prefer_clean=False,
        ext_priority=["mp4"], max_height=1080, ladder=[1080, 720],
    )
    assert [o.id for o in options] == ["ok"]


def test_format_count_is_capped(service):
    formats = [fmt(id=f"h{h}", vcodec="avc1", acodec="mp4a", height=h, tbr=h) for h in range(240, 2200, 60)]
    options = service._select_formats(
        formats, audio_only=False, require_progressive=False, prefer_clean=False,
        ext_priority=["mp4"], max_height=2160, ladder=[2160, 1440, 1080, 720, 480, 360],
    )
    assert len(options) <= service.settings.max_formats_returned


# ------------------------------------------------------- payload building

def test_build_result_sanitises_and_formats(service):
    info = {
        "id": "abc", "extractor_key": "TikTok",
        "title": "  weird\n\ttitle   with <html>  ",
        "thumbnail": "//cdn.example/t.jpg",
        "duration": 61.9, "uploader": "user", "upload_date": "20260102",
        "is_live": True, "availability": "private",
        "webpage_url": "https://www.tiktok.com/@u/video/abc",
        "formats": [fmt(id="1", vcodec="avc1", acodec="mp4a", height=1080, tbr=2000,
                        filesize_approx=5_000_000)],
    }
    result = service._build_result(info, {"prefer_clean_stream": False, "max_height": 1080}, 42)
    data = asdict(result)
    assert data["title"] == "weird title with <html>"
    assert data["thumbnail"] == "https://cdn.example/t.jpg"
    assert data["duration_text"] == "1:01"
    assert data["upload_date"] == "2026-01-02"
    assert data["is_live"] is True
    assert "live_stream" in data["warnings"] and "availability:private" in data["warnings"]
    assert data["formats"][0]["filesize_text"].endswith("MB")
    assert data["formats"][0]["approx_note"] == "estimated"


def test_build_result_drops_unsafe_thumbnails(service):
    info = {"id": "x", "title": "t", "thumbnail": "javascript:alert(1)", "formats": [], "duration": None}
    data = asdict(service._build_result(info, {}, 1))
    assert data["thumbnail"] is None
    assert "no_formats" in data["warnings"]


def test_asset_urls_are_scheme_filtered(service):
    assert ExtractionService._safe_asset_url("https://a/b.jpg").startswith("https://")
    assert ExtractionService._safe_asset_url("data:image/png;base64,AA") is None
    assert ExtractionService._safe_asset_url("http://" + "a" * 3000) is None
    assert ExtractionService._safe_asset_url(None) is None


# --------------------------------------------------------------- playlist

def test_flatten_playlist_takes_first_entry_only():
    video = {"id": "v1", "title": "One", "formats": [fmt(id="1")]}
    out = flatten_playlist(
        {"_type": "playlist", "id": "PL", "title": "Mix", "entries": [video, {"id": "v2"}]},
        lambda url: video,
    )
    assert out["id"] == "v1" and "entries" not in out


def test_flatten_playlist_resolves_a_stub_entry():
    seen = []

    def resolver(url):
        seen.append(url)
        return {"id": "deep", "formats": [fmt(id="9")]}

    out = flatten_playlist(
        {"_type": "playlist", "entries": [{"_type": "url", "url": "https://x/1"}]}, resolver
    )
    assert out["id"] == "deep" and seen == ["https://x/1"], "the stub must be resolved exactly once"


def test_flatten_playlist_rejects_an_empty_collection():
    from app.extraction import ExtractError

    with pytest.raises(ExtractError) as exc:
        flatten_playlist({"_type": "playlist", "entries": []}, lambda url: {})
    assert exc.value.code == "notfound" and exc.value.status == 404


def test_plain_video_info_passes_through_untouched():
    info = {"id": "v", "formats": []}
    assert flatten_playlist(info, lambda url: {}) is info


# ------------------------------------------------------------ error mapping

@pytest.mark.parametrize(
    "text,code,status",
    [
        ("ERROR: [youtube] Sign in to confirm you're not a bot", "upstream_rate_limit", 429),
        ("ERROR: Unsupported URL: https://nope.example/x", "unsupported", 422),
        ("ERROR: [instagram] Login required", "private", 403),
        ("ERROR: [facebook] Video unavailable", "notfound", 404),
        ("ERROR: [reddit] Geo-blocked: not available in your country", "geo_or_forbidden", 451),
        ("ERROR: unable to connect: timed out", "timeout", 504),
        ("ERROR: [generic] TLS handshake failed", "network", 502),
        ("ERROR: totally unknown condition", "extract_failed", 502),
    ],
)
def test_classification_table(text, code, status):
    error = ExtractionService._classify(text)
    assert (error.code, error.status) == (code, status)


def test_formatter_helpers():
    assert human_duration(0) == "0:00"
    assert human_duration(3725) == "1:02:05"
    assert human_duration(None) is None
    assert human_filesize(1536) == "1.5 KB"
    assert human_filesize(5 * 1024**3) == "5.0 GB"
    assert human_filesize(1024**3 * 120) == "120 GB"
    assert human_filesize(0) is None
