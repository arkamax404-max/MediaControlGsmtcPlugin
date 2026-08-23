import hashlib
import json
import math
import re
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone


MAX_TEXT_LENGTH = 160
ARTWORK_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _text(value):
    return str(value or "").strip()[:MAX_TEXT_LENGTH]


def _artwork_id(value):
    return value if isinstance(value, str) and ARTWORK_ID_PATTERN.fullmatch(value) else None


@dataclass(frozen=True)
class MediaState:
    available: bool = False
    is_playing: bool = False
    title: str = ""
    artist: str = ""
    artwork_id: str | None = None
    source: str = ""
    timeline_available: bool = False
    position_seconds: float = 0.0
    duration_seconds: float = 0.0
    playback_rate: float = 1.0
    position_updated_at: str = ""
    audio_available: bool = False
    volume_percent: int | None = None
    is_muted: bool = False
    audio_session_count: int = 0
    audio_mixed: bool = False
    revision: int = 0
    updated_at: str = ""

    def public(self):
        return {
            "available": self.available,
            "is_playing": self.is_playing,
            "title": self.title,
            "artist": self.artist,
            "artwork_id": self.artwork_id,
            "source": self.source,
            "timeline_available": self.timeline_available,
            "position_seconds": self.position_seconds,
            "duration_seconds": self.duration_seconds,
            "playback_rate": self.playback_rate,
            "position_updated_at": self.position_updated_at,
            "audio_available": self.audio_available,
            "volume_percent": self.volume_percent,
            "is_muted": self.is_muted,
            "audio_session_count": self.audio_session_count,
            "audio_mixed": self.audio_mixed,
            "revision": self.revision,
            "updated_at": self.updated_at,
        }


def _finite_number(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def normalize_timeline(raw):
    available = bool(raw.get("timeline_available"))
    duration = max(0.0, _finite_number(raw.get("duration_seconds")))
    position = max(0.0, _finite_number(raw.get("position_seconds")))
    rate = _finite_number(raw.get("playback_rate"), 1.0)
    updated_at = _text(raw.get("position_updated_at"))
    if not available or duration <= 0 or not updated_at:
        return {
            "timeline_available": False,
            "position_seconds": 0.0,
            "duration_seconds": 0.0,
            "playback_rate": 1.0,
            "position_updated_at": "",
        }
    return {
        "timeline_available": True,
        "position_seconds": min(position, duration),
        "duration_seconds": duration,
        "playback_rate": rate if rate > 0 else 1.0,
        "position_updated_at": updated_at,
    }


def normalize_state(raw):
    timeline = normalize_timeline(raw)
    return MediaState(
        available=bool(raw.get("available")),
        is_playing=bool(raw.get("is_playing")) if raw.get("available") else False,
        title=_text(raw.get("title")),
        artist=_text(raw.get("artist")),
        artwork_id=_artwork_id(raw.get("artwork_id")),
        source=_text(raw.get("source")),
        **timeline,
    )


class MediaStateCache:
    def __init__(self, clock=None):
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._state = MediaState()

    def update(self, raw):
        candidate = normalize_state(raw)
        with self._lock:
            if "timeline_available" not in raw:
                candidate = replace(
                    candidate,
                    timeline_available=self._state.timeline_available,
                    position_seconds=self._state.position_seconds,
                    duration_seconds=self._state.duration_seconds,
                    playback_rate=self._state.playback_rate,
                    position_updated_at=self._state.position_updated_at,
                )
            candidate = replace(
                candidate,
                audio_available=self._state.audio_available,
                volume_percent=self._state.volume_percent,
                is_muted=self._state.is_muted,
                audio_session_count=self._state.audio_session_count,
                audio_mixed=self._state.audio_mixed,
            )
            return self._replace(candidate)

    def update_timeline(self, raw):
        timeline = normalize_timeline(raw)
        with self._lock:
            candidate = replace(self._state, **timeline)
            return self._replace(candidate)

    def update_audio(self, raw):
        available = bool(raw.get("audio_available"))
        volume = raw.get("volume_percent")
        with self._lock:
            candidate = replace(
                self._state,
                audio_available=available,
                volume_percent=max(0, min(100, int(volume)))
                if available and volume is not None
                else None,
                is_muted=bool(raw.get("is_muted")) if available else False,
                audio_session_count=max(0, int(raw.get("audio_session_count", 0)))
                if available
                else 0,
                audio_mixed=bool(raw.get("audio_mixed")) if available else False,
            )
            return self._replace(candidate)

    def audio_unavailable(self):
        return self.update_audio({"audio_available": False})

    def _replace(self, candidate):
        current_payload = self._comparable(self._state)
        candidate_payload = self._comparable(candidate)
        revision = self._state.revision
        if candidate_payload != current_payload:
            revision += 1
        self._state = replace(
            candidate,
            revision=revision,
            updated_at=self._clock().isoformat(),
        )
        return self._state

    def unavailable(self):
        return self.update({"available": False, "timeline_available": False})

    def get(self):
        with self._lock:
            return self._state

    @staticmethod
    def _comparable(state):
        payload = state.public()
        payload.pop("revision")
        payload.pop("updated_at")
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def fingerprint(self):
        payload = self._comparable(self.get()).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()
