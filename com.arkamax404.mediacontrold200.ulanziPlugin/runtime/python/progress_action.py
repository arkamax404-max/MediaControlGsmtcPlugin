from __future__ import annotations

import base64
import math
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from html import escape
from typing import Callable, Mapping

from progress_state import ProgressState, extrapolate_position, format_progress_time, next_progress_mode


ACTION_UUID = "com.arkamax404.ulanzi.mediacontrol.progress"
_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


@dataclass(frozen=True)
class ProgressSettings:
    progress_color: str = "#1DB954"
    track_color: str = "#333333"
    text_color: str = "#FFFFFF"
    background_color: str = "#000000"
    stroke_width: int = 14


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
    mode: str
    settings: ProgressSettings


@dataclass
class _Context:
    generation: int
    version: int
    active: bool
    mode: str
    settings: ProgressSettings
    committed_signature: str | None = None
    persistence_pending: bool = False
    persistence_attempts: int = 0


def normalize_progress_settings(raw: object = None) -> ProgressSettings:
    values = raw if isinstance(raw, Mapping) else {}

    def color(name: str, default: str) -> str:
        value = values.get(name)
        return str(value).upper() if isinstance(value, str) and _COLOR.fullmatch(value) else default

    width_value = values.get("strokeWidth", 14)
    try:
        number = 0.0 if width_value is None or (isinstance(width_value, str) and not width_value.strip()) else float(width_value)
        width = math.floor(number + 0.5) if math.isfinite(number) else 14
    except (TypeError, ValueError):
        width = 14
    return ProgressSettings(
        color("progressColor", "#1DB954"), color("trackColor", "#333333"),
        color("textColor", "#FFFFFF"), color("backgroundColor", "#000000"),
        max(6, min(30, width)),
    )


def progress_settings_payload(settings: ProgressSettings) -> dict[str, object]:
    return {
        "progressColor": settings.progress_color,
        "trackColor": settings.track_color,
        "textColor": settings.text_color,
        "backgroundColor": settings.background_color,
        "strokeWidth": settings.stroke_width,
    }


def settings_match(raw: object, settings: ProgressSettings) -> bool:
    if not isinstance(raw, Mapping):
        return False
    canonical = progress_settings_payload(settings)
    return all(raw.get(name) == value for name, value in canonical.items())


def _fixed_three(value: float) -> str:
    if not math.isfinite(value) or value < 0:
        raise ValueError("SVG numbers must be finite and non-negative")
    numerator, denominator = value.as_integer_ratio()
    units, remainder = divmod(numerator * 1000, denominator)
    if remainder * 2 >= denominator:
        units += 1
    return f"{units // 1000}.{units % 1000:03d}"


def render_progress_svg(progress: object, text: object, settings: ProgressSettings) -> str:
    ratio = float(progress) if isinstance(progress, (int, float)) and not isinstance(progress, bool) else 0.0
    ratio = max(0.0, min(1.0, ratio if math.isfinite(ratio) else 0.0))
    value = str(text)
    radius = 70
    circumference = 2 * math.pi * radius
    if ":" in value:
        available_radius = radius - settings.stroke_width / 2 - 4
        width_em = sum(0.29 if character in ":." else 0.58 for character in value)
        font_size = max(16, min(42, math.floor(2 * available_radius / math.sqrt(width_em ** 2 + 1))))
        estimated_width = width_em * font_size
        max_width = 2 * math.sqrt(available_radius ** 2 - (font_size / 2) ** 2)
        width_constraint = (f' textLength="{_fixed_three(max_width)}" lengthAdjust="spacingAndGlyphs"'
                            if estimated_width > max_width else "")
    else:
        font_size = 34 if len(value) > 6 else 42
        width_constraint = ""
    baseline = 98 + (font_size * 0.8 - font_size * 0.2) / 2
    baseline_text = str(int(baseline)) if baseline.is_integer() else str(baseline)
    arc = ratio * circumference
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="196" height="196" viewBox="0 0 196 196">'
            f'<rect width="196" height="196" fill="{settings.background_color}"/>'
            f'<circle cx="98" cy="98" r="70" fill="none" stroke="{settings.track_color}" stroke-width="{settings.stroke_width}"/>'
            f'<circle cx="98" cy="98" r="70" fill="none" stroke="{settings.progress_color}" stroke-width="{settings.stroke_width}" stroke-linecap="round" transform="rotate(-90 98 98)" stroke-dasharray="{_fixed_three(arc)} {_fixed_three(circumference)}"/>'
            f'<text x="98" y="{baseline_text}" fill="{settings.text_color}" font-family="Arial, sans-serif" font-size="{font_size}" font-weight="700" text-anchor="middle"{width_constraint}>{escape(value, quote=True).replace("&#x27;", "&apos;")}</text>'
            '</svg>')


def svg_data_uri(svg: str) -> str:
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


class ProgressActionModel:
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
        if not isinstance(event, Mapping):
            return ()
        context = event.get("context")
        action = event.get("uuid", event.get("action"))
        if action != ACTION_UUID or not isinstance(context, str) or not context:
            return ()
        raw = event.get("param")
        settings = normalize_progress_settings(raw)
        with self._lock:
            if self._shutdown:
                return ()
            self._next_generation += 1
            entry = _Context(self._next_generation, 1, True, "remaining", settings,
                             persistence_pending=not settings_match(raw, settings))
            self._contexts[context] = entry
            return (self._request(context, entry),)

    def clear(self, event: object) -> bool:
        items = event.get("param", ()) if isinstance(event, Mapping) else ()
        try:
            snapshot = tuple(items)
        except TypeError:
            snapshot = ()
        contexts = tuple(item.get("context") for item in snapshot
                         if isinstance(item, Mapping))
        with self._lock:
            changed = False
            for context in contexts:
                if isinstance(context, str):
                    changed |= self._contexts.pop(context, None) is not None
        return changed

    def shutdown(self) -> None:
        with self._lock:
            self._shutdown = True
            self._contexts.clear()

    def set_active(self, event: object) -> tuple[RenderRequest, ...]:
        context = event.get("context") if isinstance(event, Mapping) else None
        active = not (isinstance(event, Mapping) and event.get("active") is False)
        return self._mutate(context, lambda entry: setattr(entry, "active", active), active)

    def run(self, event: object) -> tuple[RenderRequest, ...]:
        context = event.get("context") if isinstance(event, Mapping) else None
        return self._mutate(context, lambda entry: setattr(entry, "mode", next_progress_mode(entry.mode)))

    def receive_settings(self, event: object,
                         persist: bool = False) -> tuple[RenderRequest, ...]:
        if isinstance(event, Mapping):
            context = event.get("context")
            raw = event.get("param", event.get("settings", {}))
        else:
            context, raw = None, {}
        settings = normalize_progress_settings(raw)
        def change(entry: _Context) -> None:
            entry.settings = settings
            if persist or not settings_match(raw, settings):
                entry.persistence_pending = True
                entry.persistence_attempts = 0
            else:
                entry.persistence_pending = False
                entry.persistence_attempts = 0

        return self._mutate(context, change)

    def requests(self) -> tuple[RenderRequest, ...]:
        with self._lock:
            return tuple(self._request(context, entry) for context, entry in self._contexts.items()
                          if entry.active)

    def is_current(self, request: RenderRequest) -> bool:
        with self._lock:
            entry = self._matching(request)
            return bool(entry and entry.active)

    def reserve_display_send(self, intent: SendIntent) -> bool:
        """Linearize a display send without holding the lock during SDK code."""
        request = RenderRequest(intent.context, intent.generation, intent.version)
        with self._lock:
            entry = self._matching(request)
            return bool(entry and entry.active
                        and entry.committed_signature != intent.signature)

    def persistence_requests(self) -> tuple[PersistenceRequest, ...]:
        with self._lock:
            return tuple(
                PersistenceRequest(context, entry.generation, entry.version,
                                   progress_settings_payload(entry.settings))
                for context, entry in self._contexts.items()
                if entry.active and entry.persistence_pending
            )

    def is_persistence_current(self, request: PersistenceRequest) -> bool:
        with self._lock:
            entry = self._matching(request)
            return bool(entry and entry.active and entry.persistence_pending)

    def reserve_persistence_send(self, request: PersistenceRequest) -> bool:
        """Linearize a settings send without holding the lock during SDK code."""
        with self._lock:
            entry = self._matching(request)
            return bool(entry and entry.active and entry.persistence_pending)

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

    def render(self, request: RenderRequest, state: ProgressState,
               clock: Callable[[], datetime]) -> SendIntent | None:
        with self._lock:
            entry = self._matching(request)
            if entry is None or not entry.active:
                return None
            settings, mode = entry.settings, entry.mode
        if state.timeline_available:
            position = extrapolate_position(state, clock)
            fraction = position / state.duration_seconds if state.duration_seconds > 0 else 0
            text = format_progress_time(mode, position, state.duration_seconds)
        else:
            fraction, text = 0, state.label
        svg = render_progress_svg(fraction, text, settings)
        intent = SendIntent(request.context, svg_data_uri(svg), request.generation,
                            request.version, svg)
        with self._lock:
            entry = self._matching(request)
            return None if entry is None or not entry.active or entry.committed_signature == svg else intent

    def acknowledge(self, intent: SendIntent, success: bool) -> bool:
        request = RenderRequest(intent.context, intent.generation, intent.version)
        with self._lock:
            entry = self._matching(request)
            if entry is None or not entry.active:
                return False
            if success:
                entry.committed_signature = intent.signature
            return True

    def _mutate(self, context: object, change, emit: bool = True) -> tuple[RenderRequest, ...]:
        if not isinstance(context, str):
            return ()
        with self._lock:
            if self._shutdown:
                return ()
            entry = self._contexts.get(context)
            if entry is None:
                return ()
            change(entry)
            entry.version += 1
            entry.committed_signature = None
            return (self._request(context, entry),) if emit and entry.active else ()

    def _matching(self, request: RenderRequest) -> _Context | None:
        entry = self._contexts.get(request.context)
        return entry if entry and (entry.generation, entry.version) == (request.generation, request.version) else None

    @staticmethod
    def _request(context: str, entry: _Context) -> RenderRequest:
        return RenderRequest(context, entry.generation, entry.version)

    @staticmethod
    def _view(context: str, entry: _Context) -> ContextView:
        return ContextView(context, entry.generation, entry.version, entry.active,
                           entry.mode, entry.settings)
