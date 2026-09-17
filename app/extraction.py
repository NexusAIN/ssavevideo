"""
Media *URL* resolution — the only thing this service does with a video.

Design contract (this is the whole reason the product can be free):

    The server never downloads, transcodes, muxes, caches on disk or proxies a
    single media byte. yt-dlp is used purely as a manifest reader: we call
    `extract_info(download=False)`, which performs the same HTTP requests a
    browser makes to fetch the playlist / MPD / JSON that *describes* the media,
    and then we hand the resulting CDN URLs to the visitor's browser. The
    visitor's connection pays for the bytes, not this box — so bandwidth cost is
    effectively zero and there are no stored copies to answer a takedown about.

Consequences that shaped this module:

* **No ffmpeg dependency.** Anything requiring a server-side merge would need a
  real download. Instead we *prefer* progressive (audio+video already muxed)
  renditions, label the rest honestly as video-only, and list audio separately.
  `data/seo_platforms.json` sets `require_progressive` for Reddit, which is
  exactly where users hit silent files.
* **Bounded concurrency.** yt-dlp is synchronous and network-bound, so every
  call runs in a worker thread behind an `asyncio.Semaphore`. One vCPU stays
  free even under a crawler storm, and the box cannot be pinned by 1 request.
* **Sanitised output.** Titles, thumbnails and URLs are length-capped,
  HTTPS-filtered and truncated to a short list of rows a human can actually use.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional

from .config import Settings, get_settings
from .security import SafeURL, TTLCache, UnsafeURL, validate_public_url

log = logging.getLogger("ssavevideo.extract")

DEFAULT_VIDEO_EXTS = ("mp4", "webm", "mkv")


# ---------------------------------------------------------------------------
# Errors — `code` is a stable contract: main.py maps each code to a localised
# UI string, and the front-end uses it to choose an inline message + CTA.
# ---------------------------------------------------------------------------


class ExtractError(Exception):
    """Extraction failure carrying a machine code, an HTTP status and a message."""

    def __init__(self, message: str, *, code: str = "extract_failed", status: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FormatOption:
    """One download button in the result card."""

    id: str
    label: str                      # e.g. "1080p Full HD · MP4"
    kind: str                       # video | audio | video_only
    ext: str
    height: Optional[int] = None
    width: Optional[int] = None
    fps: Optional[float] = None
    vcodec: Optional[str] = None
    acodec: Optional[str] = None
    bitrate_kbps: Optional[int] = None
    filesize: Optional[int] = None
    filesize_text: Optional[str] = None
    quality_note: Optional[str] = None
    url: str = ""
    protocol: str = ""
    streamable_in_browser: bool = True
    is_default: bool = False
    has_merged_audio: bool = True
    approx_note: Optional[str] = None


@dataclass(slots=True)
class ExtractResult:
    """Payload for a successful lookup (serialised by `to_api_dict`)."""

    ok: bool
    platform: str
    id: str
    title: str
    thumbnail: Optional[str]
    duration: Optional[int]
    duration_text: Optional[str]
    uploader: Optional[str]
    webpage_url: str
    upload_date: Optional[str]
    is_live: bool
    availability: Optional[str]
    formats: list[FormatOption] = field(default_factory=list)
    resolve_ms: int = 0
    cached: bool = False
    warnings: list[str] = field(default_factory=list)

    def to_api_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Small formatting helpers (shared with the SEO layer)
# ---------------------------------------------------------------------------

_HEIGHT_LABELS = {
    4320: "8K", 2160: "4K", 1440: "2K", 1080: "Full HD", 720: "HD",
    540: "qHD", 480: "SD", 360: "SD", 240: "Low", 144: "Tiny",
}
_TITLE_NOISE = re.compile(r"\s+")


def human_filesize(num: Optional[float]) -> Optional[str]:
    if not num or num <= 0:
        return None
    units = ("B", "KB", "MB", "GB", "TB")
    size, idx = float(num), 0
    while size >= 1024 and idx < len(units) - 1:
        size, idx = size / 1024, idx + 1
    return f"{size:.0f} {units[idx]}" if size >= 100 else f"{size:.1f} {units[idx]}"


def human_duration(seconds: Optional[float]) -> Optional[str]:
    if seconds is None:
        return None
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _best_bitrate(fmt: dict) -> Optional[int]:
    for key in ("tbr", "vbr", "abr"):
        value = fmt.get(key)
        if value:
            return int(round(float(value)))
    return None


AUDIO_CONTAINERS = {"mp3", "m4a", "aac", "opus", "ogg", "flac", "wav"}
VIDEO_CONTAINERS = {"mp4", "mov", "mkv", "webm", "m4v"}


def _has_video(fmt: dict) -> bool:
    return str(fmt.get("vcodec", "none")) != "none"


def _has_audio(fmt: dict) -> bool:
    return str(fmt.get("acodec", "none")) != "none"


def classify(fmt: dict) -> tuple[str, bool, bool]:
    """
    Return (kind, has_merged_audio, codecs_are_known).

    `kind` is "video" | "audio" | "video_only". yt-dlp only knows the codec when
    ffprobe ran or the extractor parsed a manifest, so a bare file URL arrives
    with `vcodec: None` and no `acodec` key at all. Trusting that literally would
    label every downloaded MP4 "video only, no sound" — so when the codecs are
    unknown we fall back to the container, and we flag the guess in `warnings`
    instead of lying about it in the row label.
    """
    vcodec, acodec = fmt.get("vcodec"), fmt.get("acodec")
    v_is_str, a_is_str = isinstance(vcodec, str), isinstance(acodec, str)
    video_present = v_is_str and vcodec != "none"
    audio_present = a_is_str and acodec != "none"
    video_absent = v_is_str and vcodec == "none"
    audio_absent = a_is_str and acodec == "none"

    if video_present and audio_absent:
        return "video_only", False, True
    if video_absent and audio_present:
        return "audio", False, True
    if video_present and audio_present:
        return "video", True, True

    # Nothing was verified: decide from the container and mark it as a guess.
    ext = str(fmt.get("ext") or "")
    if ext in AUDIO_CONTAINERS or (video_absent and not audio_present):
        return "audio", False, False
    # A bare .mp4/.mkv URL is a muxed file in practice (that is what those
    # containers are for), so it is offered as a normal video row.
    return "video", ext in VIDEO_CONTAINERS, False


def flatten_playlist(info: dict, resolver) -> dict:
    """
    Enforce one link -> one video.

    Visitors paste a playlist, a channel tab or an album as often as a single
    watch URL. Expanding one would multiply the upstream work of a request by N,
    so the policy is: take the first entry, and if the platform returned only a
    stub for it, resolve that one entry for real.
    """
    if not isinstance(info, dict):
        raise ExtractError("Unsupported link type.", code="unsupported", status=422)
    if info.get("_type") != "playlist" and "entries" not in info:
        return info

    entries = [entry for entry in (info.get("entries") or []) if isinstance(entry, dict)]
    if not entries:
        raise ExtractError(
            "No playable video was found at that link.", code="notfound", status=404
        )
    entry = entries[0]
    if not entry.get("formats") and (entry.get("url") or entry.get("webpage_url")):
        resolved = resolver(entry.get("url") or entry["webpage_url"])
        if isinstance(resolved, dict):
            entry = resolved
    merged = {key: value for key, value in info.items() if key not in {"entries", "_type"}}
    merged.update({k: v for k, v in entry.items() if v not in (None, [], {}, "")})
    return merged


def _codec_known(fmt: dict) -> bool:
    return classify(fmt)[2]


def _nearest_bucket(height: int, ladder: Iterable[int]) -> int:
    buckets = sorted((int(b) for b in ladder), reverse=True) or [2160, 1440, 1080, 720, 480, 360]
    for bucket in buckets:
        if height >= bucket:
            return bucket
    return buckets[-1]


class _YtdlpLogger:
    """Adapts yt-dlp's logger interface onto `logging` (debug -> trace, warnings up)."""

    def debug(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        log.info("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        # Upstream extractors are chatty by design; the exception we raise carries
        # the real signal, so this is debug-level noise for an operator, not an error.
        log.debug("yt-dlp error: %s", msg)


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


class ExtractionService:
    """Cached, bounded, async facade over yt-dlp's metadata extraction."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self.cache = TTLCache(self.settings.cache_ttl, self.settings.cache_max_entries)
        self._gate = asyncio.Semaphore(self.settings.max_concurrency)
        # Cheap counters for /metrics; a real Prometheus exporter is a drop-in here.
        self.stats: dict[str, Any] = {
            "requests": 0, "success": 0, "failed": 0,
            "cache_hits": 0, "in_flight_peak": 0,
        }
        self._in_flight = 0
        self._base_options: dict[str, Any] = {
            # --- read-only policy: resolve URLs, never fetch or write media ---
            "skip_download": True,
            "noprogress": True,
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": False,      # surface the true cause so we can map it
            "noplaylist": True,
            "playlist_items": "1",      # never expand a playlist link into N lookups
            "extract_flat": False,      # we need concrete format URLs, not just ids
            "socket_timeout": self.settings.socket_timeout,
            "retries": self.settings.retries,
            "fragment_retries": 0,
            "file_access_retries": 0,
            "concurrent_fragment_downloads": 1,
            # A plain desktop UA matches what a logged-out visitor sends. No
            # cookies, no auth, no session material ever leaves this box.
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.8",
            },
            # Belt-and-braces: nothing may touch the filesystem.
            "cachedir": False,
            "writeinfojson": False,
            "writethumbnail": False,
            "writesubtitles": False,
            "writeautomaticsub": False,
            "overwrites": None,
            # Send yt-dlp's chatter to our logger: an ERROR line printed straight to
            # stderr is unusable in a journald stream and hides the request context.
            "logger": _YtdlpLogger(),
        }
        # Ops escape hatch (SSAVE_YTDLP_OPTIONS) — e.g. bumping `retries` or
        # pinning `extractor_args` when a platform changes its player API.
        self._base_options.update(self.settings.extra_ytdlp_options)

    # ------------------------------------------------------------------ API

    async def extract(self, raw_url: str, profile: Optional[dict] = None) -> dict[str, Any]:
        """Resolve `raw_url` into the API payload (a short list of direct links)."""
        profile = profile or {}
        self.stats["requests"] += 1

        safe: SafeURL = await validate_public_url(
            raw_url,
            max_chars=self.settings.max_url_chars,
            allow_private_hosts=self.settings.allow_private_hosts,
        )
        payload = self.cache.get(safe.href)
        if isinstance(payload, dict):
            self.stats["cache_hits"] += 1
            return {**payload, "cached": True, "resolve_ms": 0}

        started = time.perf_counter()
        async with self._gate:
            self._in_flight += 1
            self._in_flight_peak = max(self.stats["in_flight_peak"], self._in_flight)
            self.stats["in_flight_peak"] = self._in_flight_peak
            loop = asyncio.get_running_loop()
            try:
                info = await asyncio.wait_for(
                    loop.run_in_executor(None, self._extract_sync, safe.href, profile),
                    timeout=self.settings.extract_timeout,
                )
            except asyncio.TimeoutError as exc:
                self.stats["failed"] += 1
                raise ExtractError(
                    "The platform took too long to answer.", code="timeout", status=504
                ) from exc
            except UnsafeURL as exc:
                self.stats["failed"] += 1
                raise ExtractError(str(exc), code=exc.reason, status=422) from exc
            except ExtractError:
                self.stats["failed"] += 1
                raise
            except Exception as exc:  # noqa: BLE001 — normalised, never re-raised raw
                log.info("unmapped extractor failure: %s: %s", type(exc).__name__, exc)
                self.stats["failed"] += 1
                raise self._classify(f"{type(exc).__name__}: {exc}") from exc
            finally:
                self._in_flight -= 1

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        result = self._build_result(info, profile, elapsed_ms)
        payload = result.to_api_dict()
        self.stats["success"] += 1
        self.cache.set(safe.href, payload)
        return payload

    # ------------------------------------------------- yt-dlp in a thread

    def _extract_sync(self, href: str, profile: dict) -> dict:
        """
        Synchronous yt-dlp call, executed in a worker thread.

        Two steps, deliberately separated: `_run_ytdlp` is the only code in this
        project that touches the network, and `flatten_playlist` is pure policy.
        That is also what makes the behaviour above testable without a socket.
        """
        options = self.options_for(profile)
        info = self._run_ytdlp(href, options)
        if not isinstance(info, dict):
            raise ExtractError("Unsupported link type.", code="unsupported", status=422)
        return flatten_playlist(info, lambda url: self._run_ytdlp(url, options))

    def options_for(self, profile: dict) -> dict:
        """The yt-dlp option dict for one page: global defaults + platform profile."""
        options = dict(self._base_options)
        ext_priority = list(profile.get("video_ext_priority") or DEFAULT_VIDEO_EXTS)
        if profile.get("audio_only"):
            options["format"] = "bestaudio/best"
        elif profile.get("require_progressive"):
            # "b" is yt-dlp's shorthand for the best *merged* file when one exists;
            # the fallbacks keep us alive on manifests that publish no such thing.
            options["format"] = "b/bv*+ba/b"
        else:
            options["format"] = "all"
        # Bias yt-dlp's own ordering so our picker sees the containers this page
        # promises (MP4 before WebM) while still receiving every rendition.
        options["format_sort"] = ["br", "res", "ext:" + ":".join(ext_priority)]
        options["socket_timeout"] = int(profile.get("socket_timeout_seconds") or self.settings.socket_timeout)
        return options

    def _run_ytdlp(self, href: str, options: dict) -> dict:
        """THE network seam. Everything below it is ours; nothing above it blocks."""
        try:
            from yt_dlp import YoutubeDL
            from yt_dlp.utils import DownloadError, ExtractorError, UnsupportedError
        except ImportError as exc:  # pragma: no cover - the image always ships yt-dlp
            raise ExtractError(
                "The extraction engine is unavailable on this server.",
                code="engine_missing", status=500,
            ) from exc

        try:
            with YoutubeDL(options) as ydl:
                return ydl.extract_info(href, download=False)
        except UnsupportedError as exc:
            raise ExtractError(
                "This site or link type is not supported.", code="unsupported", status=422
            ) from exc
        except ExtractorError as exc:
            raise self._classify(f"ExtractorError: {exc}") from exc
        except DownloadError as exc:
            raise self._classify(str(exc)) from exc
        except OSError as exc:
            raise ExtractError(
                "The platform did not answer. Try again in a few seconds.",
                code="network", status=502,
            ) from exc

    # ------------------------------------------------------- error mapping

    _CLASSIFY_TABLE: tuple[tuple[tuple[str, ...], str, int, str], ...] = (
        (("unsupported", "not supported", "no extractor", "not a valid url", "no suitable"),
         "unsupported", 422, "That site or link type is not supported yet."),
        # Before `private`: "Sign in to confirm you're not a bot" mentions signing in
        # but is a block on *us*, and telling a visitor to log in would be wrong.
        (("429", "too many requests", "rate limit", "captcha", "not a bot", "please retry later"),
         "upstream_rate_limit", 429,
         "The platform is rate-limiting this server right now. Try again in a minute."),
        (("login", "log in", "cookies", "account needed", "sign in", "age-restricted", "private",
          "members only", "not accessible", "authorisation", "authentication", "unauthorized"),
         "private", 403,
         "This video is private, age-restricted or needs a login, so it cannot be read here."),
        (("404", "not found", "does not exist", "removed", "deleted", "unavailable"),
         "notfound", 404,
         "That video was not found — it may have been deleted or the link truncated."),
        (("403", "forbidden", "geo", "not available in your country", "blocked in your region"),
         "geo_or_forbidden", 451, "The media is blocked from this region or returned Forbidden."),
        (("timed out", "timeout"), "timeout", 504,
         "The platform timed out while answering. Try again."),
        (("network", "connection", "temporary error", "resolve name", "reset by peer",
          "ssl", "tls", "certificate"),
         "network", 502, "The platform did not answer. Try again in a few seconds."),
    )

    @classmethod
    def _classify(cls, text: str) -> ExtractError:
        """Map free-form upstream chatter onto a small, translatable vocabulary."""
        lowered = text.lower()
        for needles, code, status, message in cls._CLASSIFY_TABLE:
            if any(needle in lowered for needle in needles):
                return ExtractError(message, code=code, status=status)
        return ExtractError(
            "Something went wrong while reading that link.", code="extract_failed", status=502
        )

    # ------------------------------------------------ payload construction

    def _build_result(self, info: dict, profile: dict, resolve_ms: int) -> ExtractResult:
        max_height = int(profile.get("max_height") or 2160)
        formats = self._select_formats(
            info.get("formats") or [],
            audio_only=bool(profile.get("audio_only")),
            require_progressive=bool(profile.get("require_progressive")),
            prefer_clean=bool(profile.get("prefer_clean_stream")),
            ext_priority=list(profile.get("video_ext_priority") or DEFAULT_VIDEO_EXTS),
            max_height=max_height,
            ladder=profile.get("quality_ladder") or [],
        )
        warnings: list[str] = []
        if not formats:
            warnings.append("no_formats")
        if any(not _codec_known(f) for f in (info.get("formats") or []) if isinstance(f, dict)):
            warnings.append("codecs_unverified")
        if info.get("is_live"):
            warnings.append("live_stream")
        availability = info.get("availability")
        if availability and availability != "public":
            warnings.append(f"availability:{availability}")

        raw_date = str(info.get("upload_date") or "")
        upload_date = None
        if len(raw_date) == 8 and raw_date.isdigit():
            upload_date = f"{raw_date[0:4]}-{raw_date[4:6]}-{raw_date[6:8]}"

        duration = info.get("duration")
        title = _TITLE_NOISE.sub(" ", str(info.get("title") or info.get("id") or "Video")).strip()
        return ExtractResult(
            ok=True,
            platform=str(info.get("extractor_key") or info.get("extractor") or "generic")[:40],
            id=str(info.get("id") or "")[:128],
            title=title[:240],
            thumbnail=self._safe_asset_url(info.get("thumbnail")),
            duration=int(duration) if isinstance(duration, (int, float)) else None,
            duration_text=human_duration(duration if isinstance(duration, (int, float)) else None),
            uploader=(str(info.get("uploader") or info.get("uploader_id") or "")[:120] or None),
            webpage_url=str(info.get("webpage_url") or info.get("original_url") or "")[:2000],
            upload_date=upload_date,
            is_live=bool(info.get("is_live")),
            availability=availability,
            formats=formats,
            resolve_ms=resolve_ms,
            warnings=warnings,
        )

    @staticmethod
    def _safe_asset_url(value: Optional[str]) -> Optional[str]:
        """Only ever hand the browser an http(s) asset URL of a sane length."""
        if not isinstance(value, str):
            return None
        value = value.strip()
        if value.startswith("//"):
            value = "https:" + value
        if not value.startswith(("https://", "http://")) or len(value) > 2048:
            return None
        return value

    def _select_formats(
        self,
        formats: Iterable[dict],
        *,
        audio_only: bool,
        require_progressive: bool,
        prefer_clean: bool,
        ext_priority: list[str],
        max_height: int,
        ladder: list[int],
    ) -> list[FormatOption]:
        """
        Reduce a raw yt-dlp format list to a short, honest button list.

        Rules, in order:
          1. skip DRM rows and anything without a direct http(s) URL;
          2. skip watermarked renditions when the platform profile asks for a
             clean stream (TikTok publishes both, and users pay for the badge);
          3. respect the page's media policy: audio-only pages drop video rows,
             `require_progressive` pages drop video-only rows, and rows above the
             platform's real `max_height` are ignored;
          4. collapse to one row per (kind, quality bucket, container priority)
             so a YouTube video yields ~6 useful buttons, not 34 twins;
          5. keep the higher-bitrate row per bucket, preferring the listed
             container when bitrates are within 10 % of each other;
          6. order merged video > audio > video-only, cap the count, and flag the
             safest default (highest merged row at or below 1080p) for the UI.
        """
        buckets: dict[tuple, tuple[dict, str]] = {}

        def ext_rank(fmt: dict) -> int:
            try:
                return ext_priority.index(str(fmt.get("ext") or ""))
            except ValueError:
                return len(ext_priority)

        def rank(fmt: dict) -> float:
            return float(fmt.get("tbr") or fmt.get("vbr") or fmt.get("abr") or 0)

        for fmt in formats:
            if not isinstance(fmt, dict) or fmt.get("drm"):
                continue
            url = fmt.get("url")
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                continue
            if prefer_clean:
                # TikTok & co. publish two renditions of the same upload: one with
                # the handle burned in, one clean. Offering the stamped copy would
                # make the page's headline claim a lie, so it never reaches the UI.
                identity = f"{fmt.get('format_id','')} {fmt.get('format_note','')} {url}".lower()
                if "watermark" in identity or "branding" in identity:
                    continue
            height = fmt.get("height") or 0
            kind, _merged, _known = classify(fmt)
            if audio_only and kind != "audio":
                continue
            if kind == "audio":
                key = ("audio", str(fmt.get("ext") or ""))
            elif kind == "video_only":
                if require_progressive:
                    continue  # this page promises a file that plays with sound
                key = ("vo", _nearest_bucket(int(height), ladder), ext_rank(fmt))
            else:
                if max_height and int(height) > max_height:
                    continue
                key = ("video", _nearest_bucket(int(height), ladder), ext_rank(fmt))
            current = buckets.get(key)
            if current is None:
                buckets[key] = (fmt, kind)
                continue
            current_fmt = current[0]
            if rank(fmt) > rank(current_fmt) or (
                ext_rank(fmt) < ext_rank(current_fmt) and rank(fmt) >= rank(current_fmt) * 0.9
            ):
                buckets[key] = (fmt, kind)

        options = [self._to_option(fmt, kind) for fmt, kind in buckets.values()]
        order = {"video": 0, "audio": 1, "video_only": 2}
        options.sort(key=lambda o: (order.get(o.kind, 3), -(o.height or 0), -(o.bitrate_kbps or 0)))
        options = options[: self.settings.max_formats_returned]

        if audio_only:
            default = options[0] if options else None
        else:
            default = next(
                (o for o in options if o.kind == "video" and (o.height or 0) <= 1080),
                next((o for o in options if o.kind == "video"),
                     options[0] if options else None),
            )
        for option in options:
            option.is_default = default is not None and option.id == default.id and option.ext == default.ext
        return options

    @staticmethod
    def _codec_state(fmt: dict) -> tuple[str, bool]:
        kind, merged, known = classify(fmt)
        return kind, known

    @staticmethod
    def _to_option(fmt: dict, kind: str) -> FormatOption:
        ext = str(fmt.get("ext") or "bin")
        height = fmt.get("height") or None
        width = fmt.get("width") or None
        fps = fmt.get("fps") or None
        bitrate = _best_bitrate(fmt)
        size = fmt.get("filesize") or fmt.get("filesize_approx")
        protocol = str(fmt.get("protocol") or "")

        if kind == "audio":
            label = f"{bitrate} kbps · {ext.upper()}" if bitrate else f"Audio · {ext.upper()}"
        else:
            parts: list[str] = []
            if height:
                tag = _HEIGHT_LABELS.get(int(height))
                parts.append(f"{int(height)}p {tag}".strip())
            elif width:
                parts.append(f"{int(width)} px")
            else:
                parts.append(str(fmt.get("format_note") or "Source"))
            parts.append(ext.upper())
            if fps and float(fps) >= 50:
                parts.append(f"{int(round(float(fps)))} fps")
            label = " · ".join(p for p in parts if p)

        # Fragmented streams are real files but a browser cannot save-and-play
        # them, so the UI shows "copy link" guidance instead of a download button.
        streamable = (
            ext in {"mp4", "webm", "m4a", "mp3", "mov", "opus"}
            and "m3u8" not in protocol
            and "frag" not in protocol
            and "dash" not in protocol
        )
        return FormatOption(
            id=str(fmt.get("format_id") or "")[:64],
            label=label[:48],
            kind=kind,
            ext=ext[:12],
            height=int(height) if height else None,
            width=int(width) if width else None,
            fps=round(float(fps), 2) if fps else None,
            vcodec=str(fmt.get("vcodec") or "")[:24] or None,
            acodec=str(fmt.get("acodec") or "")[:24] or None,
            bitrate_kbps=bitrate,
            filesize=int(size) if size else None,
            filesize_text=human_filesize(size),
            quality_note=(str(fmt.get("format_note"))[:48] if fmt.get("format_note") else None),
            url=str(fmt.get("url"))[:4096],
            protocol=protocol[:24],
            streamable_in_browser=bool(streamable),
            has_merged_audio=bool(classify(fmt)[1]),
            approx_note="estimated" if (size and not fmt.get("filesize")) else None,
        )
