from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from collections.abc import Mapping
from typing import Callable

from bridge_client import BridgeClient
from artwork_bundle import ArtworkBundleCache
from now_playing_action import (DISPLAY_ACTION_UUIDS, MediaSnapshot,
                                NowPlayingActionModel, normalize_media_snapshot,
                                unavailable_media_snapshot)
from progress_action import (ACTION_UUID, PersistenceRequest, ProgressActionModel,
                             RenderRequest as ProgressRenderRequest)
from progress_state import (ProgressState, extrapolate_position,
                            normalize_progress_state, unavailable_progress_state)
from transport_actions import action_uuid_from_event


POLL_INTERVAL_SECONDS = 1.5
TICK_INTERVAL_SECONDS = 1.0
WORKER_STOP_TIMEOUT_SECONDS = 2.5
MAX_PERSIST_ATTEMPTS = 3


class ProgressScheduler:
    def __init__(self, api, client: BridgeClient, model: ProgressActionModel,
                 now_playing_model: NowPlayingActionModel | None = None,
                 artwork_cache: ArtworkBundleCache | None = None,
                 clock: Callable[[], datetime] | None = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 poll_interval: float = POLL_INTERVAL_SECONDS,
                 tick_interval: float = TICK_INTERVAL_SECONDS) -> None:
        self.api = api
        self.client = client
        self.model = model
        self.now_playing_model = now_playing_model or NowPlayingActionModel()
        self.artwork_cache = artwork_cache or ArtworkBundleCache()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
        self._poll_interval = poll_interval
        self._tick_interval = tick_interval
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._artwork_lock = threading.RLock()
        self._started = False
        self._worker: threading.Thread | None = None
        self._state: ProgressState | None = None
        self._media_state: MediaSnapshot | None = None
        self._artwork_id: str | None = None
        self._next_poll = 0.0
        self._next_tick: float | None = None
        self._dirty = False
        self._retry = False
        self._now_retry = False

    @property
    def worker_alive(self) -> bool:
        return bool(self._worker and self._worker.is_alive())

    def start(self) -> bool:
        with self._lock:
            if self._started or self._stop.is_set():
                return False
            self._started = True
            self._worker = threading.Thread(target=self._work,
                                            name="ulanzi-progress-scheduler", daemon=True)
            self._worker.start()
        return True

    def stop(self, timeout: float = WORKER_STOP_TIMEOUT_SECONDS) -> bool:
        with self._artwork_lock:
            self.artwork_cache.close()
            self.now_playing_model.shutdown()
        self.model.shutdown()
        self._stop.set()
        self._wake.set()
        return self.wait_stopped(timeout)

    def wait_stopped(self, timeout: float | None = None) -> bool:
        worker = self._worker
        if worker and worker.is_alive() and threading.current_thread() is not worker:
            worker.join(None if timeout is None else max(0.0, timeout))
        return not self.worker_alive

    def handle_add(self, event) -> bool:
        try:
            action = action_uuid_from_event(event)
            if action != ACTION_UUID and action not in DISPLAY_ACTION_UUIDS:
                return False
            with self._artwork_lock:
                had_active = bool(self.model.requests() or self.now_playing_model.requests())
                self.artwork_cache.invalidate()
                self.model.clear({"param": [event]})
                self.now_playing_model.clear({"param": [event]})
                requests = (self.model.add(event) if action == ACTION_UUID
                            else self.now_playing_model.add(event))
        except Exception:
            return False
        return self._change(requests, poll_if_first=not had_active)

    def handle_run(self, event) -> bool:
        if action_uuid_from_event(event) != ACTION_UUID:
            return False
        try:
            return self._change(self.model.run(event))
        except Exception:
            return False

    def handle_clear(self, event) -> bool:
        try:
            with self._artwork_lock:
                self.artwork_cache.invalidate()
                changed = self.model.clear(event) | self.now_playing_model.clear(event)
        except Exception:
            return False
        if changed:
            self._signal()
        return changed

    def handle_set_active(self, event) -> bool:
        try:
            with self._artwork_lock:
                had_active = bool(self.model.requests() or self.now_playing_model.requests())
                self.artwork_cache.invalidate()
                requests = self.model.set_active(event) + self.now_playing_model.set_active(event)
        except Exception:
            return False
        return self._change(requests, poll_if_first=not had_active)

    def request_poll(self) -> None:
        with self._lock:
            self._next_poll = 0.0
        self._signal()

    def handle_settings(self, event) -> bool:
        return self._handle_settings_event(event, "settings", False)

    def handle_property_settings(self, event) -> bool:
        return self._handle_settings_event(event, "param", True)

    def _handle_settings_event(self, event, payload_name: str, persist: bool) -> bool:
        if not isinstance(event, Mapping):
            return False
        try:
            context = event.get("context")
            raw = event.get(payload_name)
        except Exception:
            return False
        if not isinstance(context, str) or not context or not isinstance(raw, Mapping):
            return False
        return self._change(self.model.receive_settings(
            {"context": context, "settings": raw}, persist=persist
        ))

    def _change(self, requests: tuple[object, ...], poll_if_first: bool = False) -> bool:
        if not requests:
            return False
        with self._lock:
            self._dirty = True
            if poll_if_first:
                self._next_poll = 0.0
        self._signal()
        return True

    def _signal(self) -> None:
        if not self._stop.is_set():
            self._wake.set()

    def _work(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            requests = self.model.requests()
            now_requests = self.now_playing_model.requests()
            if not requests and not now_requests:
                self._state = None
                self._media_state = None
                self._artwork_id = None
                self.artwork_cache.clear()
                self._next_tick = None
                self._wake.wait()
                continue
            now = self._monotonic()
            polled = now >= self._next_poll
            if polled:
                started = now
                try:
                    result = self.client.get_state(cancelled=self._stop.is_set)
                    state = (normalize_progress_state(result.state, self._clock)
                             if result.ok else unavailable_progress_state(result.status))
                    media_state = (normalize_media_snapshot(result.state, self._clock)
                                   if result.ok else unavailable_media_snapshot(result.status))
                except Exception:
                    state = unavailable_progress_state()
                    media_state = unavailable_media_snapshot()
                changed = state != self._state
                media_changed = media_state != self._media_state
                self._state = state
                self._media_state = media_state
                artwork_changed = media_state.artwork_id != self._artwork_id
                if artwork_changed:
                    self._artwork_id = media_state.artwork_id
                    if self._artwork_id is None:
                        self.artwork_cache.clear()
                    else:
                        self.artwork_cache.begin(self._artwork_id)
                self._next_poll = started + self._poll_interval
            else:
                changed = False
                media_changed = False
                artwork_changed = False
            tick = self._next_tick is not None and now >= self._next_tick
            with self._lock:
                dirty, self._dirty = self._dirty, False
            state = self._state
            if state is not None and (changed or tick or dirty or self._retry):
                persistence_retry = self._persist_all(self.model.persistence_requests())
                self._retry = self._render_all(requests, state) or persistence_retry
            media_state = self._media_state
            if media_state is not None and (media_changed or artwork_changed
                                            or dirty or self._now_retry):
                self._now_retry = self._render_now_all(
                    now_requests, media_state,
                    self.artwork_cache.get(media_state.artwork_id)
                    if media_state.artwork_id else None)
            if (polled and media_state is not None
                    and media_state.online and media_state.available
                    and media_state.artwork_id
                    and self.artwork_cache.get(media_state.artwork_id) is None):
                with self._artwork_lock:
                    fetch_requests = self.now_playing_model.requests()
                    reservation = (self.artwork_cache.reserve(
                        media_state.artwork_id, fetch_requests) if fetch_requests else None)
                if reservation is not None:
                    self._fetch_artwork(media_state, reservation)
            now = self._monotonic()
            playing = (requests and state and state.timeline_available and state.is_playing
                       and extrapolate_position(state, self._clock) < state.duration_seconds)
            if playing and (tick or self._next_tick is None):
                self._next_tick = now + self._tick_interval
            elif not playing:
                self._next_tick = None
            deadlines = [self._next_poll]
            if self._next_tick is not None:
                deadlines.append(self._next_tick)
            self._wake.wait(max(0.0, min(deadlines) - self._monotonic()))

    def _render_all(self, requests: tuple[ProgressRenderRequest, ...], state: ProgressState) -> bool:
        retry = False
        for request in requests:
            intent = None
            if self._stop.is_set():
                return retry
            try:
                intent = self.model.render(request, state, self._clock)
                if intent is None or not self.model.reserve_display_send(intent):
                    continue
                success = bool(self.api.setBaseDataIcon(intent.context, intent.data_uri, ""))
                acknowledged = self.model.acknowledge(intent, success)
                retry |= acknowledged and not success
            except Exception:
                if intent is not None:
                    self.model.acknowledge(intent, False)
                retry = True
        return retry

    def _render_now_all(self, requests, state: MediaSnapshot, bundle) -> bool:
        retry = False
        for request in requests:
            intent = None
            if self._stop.is_set():
                return retry
            try:
                intent = self.now_playing_model.render(request, state, bundle)
                if intent is None or not self.now_playing_model.reserve_send(intent):
                    continue
                sender = (self.api.setBaseDataIcon if intent.method == "setBaseDataIcon"
                          else self.api.setPathIcon)
                success = bool(sender(intent.context, intent.image, intent.text))
                acknowledged = self.now_playing_model.acknowledge(intent, success)
                retry |= acknowledged and not success
            except Exception:
                if intent is not None:
                    self.now_playing_model.acknowledge(intent, False)
                retry = True
        return retry

    def _fetch_artwork(self, state: MediaSnapshot, reservation) -> None:
        artwork_id = state.artwork_id
        try:
            result = self.client.get_artwork(artwork_id, cancelled=self._stop.is_set)
        except Exception:
            return
        if not result.ok or not self.artwork_cache.install(reservation, result.bundle):
            return
        current = self.now_playing_model.requests()
        relevant = tuple(request for request in reservation.relevance if request in current)
        media_state = self._media_state
        if (self._stop.is_set() or media_state is None
                or media_state.artwork_id != artwork_id or not relevant):
            return
        self._now_retry = self._render_now_all(relevant, media_state, result.bundle)

    def _persist_all(self, requests: tuple[PersistenceRequest, ...]) -> bool:
        retry = False
        for request in requests:
            if self._stop.is_set():
                return retry
            try:
                if not self.model.reserve_persistence_send(request):
                    continue
                success = bool(self.api.setSettings(request.settings, request.context))
            except Exception:
                success = False
            if self.model.acknowledge_persistence(
                    request, success, MAX_PERSIST_ATTEMPTS):
                retry |= not success and self.model.is_persistence_current(request)
        return retry


def register_progress_handlers(api, scheduler: ProgressScheduler) -> None:
    api.onAdd(scheduler.handle_add)
    api.onClear(scheduler.handle_clear)
    api.onSetActive(scheduler.handle_set_active)
    api.onParamFromPlugin(scheduler.handle_property_settings)
    api.onDidReceiveSettings(scheduler.handle_settings)
