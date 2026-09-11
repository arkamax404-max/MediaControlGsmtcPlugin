from __future__ import annotations

import base64
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from html import escape
from math import isfinite, isnan
from typing import Callable

from artwork_bundle import ARTWORK_ID_PATTERN, ArtworkBundle
from progress_state import ProgressState, extrapolate_position


ACTION_UUID = "com.arkamax404.ulanzi.mediacontrol.nowplaying"
MUTE_TOGGLE_UUID = "com.arkamax404.ulanzi.mediacontrol.mute-toggle"
DEFAULT_AUDIO_TARGET = "process:spotify.exe"
DEFAULT_AUDIO_ICON_COLOR = "#1DB954"
DEFAULT_BADGE_COLOR = "#1DB954"
DEFAULT_NOW_PLAYING_ACCENT_COLOR = "#1DB954"
_COLOR = re.compile(r"#[0-9A-Fa-f]{6}")
_PNG_DATA_URI = re.compile(r"data:image/png;base64,[A-Za-z0-9+/]+={0,2}")
MOSAIC_ACTIONS = {
    "com.arkamax404.ulanzi.mediacontrol.artwork-top-left":
        (0, "./assets/artwork-top-left.svg", "Artwork Top Left"),
    "com.arkamax404.ulanzi.mediacontrol.artwork-top-right":
        (1, "./assets/artwork-top-right.svg", "Artwork Top Right"),
    "com.arkamax404.ulanzi.mediacontrol.artwork-bottom-left":
        (2, "./assets/artwork-bottom-left.svg", "Artwork Bottom Left"),
    "com.arkamax404.ulanzi.mediacontrol.artwork-bottom-right":
        (3, "./assets/artwork-bottom-right.svg", "Artwork Bottom Right"),
}
SECONDARY_ACTIONS = frozenset(("none", "previous", "toggle", "next",
                               "volume-up", "volume-down", "mute-toggle"))
AUDIO_ACTIONS = {
    "com.arkamax404.ulanzi.mediacontrol.volume-up": "./assets/volume-up.svg",
    "com.arkamax404.ulanzi.mediacontrol.volume-down": "./assets/volume-down.svg",
    MUTE_TOGGLE_UUID: "./assets/mute.svg",
}
TOGGLE_UUID = "com.arkamax404.ulanzi.mediacontrol.toggle"
PREVIOUS_UUID = "com.arkamax404.ulanzi.mediacontrol.previous"
NEXT_UUID = "com.arkamax404.ulanzi.mediacontrol.next"
TRANSPORT_DISPLAY = {
    TOGGLE_UUID: "./assets/play.svg",
    PREVIOUS_UUID: "./assets/previous.svg",
    NEXT_UUID: "./assets/next.svg",
}
COLOR_ACTIONS = frozenset((*AUDIO_ACTIONS, *TRANSPORT_DISPLAY))
DISPLAY_ACTION_UUIDS = frozenset((ACTION_UUID, *MOSAIC_ACTIONS, *AUDIO_ACTIONS,
                                  *TRANSPORT_DISPLAY))
STATE_MAX_AGE_SECONDS = 15
TEXT_LIMIT = 48
OFFLINE_ICON = "./assets/offline.svg"
MUSIC_ICON = "./assets/music.svg"
PLAY_ICON = "./assets/play.svg"
PAUSE_ICON = "./assets/pause.svg"
JS_TRIM_CHARACTERS = frozenset(
    "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000\ufeff"
)
STATUS_LABELS = {
    "configuration": "Companion setup required",
    "incompatible": "Incompatible companion",
    "offline": "Offline",
    "no_session": "Offline",
}


@dataclass(frozen=True)
class AudioSource:
    target: str
    label: str
    volume_percent: int
    is_muted: bool
    session_count: int
    mixed: bool


@dataclass(frozen=True)
class MediaSnapshot:
    online: bool
    available: bool
    is_playing: bool
    title: str
    artist: str
    artwork_id: str | None
    status: str
    audio_available: bool = False
    volume_percent: int | None = None
    is_muted: bool = False
    audio_mixed: bool = False
    audio_sources: tuple[AudioSource, ...] = ()


@dataclass(frozen=True)
class RenderRequest:
    context: str
    generation: int
    version: int


@dataclass(frozen=True)
class RenderIntent:
    context: str
    generation: int
    version: int
    method: str
    image: str
    text: str
    signature: tuple[str, str, str]


@dataclass(frozen=True)
class ContextView:
    context: str
    generation: int
    version: int
    active: bool
    action: str
    audio_target: str
    icon_color: str
    show_progress: bool
    accent_color: str
    secondary_action: str
    badge_color: str


@dataclass
class _Context:
    generation: int
    action: str
    version: int = 1
    active: bool = True
    committed_signature: tuple[str, str, str] | None = None
    audio_target: str = DEFAULT_AUDIO_TARGET
    icon_color: str = DEFAULT_AUDIO_ICON_COLOR
    show_progress: bool = True
    accent_color: str = DEFAULT_NOW_PLAYING_ACCENT_COLOR
    secondary_action: str = "none"
    badge_color: str = DEFAULT_BADGE_COLOR


def unavailable_media_snapshot(reason: str = "unavailable") -> MediaSnapshot:
    status = reason if reason in ("configuration", "incompatible") else "offline"
    return MediaSnapshot(False, False, False, "", "", None, status,
                         False, None, False, False, ())


def normalize_media_snapshot(
    payload: object,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> MediaSnapshot:
    if not isinstance(payload, Mapping):
        return unavailable_media_snapshot()
    try:
        updated_at = _timestamp(payload.get("updated_at"))
        now = _utc(clock())
        if updated_at is None or now is None:
            return unavailable_media_snapshot()
        age = (now - updated_at).total_seconds()
        if age < 0 or age > STATE_MAX_AGE_SECONDS:
            return unavailable_media_snapshot()
        available = payload.get("available") is True
        is_playing = available and payload.get("is_playing") is True
        title = _text(payload.get("title"))
        artist = _text(payload.get("artist"))
        candidate = payload.get("artwork_id")
        audio_available = payload.get("audio_available") is True
        volume_candidate = payload.get("volume_percent")
        is_muted = payload.get("is_muted") is True
        audio_mixed = payload.get("audio_mixed") is True
        audio_sources = _normalize_audio_sources(payload.get("audio_sources"))
    except Exception:
        return unavailable_media_snapshot()
    artwork_id = candidate if isinstance(candidate, str) and ARTWORK_ID_PATTERN.fullmatch(candidate) else None
    return MediaSnapshot(True, available, is_playing,
                         title, artist, artwork_id, "ready" if available else "no_session",
                         audio_available, _volume_percent(volume_candidate),
                         is_muted, audio_mixed, audio_sources)


def now_playing_text(snapshot: MediaSnapshot) -> str:
    values = tuple(value for value in (_text(snapshot.title), _text(snapshot.artist)) if value)
    return "\n".join(values) or "Playing"


def _audio_state_label(snapshot: MediaSnapshot) -> str:
    if not snapshot.audio_available:
        return "No audio"
    return ("Mixed" if snapshot.audio_mixed else "Muted" if snapshot.is_muted
            else f"{snapshot.volume_percent}%"
            if snapshot.volume_percent is not None else "null%")


def render_audio_icon_svg(action: str, color: str = DEFAULT_AUDIO_ICON_COLOR) -> str:
    color = normalize_audio_icon_color(color)
    detail = ('M59 38a18 18 0 0 1 0 24M78 40v20M68 50h20'
              if action.endswith("volume-up")
              else 'M59 38a18 18 0 0 1 0 24M68 50h20')
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="196" height="196" '
            'viewBox="0 0 100 100"><rect width="100" height="100" rx="18" fill="#121212"/>'
            f'<path fill="{color}" d="M18 42h14l18-15v46L32 58H18z"/>'
            f'<path fill="none" stroke="{color}" stroke-width="7" stroke-linecap="round" '
            f'd="{detail}"/></svg>')


def audio_icon_data_uri(action: str, color: str = DEFAULT_AUDIO_ICON_COLOR) -> str:
    svg = render_audio_icon_svg(action, color)
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


def render_mute_toggle_svg(label: str, waves: bool,
                           color: str = DEFAULT_AUDIO_ICON_COLOR) -> str:
    color = normalize_audio_icon_color(color)
    glyph = (f'<path fill="none" stroke="{color}" stroke-width="7" stroke-linecap="round" '
             'd="M61 37a19 19 0 0 1 0 26M72 27a33 33 0 0 1 0 46"/>' if waves else
             f'<path fill="none" stroke="{color}" stroke-width="8" stroke-linecap="round" '
             'd="m64 39 22 22m0-22L64 61"/>')
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="196" height="196" viewBox="0 0 196 196">'
            '<rect width="196" height="196" rx="35.28" fill="#121212"/>'
            f'<text x="98" y="38" fill="#ffffff" font-family="Arial, sans-serif" font-size="38" '
            f'font-weight="700" text-anchor="middle">{escape(label, quote=True)}</text>'
            '<g transform="translate(-5 -2) scale(2)">'
            f'<path fill="{color}" d="M17 42h15l19-16v48L32 58H17z"/>{glyph}</g></svg>')


def mute_toggle_data_uri(label: str, waves: bool,
                         color: str = DEFAULT_AUDIO_ICON_COLOR) -> str:
    svg = render_mute_toggle_svg(label, waves, color)
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


def render_transport_icon_svg(action: str, playing: bool = False,
                              color: str = DEFAULT_AUDIO_ICON_COLOR) -> str:
    color = normalize_audio_icon_color(color)
    if action == TOGGLE_UUID:
        path = "M29 24h15v52H29zm27 0h15v52H56z" if playing else "m34 24 45 26-45 26z"
    elif action == PREVIOUS_UUID:
        path = "M25 25h9v50h-9zm11 25 39-25v50z"
    else:
        path = "m25 25 39 25-39 25zm41 0h9v50h-9z"
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="196" height="196" '
            'viewBox="0 0 100 100"><rect width="100" height="100" rx="18" fill="#121212"/>'
            f'<path fill="{color}" d="{path}"/></svg>')


def transport_icon_data_uri(action: str, playing: bool = False,
                            color: str = DEFAULT_AUDIO_ICON_COLOR) -> str:
    svg = render_transport_icon_svg(action, playing, color)
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


def render_artwork_tile_svg(artwork: str, tile_action: str, secondary_action: str,
                            playing: bool = False, muted: bool = False,
                            badge_color: str = DEFAULT_BADGE_COLOR) -> str:
    if (not isinstance(artwork, str) or not _PNG_DATA_URI.fullmatch(artwork)
            or tile_action not in MOSAIC_ACTIONS
            or normalize_secondary_action(secondary_action) == "none"):
        return ""
    secondary_action = normalize_secondary_action(secondary_action)
    badge_color = normalize_badge_color(badge_color)
    left = "left" in tile_action
    top = "top" in tile_action
    cx, cy = (22 if left else 174), (22 if top else 174)
    if secondary_action == "toggle":
        path = ("M29 24h15v52H29zm27 0h15v52H56z" if playing
                else "m34 24 45 26-45 26z")
        glyph = f'<path fill="#FFFFFF" d="{path}"/>'
    elif secondary_action == "previous":
        glyph = '<path fill="#FFFFFF" d="M25 25h9v50h-9zm11 25 39-25v50z"/>'
    elif secondary_action == "next":
        glyph = '<path fill="#FFFFFF" d="m25 25 39 25-39 25zm41 0h9v50h-9z"/>'
    else:
        detail = ("M59 38a18 18 0 0 1 0 24M78 40v20M68 50h20"
                  if secondary_action == "volume-up" else
                  "M59 38a18 18 0 0 1 0 24M68 50h20"
                  if secondary_action == "volume-down" else
                  "M61 37a19 19 0 0 1 0 26M72 27a33 33 0 0 1 0 46"
                  if muted else "m64 39 22 22m0-22L64 61")
        glyph = ('<path fill="#FFFFFF" d="M18 42h14l18-15v46L32 58H18z"/>'
                 f'<path fill="none" stroke="#FFFFFF" stroke-width="7" '
                 f'stroke-linecap="round" d="{detail}"/>')
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="196" height="196" '
            'viewBox="0 0 196 196">'
            f'<image width="196" height="196" href="{artwork}"/>'
            f'<circle cx="{cx}" cy="{cy}" r="18" fill="{badge_color}"/>'
            f'<g transform="translate({cx - 15} {cy - 15}) scale(0.3)">{glyph}</g></svg>')


def artwork_tile_data_uri(artwork: str, tile_action: str, secondary_action: str,
                          playing: bool = False, muted: bool = False,
                          badge_color: str = DEFAULT_BADGE_COLOR) -> str:
    if normalize_secondary_action(secondary_action) == "none":
        return artwork if isinstance(artwork, str) and _PNG_DATA_URI.fullmatch(artwork) else ""
    svg = render_artwork_tile_svg(
        artwork, tile_action, secondary_action, playing, muted, badge_color)
    return ("data:image/svg+xml;base64,"
            + base64.b64encode(svg.encode("utf-8")).decode("ascii")) if svg else ""


def render_now_playing_artwork_svg(artwork: str, playing: bool,
                                   progress: ProgressState | None = None,
                                   clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                                   show_progress: bool = True,
                                   accent_color: str = DEFAULT_NOW_PLAYING_ACCENT_COLOR) -> str:
    if not isinstance(artwork, str) or not _PNG_DATA_URI.fullmatch(artwork):
        return ""
    accent_color = normalize_now_playing_accent_color(accent_color)
    glyph = ('<path d="M162 19l14 9-14 9z" fill="#FFFFFF"/>' if playing else
             '<path d="M161 19h5v18h-5zm10 0h5v18h-5z" fill="#FFFFFF"/>')
    progress_bar = ""
    if show_progress and isinstance(progress, ProgressState) and progress.timeline_available:
        ratio = (extrapolate_position(progress, clock) / progress.duration_seconds
                 if progress.duration_seconds > 0 else 0.0)
        width = max(0.0, min(196.0, 196.0 * ratio))
        progress_bar = (
            '<rect x="0" y="189" width="196" height="7" '
            'fill="#121212" opacity="0.72"/>'
            f'<rect x="0" y="189" width="{width:.3f}" height="7" '
            f'fill="{accent_color}"/>'
        )
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="196" height="196" '
            'viewBox="0 0 196 196">'
            f'<image width="196" height="196" href="{artwork}"/>'
            f'<circle cx="168" cy="28" r="18" fill="{accent_color}"/>{glyph}{progress_bar}</svg>')


def now_playing_artwork_data_uri(artwork: str, playing: bool,
                                 progress: ProgressState | None = None,
                                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                                 show_progress: bool = True,
                                 accent_color: str = DEFAULT_NOW_PLAYING_ACCENT_COLOR) -> str:
    svg = render_now_playing_artwork_svg(
        artwork, playing, progress, clock, show_progress, accent_color)
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


class NowPlayingActionModel:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._contexts: dict[str, _Context] = {}
        self._next_generation = 0
        self._shutdown = False

    def context(self, context: str) -> ContextView | None:
        context = _identity(context)
        if context is None:
            return None
        with self._lock:
            entry = self._contexts.get(context)
            return (ContextView(context, entry.generation, entry.version, entry.active,
                                 entry.action, entry.audio_target, entry.icon_color,
                                 entry.show_progress, entry.accent_color,
                                 entry.secondary_action, entry.badge_color)
                    if entry else None)

    def add(self, event: object) -> tuple[RenderRequest, ...]:
        action, context = _event_identity(event)
        if action not in DISPLAY_ACTION_UUIDS or not context:
            return ()
        raw = event.get("param") if isinstance(event, Mapping) else None
        target = (normalize_audio_target(raw.get("audioTarget"))
                  if action in AUDIO_ACTIONS and isinstance(raw, Mapping) else None)
        icon_color = (normalize_audio_icon_color(raw.get("iconColor"))
                      if action in COLOR_ACTIONS and isinstance(raw, Mapping)
                      else DEFAULT_AUDIO_ICON_COLOR)
        show_progress = (raw.get("showProgress") is not False
                         if action == ACTION_UUID and isinstance(raw, Mapping) else True)
        accent_color = (normalize_now_playing_accent_color(raw.get("accentColor"))
                        if action == ACTION_UUID and isinstance(raw, Mapping)
                        else DEFAULT_NOW_PLAYING_ACCENT_COLOR)
        secondary_action = (normalize_secondary_action(raw.get("secondaryAction"))
                            if action in MOSAIC_ACTIONS and isinstance(raw, Mapping)
                            else "none")
        badge_color = (normalize_badge_color(raw.get("badgeColor"))
                       if action in MOSAIC_ACTIONS and isinstance(raw, Mapping)
                       else DEFAULT_BADGE_COLOR)
        if action in MOSAIC_ACTIONS and isinstance(raw, Mapping):
            target = normalize_audio_target(raw.get("audioTarget"))
        with self._lock:
            if self._shutdown:
                return ()
            self._next_generation += 1
            entry = _Context(self._next_generation, action,
                             audio_target=target or DEFAULT_AUDIO_TARGET,
                             icon_color=icon_color, show_progress=show_progress,
                             accent_color=accent_color,
                             secondary_action=secondary_action, badge_color=badge_color)
            self._contexts[context] = entry
            return (self._request(context, entry),)

    def clear(self, event: object) -> bool:
        items = event.get("param", ()) if isinstance(event, Mapping) else ()
        try:
            snapshot = tuple(items)
            contexts = tuple(
                context for item in snapshot if isinstance(item, Mapping)
                if (context := _identity(item.get("context"))) is not None
            )
        except Exception:
            contexts = ()
        with self._lock:
            changed = False
            for context in contexts:
                changed |= self._contexts.pop(context, None) is not None
            return changed

    def set_active(self, event: object) -> tuple[RenderRequest, ...]:
        if not isinstance(event, Mapping):
            return ()
        try:
            context = _identity(event.get("context"))
            active = event.get("active") is not False
        except Exception:
            return ()
        if context is None:
            return ()
        with self._lock:
            entry = self._contexts.get(context)
            if self._shutdown or entry is None:
                return ()
            entry.active = active
            entry.version += 1
            entry.committed_signature = None
            return (self._request(context, entry),) if active else ()

    def requests(self) -> tuple[RenderRequest, ...]:
        with self._lock:
            return tuple(self._request(context, entry) for context, entry in self._contexts.items()
                         if entry.active)

    def receive_settings(self, event: object) -> tuple[RenderRequest, ...]:
        if not isinstance(event, Mapping):
            return ()
        context = _identity(event.get("context"))
        raw = event.get("settings", event.get("param"))
        if context is None or not isinstance(raw, Mapping):
            return ()
        icon_color = normalize_audio_icon_color(raw.get("iconColor"))
        with self._lock:
            entry = self._contexts.get(context)
            if (self._shutdown or entry is None
                    or entry.action not in COLOR_ACTIONS and entry.action != ACTION_UUID
                    and entry.action not in MOSAIC_ACTIONS):
                return ()
            if entry.action == ACTION_UUID:
                entry.show_progress = raw.get("showProgress") is not False
                entry.accent_color = normalize_now_playing_accent_color(raw.get("accentColor"))
            elif entry.action in MOSAIC_ACTIONS:
                entry.secondary_action = normalize_secondary_action(raw.get("secondaryAction"))
                entry.audio_target = normalize_audio_target(
                    raw.get("audioTarget")) or DEFAULT_AUDIO_TARGET
                entry.badge_color = normalize_badge_color(raw.get("badgeColor"))
            elif entry.action in AUDIO_ACTIONS:
                entry.audio_target = normalize_audio_target(
                    raw.get("audioTarget")) or DEFAULT_AUDIO_TARGET
            if entry.action in COLOR_ACTIONS:
                entry.icon_color = icon_color
            entry.version += 1
            entry.committed_signature = None
            return (self._request(context, entry),) if entry.active else ()

    def audio_target_from_event(self, event: object) -> str | None:
        context = _identity(event.get("context")) if isinstance(event, Mapping) else None
        if context is None:
            return None
        with self._lock:
            entry = self._contexts.get(context)
            return (entry.audio_target if entry and (entry.action in AUDIO_ACTIONS
                    or entry.action in MOSAIC_ACTIONS
                    and entry.secondary_action in ("volume-up", "volume-down", "mute-toggle"))
                    else None)

    def secondary_command_from_event(self, event: object) -> str | None:
        context = _identity(event.get("context")) if isinstance(event, Mapping) else None
        if context is None:
            return None
        with self._lock:
            entry = self._contexts.get(context)
            return (entry.secondary_action if entry and entry.action in MOSAIC_ACTIONS
                    and entry.secondary_action != "none" else None)

    def audio_contexts(self) -> tuple[ContextView, ...]:
        with self._lock:
            return tuple(ContextView(context, entry.generation, entry.version, entry.active,
                                     entry.action, entry.audio_target, entry.icon_color,
                                     entry.show_progress, entry.accent_color,
                                     entry.secondary_action,
                                     entry.badge_color)
                         for context, entry in self._contexts.items()
                         if entry.action in AUDIO_ACTIONS)

    def artwork_progress_active(self) -> bool:
        with self._lock:
            return any(entry.active and entry.action == ACTION_UUID and entry.show_progress
                       for entry in self._contexts.values())

    def render(self, request: RenderRequest, snapshot: MediaSnapshot,
               bundle: ArtworkBundle | None = None, progress: ProgressState | None = None,
               clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) \
            -> RenderIntent | None:
        if not isinstance(request, RenderRequest) or not isinstance(snapshot, MediaSnapshot):
            return None
        request = _canonical_request(request)
        if request is None:
            return None
        with self._lock:
            entry = self._matching(request)
            action = entry.action if entry and entry.active else None
            audio_target = entry.audio_target if entry else DEFAULT_AUDIO_TARGET
            icon_color = entry.icon_color if entry else DEFAULT_AUDIO_ICON_COLOR
            show_progress = entry.show_progress if entry else True
            accent_color = (entry.accent_color if entry
                            else DEFAULT_NOW_PLAYING_ACCENT_COLOR)
            secondary_action = entry.secondary_action if entry else "none"
            badge_color = entry.badge_color if entry else DEFAULT_BADGE_COLOR
        if action is None:
            return None
        online, available = snapshot.online, snapshot.available
        playing, artwork_id = snapshot.is_playing, snapshot.artwork_id
        status, text = snapshot.status, _payload_text(now_playing_text(snapshot))
        matching = isinstance(bundle, ArtworkBundle) and bundle.artwork_id == artwork_id
        mosaic = MOSAIC_ACTIONS.get(action)
        audio = AUDIO_ACTIONS.get(action)
        transport = TRANSPORT_DISPLAY.get(action)
        if not online and (mosaic is not None or audio is not None
                           or transport is not None):
            method, image = "setPathIcon", OFFLINE_ICON
            text = STATUS_LABELS.get(status, "Offline")
        elif mosaic is not None:
            tile, fallback, text = mosaic
            if available and matching:
                muted = (_snapshot_for_audio_target(snapshot, audio_target).is_muted
                         if secondary_action == "mute-toggle" else False)
                method, image, text = "setBaseDataIcon", artwork_tile_data_uri(
                    bundle.tiles[tile], action, secondary_action, playing, muted,
                    badge_color), ""
            else:
                method, image = "setPathIcon", fallback
        elif audio is not None:
            snapshot = _snapshot_for_audio_target(snapshot, audio_target)
            if action == MUTE_TOGGLE_UUID:
                method = "setBaseDataIcon"
                image = mute_toggle_data_uri(_audio_state_label(snapshot),
                                             snapshot.audio_available and snapshot.is_muted,
                                             icon_color)
                text = ""
            else:
                method, image = "setBaseDataIcon", audio_icon_data_uri(action, icon_color)
                text = _audio_state_label(snapshot)
        elif transport is not None:
            if not available:
                method, image, text = "setPathIcon", OFFLINE_ICON, "Offline"
            elif action == TOGGLE_UUID:
                method = "setBaseDataIcon"
                image = transport_icon_data_uri(action, playing, icon_color)
                text = "Pause" if playing else "Play"
            else:
                method = "setBaseDataIcon"
                image = transport_icon_data_uri(action, False, icon_color)
                text = "Previous" if action == PREVIOUS_UUID else "Next"
        elif not online or not available:
            method, image = "setPathIcon", OFFLINE_ICON
            text = STATUS_LABELS.get(status, "Offline")
        elif matching:
            artwork = bundle.color if playing else bundle.grayscale
            method = "setBaseDataIcon"
            image = now_playing_artwork_data_uri(
                artwork, playing, progress, clock, show_progress=show_progress,
                accent_color=accent_color)
        else:
            method, image = "setPathIcon", MUSIC_ICON
        signature = (method, image, text)
        intent = RenderIntent(request.context, request.generation, request.version,
                              method, image, text, signature)
        with self._lock:
            entry = self._matching(request)
            return None if entry is None or not entry.active \
                or entry.committed_signature == signature else intent

    def reserve_send(self, intent: RenderIntent) -> bool:
        if not isinstance(intent, RenderIntent):
            return False
        intent = _canonical_intent(intent)
        if intent is None:
            return False
        with self._lock:
            entry = self._matching(intent)
            return bool(entry and entry.active and entry.committed_signature != intent.signature)

    def acknowledge(self, intent: RenderIntent, success: bool) -> bool:
        if not isinstance(intent, RenderIntent):
            return False
        intent = _canonical_intent(intent)
        if intent is None:
            return False
        try:
            success = bool(success)
        except Exception:
            return False
        with self._lock:
            entry = self._matching(intent)
            if entry is None or not entry.active:
                return False
            if success:
                entry.committed_signature = intent.signature
            return True

    def shutdown(self) -> None:
        with self._lock:
            self._shutdown = True
            self._contexts.clear()

    def _matching(self, request: RenderRequest) -> _Context | None:
        entry = self._contexts.get(request.context)
        return entry if entry and (entry.generation, entry.version) == \
            (request.generation, request.version) else None

    @staticmethod
    def _request(context: str, entry: _Context) -> RenderRequest:
        return RenderRequest(context, entry.generation, entry.version)


def _text(value: object) -> str:
    value = "" if not _js_truthy(value) else _js_string(value)
    start, end = 0, len(value)
    while start < end and value[start] in JS_TRIM_CHARACTERS:
        start += 1
    while end > start and value[end - 1] in JS_TRIM_CHARACTERS:
        end -= 1
    encoded = value[start:end].encode("utf-16-le", "surrogatepass")
    return encoded[:TEXT_LIMIT * 2].decode("utf-16-le", "surrogatepass")


def _identity(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str.__str__(str(value))
    except Exception:
        return None


def _payload_text(value: str) -> str:
    return "".join("\ufffd" if 0xD800 <= ord(character) <= 0xDFFF else character
                   for character in value)


def _event_identity(event: object) -> tuple[str | None, str | None]:
    if not isinstance(event, Mapping):
        return None, None
    try:
        context = _identity(event.get("context"))
        candidate = event.get("uuid")
        if not _js_truthy(candidate):
            candidate = event.get("action")
    except Exception:
        return None, None
    if not _js_truthy(candidate):
        candidate = None
    identity = _identity(candidate)
    if candidate is not None and identity is None:
        return None, context
    if identity is None and context is not None:
        identity = context.split("___", 1)[0]
    return identity, context


def _canonical_request(value: RenderRequest | RenderIntent) -> RenderRequest | None:
    try:
        context = _identity(value.context)
        generation = _integer(value.generation)
        version = _integer(value.version)
        return RenderRequest(context, generation, version) \
            if context is not None and generation is not None and version is not None else None
    except Exception:
        return None


def _canonical_intent(value: RenderIntent) -> RenderIntent | None:
    try:
        context = _identity(value.context)
        generation = _integer(value.generation)
        version = _integer(value.version)
        method = _identity(value.method)
        image = _identity(value.image)
        text = _identity(value.text)
        signature_value = value.signature
        if not isinstance(signature_value, (tuple, list)):
            return None
        signature_items = tuple(signature_value)
        signature = tuple(_identity(item) for item in signature_items)
    except Exception:
        return None
    if (context is None or generation is None or version is None
            or method is None or image is None or text is None
            or len(signature) != 3 or any(item is None for item in signature)
            or signature != (method, image, text)):
        return None
    return RenderIntent(context, generation, version, method, image, text, signature)


def _integer(value: object) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    try:
        return int.__int__(value)
    except Exception:
        return None


def _volume_percent(value: object) -> int | None:
    volume = _integer(value)
    return None if volume is None else max(0, min(100, volume))


def normalize_audio_target(value: object) -> str | None:
    if value == "system":
        return value
    if not isinstance(value, str) or not value.startswith("process:"):
        return None
    process = value[8:].strip().casefold()
    if (not process or len(process) > 128 or "/" in process or "\\" in process
            or any(ord(char) < 32 for char in process)):
        return None
    return "process:" + process


def normalize_secondary_action(value: object) -> str:
    return value if isinstance(value, str) and value in SECONDARY_ACTIONS else "none"


def normalize_audio_icon_color(value: object) -> str:
    return value.upper() if isinstance(value, str) and _COLOR.fullmatch(value) \
        else DEFAULT_AUDIO_ICON_COLOR


def normalize_badge_color(value: object) -> str:
    return value.upper() if isinstance(value, str) and _COLOR.fullmatch(value) \
        else DEFAULT_BADGE_COLOR


def normalize_now_playing_accent_color(value: object) -> str:
    return value.upper() if isinstance(value, str) and _COLOR.fullmatch(value) \
        else DEFAULT_NOW_PLAYING_ACCENT_COLOR


def _normalize_audio_sources(value: object) -> tuple[AudioSource, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    sources = []
    for item in value[:64]:
        if not isinstance(item, Mapping):
            continue
        target = normalize_audio_target(item.get("target"))
        label = _text(item.get("label"))
        volume = _volume_percent(item.get("volume_percent"))
        count = _integer(item.get("session_count"))
        if target is None or not label or volume is None or count is None or count < 0:
            continue
        sources.append(AudioSource(target, label, volume, item.get("is_muted") is True,
                                   count, item.get("mixed") is True))
    return tuple(sources)


def _snapshot_for_audio_target(snapshot: MediaSnapshot, target: str) -> MediaSnapshot:
    source = next((item for item in snapshot.audio_sources if item.target == target), None)
    if source is None:
        if not snapshot.audio_sources and target == DEFAULT_AUDIO_TARGET:
            return snapshot
        return replace(snapshot, audio_available=False, volume_percent=None,
                       is_muted=False, audio_mixed=False)
    return replace(snapshot, audio_available=True, volume_percent=source.volume_percent,
                   is_muted=source.is_muted, audio_mixed=source.mixed)


def _js_truthy(value: object) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return str.__len__(value) != 0
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except (OverflowError, ValueError):
            return True
        return number != 0 and not isnan(number)
    return True


def _js_string(value: object, seen: set[int] | None = None) -> str:
    if isinstance(value, str):
        return str.__str__(value)
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _js_number_string(value)
    if isinstance(value, list):
        seen = set() if seen is None else seen
        marker = id(value)
        if marker in seen:
            return ""
        seen.add(marker)
        try:
            return ",".join(_js_string(item, seen) for item in value)
        finally:
            seen.remove(marker)
    return "[object Object]"


def _js_number_string(value: int | float) -> str:
    try:
        number = float(value)
    except OverflowError:
        number = float("-inf") if value < 0 else float("inf")
    if not isfinite(number):
        return "NaN" if isnan(number) else "-Infinity" if number < 0 else "Infinity"
    if number == 0:
        return "0"
    sign = "-" if number < 0 else ""
    decimal = Decimal(repr(abs(number)))
    digits = list(decimal.as_tuple().digits)
    exponent = decimal.as_tuple().exponent
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    text = "".join(str(digit) for digit in digits)
    position = len(text) + exponent
    if 0 < position <= 21:
        body = text + "0" * (position - len(text)) if len(text) <= position \
            else text[:position] + "." + text[position:]
    elif -6 < position <= 0:
        body = "0." + "0" * -position + text
    else:
        fraction = "." + text[1:] if len(text) > 1 else ""
        scientific_exponent = position - 1
        body = f"{text[0]}{fraction}e{'+' if scientific_exponent >= 0 else ''}{scientific_exponent}"
    return sign + body


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _utc(value: object) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    return value.astimezone(timezone.utc) if value.utcoffset() == timezone.utc.utcoffset(value) else None
