# SsaveVideo — `ssavevideo.com`

A video downloader for ten platforms and one input box. FastAPI + Jinja2,
server-rendered, programmatic-SEO pages generated from a single JSON catalog.

**The one architectural decision everything else follows from: this server never
downloads, transcodes or stores media.** It runs `yt-dlp` to *resolve* a share URL
into direct, signed CDN links and returns those links as JSON. The visitor's browser
then fetches the file from the platform's own edge. So the box that serves this app
pays for a metadata request (~10–60 KB) instead of a 200 MB transfer, which is why
it can be cheap enough to exist.

```
visitor ── POST /api/extract ──▶ SsaveVideo ── yt-dlp (info only) ──▶ platform API
   ▲                                   │
   └────── direct CDN URL(s) ──────────┘         (browser ──▶ CDN, bytes never touch us)
```

---

## Quickstart

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload            # http://127.0.0.1:8080
```

That is the whole runtime: five dependencies, no Node, no ffmpeg, no database, no
Redis. The compiled stylesheet is committed, so `pip install` is genuinely enough to
run the site.

Or with Docker (multi-stage: Tailwind compiled in a Node stage, Python 3.12 slim at
runtime, non-root, healthchecked):

```bash
docker build -t ssavevideo .
docker run --rm -p 8080:8080 -e SSAVE_SITE_URL=https://ssavevideo.com ssavevideo
```

Useful commands:

| Command | What it does |
| --- | --- |
| `pytest` | 183 tests: SEO contract, page rendering, format picker, SSRF, live extraction |
| `npm run build:css` | recompile Tailwind → `app/static/css/app.css` (only after markup/class changes) |
| `python scripts/build_og_images.py` | regenerate the 15 `og-*.png` share cards (needs `pillow`) |
| `scripts/dev.sh start\|stop\|restart\|log` | background dev server on `:8080`, log at `/tmp/ssave.log` |

---

## Layout

```
data/seo_platforms.json   ← THE product: 10 platforms × copy, colors, FAQ, extraction profile
data/locales.json         ← 4 locales × 98 UI strings + localized page templates
data/legal.json           ← Terms / Privacy / DMCA (English-only by design)
app/config.py             ← frozen Settings, every knob from SSAVE_* env vars
app/security.py           ← SSRF guard, rate limiter, TTL memo cache
app/extraction.py         ← yt-dlp orchestration, format picker, error vocabulary
app/content.py            ← catalog → PageSpec; title/description length fitting, JSON-LD, sitemap
app/main.py               ← routes, response headers, robots/sitemap, /api/*, catch-all pSEO router
app/templates/views/tool.html      ← the single template every tool page renders from
app/templates/partials/            ← header, hero, sections (macros), footer, icons
app/static/js/app.js               ← one deferred IIFE: paste, extract, render, offline cache
assets-src/tailwind.input.css      ← Tailwind layer primitives (ss-*), compiled by npm
tests/                             ← see “Testing”
```

There is exactly **one** page template. Adding a platform means adding a JSON object,
not writing HTML — which is the only reason ten platform pages and four locales stay
consistent.

---

## Adding a platform

Append to `platforms[]` in `data/seo_platforms.json`. Required keys, all enforced by
`tests/test_seo_schema.py`:

```jsonc
{
  "key": "twitch", "slug": "twitch-downloader", "path": "twitch",
  "name": "Twitch", "display_name": "Twitch",
  "icon": "twitch",                       // must exist in partials/icons.html
  "domains": ["twitch.tv", "clips.twitch.tv"],
  "url_placeholder": "https://clips.twitch.tv/…",
  "gradient": "from-purple-600 …", "chip": "…", "button": "…",
  "gradient_hex": "#772ce8", "on_gradient_text": "#ffffff",   // for the OG image script
  "meta_title": "…",                      // 50–60 chars, fitted automatically
  "meta_description": "…",                // 140–155 chars
  "h1": "…", "hero_subtext": "…", "badges": ["…"],
  "extraction": {                           // → app/extraction.py profile
    "audio_only": false,
    "require_progressive": false,           // true = never offer a silent video-only stream
    "prefer_clean_stream": true,            // true = drop watermarked renditions
    "max_height": 1080,
    "video_ext_priority": ["mp4", "webm"],
    "quality_ladder": [1080, 720, 480, 360]
  },
  "steps":   [ { "title": "…", "text": "…" }, … 3 ],
  "features":[ { "icon": "…", "title": "…", "text": "…" }, … 3 ],
  "faq":     [ { "q": "…", "a": "…" }, … >=5 ],
  "related": ["slug1", "slug2", "slug3"]
}
```

Every page then gets, automatically: canonical + hreflang alternates for all four
locales, OG/Twitter cards, `WebApplication` + `FAQPage` + `BreadcrumbList` JSON-LD,
sitemap entry, related-tools grid, mega-footer link, and its own extraction behavior.
Titles and descriptions that fall outside the 50–60 / 140–155 ranges are re-fitted at
load time (`content.py` → `fit_title` / `fit_description`), so the catalog can be
written by a human and still ship valid SERP snippets.

Then add UI strings for new keys to `data/locales.json` (all four locales — a missing
key falls back to English, which is visible in the UI) and re-run the tests.

---

## Configuration

All optional; sane defaults for a single-box deploy.

| Variable | Default | Notes |
| --- | --- | --- |
| `SSAVE_SITE_URL` | `https://ssavevideo.com` | canonical origin; drives `<link rel=canonical>`, hreflang, sitemap, OG URLs |
| `SSAVE_BRAND` | `SsaveVideo` | copy + JSON-LD name |
| `SSAVE_ENV` | `development` | `production` adds HSTS and tightens error output |
| `SSAVE_MAX_CONCURRENCY` | `6` | simultaneous upstream extractions (asyncio gate) |
| `SSAVE_RATE_LIMIT` / `SSAVE_RATE_WINDOW` | `20` / `60` | lookups per client per window; 429 + `Retry-After` |
| `SSAVE_CACHE_TTL` / `SSAVE_CACHE_MAX_ENTRIES` | `900` / `512` | memoised resolutions — the single biggest lever on upstream load |
| `SSAVE_EXTRACT_TIMEOUT` | `25` | wall-clock budget per extraction, then 504 |
| `SSAVE_SOCKET_TIMEOUT` / `SSAVE_RETRIES` | `12` / `1` | passed to yt-dlp |
| `SSAVE_TRUST_PROXY_XFF` | `true` | trust `X-Forwarded-For` for the rate-limit key. **Set `false` if you are not behind a proxy**, or anyone can spoof the header and buy a free quota |
| `SSAVE_MAX_FORMATS` | `10` | cap on rows returned per video |
| `SSAVE_MAX_URL_CHARS` | `2000` | longest accepted URL (refused before DNS) |
| `SSAVE_YTDLP_OPTIONS` | — | JSON merged into the yt-dlp option dict; ops escape hatch (e.g. `{"proxy":"socks5://…"}`) |
| `SSAVE_ALLOW_PRIVATE_HOSTS` | `false` | **dev/tests only** — disables the SSRF guard's private-address rule |
| `PORT`, `WEB_CONCURRENCY` | `8080`, `2` | container entrypoint only |

---

## Security model

Read `app/security.py` before deleting anything from it. A URL-resolving endpoint is a
server-side request forgery gadget by construction: you are asking your server to
fetch an attacker-supplied address. What the guard does, in order:

1. scheme allowlist (`http`/`https`), length cap, control-character/whitespace refusal;
2. user:pass in the URL is stripped (also stops credential-smuggling into logs);
3. hostname must resolve; **every** returned address is checked, so DNS-rebinding to
   `127.0.0.1` and `//0177.0.0.1` octal tricks are refused;
4. refused: loopback, RFC1918, link-local (cloud metadata at `169.254.169.254`),
   unique-local, multicast, unspecified, reserved, CGNAT `100.64.0.0/10`, docs and
   benchmark ranges — implemented as "`ip.is_global` and not anything-special";
5. dangerous ports (25/110/143/445/587/3306/5432/6379/9200/11211/27017) refused;
   named internal hosts (`metadata.google.internal`, `*.internal`, `host.docker.internal`) refused;
6. resolution runs in a thread with a timeout so a slow attacker-controlled DNS server
   cannot pin a worker.

Plus: rate limiting per client with `Retry-After`; a hard concurrency gate so one viral
page cannot exhaust the thread pool; response headers (`CSP` without `unsafe-inline`,
`X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`, `Permissions-Policy`, HSTS in
production); `/api/` disallowed in `robots.txt`; `X-Robots-Tag: noindex, follow` on
404s; no user HTML is ever interpolated into the DOM (the client writes `textContent`
only, and yt-dlp-supplied titles/thumbnails are sanitised and scheme-checked
server-side).

---

## SEO contract (regression-tested, not aspirational)

* One canonical per page: `https://ssavevideo.com{{ current_path }}`, self-referencing,
  no query strings; localized roots 308 to their canonical form.
* Full `hreflang` set (4 locales + `x-default`) emitted from the same function that
  builds URLs, so alternates can't drift from reality.
* JSON-LD `WebApplication` (`offers.price: 0`, `USD`), `FAQPage` built from the same
  FAQ array the page renders — a mismatch between markup and visible text is a manual
  action risk, so `tests/test_pages_seo.py` asserts every FAQ question appears
  verbatim in the HTML.
* `/sitemap.xml` (48 URLs, `lastmod` timestamps) + `/sitemap-news.xml` (valid
  `<url><news:news>` nesting), both advertised in `/robots.txt`, which allows all tool
  pages and disallows `/api/`, `?utm_*`, `?ref*`.
* Guides and legal pages are English-only and **301** their localized paths, so
  `/es/guia/…` never becomes a thin duplicate.
* Zero-JS layout: the whole eight-section page renders server-side, including the FAQ
  accordion; JS only adds extraction.

---

## Deployment notes

* **Workers multiply in-process state.** The rate limiter and the memo cache live per
  Uvicorn worker. `WEB_CONCURRENCY=4` means an effective `4 × SSAVE_RATE_LIMIT` per
  box and four copies of the cache. Either run one worker per core and divide
  `SSAVE_RATE_LIMIT` accordingly, or move both to Redis (a clean seam:
  `RateLimiter` and `TTLCache` in `app/security.py` are the only two classes to swap).
* **Set `SSAVE_MAX_CONCURRENCY` to what your egress can absorb.** yt-dlp work is
  network-bound and happens in a threadpool; 6 is a conservative default for a 1 vCPU
  box. Raising it raises throughput *and* the chance the platform starts throttling you.
* **Cache at the edge too.** Successful responses carry `X-Extract-Cache: HIT|MISS` and
  short public `max-age` on HTML. A CDN that honours `Vary`-free cacheable POSTs is rare,
  so the in-process TTL is what usually matters; keep `SSAVE_CACHE_TTL` ≥ 600.
* **`SSAVE_TRUST_PROXY_XFF=false` when there is no proxy in front**, otherwise the
  rate-limit key is spoofable.
* **Expect to be rate-limited by the platforms**, not by your own limits. Keep
  `SSAVE_RETRIES` low (1) and let the 429/504 error codes reach the UI honestly — a
  downloader that hides upstream throttling just looks broken to users.
* The extractor is a moving target: `yt-dlp` breaks when a platform changes. Pin a
  version in production, upgrade deliberately, and keep the live integration test in CI
  (`pytest -m slow`).

---

## Testing

```bash
pytest                      # 183 tests, ~9s, no network except the loopback fixture
pytest -m slow              # only the real-engine integration module
pytest tests/test_pages_seo.py -k hreflang
```

| File | Covers |
| --- | --- |
| `tests/test_seo_schema.py` | the catalog itself: required keys, slug/path/domain validity, meta length budgets, ≥5 FAQ per platform, icon references, gradient pairs |
| `tests/test_pages_seo.py` | rendered HTML for every locale/platform: title/canonical/hreflang/OG/Twitter, JSON-LD validity + FAQ/markup parity, sitemap + robots XML, page section order, no inline handlers, cache headers, redirects, 404 policy |
| `tests/test_api_extract.py` | `/api/extract` contract: validation, error-code mapping, cache HIT/MISS, per-platform profiles (`require_progressive`, `prefer_clean_stream`, `max_height`), rate limiting, XFF keying |
| `tests/test_extraction.py` | format picker: merged vs `video_only` labelling, bucketing, DRM/fragmented/URL-less rows dropped, playlist→first-entry policy, `human_*`, classification table |
| `tests/test_pages_seo.py` covers the HTML 500 page |
| `tests/test_security.py` | SSRF refusals (metadata IPs, loopback, CGNAT, octal/IPv6, ports, credentials), limiter windows + memory bound, TTL cache eviction/expiry |
| `tests/test_integration_real_extraction.py` | **no mocks**: a real MP4 over a localhost HTTP server through real `yt-dlp`, asserting the server never calls the downloader |

---

## Known limits (deliberate)

* **No conversion.** YouTube serves an audio stream, not an `.mp3`; the UI says so
  instead of lying, and there is no ffmpeg step. Adding one means storing bytes, which
  is the trade the whole design refuses.
* **Reddit** gets `require_progressive`: silent DASH video is dropped rather than
  offered, because we cannot mux audio in.
* **Codecs are not probed.** `ffprobe` never runs, so container-level labels are used;
  when a manifest omits codec fields the payload carries a `codecs_unverified` warning.
* **DRM content is refused**, always — a row with `drm` never reaches the response.
* Private/age-gated/regional content returns `403/451` copy explaining exactly that.
* Legal pages and guides are English-only until someone translates them; localized
  URLs 301 to `/`, they do not half-translate.
