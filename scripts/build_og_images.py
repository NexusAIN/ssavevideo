#!/usr/bin/env python3
"""
Generate the Open Graph / Twitter preview cards for every pSEO page.

Why a build step instead of a runtime endpoint: an OG image is requested by
crawlers and messengers (often with bursts of 20+ identical fetches when a link
goes viral), so rendering a PNG on demand would put image work on the hot path
of a box that is supposed to do none. Pre-rendering makes it a static file the
CDN serves for free, and it keeps the runtime dependency list at "fastapi +
yt-dlp".

The colors, brand names and taglines all come from data/seo_platforms.json, so
the card can never drift from the page it previews. Requires Pillow + the venv:
    python3 scripts/build_og_images.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - build tooling only
    sys.exit("Pillow is required for the asset build: pip install pillow")

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "data" / "seo_platforms.json"
OUT_DIR = ROOT / "app" / "static" / "img"
WIDTH, HEIGHT = 1200, 630
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)


def font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    candidates = list(FONT_CANDIDATES) if bold else list(FONT_CANDIDATES[1:]) + list(FONT_CANDIDATES)
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def mix(a: str, b: str, t: float) -> str:
    """Blend two hex colors — Pillow's RGB canvas has no alpha, so 'transparency'
    in this file always means 'mixed toward the background colour'."""
    ca, cb = hex_to_rgb(a), hex_to_rgb(b)
    return "#%02x%02x%02x" % tuple(round(ca[i] + (cb[i] - ca[i]) * t) for i in range(3))


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def vertical_gradient(top: str, bottom: str) -> Image.Image:
    """Linear top->bottom blend, drawn as 1px rows (fast, no numpy dependency)."""
    start, stop = hex_to_rgb(top), hex_to_rgb(bottom)
    img = Image.new("RGB", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(img)
    for y in range(HEIGHT):
        t = y / (HEIGHT - 1)
        color = tuple(round(start[i] + (stop[i] - start[i]) * t) for i in range(3))
        draw.line([(0, y), (WIDTH, y)], fill=color)
    return img


def wrap(draw: ImageDraw.ImageDraw, text: str, font_obj, max_width: int, max_lines: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font_obj) <= max_width or not current:
            current = trial
            continue
        lines.append(current)
        current = word
        if len(lines) == max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) == max_lines and draw.textlength(lines[-1], font=font_obj) > max_width:
        lines[-1] = lines[-1][: max(0, len(lines[-1]) - 4)].rstrip() + "…"
    return lines


def pill(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, fill: str, text_fill: str, fnt) -> int:
    """Rounded tag. Returns the advance width so callers can lay out a row."""
    x, y = xy
    pad_x, pad_y = 24, 12
    width = int(draw.textlength(text, font=fnt)) + pad_x * 2
    height = fnt.size + pad_y * 2
    draw.rounded_rectangle((x, y, x + width, y + height), radius=height // 2, fill=fill)
    draw.text((x + pad_x, y + pad_y - 3), text, font=fnt, fill=text_fill)
    return width + 14


def card(
    *,
    eyebrow: str,
    headline: str,
    subline: str,
    colors: tuple[str, str],
    on_dark: bool,
    pills: list[str],
    icon_glyph: str = "",
) -> Image.Image:
    """
    One 1200x630 preview card.

    Contrast is computed, not guessed: `on_dark` says whether the gradient needs
    white ink. Chips and the eyebrow pill are always the *opposite* pair
    (solid background + dark or light label) so text can never end up the same
    colour as its own badge — the failure mode of a hand-tuned card.
    """
    img = vertical_gradient(colors[0], colors[1])
    draw = ImageDraw.Draw(img)
    ink = "#ffffff" if on_dark else "#0f172a"
    muted = "#cbd5e1" if on_dark else "#475569"
    chip_fill = "#ffffff" if on_dark else "#0f172a"
    chip_ink = "#0f172a" if on_dark else "#ffffff"
    soft = mix(colors[1], "#ffffff", 0.26) if on_dark else mix(colors[1], "#0f172a", 0.14)

    # Depth rings: cheap, deterministic, and they read as craft in a chat preview.
    for cx, cy, r in ((1090, 70, 300), (980, 620, 210), (60, 560, 150)):
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=soft, width=3)

    margin = 72
    y = 64

    # Eyebrow: "SsaveVideo · TikTok"
    eyebrow_font = font(27)
    label = f"{eyebrow}{('  ·  ' + icon_glyph) if icon_glyph else ''}"
    chip_w = int(draw.textlength(label, font=eyebrow_font)) + 46
    draw.rounded_rectangle((margin, y, margin + chip_w, y + 54), radius=27, fill=chip_fill)
    draw.text((margin + 23, y + 13), label, font=eyebrow_font, fill=chip_ink)
    y += 92

    # Headline: up to 3 wrapped lines, auto-shrunk so it can never be clipped.
    size = 78
    while size > 52:
        lines = wrap(draw, headline, font(size), WIDTH - margin * 2, 3)
        if len(lines) <= 3 and all(draw.textlength(line, font=font(size)) <= WIDTH - margin * 2 for line in lines):
            break
        size -= 4
    for line in lines:
        draw.text((margin, y), line, font=font(size), fill=ink)
        y += int(size * 1.12)
    y += 14

    sub_font = font(29)
    for line in wrap(draw, subline, sub_font, WIDTH - margin * 2 - 40, 3):
        draw.text((margin, y), line, font=sub_font, fill=muted)
        y += 42

    # Bottom row of benefit chips.
    x = margin
    py = HEIGHT - 168
    for label in pills[:3]:
        x += pill(draw, (x, py), label, chip_fill, chip_ink, font(23))

    # Domain strip: what a sharing human should trust.
    draw.rectangle((0, HEIGHT - 76, WIDTH, HEIGHT), fill="#000000" if on_dark else "#ffffff")
    domain_font = font(26)
    draw.text((margin, HEIGHT - 58), "ssavevideo.com", font=domain_font, fill="#ffffff" if on_dark else "#0f172a")
    url_w = draw.textlength("ssavevideo.com", font=domain_font)
    draw.text(
        (margin + url_w + 28, HEIGHT - 56),
        "free  ·  no signup  ·  no watermark  ·  no app",
        font=font(23),
        fill="#94a3b8" if on_dark else "#64748b",
    )
    return img


def main() -> int:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    site = schema["site"]
    written: list[Path] = []

    # Default / home card, reused by legal and error pages.
    home = card(
        eyebrow=site["brand"],
        icon_glyph="Video Downloader",
        headline=site["h1"],
        subline=site["hero_subtext"],
        colors=("#4f46e5", "#c026d3"),
        on_dark=True,
        pills=["10 platforms", "Up to 4K MP4", "Audio extraction", "Nothing stored"],
    )
    path = OUT_DIR / "og-default.png"
    home.save(path, optimize=True)
    written.append(path)

    for platform in schema["platforms"]:
        colors = tuple(platform.get("gradient_hex") or ("#4f46e5", "#c026d3"))
        # Bright brand gradients (cyan, amber) need dark ink to stay readable;
        # relative luminance decides it rather than a hand-maintained flag.
        luminance = 0.0
        for value in (colors[0], colors[1]):
            r, g, b = hex_to_rgb(value)
            luminance += 0.2126 * r + 0.7152 * g + 0.0722 * b
        on_dark = (luminance / 2) < 150
        image = card(
            eyebrow=site["brand"],
            icon_glyph=platform["name"],
            headline=platform["h1"].split(" — ")[0] if len(platform["h1"]) > 40 else platform["h1"],
            subline=platform["hero_subtext"],
            colors=(colors[0], colors[1]),
            on_dark=on_dark,
            pills=list(platform.get("badges") or [])[:3],
        )
        target = OUT_DIR / f"og-{platform['key']}.png"
        image.save(target, optimize=True)
        written.append(target)

    for guide in schema.get("guides", []):
        image = card(
            eyebrow=site["brand"],
            icon_glyph="Guide",
            headline=guide["h1"],
            subline=guide["meta_description"],
            colors=("#0f172a", "#4f46e5"),
            on_dark=True,
            pills=["How-to", "No fluff", "Updated 2026"],
        )
        target = OUT_DIR / f"og-{guide['key']}.png"
        image.save(target, optimize=True)
        written.append(target)

    for path in written:
        print(f"  {path.relative_to(ROOT)}  {path.stat().st_size // 1024} KB")
    print(f"generated {len(written)} preview cards")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
