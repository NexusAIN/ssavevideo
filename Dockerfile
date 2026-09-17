# syntax=docker/dockerfile:1
#
# SsaveVideo — production image.
#
# Two build stages, and the reason for the first one is narrow: Tailwind is a Node
# tool, but the *runtime* must not carry a Node toolchain. So the CSS is compiled in
# `web` and copied into a slim Python image. Nothing else about this app needs a
# browser toolchain — it is server-rendered HTML plus one hand-written JS module.
#
# Deliberately absent, and that is the point:
#   • no ffmpeg — we transcode nothing; the whole architecture is "resolve URLs,
#     never move bytes", and installing ffmpeg is the first step back to a server
#     that pays for bandwidth.
#   • no media volume — nothing is written to disk at runtime.

# ----------------------------------------------------------------------------
# Stage 1 — compile the Tailwind layer into app/static/css/app.css
# ----------------------------------------------------------------------------
FROM node:20-alpine AS web
WORKDIR /build

# `npm ci` needs a lockfile; if you ship without one, fall back to `npm install`.
COPY package.json package-lock.json* ./
RUN --mount=type=cache,target=/root/.npm \
    if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; \
    else npm install --no-audit --no-fund; fi

COPY tailwind.config.js ./
COPY assets-src ./assets-src
COPY app/templates ./app/templates
COPY data ./data
# `content` globs in tailwind.config.js read the templates and data/*.json, so those
# files must be present for the purge step to keep every class the JSON references.
RUN npx tailwindcss -i ./assets-src/tailwind.input.css -o ./out/app.css --minify

# ----------------------------------------------------------------------------
# Stage 2 — runtime
# ----------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# PYTHONUNBUFFERED keeps logs flowing to `docker logs`; no pip cache + no .pyc to
# keep the layer small and startup predictable.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # The image must never need the developer escape hatch. Explicit is better than
    # implicit: if this is ever set to 1, SSRF protection is off.
    SSAVE_ALLOW_PRIVATE_HOSTS=0 \
    SSAVE_ENV=production

# curl is only used by HEALTHCHECK; git is not needed (yt-dlp is pinned on PyPI).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/ssavevideo

COPY requirements.txt ./
RUN pip install -r requirements.txt

# Application code. `app/` holds templates and static assets on purpose — this is a
# server-rendered site, so shipping the Python package ships its views.
COPY app ./app
COPY data ./data
COPY scripts ./scripts

# Compiled stylesheet from stage 1.
COPY --from=web /build/out/app.css ./app/static/css/app.css

# Run as an unprivileged user. Port 8080 is >1024, so no capabilities are required;
# /metrics and the rate limiter are in-process state, so one container = one brain.
RUN groupadd --system --gid 10001 ssave \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin ssave \
    && chown -R ssave:ssave /srv/ssavevideo
USER ssave:ssave

EXPOSE 8080

# Uvicorn's own worker pool. Keep this at 1–2 per CPU: each worker duplicates the
# in-process rate limiter and cache, so scaling workers scales those limits too
# (see README → "Deployment notes"). Prefer scaling with one worker per core and a
# shared edge cache in front.
ENV WEB_CONCURRENCY=2 \
    PORT=8080

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port \"${PORT}\" --workers \"${WEB_CONCURRENCY}\" --log-level info --no-server-header"]

# `/healthz` is dependency-free on purpose: it answers from process memory, so this
# check fails only when the app is genuinely stuck — not when YouTube is slow.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null || exit 1
