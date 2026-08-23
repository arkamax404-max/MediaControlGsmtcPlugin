import asyncio
import math
from datetime import datetime, timedelta, timezone

from .state import MAX_THUMBNAIL_BYTES, thumbnail_data_uri


SPOTIFY_IDENTIFIERS = ("spotify.exe", "spotifyab.spotifymusic")


def timespan_seconds(value):
    if isinstance(value, timedelta):
        return value.total_seconds()
    duration = getattr(value, "duration", None)
    if duration is not None:
        return float(duration) / 10_000_000
    return float(value)


def normalize_timeline_properties(timeline, playback, now=None):
    now = now or datetime.now(timezone.utc)
    unavailable = {
        "timeline_available": False,
        "position_seconds": 0.0,
        "duration_seconds": 0.0,
        "playback_rate": 1.0,
        "position_updated_at": "",
    }
    try:
        start = timespan_seconds(getattr(timeline, "start_time"))
        end = timespan_seconds(getattr(timeline, "end_time"))
        position = timespan_seconds(getattr(timeline, "position"))
        if not all(math.isfinite(value) for value in (start, end, position)):
            return unavailable
        duration = max(0.0, end - start)
        if duration <= 0:
            return unavailable
        position = max(0.0, min(duration, position - start))
        rate = float(getattr(playback, "playback_rate", 1.0) or 1.0)
        if not math.isfinite(rate) or rate <= 0:
            rate = 1.0

        last_updated = getattr(timeline, "last_updated_time", None)
        if isinstance(last_updated, datetime):
            if last_updated.tzinfo is None:
                last_updated = last_updated.replace(tzinfo=timezone.utc)
            else:
                last_updated = last_updated.astimezone(timezone.utc)
            elapsed = max(0.0, (now - last_updated).total_seconds())
            status = getattr(playback, "playback_status", None)
            if GSMTCAdapter._is_playing(status):
                position = min(duration, position + elapsed * rate)
        return {
            "timeline_available": True,
            "position_seconds": position,
            "duration_seconds": duration,
            "playback_rate": rate,
            "position_updated_at": now.astimezone(timezone.utc).isoformat(),
        }
    except (AttributeError, TypeError, ValueError, OverflowError):
        return unavailable


def select_session(sessions, current_session=None):
    sessions = list(sessions or [])
    for session in sessions:
        identifier = str(getattr(session, "source_app_user_model_id", "")).lower()
        if any(marker in identifier for marker in SPOTIFY_IDENTIFIERS):
            return session
    return current_session or (sessions[0] if sessions else None)


async def read_thumbnail(stream_reference):
    if stream_reference is None:
        return None
    stream = await stream_reference.open_read_async()
    size = int(stream.size)
    if size <= 0 or size > MAX_THUMBNAIL_BYTES:
        stream.close()
        return None

    from winrt.windows.storage.streams import DataReader

    reader = DataReader(stream)
    try:
        await reader.load_async(size)
        data = bytearray(size)
        reader.read_bytes(data)
        content_type = getattr(stream, "content_type", "image/jpeg")
        return thumbnail_data_uri(bytes(data), content_type)
    finally:
        reader.close()
        stream.close()


async def _default_manager_factory():
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager,
    )

    return await GlobalSystemMediaTransportControlsSessionManager.request_async()


class GSMTCAdapter:
    def __init__(self, cache, manager_factory=None, thumbnail_reader=None, clock=None):
        self.cache = cache
        self._manager_factory = manager_factory or _default_manager_factory
        self._thumbnail_reader = thumbnail_reader or read_thumbnail
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._manager = None
        self._session = None
        self._loop = None
        self._subscriptions = []
        self._refresh_lock = asyncio.Lock()

    async def start(self):
        self._loop = asyncio.get_running_loop()
        self._manager = await self._manager_factory()
        self._subscribe(self._manager, "sessions_changed")
        self._subscribe(self._manager, "current_session_changed")
        await self.refresh()

    async def stop(self):
        self._unsubscribe_all()
        self._session = None
        self._manager = None
        self.cache.unavailable()

    async def refresh(self):
        async with self._refresh_lock:
            try:
                sessions = self._manager.get_sessions()
                session = select_session(sessions, self._manager.get_current_session())
                if session is not self._session:
                    self._unsubscribe_session()
                    self._session = session
                    if session is not None:
                        self._subscribe(session, "media_properties_changed", session=True)
                        self._subscribe(session, "playback_info_changed", session=True)
                        self._subscribe(
                            session,
                            "timeline_properties_changed",
                            session=True,
                            handler=self._on_timeline_changed,
                        )
                if session is None:
                    self.cache.unavailable()
                    return

                properties = await session.try_get_media_properties_async()
                playback = session.get_playback_info()
                status = getattr(playback, "playback_status", None)
                thumbnail = await self._thumbnail_reader(
                    getattr(properties, "thumbnail", None)
                )
                timeline = normalize_timeline_properties(
                    session.get_timeline_properties(), playback, self._clock()
                )
                self.cache.update(
                    {
                        "available": True,
                        "is_playing": self._is_playing(status),
                        "title": getattr(properties, "title", ""),
                        "artist": getattr(properties, "artist", ""),
                        "thumbnail": thumbnail,
                        "source": getattr(session, "source_app_user_model_id", ""),
                        **timeline,
                    }
                )
            except Exception:
                self.cache.unavailable()

    async def refresh_timeline(self):
        async with self._refresh_lock:
            if self._session is None:
                self.cache.update_timeline({"timeline_available": False})
                return
            try:
                playback = self._session.get_playback_info()
                timeline = self._session.get_timeline_properties()
                self.cache.update_timeline(
                    normalize_timeline_properties(timeline, playback, self._clock())
                )
            except Exception:
                self.cache.update_timeline({"timeline_available": False})

    async def command(self, action):
        methods = {
            "previous": "try_skip_previous_async",
            "toggle": "try_toggle_play_pause_async",
            "next": "try_skip_next_async",
        }
        method_name = methods.get(action)
        if method_name is None:
            raise ValueError("Unsupported command")
        if self._session is None:
            return False
        try:
            accepted = bool(await getattr(self._session, method_name)())
            await self.refresh()
            return accepted
        except Exception:
            self.cache.unavailable()
            return False

    def _subscribe(self, source, event_name, session=False, handler=None):
        handler = handler or self._on_changed
        try:
            event = getattr(source, event_name)
            event += handler
            self._subscriptions.append((source, event_name, handler, session))
        except (AttributeError, TypeError):
            pass

    def _on_changed(self, _sender, _args):
        if self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self.refresh(), self._loop)

    def _on_timeline_changed(self, _sender, _args):
        if self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self.refresh_timeline(), self._loop)

    def _unsubscribe_session(self):
        self._unsubscribe(lambda item: item[3])

    def _unsubscribe_all(self):
        self._unsubscribe(lambda _item: True)

    def _unsubscribe(self, predicate):
        retained = []
        for item in self._subscriptions:
            if not predicate(item):
                retained.append(item)
                continue
            source, event_name, handler, _session = item
            try:
                event = getattr(source, event_name)
                event -= handler
            except (AttributeError, TypeError):
                pass
        self._subscriptions = retained

    @staticmethod
    def _is_playing(status):
        name = str(getattr(status, "name", status)).lower()
        return name == "playing" or name.endswith(".playing") or status == 4
