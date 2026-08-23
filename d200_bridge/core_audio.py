from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock
from typing import Callable, Protocol


VOLUME_STEP = 0.05


class AudioSession(Protocol):
    @property
    def process_name(self) -> str: ...

    def get_volume(self) -> float: ...

    def set_volume(self, value: float) -> None: ...

    def get_mute(self) -> bool: ...

    def set_mute(self, value: bool) -> None: ...


class SessionEnumerator(Protocol):
    def sessions(self): ...


class _PycawSession:
    def __init__(self, session, psutil_module):
        self._session = session
        self._psutil = psutil_module

    @property
    def process_name(self):
        pid = int(self._session.ProcessId)
        if pid <= 0:
            raise ValueError("Audio session has no process")
        return self._psutil.Process(pid).name()

    def get_volume(self):
        return float(self._session.SimpleAudioVolume.GetMasterVolume())

    def set_volume(self, value):
        self._session.SimpleAudioVolume.SetMasterVolume(float(value), None)

    def get_mute(self):
        return bool(self._session.SimpleAudioVolume.GetMute())

    def set_mute(self, value):
        self._session.SimpleAudioVolume.SetMute(bool(value), None)


class PycawSessionEnumerator:
    """Enumerates sessions on pycaw's default render endpoint."""

    @contextmanager
    def sessions(self):
        import comtypes
        import psutil
        from pycaw.pycaw import AudioUtilities

        comtypes.CoInitialize()
        try:
            yield [_PycawSession(session, psutil) for session in AudioUtilities.GetAllSessions()]
        finally:
            comtypes.CoUninitialize()


@dataclass(frozen=True)
class AudioCommandResult:
    status: str
    applied_count: int
    failed_count: int
    state: dict

    @property
    def ok(self):
        return self.status == "ok"

    def public(self):
        return {
            "ok": self.ok,
            "status": self.status,
            "applied_count": self.applied_count,
            "failed_count": self.failed_count,
            **self.state,
        }


class CoreAudioController:
    def __init__(self, cache, enumerator=None):
        self.cache = cache
        self._enumerator = enumerator or PycawSessionEnumerator()
        self._transaction_lock = Lock()

    def refresh(self):
        with self._transaction_lock:
            return self._refresh_locked()

    def _refresh_locked(self):
        try:
            with self._enumerator.sessions() as sessions:
                spotify = self._spotify_sessions(sessions)
                state, failures = self._read_state(spotify)
        except Exception:
            self.cache.audio_unavailable()
            return False
        self.cache.update_audio(state)
        return bool(spotify) and failures == 0

    def command(self, action):
        methods: dict[str, Callable] = {
            "volume-up": lambda sessions: self._adjust_volume(sessions, VOLUME_STEP),
            "volume-down": lambda sessions: self._adjust_volume(sessions, -VOLUME_STEP),
            "mute-toggle": self._toggle_mute,
        }
        operation = methods.get(action)
        if operation is None:
            raise ValueError("Unsupported audio command")

        with self._transaction_lock:
            return self._command_locked(operation)

    def _command_locked(self, operation):
        try:
            with self._enumerator.sessions() as sessions:
                spotify = self._spotify_sessions(sessions)
                if not spotify:
                    state = self._unavailable_state()
                    self.cache.update_audio(state)
                    return AudioCommandResult("no_audio", 0, 0, state)
                applied, failures = operation(spotify)
                state, read_failures = self._read_state(spotify)
                failures += read_failures
        except Exception:
            self.cache.audio_unavailable()
            return AudioCommandResult("failed", 0, 1, self._unavailable_state())

        self.cache.update_audio(state)
        if failures:
            status = "partial_failure" if applied else "failed"
        else:
            status = "ok"
        return AudioCommandResult(status, applied, failures, state)

    def stop(self):
        self.cache.audio_unavailable()

    @staticmethod
    def _spotify_sessions(sessions):
        selected = []
        for session in sessions:
            try:
                if session.process_name.casefold() == "spotify.exe":
                    selected.append(session)
            except Exception:
                continue
        return selected

    @staticmethod
    def _adjust_volume(sessions, delta):
        applied = 0
        failures = 0
        for session in sessions:
            try:
                current = session.get_volume()
                session.set_volume(max(0.0, min(1.0, current + delta)))
                applied += 1
            except Exception:
                failures += 1
        return applied, failures

    @staticmethod
    def _toggle_mute(sessions):
        mute_states = []
        try:
            for session in sessions:
                mute_states.append(session.get_mute())
        except Exception:
            return 0, 1

        target = not all(mute_states)
        applied = 0
        failures = 0
        for session in sessions:
            try:
                session.set_mute(target)
                applied += 1
            except Exception:
                failures += 1
        return applied, failures

    @classmethod
    def _read_state(cls, sessions):
        readings = []
        failures = 0
        for session in sessions:
            try:
                readings.append((session.get_volume(), session.get_mute()))
            except Exception:
                failures += 1
        if not readings:
            return cls._unavailable_state(), failures

        volumes = [max(0, min(100, round(volume * 100))) for volume, _mute in readings]
        mutes = [mute for _volume, mute in readings]
        return {
            "audio_available": True,
            "volume_percent": round(sum(volumes) / len(volumes)),
            "is_muted": all(mutes),
            "audio_session_count": len(readings),
            "audio_mixed": len(set(volumes)) > 1 or len(set(mutes)) > 1,
        }, failures

    @staticmethod
    def _unavailable_state():
        return {
            "audio_available": False,
            "volume_percent": None,
            "is_muted": False,
            "audio_session_count": 0,
            "audio_mixed": False,
        }
