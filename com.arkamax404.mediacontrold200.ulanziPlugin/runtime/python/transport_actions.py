from __future__ import annotations

import queue
import threading
from collections.abc import Mapping

from bridge_client import BridgeClient, BridgeResult


PLUGIN_UUID = "com.arkamax404.ulanzi.mediacontrol"
ACTION_COMMANDS = {
    f"{PLUGIN_UUID}.nowplaying": "toggle",
    f"{PLUGIN_UUID}.previous": "previous",
    f"{PLUGIN_UUID}.toggle": "toggle",
    f"{PLUGIN_UUID}.next": "next",
    f"{PLUGIN_UUID}.volume-up": "volume-up",
    f"{PLUGIN_UUID}.volume-down": "volume-down",
    f"{PLUGIN_UUID}.mute-toggle": "mute-toggle",
}
DEFAULT_QUEUE_CAPACITY = 16
WORKER_STOP_TIMEOUT_SECONDS = 2.5
_STOP = object()


def action_uuid_from_event(event) -> str | None:
    if not isinstance(event, Mapping):
        return None
    try:
        for name in ("uuid", "action"):
            value = event.get(name)
            if isinstance(value, str) and value:
                return str.__str__(str(value))
        context = event.get("context")
        if isinstance(context, str) and context:
            return str.__str__(str(context)).split("___", 1)[0]
    except Exception:
        return None
    return None


def command_from_event(event) -> str | None:
    return ACTION_COMMANDS.get(action_uuid_from_event(event))


class TransportRouter:
    def __init__(
        self,
        client: BridgeClient | None = None,
        queue_capacity: int = DEFAULT_QUEUE_CAPACITY,
    ) -> None:
        if not isinstance(queue_capacity, int) or isinstance(queue_capacity, bool) or queue_capacity <= 0:
            raise ValueError("Transport queue capacity must be positive")
        self.client = client or BridgeClient()
        self.last_result: BridgeResult | None = None
        self.last_enqueue_result: BridgeResult | None = None
        self.discarded_count = 0
        self._queue = queue.Queue(maxsize=queue_capacity)
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._started = False
        self._accepting = False
        self._progress_run = None
        self._poll_notifier = None
        self._audio_target_resolver = None
        self._worker = threading.Thread(
            target=self._work,
            name="ulanzi-bridge-transport",
            daemon=True,
        )

    @property
    def worker_alive(self) -> bool:
        return self._worker.is_alive()

    def start(self) -> bool:
        with self._state_lock:
            if self._started or self._stop_event.is_set():
                return False
            self._started = True
            self._accepting = True
            self._worker.start()
            return True

    def handle_run(self, event) -> bool:
        command = command_from_event(event)
        if command is None:
            try:
                return bool(self._progress_run and self._progress_run(event))
            except Exception:
                return False
        with self._state_lock:
            if not self._accepting:
                self.last_enqueue_result = BridgeResult(command, "stopped")
                return False
            try:
                target = (self._audio_target_resolver(event)
                          if command in ("volume-up", "volume-down", "mute-toggle")
                          and self._audio_target_resolver else None)
                self._queue.put_nowait((command, target))
            except queue.Full:
                self.last_enqueue_result = BridgeResult(command, "queue_full")
                return False
            self.last_enqueue_result = BridgeResult(command, "queued")
            return True

    def configure_runtime(self, progress_run, poll_notifier,
                          audio_target_resolver=None) -> None:
        with self._state_lock:
            self._progress_run = progress_run
            self._poll_notifier = poll_notifier
            self._audio_target_resolver = audio_target_resolver

    def stop(self, timeout: float = WORKER_STOP_TIMEOUT_SECONDS) -> bool:
        with self._state_lock:
            first_stop = not self._stop_event.is_set()
            self._accepting = False
            self._stop_event.set()
        if first_stop:
            self._discard_pending()
            self._queue.put_nowait(_STOP)
        if self._worker.is_alive() and threading.current_thread() is not self._worker:
            self._worker.join(max(0.0, timeout))
        return not self._worker.is_alive()

    def _discard_pending(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            try:
                if item is not _STOP:
                    self.discarded_count += 1
                    self.last_result = BridgeResult(str(item), "discarded")
            finally:
                self._queue.task_done()

    def _work(self) -> None:
        while True:
            command = self._queue.get()
            try:
                if command is _STOP:
                    return
                command, audio_target = command
                if self._stop_event.is_set():
                    self.discarded_count += 1
                    self.last_result = BridgeResult(str(command), "discarded")
                    continue
                try:
                    arguments = {"cancelled": self._stop_event.is_set}
                    if audio_target is not None:
                        arguments["audio_target"] = audio_target
                    self.last_result = self.client.execute(command, **arguments)
                except Exception:
                    self.last_result = BridgeResult(str(command), "unavailable")
                if self.last_result.ok:
                    try:
                        if self._poll_notifier is not None:
                            self._poll_notifier()
                    except Exception:
                        pass
            finally:
                self._queue.task_done()


def register_transport_handlers(api, router: TransportRouter) -> None:
    api.onRun(router.handle_run)
