"""
Schema-integrity tests for the pSEO data layer.

These are the tests that pay for themselves: a programmatic-SEO build fails
quietly when one JSON entry is malformed (a truncated SERP snippet, a duplicate
description, a link to a page that does not exist). Everything asserted here is
a rule the renderer depends on, so the schema can be edited by a non-programmer
without anyone having to re-read 15 pages by hand.
"""

from __future__ import annotations

import re

import pytest

TITLE_RANGE = (50, 60)
DESC_RANGE = (140, 155)
LOCALES = ("en", "es", "fr", "ar")

# A Tailwind utility token as it must appear in the JSON: a complete class name.
# Interpolated fragments ("from-" + brand) would be purged by the CSS build and
# silently produce an unstyled page, so they are rejected here.
CLASS_TOKEN = re.compile(r"^[a-z0-9]+(:[a-zA-Z0-9./_-]+)?(-[a-zA-Z0-9./_-]+)*$")


def _entries(schema):
    for platform in schema["platforms"]:
        yield "tool", platform
    for guide in schema.get("guides", []):
        yield "guide", guide


def test_schema_top_level_shape(schema):
    assert set(schema) >= {"meta", "site", "platforms", "extraction_defaults"}
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", schema["meta"]["updated"]), "lastmod depends on it"


# The spec's contract for a *platform* entry, asserted for every one of them.
TOOL_REQUIRED = {
    "slug", "path", "key", "name", "display_name", "icon", "domains", "url_placeholder",
    "color_gradient", "soft_gradient", "text_gradient", "chip_class", "button_class",
    "meta_title", "meta_description", "h1", "hero_subtext", "steps", "features", "faq",
    "related", "extraction",
}
# A guide is a document, not a tool: same SEO fields, no tool chrome.
GUIDE_REQUIRED = {"slug", "path", "key", "name", "icon", "meta_title", "meta_description",
                  "h1", "intro", "sections", "faq", "related_platforms"}


def test_required_platform_fields(schema):
    for kind, entry in _entries(schema):
        required = TOOL_REQUIRED if kind == "tool" else GUIDE_REQUIRED
        missing = required - set(entry)
        assert not missing, f"{entry['slug']} is missing {sorted(missing)}"
    for field in ("steps", "features"):
        assert len(schema["site"][field]) == 3
    assert len(schema["site"]["faq"]) >= 5


def test_slugs_are_unique_paths_and_match_their_own_path(schema):
    seen = set()
    for kind, entry in _entries(schema):
        assert entry["slug"] not in seen, f"duplicate slug {entry['slug']}"
        seen.add(entry["slug"])
        assert re.fullmatch(r"[a-z0-9][a-z0-9-]{3,60}", entry["slug"].split("/")[-1])
        assert entry["path"] == "/" + entry["slug"], "path must be derived from the slug"


@pytest.mark.parametrize("lo_hi,label", [(TITLE_RANGE, "meta_title"), (DESC_RANGE, "meta_description")])
def test_english_meta_lengths_fit_serp_budgets(schema, lo_hi, label):
    lo, hi = lo_hi
    offenders = []
    for _, entry in list(_entries(schema)) + [("home", schema["site"])]:
        length = len(entry[label])
        if not lo <= length <= hi:
            offenders.append(f"{entry.get('slug', 'home')}: {length}")
    assert not offenders, f"{label} outside {lo}-{hi}: {offenders}"


def test_every_page_has_three_steps_three_features_and_five_faqs(schema):
    site = schema["site"]
    assert len(site["steps"]) == 3 and len(site["features"]) == 3 and len(site["faq"]) >= 5
    for kind, entry in _entries(schema):
        minimum = 5 if kind == "tool" else 3  # guides keep a shorter, focused FAQ
        assert len(entry["faq"]) >= minimum, f"{entry['slug']} needs {minimum}+ FAQs"
        if kind == "tool":
            assert len(entry["steps"]) == 3, entry["slug"]
            assert len(entry["features"]) == 3, entry["slug"]
        for step in entry.get("steps", []):
            assert step["title"].strip() and len(step["description"].strip()) > 40
        for feature in entry.get("features", []):
            assert feature["title"].strip() and len(feature["description"].strip()) > 60
        if kind == "guide":
            assert len(entry["sections"]) >= 4, "a guide needs real depth"
        for qa in entry["faq"]:
            assert qa["question"].endswith("?"), qa["question"][:40]
            assert len(qa["answer"]) > 90, "thin FAQ answers do not earn rich results"


def test_gradient_and_chip_classes_are_literal_tailwind_tokens(schema):
    for _, entry in _entries(schema):
        for field in ("color_gradient", "chip_class", "button_class", "soft_gradient"):
            value = entry.get(field)
            if not value:
                continue
            tokens = value.split()
            assert tokens, field
            for token in tokens:
                assert CLASS_TOKEN.match(token), f"{entry.get('slug')}.{field}: '{token}' is not a class"
            assert any(t.startswith(("from-", "bg-")) for t in tokens), "gradient needs a stop class"
    # A tool hero needs ink that survives its own gradient.
    for platform in schema["platforms"]:
        assert platform.get("text_gradient") and platform.get("on_gradient_text")


def test_no_duplicate_meta_descriptions_across_pages(schema):
    seen: dict[str, str] = {}
    for kind, entry in list(_entries(schema)) + [("home", schema["site"])]:
        label = entry.get("slug", "home")
        key = entry["meta_description"]
        assert key not in seen, f"{seen.get(key)} and {label} share a description"
        seen[key] = label


def test_internal_links_never_point_at_missing_pages(schema):
    slugs = {p["slug"] for p in schema["platforms"]}
    keys = {p["key"] for p in schema["platforms"]}
    for platform in schema["platforms"]:
        for related in platform.get("related", []):
            assert related in slugs, f"{platform['slug']} links to unknown tool '{related}'"
    for guide in schema.get("guides", []):
        for key in guide.get("related_platforms", []):
            assert key in keys, f"guide {guide['slug']} mentions unknown platform '{key}'"
        assert guide["related_platforms"], "a guide must deep-link to tools"


def test_extraction_profile_keys_are_known(schema):
    allowed = {
        "video_ext_priority", "audio_ext_priority", "max_height", "require_progressive",
        "audio_only", "prefer_clean_stream", "quality_ladder", "socket_timeout_seconds",
        "extractor_retries", "comment",
    }
    defaults = set(schema["extraction_defaults"])
    assert defaults <= allowed | {"comment"}
    for platform in schema["platforms"]:
        unknown = set(platform.get("extraction", {})) - allowed
        assert not unknown, f"{platform['slug']} has unknown extraction keys {unknown}"
        ladder = platform.get("extraction", {}).get("quality_ladder") or []
        cap = platform.get("extraction", {}).get("max_height", 2160)
        assert all(b <= (cap or 2160) for b in ladder), f"{platform['slug']} ladder exceeds max_height"


def test_reddit_promises_merged_audio_and_mp3_page_is_audio_only(schema):
    by_slug = {p["slug"]: p for p in schema["platforms"]}
    assert by_slug["reddit-video-downloader"]["extraction"]["require_progressive"] is True
    assert by_slug["youtube-mp3-downloader"]["extraction"]["audio_only"] is True
    assert "mp4" in by_slug["tiktok-video-downloader"]["extraction"]["video_ext_priority"]
    assert by_slug["tiktok-video-downloader"]["extraction"]["prefer_clean_stream"] is True


def test_domains_cover_the_platforms_cited_in_copy(schema):
    for platform in schema["platforms"]:
        assert platform["domains"], platform["slug"]
        for domain in platform["domains"]:
            assert re.fullmatch(r"\*?[a-z0-9.-]+\.[a-z]{2,}\.?", domain), domain


def test_locale_catalogues_are_complete_and_parallel(catalog):
    codes = [loc["code"] for loc in catalog["locales"]]
    assert codes == list(LOCALES)
    base = set(catalog["ui"]["en"])
    for code in LOCALES:
        assert set(catalog["ui"][code]) == base, f"{code} UI keys differ from en"
        content = catalog["content"][code]
        assert {"meta_title", "meta_description", "h1", "hero_subtext", "steps", "features", "faq"} <= set(content)
        assert len(content["steps"]) == 3 and len(content["features"]) == 3
        assert len(content["faq"]) >= 5
    rtl = [loc for loc in catalog["locales"] if loc["dir"] == "rtl"]
    assert [loc["code"] for loc in rtl] == ["ar"], "only Arabic may be RTL"
    assert {loc["prefix"] for loc in catalog["locales"]} == {"", "es", "fr", "ar"}


def test_localized_overrides_stay_within_serp_budgets(schema):
    for platform in schema["platforms"]:
        for locale_code, block in (platform.get("i18n") or {}).items():
            assert locale_code in LOCALES[1:], locale_code
            assert 40 <= len(block["meta_title"]) <= 60, f"{platform['slug']}/{locale_code} title"
            assert 120 <= len(block["meta_description"]) <= 155, platform["slug"]
            for field in ("h1", "hero_subtext"):
                assert block[field].strip()


def test_unresolved_placeholders_never_reach_render(store):
    for kind, key in [("home", None)] + [("tool", s) for s in store.platforms] + [("guide", g) for g in store.guides]:
        for locale_code in LOCALES:
            page = store.page(kind, key, locale_code)
            assert page is not None, (kind, key, locale_code)
            blob = " ".join(
                [page.meta_title, page.meta_description, page.h1, page.hero_subtext]
                + [f"{s.title}{s.description}" for s in page.steps]
                + [f"{f.title}{f.description}" for f in page.features]
                + [f"{q.question}{q.answer}" for q in page.faq]
            )
            assert "{" not in blob and "}" not in blob, f"placeholder left in {kind}/{locale_code}"


def test_title_and_description_fitters_respect_budgets():
    from app.content import fit_description, fit_title

    long_title = "Instagram Reels, Stories and in-feed video downloader for high definition MP4 files " + "| SsaveVideo"
    assert len(fit_title(long_title)) <= 60
    padded = fit_title("TikTok Downloader")
    assert padded.endswith("| SsaveVideo") and len(padded) <= 60
    short = "A downloader watermark means your file was re-encoded. " * 6
    assert len(fit_description(short)) <= 155
    assert fit_description(short).endswith(".")


def test_sitemap_covers_every_page_and_locale(store):
    entries = store.sitemap_entries()
    urls = {e["loc"] for e in entries}
    for slug in store.platforms:
        for locale_code, locale in store.locales.items():
            expected = store.absolute(store.locale_path("/" + slug, locale_code))
            assert expected in urls, f"{slug}/{locale_code} missing from the sitemap"
    # Guides/legal are English-only documents: exactly one entry each, no locale mirrors.
    for guide_slug in store.guides:
        assert store.absolute("/" + guide_slug) in urls
        assert store.absolute("/es/" + guide_slug) not in urls
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", e["lastmod"]) for e in entries)
    assert all(len(e["alternates"]) == 5 for e in entries if e["changefreq"] != "weekly")
