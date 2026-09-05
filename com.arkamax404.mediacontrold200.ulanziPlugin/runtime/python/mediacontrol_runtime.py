from __future__ import annotations

import signal
import sys
import threading
from dataclasses import dataclass
from typing import Callable, Sequence, TextIO

from bridge_client import BridgeClient, bridge_origin_from_future
from artwork_bundle import ArtworkBundleCache
from now_playing_action import NowPlayingActionModel
from progress_action import ProgressActionModel
from progress_scheduler import (WORKER_STOP_TIMEOUT_SECONDS as PROGRESS_STOP_TIMEOUT_SECONDS,
                                ProgressScheduler, register_progress_handlers)
from transport_actions import (WORKER_STOP_TIMEOUT_SECONDS as ROUTER_STOP_TIMEOUT_SECONDS,
                               TransportRouter, register_transport_handlers)


PLUGIN_UUID = "com.arkamax404.ulanzi.mediacontrol"
API_CLOSE_TIMEOUT_SECONDS = 2.5
API_WAIT_TIMEOUT_SECONDS = 2.5
EXTERNAL_STOP_GRACE_SECONDS = 0.5
BOUNDED_SHUTDOWN_SECONDS = (PROGRESS_STOP_TIMEOUT_SECONDS + ROUTER_STOP_TIMEOUT_SECONDS
                            + API_CLOSE_TIMEOUT_SECONDS + API_WAIT_TIMEOUT_SECONDS)
EXTERNAL_STOP_WAIT_SECONDS = BOUNDED_SHUTDOWN_SECONDS + EXTERNAL_STOP_GRACE_SECONDS


@dataclass(frozen=True)
class HostArguments:
    address: str
    port: str
    language: str
    future: tuple[str, ...]
    raw: tuple[str, ...]


@dataclass(frozen=True)
class ShutdownResult:
    settled: bool
    success: bool
    reason: str

    def __bool__(self) -> bool:
        return self.success


SHUTDOWN_REQUESTED = ShutdownResult(False, False, "requested")
SHUTDOWN_WAIT_TIMEOUT = ShutdownResult(False, False, "wait_timeout")


def parse_host_arguments(argv: Sequence[str]) -> HostArguments:
    raw = tuple(argv)
    return HostArguments(
        address=raw[0] if len(raw) > 0 else "127.0.0.1",
        port=raw[1] if len(raw) > 1 else "3906",
        language=raw[2] if len(raw) > 2 else "en",
        future=raw[3:],
        raw=raw,
    )


def create_ulanzi_api():
    from ulanzi_api import UlanziApi

    return UlanziApi()


class Runtime:
    def __init__(
        self,
        api_factory: Callable[[], object] = create_ulanzi_api,
        router: TransportRouter | None = None,
        router_factory: Callable[[HostArguments], TransportRouter] | None = None,
        progress_model_factory: Callable[[], ProgressActionModel] = ProgressActionModel,
        now_playing_model_factory: Callable[[], NowPlayingActionModel] = NowPlayingActionModel,
        artwork_cache_factory: Callable[[], ArtworkBundleCache] = ArtworkBundleCache,
        progress_scheduler_factory: Callable[..., ProgressScheduler] = ProgressScheduler,
    ) -> None:
        self._api_factory = api_factory
        self._api = None
        self._lock = threading.Lock()
        self._stop_requested = threading.Event()
        self._shutdown_settled = threading.Event()
        self._shutdown_result: ShutdownResult | None = None
        self._cleanup_state: ShutdownResult | None = None
        self._run_started = False
        self._run_thread: int | None = None
        self._api_close_called = False
        self._router_stop_called = False
        self._progress_stop_called = False
        self._api_wait_thread: threading.Thread | None = None
        self._api_wait_done = threading.Event()
        self._api_wait_failed = False
        self.stop_reason: str | None = None
        self.router = router
        self.progress_model: ProgressActionModel | None = None
        self.now_playing_model: NowPlayingActionModel | None = None
        self.artwork_cache: ArtworkBundleCache | None = None
        self.progress_scheduler: ProgressScheduler | None = None
        self._progress_model_factory = progress_model_factory
        self._now_playing_model_factory = now_playing_model_factory
        self._artwork_cache_factory = artwork_cache_factory
        self._progress_scheduler_factory = progress_scheduler_factory
        self._router_factory = router_factory or (
            lambda arguments: TransportRouter(
                BridgeClient(origin=bridge_origin_from_future(arguments.future))
            )
        )

    @property
    def stopped(self) -> bool:
        with self._lock:
            return bool(self._shutdown_result and self._shutdown_result.success)

    @property
    def shutdown_result(self) -> ShutdownResult | None:
        with self._lock:
            return self._shutdown_result

    @property
    def cleanup_state(self) -> ShutdownResult | None:
        with self._lock:
            return self._cleanup_state

    def request_stop(self, reason: str) -> ShutdownResult:
        with self._lock:
            if self.stop_reason is None:
                self.stop_reason = reason
        self._stop_requested.set()
        return SHUTDOWN_REQUESTED

    def stop(self, reason: str) -> ShutdownResult:
        self.request_stop(reason)
        with self._lock:
            result = self._shutdown_result
            run_started = self._run_started
            run_thread = self._run_thread
        if result is not None:
            return result
        if not run_started or run_thread == threading.get_ident():
            return SHUTDOWN_REQUESTED
        if not self._shutdown_settled.wait(EXTERNAL_STOP_WAIT_SECONDS):
            cleanup_state = self.cleanup_state
            return cleanup_state if cleanup_state is not None else SHUTDOWN_WAIT_TIMEOUT
        result = self.shutdown_result
        return result if result is not None else SHUTDOWN_WAIT_TIMEOUT

    def _close_api_once(self, api) -> bool:
        with self._lock:
            if self._api_close_called:
                return True
            self._api_close_called = True
        completed = threading.Event()
        errors = []

        def close_api():
            try:
                api.close()
            except Exception as error:
                errors.append(error)
            finally:
                completed.set()

        close_thread = threading.Thread(
            target=close_api,
            name="ulanzi-api-close",
            daemon=True,
        )
        close_thread.start()
        if not completed.wait(API_CLOSE_TIMEOUT_SECONDS):
            return False
        if errors:
            raise errors[0]
        return True

    def _start_api_wait(self, api) -> None:
        def wait_api():
            try:
                api.wait()
            except Exception:
                self._api_wait_failed = True
            finally:
                self._api_wait_done.set()
                self.request_stop("api-wait-ended")

        self._api_wait_thread = threading.Thread(
            target=wait_api,
            name="ulanzi-api-wait",
            daemon=True,
        )
        self._api_wait_thread.start()

    def _cleanup(self, setup_failure: str | None = None) -> ShutdownResult:
        failure = setup_failure
        router = self.router
        progress = self.progress_scheduler
        api = self._api
        if progress is not None and not self._progress_stop_called:
            self._progress_stop_called = True
            try:
                if not progress.stop(PROGRESS_STOP_TIMEOUT_SECONDS):
                    failure = failure or "progress_worker_alive"
                    self._set_cleanup_state(ShutdownResult(False, False, failure))
                    progress.wait_stopped()
            except Exception:
                failure = failure or "progress_stop_failed"
                self._set_cleanup_state(ShutdownResult(False, False, failure))
                if progress.worker_alive:
                    progress.wait_stopped()
        if router is not None and not self._router_stop_called:
            self._router_stop_called = True
            try:
                if not router.stop(ROUTER_STOP_TIMEOUT_SECONDS):
                    failure = failure or "worker_alive"
            except Exception:
                failure = failure or "router_stop_failed"
        if api is not None:
            try:
                if not self._close_api_once(api):
                    failure = failure or "api_close_timeout"
            except Exception:
                failure = failure or "api_close_failed"
        if self._api_wait_thread is not None:
            if not self._api_wait_done.wait(API_WAIT_TIMEOUT_SECONDS):
                failure = failure or "api_wait_timeout"
            elif self._api_wait_failed:
                failure = failure or "api_wait_failed"
        return self._publish_result(ShutdownResult(True, failure is None, failure or "stopped"))

    def _set_cleanup_state(self, state: ShutdownResult) -> ShutdownResult:
        with self._lock:
            if self._cleanup_state is None:
                self._cleanup_state = state
            return self._cleanup_state

    def _publish_result(self, result: ShutdownResult) -> ShutdownResult:
        with self._lock:
            if self._shutdown_result is None:
                self._shutdown_result = result
                self._cleanup_state = result
                self._shutdown_settled.set()
            return self._shutdown_result

    def _watch_stdin(self, stream: TextIO) -> None:
        while stream.read(4096) not in ("", b""):
            pass
        self.request_stop("stdin-eof")

    def run(self, arguments: HostArguments, stdin: TextIO = sys.stdin) -> int:
        with self._lock:
            if self._run_started:
                return 1
            self._run_started = True
            self._run_thread = threading.get_ident()
        if self._stop_requested.is_set():
            result = self._publish_result(ShutdownResult(True, True, "stopped"))
            return 0 if result.success else 1

        setup_failure = None
        try:
            api = self._api_factory()
            self._api = api
            if not self._stop_requested.is_set() and self.router is None:
                self.router = self._router_factory(arguments)
            if not self._stop_requested.is_set() and self.router is not None:
                self.router.start()
            if not self._stop_requested.is_set():
                api.onClose(lambda _event: self.request_stop("websocket-close"))
                register_transport_handlers(api, self.router)
                client = getattr(self.router, "client", None)
                if client is not None:
                    self.progress_model = self._progress_model_factory()
                    self.now_playing_model = self._now_playing_model_factory()
                    self.artwork_cache = self._artwork_cache_factory()
                    self.progress_scheduler = self._progress_scheduler_factory(
                        api, client, self.progress_model,
                        self.now_playing_model, self.artwork_cache
                    )
                    configure = getattr(self.router, "configure_runtime", None)
                    if configure is not None:
                        configure(self.progress_scheduler.handle_run,
                                  self.progress_scheduler.request_poll,
                                  self.now_playing_model.audio_target_from_event)
                    self.progress_scheduler.start()
                    register_progress_handlers(api, self.progress_scheduler)
                api.connect(PLUGIN_UUID, argv=list(arguments.raw))
                self._start_api_wait(api)
            if not self._stop_requested.is_set():
                threading.Thread(
                    target=self._watch_stdin,
                    args=(stdin,),
                    name="ulanzi-stdin-lifecycle",
                    daemon=True,
                ).start()
        except Exception:
            setup_failure = "setup_failed"
            self.request_stop(setup_failure)

        self._stop_requested.wait()
        result = self._cleanup(setup_failure)
        return 0 if result.success else 1


def main(argv: Sequence[str] | None = None, stdin: TextIO = sys.stdin) -> int:
    runtime = Runtime()

    def stop_from_signal(signum, _frame) -> None:
        runtime.request_stop(signal.Signals(signum).name.lower())

    signal.signal(signal.SIGINT, stop_from_signal)
    signal.signal(signal.SIGTERM, stop_from_signal)
    return runtime.run(parse_host_arguments(sys.argv[1:] if argv is None else argv), stdin)


if __name__ == "__main__":
    raise SystemExit(main())
