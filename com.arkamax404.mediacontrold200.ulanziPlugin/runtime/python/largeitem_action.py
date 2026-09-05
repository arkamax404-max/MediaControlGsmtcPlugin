from __future__ import annotations

import hashlib
import math
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from artwork_bundle import ArtworkBundle
from largeitem_renderer import (
    LargeItemSettings,
    LargeItemView,
    render_largeitem,
    svg_data_uri,
)
from now_playing_action import MediaSnapshot
from progress_state import ProgressState, extrapolate_position, format_progress_time


ACTION_UUID = "com.arkamax404.ulanzi.mediacontrol.largeitem-nowplaying"
KEY = "3_2"
COLOR_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")


@dataclass(frozen=True)
class RenderRequest:
    context: str
    generation: int
    version: int


@dataclass(frozen=True)
class SendIntent:
    context: str
    data_uri: str
    generation: int
    version: int
    signature: str


@dataclass(frozen=True)
class PersistenceRequest:
    context: str
    generation: int
    version: int
    settings: dict[str, object]


@dataclass(frozen=True)
class ContextView:
    context: str
    generation: int
    version: int
    active: bool
    settings: LargeItemSettings


@dataclass
class _Context:
    generation: int
    version: int
    active: bool
    settings: LargeItemSettings
    committed_signature: str | None = None
    persistence_pending: bool = False
    persistence_attempts: int = 0


def normalize_largeitem_settings(raw: object) -> LargeItemSettings:
    source = raw if isinstance(raw, Mapping) else {}

    def boolean(name: str, default: bool) -> bool:
        value = source.get(name)
        return value if isinstance(value, bool) else default

    def choice(name: str, accepted: tuple[str, ...], default: str) -> str:
        value = source.get(name)
        return value if isinstance(value, str) and value in accepted else default

    def color(name: str, default: str) -> str:
        value = source.get(name)
        return value.upper() if isinstance(value, str) and COLOR_PATTERN.fullmatch(value) else default

    return LargeItemSettings(
        show_artwork=boolean("showArtwork", True),
        paused_artwork=choice("pausedArtwork", ("color", "grayscale"), "grayscale"),
        show_progress=boolean("showProgress", True),
        show_elapsed=boolean("showElapsed", False),
        show_remaining=boolean("showRemaining", True),
        background_color=color("backgroundColor", "#0B0D10"),
        primary_color=color("primaryColor", "#FFFFFF"),
        secondary_color=color("secondaryColor", "#B8BEC8"),
        accent_color=color("accentColor", "#1DB954"),
        fit=choice("fit", ("contain", "cover"), "contain"),
        small_view_mode=2,
    )


def largeitem_settings_payload(settings: LargeItemSettings) -> dict[str, object]:
    return {
        "showArtwork": settings.show_artwork,
        "pausedArtwork": settings.paused_artwork,
        "showProgress": settings.show_progress,
        "showElapsed": settings.show_elapsed,
        "showRemaining": settings.show_remaining,
        "backgroundColor": settings.background_color,
        "primaryColor": settings.primary_color,
        "secondaryColor": settings.secondary_color,
        "accentColor": settings.accent_color,
        "fit": settings.fit,
        "SmallViewMode": settings.small_view_mode,
    }


def settings_match(raw: object, settings: LargeItemSettings) -> bool:
    if not isinstance(raw, Mapping):
        return False
    canonical = largeitem_settings_payload(settings)
    return all(raw.get(name) == value for name, value in canonical.items())


class LargeItemActionModel:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._contexts: dict[str, _Context] = {}
        self._next_generation = 0
        self._shutdown = False

    def context(self, context: str) -> ContextView | None:
        with self._lock:
            entry = self._contexts.get(context)
            return self._view(context, entry) if entry else None

    def add(self, event: object) -> tuple[RenderRequest, ...]:
        identity = _event_identity(event)
        if identity is None:
            return ()
        context, raw = identity
        settings = normalize_largeitem_settings(raw)
        with self._lock:
            if self._shutdown:
                return ()
            self._next_generation += 1
            entry = _Context(
                self._next_generation,
                1,
                True,
                settings,
                persistence_pending=not settings_match(raw, settings),
            )
            self._contexts[context] = entry
            return (self._request(context, entry),)

    def clear(self, event: object) -> bool:
        items = event.get("param", ()) if isinstance(event, Mapping) else ()
        try:
            contexts = tuple(
                item.get("context") for item in tuple(items)
                if isinstance(item, Mapping) and isinstance(item.get("context"), str)
            )
        except Exception:
            contexts = ()
        with self._lock:
            changed = False
            for context in contexts:
                changed |= self._contexts.pop(context, None) is not None
            return changed

    def set_active(self, event: object) -> tuple[RenderRequest, ...]:
        context = event.get("context") if isinstance(event, Mapping) else None
        active = not (isinstance(event, Mapping) and event.get("active") is False)
        if not isinstance(context, str):
            return ()
        with self._lock:
            entry = self._contexts.get(context)
            if entry is None or self._shutdown:
                return ()
            entry.active = active
            entry.version += 1
            entry.committed_signature = None
            return (self._request(context, entry),) if active else ()

    def receive_settings(self, event: object, persist: bool = False) -> tuple[RenderRequest, ...]:
        if not isinstance(event, Mapping):
            return ()
        context, raw = event.get("context"), event.get("settings")
        if not isinstance(context, str):
            return ()
        settings = normalize_largeitem_settings(raw)
        with self._lock:
            entry = self._contexts.get(context)
            if entry is None or self._shutdown:
                return ()
            changed = entry.settings != settings
            entry.settings = settings
            entry.persistence_pending = persist or not settings_match(raw, settings)
            entry.persistence_attempts = 0
            if changed:
                entry.version += 1
                entry.committed_signature = None
            return (self._request(context, entry),) if entry.active and (changed or entry.persistence_pending) else ()

    def requests(self) -> tuple[RenderRequest, ...]:
        with self._lock:
            return tuple(
                self._request(context, entry)
                for context, entry in self._contexts.items() if entry.active
            )

    def persistence_requests(self) -> tuple[PersistenceRequest, ...]:
        with self._lock:
            return tuple(
                PersistenceRequest(context, entry.generation, entry.version,
                                   largeitem_settings_payload(entry.settings))
                for context, entry in self._contexts.items()
                if entry.active and entry.persistence_pending
            )

    def reserve_persistence_send(self, request: PersistenceRequest) -> bool:
        with self._lock:
            entry = self._matching(request)
            return bool(entry and entry.active and entry.persistence_pending)

    def is_persistence_current(self, request: PersistenceRequest) -> bool:
        return self.reserve_persistence_send(request)

    def acknowledge_persistence(self, request: PersistenceRequest, success: bool,
                                max_attempts: int) -> bool:
        with self._lock:
            entry = self._matching(request)
            if entry is None or not entry.active or not entry.persistence_pending:
                return False
            entry.persistence_attempts += 1
            if success or entry.persistence_attempts >= max_attempts:
                entry.persistence_pending = False
            return True

    def render(self, request: RenderRequest, media: MediaSnapshot, progress: ProgressState,
               bundle: ArtworkBundle | None, clock: Callable[[], datetime]) -> SendIntent | None:
        with self._lock:
            entry = self._matching(request)
            if entry is None or not entry.active:
                return None
            settings = entry.settings
        ratio = None
        elapsed = ""
        remaining = ""
        if progress.timeline_available and progress.duration_seconds > 0:
            position = extrapolate_position(progress, clock)
            ratio = position / progress.duration_seconds
            if not math.isfinite(ratio):
                ratio = None
            elapsed = format_progress_time("elapsed", position, progress.duration_seconds)
            remaining = format_progress_time("remaining", position, progress.duration_seconds)
        artwork = None
        if bundle is not None and bundle.artwork_id == media.artwork_id:
            artwork = bundle.color if media.is_playing or settings.paused_artwork == "color" else bundle.grayscale
        view = LargeItemView(
            media.status,
            media.title,
            media.artist,
            media.is_playing,
            artwork,
            ratio,
            elapsed,
            remaining,
            settings,
        )
        svg = render_largeitem(view)
        data_uri = svg_data_uri(svg)
        signature = hashlib.sha256(data_uri.encode("ascii")).hexdigest()
        intent = SendIntent(request.context, data_uri, request.generation, request.version, signature)
        with self._lock:
            entry = self._matching(request)
            return None if entry is None or not entry.active or entry.committed_signature == signature else intent

    def reserve_send(self, intent: SendIntent) -> bool:
        with self._lock:
            entry = self._matching(intent)
            return bool(entry and entry.active and entry.committed_signature != intent.signature)

    def acknowledge(self, intent: SendIntent, success: bool) -> bool:
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

    def _matching(self, request: object) -> _Context | None:
        context = getattr(request, "context", None)
        entry = self._contexts.get(context)
        return entry if entry and (entry.generation, entry.version) == (
            getattr(request, "generation", None), getattr(request, "version", None)
        ) else None

    @staticmethod
    def _request(context: str, entry: _Context) -> RenderRequest:
        return RenderRequest(context, entry.generation, entry.version)

    @staticmethod
    def _view(context: str, entry: _Context) -> ContextView:
        return ContextView(context, entry.generation, entry.version, entry.active, entry.settings)


def _event_identity(event: object) -> tuple[str, object] | None:
    if not isinstance(event, Mapping):
        return None
    if event.get("uuid", event.get("action")) != ACTION_UUID:
        return None
    context = event.get("context")
    if not isinstance(context, str) or not context:
        return None
    explicit_key = event.get("key")
    parts = context.split("___", 2)
    key = explicit_key if isinstance(explicit_key, str) else (parts[1] if len(parts) == 3 else None)
    if key != KEY:
        return None
    return context, event.get("param")
