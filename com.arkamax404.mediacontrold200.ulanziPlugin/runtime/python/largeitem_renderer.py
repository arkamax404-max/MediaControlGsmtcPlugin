from __future__ import annotations

import base64
import math
import re
import unicodedata
from dataclasses import dataclass
from html import escape


WIDTH = 458
HEIGHT = 196
_COLOR_PATTERN = re.compile(r"^#[0-9A-F]{6}$")
_PNG_URI_PATTERN = re.compile(r"^data:image/png;base64,[A-Za-z0-9+/]+={0,2}$")
_STATUS_LABELS = {
    "offline": "Media service offline",
    "no_session": "Nothing playing",
    "incompatible": "Update required",
    "configuration": "Setup required",
}


@dataclass(frozen=True)
class LargeItemSettings:
    show_artwork: bool = True
    paused_artwork: str = "grayscale"
    show_progress: bool = True
    show_elapsed: bool = False
    show_remaining: bool = True
    background_color: str = "#0B0D10"
    primary_color: str = "#FFFFFF"
    secondary_color: str = "#B8BEC8"
    accent_color: str = "#1DB954"
    fit: str = "contain"
    small_view_mode: int = 2


@dataclass(frozen=True)
class LargeItemView:
    status: str
    title: str
    artist: str
    is_playing: bool
    artwork_data_uri: str | None
    progress_ratio: float | None
    elapsed_text: str
    remaining_text: str
    settings: LargeItemSettings


def svg_data_uri(svg: str) -> str:
    return "data:image/svg+xml;base64," + base64.b64encode(
        svg.encode("utf-8", "replace")
    ).decode("ascii")


def render_largeitem(view: LargeItemView) -> str:
    settings = view.settings if isinstance(view.settings, LargeItemSettings) else LargeItemSettings()
    background = _color(settings.background_color, "#0B0D10")
    primary = _color(settings.primary_color, "#FFFFFF")
    secondary = _color(settings.secondary_color, "#B8BEC8")
    accent = _color(settings.accent_color, "#1DB954")
    status = view.status if view.status in {"ready", *_STATUS_LABELS} else "offline"
    show_artwork = settings.show_artwork and status == "ready"
    text_x = 204 if show_artwork else 16
    text_width = 246 if show_artwork else 426
    title_width = text_width - 42 if status == "ready" else text_width
    title = _safe_text(view.title) or "Unknown track"
    artist = _safe_text(view.artist) or "Unknown artist"
    if status != "ready":
        title = _STATUS_LABELS[status]
        artist = "Media Control for D200"
    title_lines = _wrap(title, title_width, 25, 2)
    artist_line = _wrap(artist, text_width, 22, 1)[0]
    artwork = _artwork(view.artwork_data_uri, settings.fit) if show_artwork else ""
    if show_artwork and not artwork:
        artwork = _fallback_artwork(background, secondary)
    playback = _playback_glyph(view.is_playing, accent) if status == "ready" else ""
    progress = _progress(view, text_x, text_width, secondary, accent)
    title_nodes = "".join(
        f'<text x="{text_x}" y="{54 + index * 30}" fill="{primary}" '
        f'font-family="Arial, sans-serif" font-size="25" font-weight="700"'
        f'{_text_constraint(line, title_width, 25)}>'
        f'{_xml(line)}</text>'
        for index, line in enumerate(title_lines)
    )
    artist_y = 118 if len(title_lines) == 2 else 94
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
        f'viewBox="0 0 {WIDTH} {HEIGHT}">'
        f'<rect width="{WIDTH}" height="{HEIGHT}" fill="{background}"/>'
        f'{artwork}{playback}{title_nodes}'
        f'<text x="{text_x}" y="{artist_y}" fill="{secondary}" '
        f'font-family="Arial, sans-serif" font-size="22"'
        f'{_text_constraint(artist_line, text_width, 22)}>{_xml(artist_line)}</text>'
        f'{progress}</svg>'
    )


def render_largeitem_data_uri(view: LargeItemView) -> str:
    return svg_data_uri(render_largeitem(view))


def _artwork(value: object, fit: object) -> str:
    if not isinstance(value, str) or not _PNG_URI_PATTERN.fullmatch(value):
        return ""
    mode = "slice" if fit == "cover" else "meet"
    return (
        '<image x="8" y="8" width="180" height="180" '
        f'preserveAspectRatio="xMidYMid {mode}" href="{value}"/>'
    )


def _fallback_artwork(background: str, secondary: str) -> str:
    return (
        f'<rect x="8" y="8" width="180" height="180" rx="12" fill="{background}" '
        f'stroke="{secondary}" stroke-width="2"/>'
        f'<path d="M70 70v58c0 13-22 13-22 0s22-13 22 0V82l58-12v46c0 13-22 13-22 0s22-13 22 0V58z" '
        f'fill="{secondary}"/>'
    )


def _playback_glyph(playing: bool, accent: str) -> str:
    glyph = '<path d="M424 19l14 9-14 9z" fill="#FFFFFF"/>' if playing else (
        '<path d="M423 19h5v18h-5zm10 0h5v18h-5z" fill="#FFFFFF"/>'
    )
    return f'<circle cx="430" cy="28" r="18" fill="{accent}"/>{glyph}'


def _progress(view: LargeItemView, x: int, width: int, secondary: str, accent: str) -> str:
    if not view.settings.show_progress or view.status != "ready":
        return ""
    ratio = view.progress_ratio
    if not isinstance(ratio, (int, float)) or isinstance(ratio, bool) or not math.isfinite(ratio):
        ratio = 0.0
    ratio = max(0.0, min(1.0, float(ratio)))
    filled = _fixed(width * ratio)
    labels = []
    if view.settings.show_elapsed:
        labels.append((x, "start", _safe_text(view.elapsed_text)))
    if view.settings.show_remaining:
        labels.append((x + width, "end", _safe_text(view.remaining_text)))
    times = "".join(
        f'<text x="{position}" y="158" fill="{secondary}" font-family="Arial, sans-serif" '
        f'font-size="13" text-anchor="{anchor}">{_xml(label)}</text>'
        for position, anchor, label in labels if label
    )
    return (
        f'{times}<rect x="{x}" y="170" width="{width}" height="8" rx="4" fill="{secondary}" opacity="0.35"/>'
        f'<rect x="{x}" y="170" width="{filled}" height="8" rx="4" fill="{accent}"/>'
    )


def _safe_text(value: object) -> str:
    raw = str(value or "")
    text = unicodedata.normalize(
        "NFC",
        "".join("�" if 0xD800 <= ord(character) <= 0xDFFF else character
                for character in raw),
    )
    return " ".join(text.replace("\x00", "").split())[:192]


def _wrap(value: str, width: int, font_size: int, lines: int) -> tuple[str, ...]:
    capacity = max(1, int(width / (font_size * 0.56)))
    remaining = value
    output = []
    for index in range(lines):
        if len(remaining) <= capacity:
            output.append(remaining)
            break
        cut = remaining.rfind(" ", 0, capacity + 1)
        if cut < capacity // 2:
            cut = capacity
        output.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
        if index == lines - 1 and remaining:
            output[-1] = output[-1][:-1].rstrip() + "…"
    return tuple(output or ("",))


def _color(value: object, fallback: str) -> str:
    candidate = str(value).upper()
    return candidate if _COLOR_PATTERN.fullmatch(candidate) else fallback


def _text_constraint(value: str, width: int, font_size: int) -> str:
    estimated = sum(
        0 if unicodedata.combining(character) else
        1 if unicodedata.east_asian_width(character) in ("W", "F") else
        .9 if character in "WM@%&" else
        .3 if character in "ilI.,:;|'" else .56
        for character in value
    ) * font_size
    return (f' textLength="{width}" lengthAdjust="spacingAndGlyphs"'
            if estimated > width else "")


def _xml(value: str) -> str:
    return escape(value, quote=True).replace("&#x27;", "&apos;")


def _fixed(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")
