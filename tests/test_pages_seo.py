"""
Rendered-page tests: the technical-SEO contract, routing behaviour and the
accessibility/marketing structure the templates promise.

Assertions look at the *rendered HTML*, because that is what Googlebot and a
screen reader receive — not at the Python objects behind them.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from xml.etree import ElementTree as ET

import pytest

PLATFORM_SLUGS = [
    "youtube-video-downloader",
    "youtube-shorts-downloader",
    "youtube-mp3-downloader",
    "tiktok-video-downloader",
    "instagram-video-downloader",
    "facebook-video-downloader",
    "reddit-video-downloader",
    "pinterest-video-downloader",
    "threads-video-downloader",
    "x-video-downloader",
]


def ld_blocks(html: str) -> list[dict]:
    """Parse every JSON-LD block exactly as a crawler would."""
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    assert blocks, "page has no structured data"
    return [json.loads(block) for block in blocks]


def ld_types(html: str) -> set[str]:
    return {block["@type"] for block in ld_blocks(html)}


@pytest.mark.parametrize("slug", PLATFORM_SLUGS)
def test_tool_pages_render_with_full_seo_stack(client, slug):
    response = client.get(f"/{slug}")
    assert response.status_code == 200
    html = response.text

    assert f'<link rel="canonical" href="https://ssavevideo.com/{slug}">' in html
    for lang in ("en", "es", "fr", "ar", "x-default"):
        assert f'hreflang="{lang}"' in html, f"missing hreflang {lang}"
    for tag in ('property="og:title"', 'property="og:description"', 'property="og:image"',
                'property="og:url"', 'name="twitter:card"', 'name="twitter:image"'):
        assert tag in html, tag
    assert 'name="robots" content="index, follow' in html
    assert "<title>" in html and "</title>" in html

    types = ld_types(html)
    assert {"WebApplication", "FAQPage", "BreadcrumbList"} <= types, types


@pytest.mark.parametrize("slug", PLATFORM_SLUGS)
def test_web_application_schema_declares_it_free(client, slug):
    app_block = next(b for b in ld_blocks(client.get(f"/{slug}").text) if b["@type"] == "WebApplication")
    assert app_block["offers"]["price"] == "0"
    assert app_block["offers"]["priceCurrency"] == "USD"
    assert app_block["isAccessibleForFree"] is True
    assert app_block["url"].endswith(f"/{slug}")
    assert len(app_block["featureList"]) >= 4


@pytest.mark.parametrize("slug", PLATFORM_SLUGS)
def test_faq_schema_mirrors_the_visible_accordion(client, slug):
    html = client.get(f"/{slug}").text
    faq = next(b for b in ld_blocks(html) if b["@type"] == "FAQPage")
    questions = [q["name"] for q in faq["mainEntity"]]
    assert len(questions) >= 5
    for question in questions:
        # Every question advertised in JSON-LD must be visible on the page: a
        # mismatch between markup and content is a manual-action risk.
        flat = re.sub(r"\s+", " ", html_lib.unescape(html))
        assert re.sub(r"\s+", " ", question) in flat, question
    for qa in faq["mainEntity"]:
        assert qa["acceptedAnswer"]["@type"] == "Answer"
        assert len(qa["acceptedAnswer"]["text"]) > 80


def test_breadcrumb_schema_is_absolute_and_sequential(client):
    html = client.get("/tiktok-video-downloader").text
    crumbs = next(b for b in ld_blocks(html) if b["@type"] == "BreadcrumbList")["itemListElement"]
    assert [c["position"] for c in crumbs] == list(range(1, len(crumbs) + 1))
    assert all(c["item"].startswith("https://ssavevideo.com/") for c in crumbs)
    assert crumbs[0]["name"] == "Home"
    assert crumbs[-1]["name"] == "TikTok"


@pytest.fixture()
def fault_client(app):
    """
    Same app, but the client must not re-raise server exceptions — these two tests
    are *about* the 500 response, and TestClient's default would propagate it.
    """
    from fastapi.testclient import TestClient

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_500_on_a_page_url_is_html_and_noindex(fault_client, monkeypatch):
    """
    A page failure answers as a page, not as an API envelope: styled, localised,
    `noindex` (so an outage cannot leave thin URLs in the index) and uncacheable.
    """
    from app.content import ContentStore

    def explode(self, key, *args, **kwargs):
        # One broken catalog row, not a dead store: the error page builds its own
        # (home) page spec, so the primary path is what gets exercised.
        if key != "home":
            raise RuntimeError("simulated resolver bug")
        return original(self, key, *args, **kwargs)

    original = ContentStore.page
    monkeypatch.setattr(ContentStore, "page", explode)
    response = fault_client.get("/tiktok-video-downloader")
    assert response.status_code == 500
    assert "text/html" in response.headers["content-type"]
    assert response.headers["x-robots-tag"] == "noindex, follow"
    assert response.headers["cache-control"] == "no-store"
    assert "HTTP 500" in response.text and "We hit a snag" in response.text
    assert 'dir="ltr"' in response.text
    assert "Traceback" not in response.text and "simulated resolver bug" not in response.text


def test_error_page_falls_back_when_the_store_is_dead(fault_client, monkeypatch):
    """The failure page must not depend on the thing that failed."""
    from app.content import ContentStore

    def explode(self, *args, **kwargs):
        raise RuntimeError("catalog is gone")

    monkeypatch.setattr(ContentStore, "page", explode)
    response = fault_client.get("/")
    assert response.status_code == 500
    assert "text/html" in response.headers["content-type"]
    assert "noindex, nofollow" in response.headers["x-robots-tag"]
    assert "Temporarily unavailable" in response.text


def test_500_on_an_api_url_stays_json(fault_client, monkeypatch):
    """
    `/api/*` is the mirror case: a machine caller must get the JSON envelope, not the
    HTML failure page. Simulated at the first thing the handler awaits, so nothing in
    the extraction error vocabulary can absorb it.
    """
    from app.security import RateLimiter

    def explode(self, key):
        raise RuntimeError("limiter exploded")

    monkeypatch.setattr(RateLimiter, "acquire", explode)
    response = fault_client.post("/api/extract", json={"url": "https://www.youtube.com/watch?v=abc"})
    assert response.status_code == 500
    assert "application/json" in response.headers["content-type"]
    assert response.json()["error"]["code"] == "internal"
    # Production build: the generic localised line, never the exception text.
    assert "limiter exploded" not in response.text and "Traceback" not in response.text


def test_page_structure_follows_the_required_flow(client):
    html = client.get("/instagram-video-downloader").text
    order = ["ss-main", 'id="related"', 'id="supported"', 'id="how-to"', 'id="features"', 'id="faq"', "<footer"]
    positions = [html.find(marker) for marker in order]
    assert -1 not in positions, [m for m in positions if m == -1]
    assert positions == sorted(positions), f"sections out of order: {order}"


def test_hero_contains_form_states_and_result_card(client):
    html = client.get("/tiktok-video-downloader").text
    for hook in ('data-extract-form', 'id="ss-url"', "data-paste", "data-submit",
                 'data-state="loading"', 'data-state="error"', 'data-state="result"',
                 "data-result-formats"):
        assert hook in html, hook
    assert "data-strings=" in html, "localised client strings are not wired"


def test_no_inline_script_or_style_attributes(client):
    """
    The CSP has no 'unsafe-inline', so an inline handler or style attribute would be
    silently dead. This test keeps the templates honest about that constraint.
    """
    html = client.get("/reddit-video-downloader").text
    assert not re.search(r"\son[a-z]+\s*=\s*\"", html, re.I), "inline event handler found"
    assert not re.search(r"\sstyle\s*=\s*\"", html, re.I), "inline style attribute found"
    for attrs in re.findall(r"<script\b([^>]*)>", html):
        assert "src=" in attrs or "application/ld+json" in attrs, attrs


def test_all_platforms_are_cross_linked_from_every_tool_page(client):
    html = client.get("/youtube-video-downloader").text
    for slug in PLATFORM_SLUGS:
        assert f'href="/{slug}"' in html, f"{slug} is not reachable from a tool page"


def test_related_tools_link_only_to_existing_tools(client):
    html = client.get("/youtube-video-downloader").text
    section = re.search(r'id="related".*?</section>', html, re.S).group(0)
    hrefs = re.findall(r'href="/([a-z0-9-]+)"', section)
    assert hrefs and set(hrefs) <= set(PLATFORM_SLUGS)
    assert "youtube-shorts-downloader" in hrefs


@pytest.mark.parametrize("path", ["/es/tiktok-video-downloader", "/fr/tiktok-video-downloader", "/ar/tiktok-video-downloader"])
def test_localized_pages_are_complete_and_self_consistent(client, path):
    response = client.get(path)
    assert response.status_code == 200
    html = response.text
    code = path.split("/")[1]
    assert f'<html lang="{code}"' in html
    assert "es/tiktok-video-downloader" in html and "fr/tiktok-video-downloader" in html
    # No English body copy leaking into a translated page.
    assert "How do I download" not in html
    assert "Download in three steps" not in html
    for marker in ('id="how-to"', 'id="features"', 'id="faq"', "data-extract-form"):
        assert marker in html, marker


def test_arabic_page_is_rtl(client):
    html = client.get("/ar/youtube-mp3-downloader").text
    assert 'dir="rtl"' in html
    assert "تنزيل" in html or "فيديوهات" in html


def test_guides_and_legal_pages_render(client):
    guide = client.get("/guides/how-to-download-videos-without-watermark")
    assert guide.status_code == 200
    assert "Table of contents" in guide.text or "On this page" in guide.text
    assert {"WebApplication", "FAQPage", "Article", "BreadcrumbList"} <= ld_types(guide.text)
    assert "og:type" in guide.text and 'content="article"' in guide.text

    for doc, name in (("terms", "Terms of Service"), ("privacy", "Privacy Policy"), ("dmca", "DMCA")):
        response = client.get(f"/{doc}")
        assert response.status_code == 200, doc
        assert name in response.text
        # Legal pages are indexable but not link-farm fodder: no localized mirrors.
        assert 'hreflang="x-default"' not in response.text


def test_sitemap_is_valid_xml_with_alternates(client):
    response = client.get("/sitemap.xml")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    root = ET.fromstring(response.text)
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9", "x": "http://www.w3.org/1999/xhtml"}
    urls = root.findall("s:url", ns)
    assert len(urls) == 48, len(urls)  # (1 home + 10 tools) x 4 locales + 4 guides
    for url in urls:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", url.find("s:lastmod", ns).text)
        assert float(url.find("s:priority", ns).text) in {0.6, 0.7, 0.8, 0.9, 1.0}
    links = urls[1].findall("x:link", ns)
    assert {l.get("hreflang") for l in links} == {"en", "es", "fr", "ar", "x-default"}
    assert all(l.get("href", "").startswith("https://ssavevideo.com/") for l in links)


def test_news_sitemap_lists_only_guides(client):
    root = ET.fromstring(client.get("/sitemap-news.xml").text)
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9", "n": "http://www.google.com/schemas/sitemap-news/0.9"}
    news = root.findall("s:url/n:news", ns)
    assert len(news) == 4
    assert all(node.find("n:title", ns) is not None for node in news)


def test_robots_txt_allows_pages_and_denies_the_api(client):
    response = client.get("/robots.txt")
    assert response.status_code == 200
    body = response.text
    assert "Disallow: /api/" in body
    assert "Sitemap: https://ssavevideo.com/sitemap.xml" in body
    # The default group must still allow the tool pages.
    default = body.split("User-agent: *")[1].split("User-agent:")[0]
    assert "Allow: /" in default
    assert "Disallow: /youtube" not in default


def test_404_is_a_real_page_and_noindexed(client):
    response = client.get("/this-tool-does-not-exist")
    assert response.status_code == 404
    assert 'content="noindex, follow"' in response.text
    assert response.headers["x-robots-tag"] == "noindex, follow"
    for slug in PLATFORM_SLUGS[:4]:
        assert f'href="/{slug}"' in response.text, "404 must keep internal linking"


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/es/", 200),
        ("/fr/", 200),
        ("/ar/", 200),
        ("/es/guides/is-it-safe-to-download-videos-online", 301),  # EN-only document
        ("/es/terms", 301),
        ("/TikTok_Video", 404),
        ("/%2e%2e/%2e%2e/etc/passwd", 404),
        ("/api/nope", 404),
    ],
)
def test_routing_rules(client, path, expected):
    response = client.get(path, follow_redirects=False)
    assert response.status_code == expected, (path, response.status_code, response.text[:200])


def test_trailing_slash_is_normalised_permanently(client):
    response = client.get("/tiktok-video-downloader/", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == "/tiktok-video-downloader"


def test_hreflang_is_also_sent_as_a_link_header(client):
    response = client.get("/tiktok-video-downloader")
    link = response.headers["link"]
    assert 'rel="canonical"' in link
    for lang in ("en", "es", "fr", "ar", "x-default"):
        assert f'hreflang="{lang}"' in link, lang


def test_security_headers_on_every_response(client):
    response = client.get("/")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"].startswith("strict-origin")
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert "unsafe-inline" not in response.headers["content-security-policy"]
    assert "strict-transport-security" in response.headers  # settings.env == production here


def test_html_caching_policy(client):
    page = client.get("/").headers["cache-control"]
    assert "max-age=60" in page and "s-maxage" in page
    css = client.get("/static/css/app.css").headers["cache-control"]
    assert "immutable" in css
    # A loopback URL is rejected by the SSRF guard *before* any network work, so
    # this assertion costs nothing in the test environment.
    api = client.post("/api/extract", json={"url": "http://127.0.0.1:9/x.mp4"}).headers["cache-control"]
    assert api == "no-store"


def test_health_and_metrics(client):
    health = client.get("/healthz").json()
    assert health["status"] in {"ok", "degraded"}
    assert health["engine"]["media_storage"] == "disabled by design"
    metrics = client.get("/metrics").text
    assert "ssave_extract_requests_total" in metrics
    assert "# TYPE" in metrics


def test_webmanifest_and_llms_txt(client):
    manifest = client.get("/manifest.webmanifest")
    assert manifest.status_code == 200
    assert manifest.json()["name"].startswith("SsaveVideo")
    llms = client.get("/llms.txt")
    assert "/tiktok-video-downloader" in llms.text
    assert "POST /api/extract" in llms.text


def test_assets_are_served(client):
    assert client.get("/static/img/favicon.svg").headers["content-type"].startswith("image/svg")
    assert client.get("/static/js/theme.js").status_code == 200
    assert client.get("/static/js/app.js").status_code == 200
    # Every tool page must have a preview image that exists (no 404 OG cards).
    for platform in PLATFORM_SLUGS:
        html = client.get(f"/{platform}").text
        og = re.search(r'property="og:image" content="https://ssavevideo\.com([^"]+)"', html).group(1)
        assert client.get(og).status_code == 200, f"{platform}: {og} missing"


def test_openapi_documents_the_error_contract(client):
    spec = client.get("/api/openapi.json").json()
    extract = spec["paths"]["/api/extract"]["post"]
    assert {403, 404, 422, 429, 502, 504} <= {int(code) for code in extract["responses"]}
    assert client.get("/api/docs").status_code == 200
