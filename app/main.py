"""
ssavevideo.com — FastAPI application entrypoint.

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers

Responsibilities of this module, and nothing else:
  * build the app: middleware, static files, template env, dependency wiring;
  * the routing engine: `/` (home), `/{platform-slug}`, `/guides/{slug}`, the
    legal documents, and the locale-prefixed mirrors `/es/...`, `/fr/...`,
    `/ar/...` — all resolved from `data/seo_platforms.json` and rendered by one
    Jinja template per view type (100% SSR, so Googlebot sees finished HTML);
  * `POST /api/extract`: the only endpoint that talks to the outside world;
  * the SEO surface: `sitemap.xml`, `robots.txt`, hreflang alternates, JSON-LD;
  * the operational surface: `/healthz`, `/metrics`, security headers, limits.

It deliberately contains no HTML and no yt-dlp calls: markup lives in
`app/templates`, media resolution in `app/extraction.py`, copy rules in
`app/content.py`. That separation is what lets a copy or platform change ship
without touching code.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from starlette.middleware.gzip import GZipMiddleware

from .config import STATIC_DIR, TEMPLATE_DIR, Settings, get_settings
from .content import ContentStore, PageSpec
from .extraction import ExtractError, ExtractionService
from .schemas import (
    ErrorDetail,
    ErrorResponse,
    ExtractRequest,
    ExtractResponse,
    HealthResponse,
    PlatformSummary,
)
from .security import RateLimiter, UnsafeURL

# Strings the client needs. Everything else stays in the server-rendered HTML:
# shipping one dictionary per page would be waste, and the few labels below are
# only used when the DOM is built after the first paint.
JS_UI_KEYS = (
    "hero.analyzing",
    "results.download",
    "results.copy",
    "results.copied",
    "results.merged",
    "results.video_only",
    "results.audio",
    "results.quality",
    "results.elapsed",
    "results.empty",
    "results.not_streamable",
    "error.empty",
    "error.rate",
    "error.network",
    "error.generic",
    "error.private",
    "error.notfound",
    "error.unsupported",
    "error.invalid_url",
)

log = logging.getLogger("ssavevideo.app")
BOOT_TIME = time.time()

# Tool slugs are lowercase, hyphenated and bounded — anything else is a probe.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,80}$")

# Stable extractor error code -> UI translation key (see data/locales.json).
ERROR_UI_KEYS = {
    "rate_limited": "error.rate",
    "upstream_rate_limit": "error.network",
    "geo_or_forbidden": "error.private",
    "extract_failed": "error.generic",
    "engine_missing": "error.generic",
    "no_formats": "results.empty",
    "empty": "error.empty",
    "bad_request": "error.invalid_url",
}


# Last-resort body for a page request when even the error template failed (i.e. the
# content store itself is broken). Inline on purpose: it must render with zero
# dependencies, still be HTML, and never leak a traceback.
EMERGENCY_HTML = (
    "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
    "<meta name=\"robots\" content=\"noindex, nofollow\">"
    "<title>Temporarily unavailable</title>"
    "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"></head>"
    "<body style=\"font:16px/1.6 system-ui,sans-serif;margin:0;padding:3rem 1.5rem;"
    "text-align:center\"><h1>Temporarily unavailable</h1>"
    "<p>Our resolver hit an internal error. Please try again in a few seconds.</p>"
    "</body></html>"
)


def _xml(value: str) -> str:
    """Escape for XML element text (sitemaps are XML documents, not HTML)."""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    """Application factory: takes config, returns a wired, production-shaped app."""
    settings = settings or get_settings()
    store = ContentStore(settings=settings)
    extractor = ExtractionService(settings)
    limiter = RateLimiter(settings.rate_limit, settings.rate_window)

    app = FastAPI(
        title="SsaveVideo — Video URL Resolver API",
        version="1.0.0",
        description=(
            "Programmatic interface behind ssavevideo.com. The API resolves a public video "
            "link into the platform's own direct CDN URLs. No media is ever downloaded, "
            "stored, converted or served by this server."
        ),
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings
    app.state.store = store
    app.state.extractor = extractor
    app.state.limiter = limiter

    # ------------------------------------------------------------------
    # Middleware
    # ------------------------------------------------------------------
    # GZip for text (the Tailwind sheet and JSON-LD shrink ~70 %). Media is never
    # served from here, so nothing is usefully double-compressed.
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    # The site itself is same-origin; this allowlist exists so a browser
    # extension or PWA on another origin can reuse the public resolver.
    # `allow_credentials=False` is the load-bearing part: a random page can
    # never ride a visitor's cookies against this host.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://ssavevideo.com", "https://www.ssavevideo.com"],
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-Requested-With"],
        max_age=600,
    )

    # A hand-built Environment (instead of Jinja2Templates' default) so we own
    # three things that matter for this app: autoescape on by default *and* for
    # strings (pasted video titles are attacker-controlled text), whitespace
    # control that keeps the rendered HTML readable without changing layout, and
    # an undefined-name policy that raises instead of silently rendering "".
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(("html", "xml"), default_for_string=True),
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    templates = Jinja2Templates(env=env)
    templates.env.globals["settings"] = settings
    templates.env.globals["store"] = store
    templates.env.globals["site"] = store.site
    templates.env.globals["year"] = datetime.now(timezone.utc).year
    app.state.templates = templates

    # A strict CSP is affordable precisely because the app is SSR + one external
    # script and zero inline JS. JSON-LD blocks are *data blocks*
    # (`application/ld+json`), which a conforming UA never treats as executable
    # script, so `script-src 'self'` does not suppress structured data.
    CSP = (
        "default-src 'self'; "
        "img-src 'self' https: data:; "
        "style-src 'self'; "
        "script-src 'self'; "
        "connect-src 'self'; "
        "font-src 'self' data:; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "manifest-src 'self'"
    )

    @app.middleware("http")
    async def security_and_cache_headers(request: Request, call_next) -> Response:
        """One place that decides headers, so no route can forget a security one."""
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "geolocation=(), camera=(), microphone=(), interest-cohort=(), "
            "attribution-reporting=(), browsing-topics=()",
        )
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if not request.url.path.startswith(("/static/", "/api/docs", "/api/openapi.json")):
            response.headers.setdefault("Content-Security-Policy", CSP)
        if settings.is_production:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=63072000; includeSubDomains; preload"
            )

        path = request.url.path
        if path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", settings.static_cache_control)
        elif path.startswith("/api/"):
            # Private per-visitor answer: a signed CDN link must never sit in a
            # shared cache where the next visitor could read it.
            response.headers.setdefault("Cache-Control", "no-store")
        elif request.method == "GET" and response.status_code == 200:
            response.headers.setdefault("Cache-Control", settings.html_cache_control)
        response.headers.setdefault("Vary", "Accept-Encoding")
        return response

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def client_key(request: Request) -> str:
        """Visitor identity for rate limiting (X-Forwarded-For aware)."""
        if settings.trust_proxy_xff:
            forwarded = request.headers.get("x-forwarded-for", "")
            if forwarded:
                return forwarded.split(",")[0].strip()[:64] or "unknown"
        return ((request.client.host if request.client else "unknown") or "unknown")[:64]

    def locale_of(request: Request) -> str:
        return getattr(request.state, "locale", store.default_locale)

    def page_context(request: Request, page: Optional[PageSpec], locale_code: str) -> dict[str, Any]:
        """The shared scaffold every view receives (chrome, meta, schema, links)."""
        base_path = (page.slug if page else "") or ""
        is_localized_page = bool(page) and page.kind not in {"legal", "guide"}
        alternates = store.alternates_for(base_path) if is_localized_page else []
        context: dict[str, Any] = {
            "request": request,
            "now": datetime.now(timezone.utc),
            "page": page,
            "locale": store.locales[locale_code],
            "locales": list(store.locales.values()),
            "is_home": bool(page and page.kind == "home"),
            "t": lambda key, **kw: store.t(locale_code, key, **kw),
            "nav_platforms": store.nav_platforms(locale_code),
            "all_platforms": store.all_platforms(locale_code),
            "all_guides": store.all_guides(locale_code),
            "footer_columns": store.footer(locale_code),
            "legal_docs": store.legal.get("documents", {}),
            # Optional overrides. Defined here rather than with `|default(...)`
            # in eleven templates, because the environment uses StrictUndefined:
            # a typo in a context key must be a loud error, not an empty string.
            "title_override": "",
            "description_override": "",
            "robots_override": "",
            "not_found": False,
            "doc": {},
            "alt_by_lang": {},
            "js_strings": store.js_strings(locale_code, JS_UI_KEYS),
            "og_default_image": store.absolute(store.site.get("og_image", "/static/img/og-default.png")),
            "favicon": store.absolute(store.site.get("favicon", "/static/img/favicon.svg")),
        }
        if page is not None:
            context.update(
                {
                    "current_path": page.path,
                    "home_path": store.locale_path("", page.locale.code),
                    "canonical": store.absolute(page.path),
                    # Legal documents exist in English only, so they get a
                    # self-referential (empty) hreflang set rather than pointing at
                    # translations that do not exist.
                    "alternates": alternates,
                    # hreflang -> absolute URL, so the language menu in the header
                    # and the footer can both send a visitor to the same page.
                    "alt_by_lang": {alt["hreflang"]: alt["href"] for alt in alternates},
                    "json_ld": store.json_ld(page),
                    "og_image": page.og_image or context["og_default_image"],
                }
            )
        else:  # legal / error views still need a canonical + schema
            path = request.url.path
            context.update(
                {
                    "current_path": path,
                    "canonical": store.absolute(path),
                    "alternates": [],
                    "alt_by_lang": {},
                    "home_path": store.locale_path("", locale_code),
                    "json_ld": [],
                    "og_image": context["og_default_image"],
                }
            )
        return context

    def render(
        request: Request,
        template: str,
        context: dict[str, Any],
        *,
        status: int = 200,
        headers: Optional[dict[str, str]] = None,
    ) -> HTMLResponse:
        """Single funnel to a template, so no page can skip the SEO scaffolding."""
        page: Optional[PageSpec] = context.get("page")
        locale_code = page.locale.code if page else locale_of(request)
        payload = page_context(request, page, locale_code)
        payload.update(context)
        # hreflang as HTTP headers as well as <link> tags. The header form is what a
        # crawler can honour on a non-HTML resource, it survives a CDN that rewrites
        # <head>, and Google accepts either — sending both is cheap and consistent.
        link_headers = [
            f'<{alt["href"]}>; rel="alternate"; hreflang="{alt["hreflang"]}"' for alt in (payload.get("alternates") or [])
        ]
        if payload.get("canonical"):
            link_headers.append(f'<{payload["canonical"]}>; rel="canonical"')
        merged = dict(headers or {})
        if link_headers:
            merged["Link"] = ", ".join(link_headers)
        return templates.TemplateResponse(request, template, payload, status_code=status, headers=merged or None)

    # ------------------------------------------------------------------
    # Static assets (mounted BEFORE the catch-all so files always win)
    # ------------------------------------------------------------------
    # No cache-busting query strings are needed: the stylesheet is a build
    # artifact whose content changes only when a class is added, and
    # `static_cache_control` keeps it a day (immutable) in production.
    app.mount(
        "/static",
        StaticFiles(directory=str(STATIC_DIR), html=False, check_dir=True),
        name="static",
    )

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse, include_in_schema=False, name="home")
    async def home(request: Request) -> HTMLResponse:
        """Home is the generic hub: same template, `site` block as its content."""
        page = store.page("home", None, store.default_locale)
        return render(request, "views/tool.html", {"page": page, "is_home": True})

    # ------------------------------------------------------------------
    # Extraction API
    # ------------------------------------------------------------------

    def profile_for(selector: Optional[str]) -> dict[str, Any]:
        """Merge a page's extraction profile over the global defaults (data-driven)."""
        profile = dict(store.extraction_defaults)
        if selector:
            platform = store.platforms.get(selector) or store.platforms_by_key.get(selector)
            if platform:
                profile.update(platform.get("extraction") or {})
        return profile

    @app.post(
        "/api/extract",
        response_model=ExtractResponse,
        response_model_exclude_none=False,
        responses={
            403: {"model": ErrorResponse, "description": "Private / login-required video"},
            404: {"model": ErrorResponse, "description": "Video not found"},
            422: {"model": ErrorResponse, "description": "Unsupported, unsafe or expired link"},
            429: {"model": ErrorResponse, "description": "Rate limited"},
            502: {"model": ErrorResponse, "description": "Upstream failure"},
            504: {"model": ErrorResponse, "description": "Upstream timeout"},
        },
        summary="Resolve a public video link into direct download URLs",
        tags=["extract"],
        description=(
            "Returns the platform's own CDN URLs for the requested media. This endpoint never "
            "downloads, converts or stores the video — it reads the manifest and answers, so the "
            "cost of one call is a few small HTTPS requests."
        ),
    )
    async def api_extract(payload: ExtractRequest, request: Request) -> Response:
        locale_code = payload.locale if payload.locale in store.locales else locale_of(request)

        # 1. Abuse control on the only expensive thing we expose. Per-IP, and it
        #    protects the *upstream* relationship as much as our own CPU.
        allowed, retry_after = await limiter.acquire(client_key(request))
        if not allowed:
            body = ErrorResponse(
                error=ErrorDetail(
                    code="rate_limited",
                    message=store.t(locale_code, "error.rate"),
                    hint=f"Retry in {retry_after}s",
                    retry_after=retry_after,
                )
            )
            return JSONResponse(body.model_dump(), status_code=429,
                                headers={"Retry-After": str(retry_after)})

        # 2. Resolve. Every failure path is mapped to a code the UI can translate.
        try:
            data = await extractor.extract(payload.url, profile_for(payload.platform))
        except UnsafeURL as exc:
            return json_error(exc.reason or "invalid_url", str(exc), 422, locale_code)
        except ExtractError as exc:
            return json_error(exc.code, exc.message, exc.status, locale_code)

        # 3. A video with no downloadable rendition for a logged-out visitor is a
        #    422 with the same copy the front-end shows inline, not an empty 200.
        if not data.get("formats"):
            return json_error("no_formats", store.t(locale_code, "results.empty"), 422, locale_code,
                              hint="This page exists but exposes no media file to anonymous visitors.")
        return JSONResponse(data, headers={"X-Extract-Cache": "HIT" if data.get("cached") else "MISS"})

    def json_error(code: str, message: str, status: int, locale_code: str,
                   hint: Optional[str] = None) -> JSONResponse:
        """Localised, schema-shaped error body. `message` is the fallback text."""
        ui_key = ERROR_UI_KEYS.get(code, f"error.{code}")
        translated = store.t(locale_code, ui_key)
        if translated == ui_key:  # no string for this code -> keep the engineer's text
            translated = message
        body = ErrorResponse(error=ErrorDetail(code=code, message=translated[:500], hint=hint))
        return JSONResponse(body.model_dump(), status_code=status)

    @app.get("/api/platforms", response_model=list[PlatformSummary], tags=["meta"],
             summary="The programmatic-SEO schema, machine readable")
    async def api_platforms() -> list[PlatformSummary]:
        """Lets a partner (or a future mobile app) enumerate tools + media policies."""
        return [
            PlatformSummary(
                slug=p["slug"],
                path=p["path"],
                name=p["display_name"],
                icon=p["icon"],
                domains=list(p["domains"]),
                extraction={**store.extraction_defaults, **(p.get("extraction") or {})},
            )
            for p in store.schema["platforms"]
        ]

    @app.get("/healthz", response_model=HealthResponse, tags=["meta"],
             summary="Liveness and readiness")
    async def healthz() -> JSONResponse:
        """Never touches the internet: safe as a load-balancer probe every 5 s."""
        engine = "unavailable"
        try:
            from yt_dlp import version  # lazy: health must not depend on the network

            engine = version.__version__
        except Exception:  # noqa: BLE001 - a broken venv must still answer 200-ish
            log.warning("yt-dlp import failed during health check", exc_info=True)
        payload = HealthResponse(
            status="ok" if engine != "unavailable" else "degraded",
            engine={
                "yt_dlp": engine,
                "max_concurrency": settings.max_concurrency,
                "in_flight_peak": extractor.stats.get("in_flight_peak", 0),
                "media_storage": "disabled by design",
            },
            cache=extractor.cache.stats(),
            limiter=limiter.snapshot(),
            uptime_seconds=round(time.time() - BOOT_TIME, 1),
        )
        return JSONResponse(payload.model_dump())

    @app.get("/metrics", include_in_schema=False, response_class=PlainTextResponse)
    async def metrics() -> PlainTextResponse:
        """
        Counters we already maintain, in Prometheus exposition format. No client
        dependency, and enough signal to answer "do I need Redis yet?" from a graph.
        """
        stats, cache = extractor.stats, extractor.cache.stats()
        lines = [
            "# TYPE ssave_extract_requests_total counter",
            f"ssave_extract_requests_total {stats['requests']}",
            "# TYPE ssave_extract_success_total counter",
            f"ssave_extract_success_total {stats['success']}",
            "# TYPE ssave_extract_failed_total counter",
            f"ssave_extract_failed_total {stats['failed']}",
            "# TYPE ssave_extract_cache_hits_total counter",
            f"ssave_extract_cache_hits_total {cache['hits']}",
            "# TYPE ssave_extract_cache_misses_total counter",
            f"ssave_extract_cache_misses_total {cache['misses']}",
            "# TYPE ssave_extract_upstream_seconds_total counter",
            f"ssave_extract_upstream_seconds_total {stats.get('upstream_seconds', 0.0):.2f}",
            "# TYPE ssave_rate_limited_total counter",
            f"ssave_rate_limited_total {limiter.rejections}",
            "# TYPE ssave_uptime_seconds gauge",
            f"ssave_uptime_seconds {time.time() - BOOT_TIME:.0f}",
        ]
        return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    # ------------------------------------------------------------------
    # SEO surface
    # ------------------------------------------------------------------

    @app.get("/robots.txt", include_in_schema=False, response_class=PlainTextResponse)
    async def robots_txt(request: Request) -> PlainTextResponse:
        """
        Allow every rendered tool page, deny the resolver.

        `/api/` is not a page: it is an unauthenticated link resolver with no
        content to index, and letting a crawler fetch it would burn budget on
        JSON and mint signed CDN links for nothing.
        """
        origin = settings.origin
        body = "\n".join(
            [
                "Sitemap: " + origin + "/sitemap.xml",
                "Sitemap: " + origin + "/sitemap-news.xml",
                "",
                "User-agent: *",
                "Allow: /",
                "Disallow: /api/",
                "Disallow: /*?utm_",
                "Disallow: /*?ref=",
                "Crawl-delay: 1",
                "",
                "# Assistant/LLM crawlers: content pages yes, resolver endpoint no.",
                "User-agent: GPTBot",
                "Allow: /",
                "Disallow: /api/",
                "",
                "User-agent: OAI-SearchBot",
                "Allow: /",
                "Disallow: /api/",
                "",
                "User-agent: Google-Extended",
                "Allow: /",
                "Disallow: /api/",
                "",
                "User-agent: bingbot",
                "Allow: /",
                "Disallow: /api/",
                "",
            ]
        )
        return PlainTextResponse(body, media_type="text/plain; charset=utf-8",
                                 headers={"Cache-Control": "public, max-age=3600"})

    @app.get("/sitemap.xml", include_in_schema=False, response_class=HTMLResponse)
    async def sitemap_xml(request: Request) -> HTMLResponse:
        """
        Generated per request from `seo_platforms.json`.

        With ~14 pages × 4 locales the cost is microseconds, and generating beats
        caching on correctness: a new platform in the JSON is in the sitemap the
        second it ships, with no rebuild step to forget. `lastmod` comes from
        `meta.updated`, so it only moves when content genuinely changed — the one
        honest signal a crawler can act on.
        """
        items: list[str] = []
        for entry in store.sitemap_entries():
            alternates = "\n".join(
                '    <xhtml:link rel="alternate" hreflang="{lang}" href="{href}"/>'.format(
                    lang=_xml(alt["hreflang"]), href=_xml(alt["href"])
                )
                for alt in entry["alternates"]
            )
            items.append(
                "  <url>\n"
                f"    <loc>{_xml(entry['loc'])}</loc>\n"
                f"    <lastmod>{entry['lastmod']}</lastmod>\n"
                f"    <changefreq>{entry['changefreq']}</changefreq>\n"
                f"    <priority>{entry['priority']}</priority>\n"
                f"{alternates}\n"
                "  </url>"
            )
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
            'xmlns:xhtml="http://www.w3.org/1999/xhtml">\n'
            + "\n".join(items)
            + "\n</urlset>\n"
        )
        return HTMLResponse(xml, media_type="application/xml; charset=utf-8",
                            headers={"Cache-Control": "public, max-age=1800, s-maxage=86400"})

    @app.get("/sitemap-news.xml", include_in_schema=False, response_class=HTMLResponse)
    async def sitemap_news(request: Request) -> HTMLResponse:
        """
        Only guides belong in a news sitemap. Listing tool pages as news is the
        classic pSEO mistake that gets a sitemap ignored; if the list is empty the
        file still validates, which is the safe outcome.
        """
        origin = settings.origin
        items = [
            "  <url>\n"
            f"    <loc>{_xml(origin + guide['path'])}</loc>\n"
            "  <news:news>\n"
            "    <news:publication>\n"
            f"      <news:name>{_xml(store.site['brand'])}</news:name>\n"
            "      <news:language>en</news:language>\n"
            "    </news:publication>\n"
            f"    <news:publication_date>{store.updated.strftime('%Y-%m-%d')}</news:publication_date>\n"
            f"    <news:title>{_xml(guide['meta_title'])}</news:title>\n"
            f"    <news:keywords>{_xml(guide['name'])}</news:keywords>\n"
            f"    <news:url>{_xml(origin + guide['path'])}</news:url>\n"
            "    </news:news>"
            "  </url>"
            for guide in store.schema.get("guides", [])
        ]
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
            'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">\n'
            + "\n".join(items)
            + "\n</urlset>\n"
        )
        return HTMLResponse(xml, media_type="application/xml; charset=utf-8",
                            headers={"Cache-Control": "public, max-age=1800"})

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def webmanifest() -> JSONResponse:
        origin = settings.origin
        return JSONResponse(
            {
                "name": f"{store.site['brand']} — Video Downloader",
                "short_name": store.site["brand"],
                "description": store.site["short_description"],
                "start_url": origin + "/",
                "scope": origin + "/",
                "display": "standalone",
                "background_color": "#020617",
                "theme_color": "#4f46e5",
                "categories": ["utilities", "productivity"],
                "icons": [
                    {
                        "src": origin + "/static/img/favicon.svg",
                        "sizes": "any",
                        "type": "image/svg+xml",
                        "purpose": "any",
                    }
                ],
            },
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        """SVG icon is the only one we ship; answer the legacy request politely."""
        return RedirectResponse("/static/img/favicon.svg", status_code=302)

    @app.get("/llms.txt", include_in_schema=False, response_class=PlainTextResponse)
    async def llms_txt() -> PlainTextResponse:
        """A machine-readable index for assistants — cheap, harmless, increasingly expected."""
        origin = settings.origin
        lines = [f"# {store.site['brand']}", "", store.site["short_description"], "", "## Tools"]
        lines += [
            f"- [{p['display_name']}]({origin}{p['path']}): {p['meta_description']}"
            for p in store.schema["platforms"]
        ]
        lines += ["", "## Guides"]
        lines += [f"- [{g['name']}]({origin}{g['path']})" for g in store.schema.get("guides", [])]
        lines += [
            "",
            "## API",
            "- POST /api/extract — resolves a public video URL into direct CDN links. "
            "No media is stored on the server.",
        ]
        return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; charset=utf-8")

    # ------------------------------------------------------------------
    # Error handling
    # ------------------------------------------------------------------

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Shape FastAPI's default 422 into the same envelope the UI understands."""
        first = (exc.errors() or [{}])[0]
        location = ".".join(str(part) for part in (first.get("loc") or [])[1:]) or "body"
        body = ErrorResponse(
            error=ErrorDetail(
                code="bad_request",
                message=f"Invalid '{location}': {first.get('msg', 'failed validation')}"[:400],
            )
        )
        return JSONResponse(body.model_dump(), status_code=422)

    @app.exception_handler(ExtractError)
    async def on_extract_error(request: Request, exc: ExtractError) -> JSONResponse:
        body = ErrorResponse(error=ErrorDetail(code=exc.code, message=exc.message[:500]))
        return JSONResponse(body.model_dump(), status_code=exc.status)

    @app.exception_handler(Exception)
    async def on_unhandled(request: Request, exc: Exception) -> Response:
        """
        Never leak a traceback or an internal path to a client. Log the real cause;
        show the visitor the localised generic line. In non-production the cause is
        appended so a developer sees it immediately.

        Content-negotiated on purpose: an `/api/*` caller gets the JSON envelope, but
        a *page* URL gets the HTML failure page. Handing Googlebot a JSON body for
        `/tiktok-video-downloader` during a wobble is both unreadable for humans and a
        wasted URL, so the two surfaces are answered in their own shape.
        """
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        locale_code = locale_of(request)
        if not request.url.path.startswith("/api"):
            try:
                return server_error(request, locale_code)
            except Exception:  # the error page itself failed -> last-resort body
                log.exception("error page rendering failed too")
                return HTMLResponse(EMERGENCY_HTML, status_code=500,
                                   headers={"Cache-Control": "no-store",
                                            "X-Robots-Tag": "noindex, nofollow"})
        body = ErrorResponse(
            error=ErrorDetail(code="internal", message=store.t(locale_code, "error.generic"))
        )
        payload = body.model_dump()
        if not settings.is_production:
            payload["error"]["hint"] = f"{type(exc).__name__}: {exc}"[:300]
        return JSONResponse(payload, status_code=500, headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------------
    # Generic page router — REGISTERED LAST on purpose.
    #
    # Starlette matches routes in registration order, and `/{full_path:path}`
    # matches *anything*, including /robots.txt and /healthz. Declaring it first
    # would silently shadow every later endpoint, so every specific path above
    # is registered before this catch-all.
    # ------------------------------------------------------------------
    @app.get("/{full_path:path}", response_class=HTMLResponse, include_in_schema=False, name="router")
    async def router(request: Request, full_path: str) -> Response:
        """
        The routing engine for every human-facing URL.

        Resolution order:
          1. optional locale prefix (`/es/tiktok-video-downloader` -> locale `es`);
          2. `/guides/{slug}`      -> guide view (long-form pSEO article);
          3. `/{legal-key}`       -> legal document (English-only, canonical);
          4. `/{platform-slug}`   -> tool view (the pSEO template);
          5. `/{locale}/`         -> that locale's home;
          6. anything else        -> 404 page carrying `X-Robots-Tag: noindex`.
        """
        # A request that reached the catch-all for these prefixes is a miss:
        # existing files and declared API routes are matched earlier.
        if full_path.startswith(("static/", "api/")):
            return api_404(full_path)

        remainder = full_path.strip("/")
        locale_code = store.default_locale
        parts = remainder.split("/", 1)
        if parts and parts[0] in store.locales:
            locale_code = parts[0]
            remainder = parts[1] if len(parts) > 1 else ""

        # One canonical spelling per page: never a trailing slash, except on a
        # locale root. 308 (not 301) so a POST/PUT body would be preserved if a
        # crawler ever replays one.
        if full_path.endswith("/") and remainder:
            target = store.locale_path("/" + remainder.strip("/"), locale_code)
            return RedirectResponse(target, status_code=308)

        request.state.locale = locale_code

        # locale root -> localized home (English root is handled by `home` above)
        if remainder == "":
            if locale_code == store.default_locale:
                return api_404(full_path)
            page = store.page("home", None, locale_code)
            return render(request, "views/tool.html", {"page": page, "is_home": True})

        if remainder.startswith("guides/"):
            if locale_code != store.default_locale:
                return RedirectResponse("/" + remainder, status_code=301)
            page = store.page("guide", f"guides/{remainder[len('guides/'):]}", locale_code)
            if page is not None:
                return render(request, "views/guide.html", {"page": page})
            return not_found(request, locale_code)

        legal_doc = store.legal.get("documents", {}).get(remainder)
        if legal_doc is not None:
            if locale_code != store.default_locale:
                # Legal text is English-only: send the crawler to the one real URL
                # rather than minting a duplicate document per locale.
                return RedirectResponse("/" + remainder, status_code=301)
            page = store.legal_page(legal_doc, locale_code)
            return render(request, "views/legal.html", {"page": page, "doc": legal_doc})

        if SLUG_RE.match(remainder):
            page = store.page("tool", remainder, locale_code)
            if page is not None:
                return render(request, "views/tool.html", {"page": page})

        return not_found(request, locale_code)

    def api_404(path: str) -> JSONResponse:
        return JSONResponse(
            {"ok": False, "error": {"code": "not_found", "message": f"No route for /{path}"}},
            status_code=404,
        )

    def server_error(request: Request, locale_code: str) -> HTMLResponse:
        """Same chrome as the 404: localised copy, `noindex`, nothing cached."""
        page = store.page("home", None, locale_code)
        return render(
            request,
            "views/500.html",
            {
                "page": page,
                "title_override": f"{store.t(locale_code, 'error500.title')} — {store.site['brand']}",
                "description_override": store.t(locale_code, "error500.subtitle"),
                "robots_override": "noindex, follow",
            },
            status=500,
            headers={"X-Robots-Tag": "noindex, follow", "Cache-Control": "no-store"},
        )

    def not_found(request: Request, locale_code: str) -> HTMLResponse:
        """
        A real 404 *page* (not a bare status): it keeps visitors on the site via
        the platform grid, and `noindex` stops a bot sweep from creating soft-404
        inventory in Search Console.
        """
        page = store.page("home", None, locale_code)
        return render(
            request,
            "views/404.html",
            {
                "page": page,
                "not_found": True,
                # Override the (home) metadata the chrome was built from: a 404 must
                # never inherit the homepage title or its indexable robots line.
                "title_override": f"{store.t(locale_code, 'notfound.title')} — {store.site['brand']}",
                "description_override": store.t(locale_code, "notfound.subtitle"),
                "robots_override": "noindex, follow",
            },
            status=404,
            headers={"X-Robots-Tag": "noindex, follow", "Cache-Control": "no-store"},
        )

    return app


def _configure_logging() -> None:
    """Sane default logging; uvicorn's own formatter is left alone if configured."""
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        )


_configure_logging()
app = create_app()
