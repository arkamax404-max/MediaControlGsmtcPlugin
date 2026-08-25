from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime, timezone
from math import isfinite, isnan
from typing import Callable

from artwork_bundle import ARTWORK_ID_PATTERN, ArtworkBundle


ACTION_UUID = "com.arkamax404.ulanzi.mediacontrol.nowplaying"
STATE_MAX_AGE_SECONDS = 15
TEXT_LIMIT = 48
OFFLINE_ICON = "./assets/offline.svg"
MUSIC_ICON = "./assets/music.svg"
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
class MediaSnapshot:
    online: bool
    available: bool
    is_playing: bool
    title: str
    artist: str
    artwork_id: str | None
    status: str


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


@dataclass
class _Context:
    generation: int
    version: int = 1
    active: bool = True
    committed_signature: tuple[str, str, str] | None = None


def unavailable_media_snapshot(reason: str = "unavailable") -> MediaSnapshot:
    status = reason if reason in ("configuration", "incompatible") else "offline"
    return MediaSnapshot(False, False, False, "", "", None, status)


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
    except Exception:
        return unavailable_media_snapshot()
    artwork_id = candidate if isinstance(candidate, str) and ARTWORK_ID_PATTERN.fullmatch(candidate) else None
    return MediaSnapshot(True, available, is_playing,
                         title, artist, artwork_id, "ready" if available else "no_session")


def now_playing_text(snapshot: MediaSnapshot) -> str:
    values = tuple(value for value in (_text(snapshot.title), _text(snapshot.artist)) if value)
    return "\n".join(values) or "Playing"


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
            return (ContextView(context, entry.generation, entry.version, entry.active)
                    if entry else None)

    def add(self, event: object) -> tuple[RenderRequest, ...]:
        action, context = _event_identity(event)
        if action != ACTION_UUID or not context:
            return ()
        with self._lock:
            if self._shutdown:
                return ()
            self._next_generation += 1
            entry = _Context(self._next_generation)
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

    def render(self, request: RenderRequest, snapshot: MediaSnapshot,
               bundle: ArtworkBundle | None = None) -> RenderIntent | None:
        if not isinstance(request, RenderRequest) or not isinstance(snapshot, MediaSnapshot):
            return None
        request = _canonical_request(request)
        if request is None:
            return None
        online, available = snapshot.online, snapshot.available
        playing, artwork_id = snapshot.is_playing, snapshot.artwork_id
        status, text = snapshot.status, _payload_text(now_playing_text(snapshot))
        matching = isinstance(bundle, ArtworkBundle) and bundle.artwork_id == artwork_id
        if not online or not available:
            method, image = "setPathIcon", OFFLINE_ICON
            text = STATUS_LABELS.get(status, "Offline")
        elif matching:
            method, image = "setBaseDataIcon", bundle.color if playing else bundle.grayscale
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
