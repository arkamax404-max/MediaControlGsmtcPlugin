from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable


STATE_MAX_AGE_SECONDS = 15
PROGRESS_MODES = ("remaining", "elapsed", "total")
STATUS_LABELS = {
    "configuration": "Companion setup required",
    "incompatible": "Incompatible companion",
    "offline": "Offline",
    "no_timeline": "No timeline",
}


@dataclass(frozen=True)
class ProgressState:
    online: bool
    available: bool
    timeline_available: bool
    is_playing: bool
    position_seconds: float
    duration_seconds: float
    playback_rate: float
    position_updated_at: datetime | None
    status: str
    label: str


def unavailable_progress_state(reason: str = "unavailable") -> ProgressState:
    status = reason if reason in ("configuration", "incompatible") else "offline"
    return ProgressState(False, False, False, False, 0.0, 0.0, 1.0, None,
                         status, STATUS_LABELS[status])


def normalize_progress_state(
    payload: object,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> ProgressState:
    if not isinstance(payload, dict):
        return unavailable_progress_state()
    now = _utc(clock())
    updated_at = _timestamp(payload.get("updated_at"))
    if now is None or updated_at is None:
        return unavailable_progress_state()
    age = (now - updated_at).total_seconds()
    if age < 0 or age > STATE_MAX_AGE_SECONDS:
        return unavailable_progress_state()

    available = payload.get("available")
    timeline_available = payload.get("timeline_available")
    is_playing = payload.get("is_playing")
    position = _number(payload.get("position_seconds"))
    duration = _number(payload.get("duration_seconds"))
    rate = _number(payload.get("playback_rate"))
    if (not all(isinstance(value, bool) for value in
                (available, timeline_available, is_playing))
            or None in (position, duration, rate)):
        return unavailable_progress_state()

    anchor_value = payload.get("position_updated_at")
    anchor = _timestamp(anchor_value)
    if (timeline_available and anchor is None
            or not timeline_available and anchor_value != ""):
        return unavailable_progress_state()
    has_timeline = (available and timeline_available and duration > 0
                    and position >= 0 and anchor is not None)
    if not has_timeline:
        return ProgressState(True, available, False, available and is_playing,
                             0.0, 0.0, 1.0, None, "no_timeline",
                             STATUS_LABELS["no_timeline"])
    return ProgressState(True, True, True, is_playing,
                         min(position, duration), duration,
                         rate if rate > 0 else 1.0, anchor, "ready", "")


def extrapolate_position(
    state: ProgressState,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> float:
    if not state.timeline_available:
        return 0.0
    position = state.position_seconds
    now = _utc(clock())
    if state.is_playing and state.position_updated_at is not None and now is not None:
        elapsed = max(0.0, (now - state.position_updated_at).total_seconds())
        position += elapsed * state.playback_rate
    return max(0.0, min(state.duration_seconds, position))


def next_progress_mode(mode: str | None) -> str:
    current = mode if mode in PROGRESS_MODES else "remaining"
    return PROGRESS_MODES[(PROGRESS_MODES.index(current) + 1) % len(PROGRESS_MODES)]


def format_progress_time(mode: str | None, position: object, duration: object) -> str:
    safe_duration = max(0.0, _number(duration) or 0.0)
    safe_position = max(0.0, min(safe_duration, _number(position) or 0.0))
    if mode == "elapsed":
        seconds = math.floor(safe_position)
    elif mode == "total":
        seconds = math.ceil(safe_duration)
    else:
        seconds = math.ceil(safe_duration - safe_position)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return (f"{hours}:{minutes:02d}:{seconds:02d}" if hours
            else f"{minutes}:{seconds:02d}")


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _utc(parsed)


def _utc(value: object) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    if value.utcoffset() != timezone.utc.utcoffset(value):
        return None
    return value.astimezone(timezone.utc)
