"""
The pSEO data layer: turns `data/*.json` into renderable, fully-decorated pages.

Everything a page needs — copy, brand classes, meta, structured data, hreflang
graph, sitemap entry — is derived here from the schema, so the Jinja templates
stay dumb (loops and conditionals only, no business rules) and adding a platform
is a JSON edit, never a code change.

Localization model (progressive, no half-translated pages):
    1. `data/locales.json` -> `ui`      : exact UI chrome strings per locale.
    2. `data/locales.json` -> `content` : parameterized copy templates ({platform})
       so *every* platform page renders in-language in every locale.
    3. `seo_platforms.json` -> `platform.i18n` : hand-written overrides that win
       over (2) for the platforms worth the extra effort.
Meta lengths are normalised by `fit_title` / `fit_description` so SERP snippets
stay in the 50–60 / 140–155 sweet spot whatever the platform name does to them.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Optional

from .config import APP_DIR, LEGAL_FILE, LOCALES_FILE, SEO_SCHEMA_FILE, Settings, get_settings

STATIC_IMG_DIR = APP_DIR / "static" / "img"

# SERP length budgets (characters, counted on the rendered string).
TITLE_MIN, TITLE_MAX = 50, 60
DESC_MIN, DESC_MAX = 140, 155
# Localized variants get a documented tolerance: transliterations of platform
# names shift lengths by several characters, and cutting a good phrase to hit a
# soft ceiling is worse SEO than being 5 characters long.
I18N_TITLE_SLACK, I18N_DESC_SLACK = 6, 12


@dataclass(frozen=True)
class Locale:
    code: str
    hreflang: str
    label: str
    native_label: str
    dir: str
    prefix: str
    # Open Graph wants a language_REGION locale (`ar_AR`, not `ar`); the catalog
    # carries it so a translator can override without touching Python.
    og_locale: str = ""

    @property
    def og_locale_tag(self) -> str:
        return self.og_locale or self.hreflang.replace("-", "_")

    @property
    def is_default(self) -> bool:
        return not self.prefix

    @property
    def html_lang(self) -> str:
        return self.code


@dataclass(frozen=True)
class Step:
    number: int
    title: str
    description: str


@dataclass(frozen=True)
class Feature:
    title: str
    description: str
    icon: str


@dataclass(frozen=True)
class QA:
    question: str
    answer: str


@dataclass(frozen=True)
class RelatedLink:
    slug: str
    path: str
    name: str
    icon: str
    chip_class: str


@dataclass(frozen=True)
class GuideLink:
    slug: str
    path: str
    name: str
    meta_title: str


@dataclass(frozen=True)
class PageSpec:
    """Everything a template needs. Rendered from here, never recomputed inline."""

    kind: str                       # home | tool | guide
    key: str
    slug: str
    path: str                       # locale-absolute path, e.g. "/es/tiktok-video-downloader"
    locale: Locale
    name: str
    display_name: str
    icon: str
    # copy
    meta_title: str
    meta_description: str
    h1: str
    hero_subtext: str
    eyebrow: str
    badges: tuple[str, ...]
    steps: tuple[Step, ...]
    features: tuple[Feature, ...]
    faq: tuple[QA, ...]
    guide_sections: tuple[tuple[str, str], ...] = ()
    # visuals
    color_gradient: str = ""
    soft_gradient: str = ""
    text_gradient: str = ""
    chip_class: str = ""
    button_class: str = ""
    glow_class: str = ""
    # form + extraction profile
    url_placeholder: str = ""
    domains: tuple[str, ...] = ()
    extraction: dict[str, Any] = field(default_factory=dict)
    # linking
    related: tuple[RelatedLink, ...] = ()
    breadcrumbs: tuple[tuple[str, str], ...] = ()
    og_image: str = ""
    robots: str = "index, follow, max-image-preview:large, max-snippet:-1"

    # ---------------------------------------------------------------- derived

    @property
    def canonical_path(self) -> str:
        return self.path

    @property
    def is_rtl(self) -> bool:
        return self.locale.dir == "rtl"

    @property
    def keywords(self) -> tuple[str, ...]:
        """A tight, honest keyword set (feeds meta keywords + schema `keywords`)."""
        raw = f"{self.name} downloader, download {self.name} video, {self.name} mp4, no watermark"
        return tuple(part.strip() for part in raw.split(",") if part.strip())

    def faq_dicts(self) -> list[dict[str, str]]:
        return [{"question": q.question, "answer": q.answer} for q in self.faq]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fit(text: str, lo: int, hi: int, brand_suffix: str = "") -> str:
    """
    Bring a generated SEO string into a length budget.

    Strategy: try `text + brand_suffix`; if still long, drop trailing filler
    words (never mid-word) and retry; if too short, append the suffix. Titles
    are never truncated with an ellipsis — a cut-off brand is worse than a
    slightly long one.
    """
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < lo and brand_suffix and len(text) + len(brand_suffix) <= hi:
        return text + brand_suffix
    if len(text) <= hi:
        return text
    words = text.split()
    while len(" ".join(words)) > hi and len(words) > 3:
        words.pop()
    trimmed = " ".join(words)
    if brand_suffix and len(trimmed) + len(brand_suffix) <= hi and len(trimmed) < lo:
        trimmed = trimmed + brand_suffix
    return trimmed


def fit_title(text: str, *, brand_suffix: str = " | SsaveVideo", slack: int = 0) -> str:
    return _fit(text, TITLE_MIN - slack, TITLE_MAX + slack, brand_suffix)


def fit_description(text: str, *, slack: int = 0) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    hi = DESC_MAX + slack
    if len(cleaned) <= hi:
        return cleaned
    words = cleaned.split()
    while len(" ".join(words)) > hi - 1 and len(words) > 8:
        words.pop()
    out = " ".join(words).rstrip(" ,;:")
    return out + "."


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ContentStore:
    """Loads and indexes the pSEO schema + locale catalogs."""

    def __init__(self, *, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self.schema = self._read_json(SEO_SCHEMA_FILE)
        self.catalog = self._read_json(LOCALES_FILE)
        self.legal = self._read_json(LEGAL_FILE) if LEGAL_FILE.exists() else {"documents": {}}

        self.site: dict[str, Any] = self.schema["site"]
        self.extraction_defaults: dict[str, Any] = self.schema.get("extraction_defaults", {})
        self.locales: dict[str, Locale] = {
            item["code"]: Locale(**{k: item[k] for k in (
                "code", "hreflang", "label", "native_label", "dir", "prefix")},
                og_locale=item.get("og_locale", ""))
            for item in self.catalog["locales"]
        }
        self.default_locale: str = self.catalog["meta"]["default_locale"]
        self.ui: dict[str, dict[str, str]] = self.catalog["ui"]
        self.localized_content: dict[str, dict[str, Any]] = self.catalog["content"]

        self.platforms: dict[str, dict[str, Any]] = {
            p["slug"]: p for p in self.schema["platforms"]
        }
        self.platforms_by_key: dict[str, dict[str, Any]] = {
            p["key"]: p for p in self.schema["platforms"]
        }
        self.guides: dict[str, dict[str, Any]] = {g["slug"]: g for g in self.schema.get("guides", [])}
        # Bump on every deploy so `lastmod` in the sitemap reflects real changes.
        self.updated = self._parse_updated(self.schema.get("meta", {}).get("updated"))

    # ------------------------------------------------------------------ io

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    @staticmethod
    def _parse_updated(value: Optional[str]) -> datetime:
        try:
            return datetime.strptime(value or "", "%Y-%m-%d").replace(
                hour=9, minute=0, tzinfo=timezone.utc
            )
        except ValueError:
            return datetime.now(timezone.utc)

    # ------------------------------------------------------------- strings

    def t(self, locale_code: str, key: str, **fmt: Any) -> str:
        """Translate a UI key with fallback chain: locale -> default locale -> key."""
        table = self.ui.get(locale_code) or {}
        text = table.get(key)
        if text is None:
            text = (self.ui.get(self.default_locale) or {}).get(key, key)
        if fmt:
            try:
                text = text.format(**fmt)
            except (KeyError, IndexError):  # a template used an unknown placeholder
                return text
        return text

    # ------------------------------------------------------- path plumbing

    def js_strings(self, locale_code: str, keys: Iterable[str]) -> str:
        """
        Serialise the handful of UI labels the client-side renderer needs as a JSON
        blob for a `data-` attribute. JSON (not an inline <script>) because the CSP
        forbids inline scripts, and escaping is handled by Jinja's autoescape.
        """
        payload = {key: self.t(locale_code, key) for key in keys}
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def locale_path(self, base_path: str, locale_code: str) -> str:
        locale = self.locales[locale_code]
        cleaned = "/" + base_path.strip("/")
        if cleaned == "/" or cleaned == "":
            return "/" if locale.is_default else f"/{locale.prefix}/"
        if locale.is_default:
            return cleaned
        return f"/{locale.prefix}{cleaned}"

    def alternates_for(self, base_path: str) -> list[dict[str, str]]:
        """hreflang graph, always including x-default -> the canonical English page."""
        items = [
            {"hreflang": loc.hreflang, "href": self.absolute(self.locale_path(base_path, code))}
            for code, loc in self.locales.items()
        ]
        default = self.absolute(self.locale_path(base_path, self.default_locale)) or base_path
        items.append({"hreflang": "x-default", "href": default})
        return items

    def absolute(self, path: str) -> str:
        origin = self.settings.origin
        if path.startswith(("http://", "https://")):
            return path
        return f"{origin}{path if path.startswith('/') else '/' + path}"

    # ------------------------------------------------------------- page build

    def _copy_for(self, base: dict[str, Any], locale_code: str) -> dict[str, Any]:
        """
        Resolve the copy a page will render.

        Precedence, and why it is ordered this way:
          * English (canonical): the hand-written per-platform copy in
            `seo_platforms.json`, always.
          * Other locales: `platform["i18n"][locale]` if the platform has been
            translated by hand, otherwise the parameterized templates in
            `locales.json`. A non-English page *never* falls back to English
            body copy — a half-translated page is worse for users and for
            quality raters than a fully translated generic one.
        """
        is_en = locale_code == self.default_locale
        localized = (base.get("i18n") or {}).get(locale_code) or {}
        templates = self.localized_content.get(locale_code) or self.localized_content[self.default_locale]
        name = base.get("name") or base.get("display_name") or self.site["brand"]
        params = {"platform": name.lower(), "Platform": name, "brand": self.site["brand"]}

        def fmt(value: Any) -> str:
            try:
                return str(value).format(**params)
            except (KeyError, IndexError, ValueError):
                return str(value)  # an unrecognised placeholder: render verbatim

        if is_en:
            src = base
            raw_title, raw_desc = src.get("meta_title", ""), src.get("meta_description", "")
            raw_h1, raw_sub = src.get("h1", ""), src.get("hero_subtext", "")
            raw_steps, raw_features, raw_faq = (
                src.get("steps") or [], src.get("features") or [], src.get("faq") or [],
            )
            title_slack = desc_slack = 0
            brand_suffix = ""
        else:
            raw_title = localized.get("meta_title") or templates["meta_title"]
            raw_desc = localized.get("meta_description") or templates["meta_description"]
            raw_h1 = localized.get("h1") or templates["h1"]
            raw_sub = localized.get("hero_subtext") or templates["hero_subtext"]
            raw_steps = localized.get("steps") or templates["steps"]
            raw_features = localized.get("features") or templates["features"]
            raw_faq = localized.get("faq") or templates["faq"]
            title_slack, desc_slack = I18N_TITLE_SLACK, I18N_DESC_SLACK
            brand_suffix = f" | {self.site['brand']}"

        def field(item: Any, key: str) -> str:
            value = item.get(key) if isinstance(item, dict) else getattr(item, key, "")
            return fmt(value or "")

        return {
            "name": name,
            "meta_title": fit_title(fmt(raw_title), brand_suffix=brand_suffix, slack=title_slack),
            "meta_description": fit_description(fmt(raw_desc), slack=desc_slack),
            "h1": fmt(raw_h1),
            "hero_subtext": fmt(raw_sub),
            "steps": tuple(
                Step(i + 1, field(item, "title"), field(item, "description"))
                for i, item in enumerate(raw_steps)
            ),
            "features": tuple(
                Feature(field(item, "title"), field(item, "description"),
                        (item.get("icon") if isinstance(item, dict) else getattr(item, "icon", "")) or "sparkles")
                for item in raw_features
            ),
            "faq": tuple(
                QA(field(item, "question"), field(item, "answer")) for item in raw_faq
            ),
        }

    def _related_links(self, slugs: Iterable[str], locale_code: str) -> tuple[RelatedLink, ...]:
        links: list[RelatedLink] = []
        for slug in slugs:
            platform = self.platforms.get(slug)
            if not platform:
                continue
            links.append(
                RelatedLink(
                    slug=slug,
                    path=self.locale_path(platform["path"], locale_code),
                    name=platform["display_name"],
                    icon=platform["icon"],
                    chip_class=platform["chip_class"],
                )
            )
        return tuple(links)

    def page(self, kind: str, key: Optional[str], locale_code: Optional[str] = None) -> Optional[PageSpec]:
        """Build a page spec for (kind, key, locale) or None when it does not exist."""
        locale_code = locale_code or self.default_locale
        if locale_code not in self.locales:
            return None

        if kind == "home":
            base = dict(self.site)
            base["key"], base["name"] = "site", self.site["brand"]
            base.setdefault("icon", "download")
            base.setdefault("domains", ())
            base.setdefault("url_placeholder", "")
            base.setdefault("i18n", {})
            base.setdefault("extraction", {})
            base.setdefault("related", [p["slug"] for p in self.schema["platforms"][:6]])
            base_slug = ""
        elif kind == "tool":
            platform = self.platforms.get(key or "")
            if platform is None:
                return None
            base, base_slug = platform, platform["slug"]
        elif kind == "guide":
            guide = self.guides.get(key or "")
            if guide is None:
                return None
            # Guides are long-form English documents: we do not mint a localized
            # URL per locale for content we cannot fully translate, because a
            # page with localised chrome around an English article is a duplicate
            # with a language mismatch, which is worse than not existing.
            locale_code = self.default_locale
            base = dict(guide)
            base.setdefault("name", guide["name"])
            base.setdefault("badges", [])
            base.setdefault("steps", [])
            base.setdefault("features", [])
            base.setdefault("i18n", {})
            base.setdefault("domains", ())
            base["hero_subtext"] = guide.get("intro") or guide.get("meta_description", "")
            base_slug = guide["slug"]
        else:
            return None

        copy = self._copy_for(base, locale_code)
        locale = self.locales[locale_code]
        path = self.locale_path(base_slug if kind != "home" else "", locale_code)

        # Guides link to the *platforms they mention*, tools link to sibling tools.
        if kind == "guide":
            related_keys = base.get("related_platforms") or []
            related = self._related_links(
                (self.platforms_by_key[k]["slug"] for k in related_keys if k in self.platforms_by_key),
                locale_code,
            )
        else:
            related = self._related_links(base.get("related") or [], locale_code)

        breadcrumbs: tuple[tuple[str, str], ...] = ((copy["name"], path),)
        if kind == "home":
            breadcrumbs = ((self.t(locale_code, "nav.home"), self.locale_path("", locale_code)),)
        elif kind == "tool":
            breadcrumbs = (
                (self.t(locale_code, "nav.home"), self.locale_path("", locale_code)),
                (copy["name"], path),
            )
        else:
            breadcrumbs = (
                (self.t(locale_code, "nav.home"), self.locale_path("", locale_code)),
                (self.t(locale_code, "nav.guides"), self.locale_path("", locale_code)),
                (copy["name"], path),
            )

        # Per-platform OG card if the build script produced one, else the brand card.
        og_image = base.get("og_image") or f"/static/img/og-{base.get('key', 'default')}.png"
        if not (STATIC_IMG_DIR / Path(og_image).name).exists():
            og_image = self.site.get("og_image", "/static/img/og-default.png")

        guide_sections: tuple[tuple[str, str], ...] = ()
        if kind == "guide":
            guide_sections = tuple((s["title"], s["body"]) for s in base.get("sections", []))

        return PageSpec(
            kind=kind,
            key=base.get("key", base_slug or "home"),
            slug=base_slug or "",
            path=path or "/",
            locale=locale,
            name=base.get("display_name") or copy["name"],
            display_name=base.get("display_name") or copy["name"],
            icon=base.get("icon", "download"),
            meta_title=copy["meta_title"],
            meta_description=copy["meta_description"],
            h1=copy["h1"],
            hero_subtext=copy["hero_subtext"],
            eyebrow=self.t(locale_code, "hero.eyebrow"),
            badges=tuple(base.get("badges") or ()),
            steps=copy["steps"],
            features=copy["features"],
            faq=copy["faq"],
            guide_sections=guide_sections,
            color_gradient=base.get("color_gradient", self.site.get("color_gradient", "bg-gradient-to-br from-indigo-600 via-violet-600 to-fuchsia-600")),
            soft_gradient=base.get("soft_gradient", "bg-gradient-to-br from-indigo-50 via-white to-fuchsia-50 dark:from-indigo-500/10 dark:via-slate-950 dark:to-fuchsia-500/10"),
            text_gradient=base.get("text_gradient", "bg-gradient-to-r from-indigo-600 to-fuchsia-600 bg-clip-text text-transparent"),
            chip_class=base.get("chip_class", "border-slate-200 bg-white text-slate-700 dark:border-white/10 dark:bg-white/5 dark:text-slate-200"),
            button_class=base.get("button_class", "bg-gradient-to-r from-indigo-600 via-violet-600 to-fuchsia-600 text-white shadow-lg shadow-indigo-600/25 hover:brightness-110"),
            glow_class=base.get("glow_class", "shadow-indigo-500/20"),
            url_placeholder=base.get("url_placeholder", "https://"),
            domains=tuple(base.get("domains") or ()),
            extraction={**self.extraction_defaults, **(base.get("extraction") or {})},
            related=related,
            breadcrumbs=breadcrumbs,
            og_image=self.absolute(og_image),
            robots="index, follow, max-image-preview:large, max-snippet:-1",
        )

    def legal_page(self, doc: dict[str, Any], locale_code: Optional[str] = None) -> PageSpec:
        """
        Legal documents are page-shaped but not tool-shaped: no hero form, no FAQ,
        English only, still carrying canonical + schema + chrome so the shared
        layout (and the mega-footer's internal links) applies to them too.
        """
        locale_code = locale_code or self.default_locale
        base: dict[str, Any] = {
            **doc,
            "name": doc["name"],
            "display_name": doc["name"],
            "hero_subtext": doc.get("intro", ""),
            "badges": [],
            "steps": [],
            "features": [],
            "faq": [],
            "i18n": {},
            "icon": "shield",
            "domains": [],
        }
        copy = self._copy_for(base, self.default_locale)
        return PageSpec(
            kind="legal",
            key=doc["key"],
            slug="",
            path=doc["path"],
            locale=self.locales[locale_code],
            name=doc["name"],
            display_name=doc["name"],
            icon="shield",
            meta_title=copy["meta_title"],
            meta_description=copy["meta_description"],
            h1=doc["h1"],
            hero_subtext=doc.get("intro", ""),
            eyebrow=self.t(locale_code, "footer.legal"),
            badges=(),
            steps=(),
            features=(),
            faq=(),
            guide_sections=tuple((s["title"], s["body"]) for s in doc.get("sections", [])),
            color_gradient="bg-gradient-to-br from-slate-800 via-slate-900 to-slate-800",
            soft_gradient="bg-gradient-to-br from-slate-100 to-white dark:from-slate-900 dark:to-slate-950",
            text_gradient="text-slate-900 dark:text-white",
            chip_class="border-slate-200 bg-white text-slate-700 dark:border-white/10 dark:bg-white/5 dark:text-slate-200",
            button_class="bg-slate-900 text-white hover:brightness-125 dark:bg-white dark:text-slate-900",
            glow_class="shadow-slate-500/10",
            related=self._related_links(
                [p["slug"] for p in self.schema["platforms"][:4]], locale_code
            ),
            breadcrumbs=(
                (self.t(locale_code, "nav.home"), self.locale_path("", locale_code)),
                (doc["name"], doc["path"]),
            ),
            og_image=self.absolute(self.site.get("og_image", "/static/img/og-default.png")),
            robots="index, follow, nomaxsnippet" if False else "index, follow",
        )

    # ------------------------------------------------------------ structured data

    def json_ld(self, page: PageSpec) -> list[dict[str, Any]]:
        """
        JSON-LD graph: WebApplication (free), FAQPage, BreadcrumbList, and for
        guides an Article node. Kept separate so a template can inline each block
        and so tests can assert on the parsed dicts instead of scraping HTML.
        """
        origin = self.settings.origin
        url = origin + page.path
        publisher = {
            "@type": "Organization",
            "name": self.site["brand"],
            "url": origin + "/",
            "logo": {"@type": "ImageObject", "url": origin + self.site.get("favicon", "/static/img/logo.svg")},
        }
        app = {
            "@context": "https://schema.org",
            "@type": "WebApplication",
            "@id": url + "#app",
            "name": page.meta_title,
            "url": url,
            "description": page.meta_description,
            "applicationCategory": "MultimediaApplication",
            "applicationSubCategory": "Video downloader",
            "operatingSystem": "Any (web browser)",
            "browserRequirements": "Requires JavaScript disabled or enabled; works without an account",
            "isAccessibleForFree": True,
            "offers": {
                "@type": "Offer",
                "price": "0",
                "priceCurrency": "USD",
                "availability": "https://schema.org/InStock",
                "url": origin + "/",
            },
            "publisher": publisher,
            "featureList": [
                "Direct CDN download links without server-side storage",
                "HD and original-resolution MP4 selection",
                "Audio-only extraction",
                "No registration, no watermark, no daily quota",
                f"Support for {len(self.platforms)} platforms",
            ],
        }
        if page.kind != "home":
            app["mainEntityOfPage"] = {"@type": "WebPage", "@id": url}
            app["about"] = {"@type": "Thing", "name": page.name}

        blocks: list[dict[str, Any]] = [app]

        if page.faq:
            blocks.append(
                {
                    "@context": "https://schema.org",
                    "@type": "FAQPage",
                    "@id": url + "#faq",
                    "mainEntity": [
                        {
                            "@type": "Question",
                            "name": qa.question,
                            "acceptedAnswer": {"@type": "Answer", "text": qa.answer},
                        }
                        for qa in page.faq
                    ],
                }
            )

        # Breadcrumbs come from the page itself, deduplicated by normalised path so
        # "Home" can never appear twice, and each `item` is an absolute URL, which
        # is what Search Console expects for a valid BreadcrumbList.
        crumbs: list[dict[str, Any]] = []
        seen: set[str] = set()
        for label, crumb_path in page.breadcrumbs:
            normalised = crumb_path.rstrip("/") or "/"
            if normalised in seen:
                continue
            seen.add(normalised)
            crumbs.append(
                {
                    "@type": "ListItem",
                    "position": len(crumbs) + 1,
                    "name": label,
                    "item": self.absolute(crumb_path),
                }
            )
        if len(crumbs) > 1:
            blocks.append(
                {
                    "@context": "https://schema.org",
                    "@type": "BreadcrumbList",
                    "@id": url + "#breadcrumb",
                    "itemListElement": crumbs,
                }
            )

        if page.kind == "guide":
            blocks.append(
                {
                    "@context": "https://schema.org",
                    "@type": "Article",
                    "@id": url + "#article",
                    "headline": page.h1[:110],
                    "description": page.meta_description,
                    "inLanguage": page.locale.hreflang,
                    "author": publisher,
                    "publisher": publisher,
                    "dateModified": _iso(self.updated),
                    "mainEntityOfPage": url,
                }
            )
        return blocks

    # --------------------------------------------------------------- sitemap

    def sitemap_entries(self) -> list[dict[str, Any]]:
        """One <url> per page per locale, with hreflang alternates attached."""
        entries: list[dict[str, Any]] = []
        bases: list[tuple[str, str, str]] = [("", "home", "daily")]
        bases += [(p["path"], "tool", "daily") for p in self.schema["platforms"]]
        # Guides/legal exist in English only, so only they enter the sitemap once.
        bases += [(g["path"], "guide", "weekly") for g in self.schema.get("guides", [])]
        for base_path, kind, changefreq in bases:
            codes = list(self.locales) if kind in {"home", "tool"} else [self.default_locale]
            for code in codes:
                loc = self.locales[code]
                path = self.locale_path(base_path, code)
                entries.append(
                    {
                        "loc": self.absolute(path),
                        "lastmod": _iso(self.updated),
                        "changefreq": changefreq,
                        "priority": self._sitemap_priority(kind, loc),
                        "alternates": self.alternates_for(base_path),
                    }
                )
        return entries

    @staticmethod
    def _sitemap_priority(kind: str, locale: Locale) -> str:
        """Canonical-locale pages rank highest; alternates a notch below."""
        base = {"home": 1.0, "tool": 0.9, "guide": 0.7}[kind]
        if not locale.is_default:
            base -= 0.1
        return f"{base:.1f}"

    # ------------------------------------------------------------ navigation

    def nav_platforms(self, locale_code: str, limit: int = 5) -> list[dict[str, Any]]:
        slugs = self.site.get("nav_quick_links") or [p["slug"] for p in self.schema["platforms"][:limit]]
        out = []
        for slug in slugs[:limit]:
            platform = self.platforms.get(slug)
            if platform:
                out.append(
                    {
                        "path": self.locale_path(platform["path"], locale_code),
                        "name": platform["name"],
                        "icon": platform["icon"],
                    }
                )
        return out

    def all_platforms(self, locale_code: str) -> list[dict[str, Any]]:
        return [
            {
                "path": self.locale_path(p["path"], locale_code),
                "name": p["display_name"],
                "icon": p["icon"],
                "chip_class": p["chip_class"],
                "blurb": p["hero_subtext"],
                "gradient": p["color_gradient"],
            }
            for p in self.schema["platforms"]
        ]

    def all_guides(self, locale_code: str = "") -> list[GuideLink]:
        """
        Guide links always resolve to the canonical English URL: the documents are
        not localized, so every locale's footer/nav points at the one real page.
        """
        return [
            GuideLink(
                slug=g["slug"],
                path=g["path"],
                name=g["name"],
                meta_title=g["meta_title"],
            )
            for g in self.schema.get("guides", [])
        ]

    def footer(self, locale_code: str) -> list[dict[str, Any]]:
        """Footer columns resolved to real hrefs (platforms, guides, legal)."""
        columns: list[dict[str, Any]] = []
        for column in self.site.get("footer_columns", []):
            links: list[dict[str, str]] = []
            if column.get("links"):
                for slug in column["links"]:
                    platform = self.platforms.get(slug)
                    if platform:
                        links.append(
                            {
                                "href": self.locale_path(platform["path"], locale_code),
                                "label": platform["display_name"],
                            }
                        )
            if column.get("guides"):
                # English-only documents: no locale prefix, one canonical target.
                for guide in self.schema.get("guides", []):
                    links.append({"href": guide["path"], "label": guide["name"]})
            if column.get("legal"):
                for key in self.legal.get("documents", {}):
                    # Legal docs are English-only: link the canonical URL directly.
                    links.append({"href": self.legal['documents'][key].get("path", f"/{key}"), "label_key": f"legal.{key}"})
            columns.append({"title_key": column["title"], "links": links})
        return columns


# ---------------------------------------------------------------------------
# Module-level accessor
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_store() -> ContentStore:
    """One parsed, indexed copy of the schema for the life of the process."""
    return ContentStore()


def render_store(settings: Settings) -> ContentStore:  # pragma: no cover - test hook
    return ContentStore(settings=settings)
