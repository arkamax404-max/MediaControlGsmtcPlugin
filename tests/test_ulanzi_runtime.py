import importlib.util
import io
import json
import sys
import threading
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
PLUGIN = ROOT / "com.arkamax404.mediacontrold200.ulanziPlugin"
RUNTIME_PATH = PLUGIN / "runtime" / "python" / "mediacontrol_runtime.py"
RUNTIME_DIRECTORY = RUNTIME_PATH.parent


def load_runtime_module():
    sys.path.insert(0, str(RUNTIME_DIRECTORY))
    spec = importlib.util.spec_from_file_location("mediacontrol_runtime", RUNTIME_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(RUNTIME_DIRECTORY))
    return module


class FakeApi:
    def __init__(self):
        self.close_calls = 0
        self.connect_calls = []
        self.connected = threading.Event()
        self.close_callback = None
        self.run_callback = None
        self.listeners = {name: [] for name in
                          ("add", "run", "clear", "setactive", "paramfromplugin",
                           "didReceiveSettings")}
        self.wait_calls = 0
        self.wait_release = threading.Event()

    def onClose(self, callback):
        self.close_callback = callback
        return self

    def onRun(self, callback):
        self.listeners["run"].append(callback)
        self.run_callback = lambda event: any(callback(event) for callback in self.listeners["run"])
        return self

    def onAdd(self, callback):
        self.listeners["add"].append(callback); return self

    def onClear(self, callback):
        self.listeners["clear"].append(callback); return self

    def onSetActive(self, callback):
        self.listeners["setactive"].append(callback); return self

    def onDidReceiveSettings(self, callback):
        self.listeners["didReceiveSettings"].append(callback); return self

    def onParamFromPlugin(self, callback):
        self.listeners["paramfromplugin"].append(callback); return self

    def connect(self, uuid, **kwargs):
        self.connect_calls.append((uuid, kwargs))
        self.connected.set()

    def close(self):
        self.close_calls += 1
        self.wait_release.set()

    def wait(self):
        self.wait_calls += 1
        self.wait_release.wait(10)

    def emit_close(self):
        return self.close_callback(None)


class UlanziRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime_module = load_runtime_module()

    def test_parses_host_arguments_and_preserves_future_values_exactly(self):
        raw = ["localhost", "4567", "es-ES", "--future=value with spaces", 'quoted"value']
        parsed = self.runtime_module.parse_host_arguments(raw)
        self.assertEqual((parsed.address, parsed.port, parsed.language), tuple(raw[:3]))
        self.assertEqual(parsed.future, tuple(raw[3:]))
        self.assertEqual(parsed.raw, tuple(raw))

    def test_manifest_and_folder_use_the_approved_noncolliding_identity(self):
        self.assertEqual(PLUGIN.name, "com.arkamax404.mediacontrold200.ulanziPlugin")
        manifest = json.loads((PLUGIN / "manifest.json").read_text("utf-8"))
        self.assertEqual(manifest["UUID"], self.runtime_module.PLUGIN_UUID)
        self.assertEqual(manifest["CodePath"], "src/app.js")
        self.assertTrue(all(
            action["UUID"].startswith(self.runtime_module.PLUGIN_UUID + ".")
            for action in manifest["Actions"]
        ))

    def test_parser_supplies_sdk_defaults_without_discarding_partial_arguments(self):
        parsed = self.runtime_module.parse_host_arguments(["10.0.0.2"])
        self.assertEqual((parsed.address, parsed.port, parsed.language), ("10.0.0.2", "3906", "en"))
        self.assertEqual(parsed.raw, ("10.0.0.2",))

    def test_websocket_close_stops_runtime_once(self):
        api = FakeApi()
        runtime = self.runtime_module.Runtime(lambda: api)
        arguments = self.runtime_module.parse_host_arguments(["127.0.0.1", "3906", "en", "future"])
        read_gate = threading.Event()

        class BlockingInput:
            def read(self, _size):
                read_gate.wait(2)
                return ""

        thread = threading.Thread(target=runtime.run, args=(arguments, BlockingInput()))
        thread.start()
        self.assertTrue(api.connected.wait(2))
        self.assertIsNotNone(api.run_callback)
        api.emit_close()
        thread.join(2)
        read_gate.set()

        self.assertFalse(thread.is_alive())
        self.assertEqual(api.connect_calls, [(self.runtime_module.PLUGIN_UUID, {"argv": list(arguments.raw)})])
        self.assertEqual((api.close_calls, api.wait_calls), (1, 1))
        self.assertEqual(runtime.stop_reason, "websocket-close")
        self.assertFalse(runtime.router.worker_alive)
        self.assertFalse(runtime.progress_scheduler.worker_alive)
        self.assertTrue(runtime.stop("duplicate"))
        self.assertEqual(api.close_calls, 1)

    def test_runtime_owns_shared_client_scheduler_and_exact_pinned_callbacks(self):
        api = FakeApi()
        runtime = self.runtime_module.Runtime(lambda: api)
        thread = threading.Thread(target=runtime.run,
                                  args=(self.runtime_module.parse_host_arguments([]), io.StringIO("")))
        thread.start(); self.assertTrue(api.connected.wait(1)); thread.join(2)
        self.assertIs(runtime.progress_scheduler.client, runtime.router.client)
        self.assertIs(runtime.progress_scheduler.model, runtime.progress_model)
        self.assertIs(runtime.progress_scheduler.now_playing_model, runtime.now_playing_model)
        self.assertIs(runtime.progress_scheduler.artwork_cache, runtime.artwork_cache)
        self.assertEqual({name: len(items) for name, items in api.listeners.items()}, {
            "add": 1, "run": 1, "clear": 1, "setactive": 1,
            "paramfromplugin": 1, "didReceiveSettings": 1,
        })
        self.assertFalse(runtime.progress_scheduler.worker_alive)

    def test_failed_transport_event_does_not_stop_runtime(self):
        api = FakeApi()

        class FailedRouter:
            def __init__(self):
                self.events = []
                self.starts = 0
                self.stops = 0

            def start(self):
                self.starts += 1
                return self.starts == 1

            def stop(self, _timeout=None):
                self.stops += 1
                return True

            def handle_run(self, event):
                self.events.append(event)
                return False

        router = FailedRouter()
        runtime = self.runtime_module.Runtime(lambda: api, router=router)
        arguments = self.runtime_module.parse_host_arguments(["127.0.0.1", "3906", "en"])
        read_gate = threading.Event()

        class BlockingInput:
            def read(self, _size):
                read_gate.wait(2)
                return ""

        thread = threading.Thread(target=runtime.run, args=(arguments, BlockingInput()))
        thread.start()
        self.assertTrue(api.connected.wait(2))
        event = {"uuid": "com.arkamax404.ulanzi.mediacontrol.next"}
        self.assertFalse(api.run_callback(event))
        self.assertEqual(router.events, [event])
        self.assertFalse(runtime.stopped)
        self.assertEqual(api.close_calls, 0)
        api.emit_close()
        thread.join(2)
        read_gate.set()
        self.assertFalse(thread.is_alive())
        self.assertEqual((router.starts, router.stops), (1, 1))

    def test_stdin_eof_and_repeated_stop_are_idempotent(self):
        api = FakeApi()
        runtime = self.runtime_module.Runtime(lambda: api)
        arguments = self.runtime_module.parse_host_arguments([])

        self.assertEqual(runtime.run(arguments, io.StringIO("")), 0)
        self.assertEqual(runtime.stop_reason, "stdin-eof")
        self.assertEqual((api.close_calls, api.wait_calls), (1, 1))
        self.assertTrue(runtime.stop("signal"))
        api.emit_close()
        self.assertEqual(api.close_calls, 1)

    def test_stop_before_run_creates_no_resources_and_run_exits_cleanly(self):
        api_factory_calls = []
        router_factory_calls = []
        progress_model_factory_calls = []
        progress_scheduler_factory_calls = []
        runtime = self.runtime_module.Runtime(
            lambda: api_factory_calls.append(True),
            router_factory=lambda _arguments: router_factory_calls.append(True),
            progress_model_factory=lambda: progress_model_factory_calls.append(True),
            progress_scheduler_factory=lambda *_args: progress_scheduler_factory_calls.append(True),
        )

        requested = runtime.stop("pre-run")
        self.assertEqual(requested, self.runtime_module.SHUTDOWN_REQUESTED)
        self.assertIsNone(runtime.shutdown_result)
        self.assertEqual(runtime.run(self.runtime_module.parse_host_arguments([]), io.StringIO("")), 0)
        self.assertEqual(api_factory_calls, [])
        self.assertEqual(router_factory_calls, [])
        self.assertEqual(progress_model_factory_calls, [])
        self.assertEqual(progress_scheduler_factory_calls, [])
        self.assertIsNone(runtime.router)
        self.assertIsNone(runtime.progress_model)
        self.assertIsNone(runtime.progress_scheduler)
        self.assertEqual(runtime.shutdown_result, self.runtime_module.ShutdownResult(True, True, "stopped"))

    def test_stop_during_router_start_stops_started_router_once(self):
        api = FakeApi()

        class BlockingStartRouter:
            def __init__(self):
                self.start_entered = threading.Event()
                self.release_start = threading.Event()
                self.start_calls = 0
                self.stop_calls = 0
                self.worker_alive = False

            def start(self):
                self.start_calls += 1
                self.worker_alive = True
                self.start_entered.set()
                self.release_start.wait(2)
                return True

            def stop(self, _timeout=None):
                self.stop_calls += 1
                self.worker_alive = False
                return True

            def handle_run(self, _event):
                return False

        router = BlockingStartRouter()
        runtime = self.runtime_module.Runtime(lambda: api, router=router)
        arguments = self.runtime_module.parse_host_arguments([])
        run_result = []
        run_thread = threading.Thread(target=lambda: run_result.append(runtime.run(arguments, io.StringIO(""))))
        run_thread.start()
        self.assertTrue(router.start_entered.wait(1))
        stop_result = []
        stop_thread = threading.Thread(target=lambda: stop_result.append(runtime.stop("during-start")))
        stop_thread.start()
        self.assertTrue(stop_thread.is_alive())
        router.release_start.set()
        stop_thread.join(2)
        run_thread.join(2)
        self.assertFalse(stop_thread.is_alive())
        self.assertFalse(run_thread.is_alive())
        self.assertEqual(run_result, [0])
        self.assertTrue(stop_result[0])
        self.assertEqual((router.start_calls, router.stop_calls, router.worker_alive), (1, 1, False))
        self.assertEqual(api.close_calls, 1)
        self.assertEqual(api.connect_calls, [])

    def test_four_concurrent_stops_wait_for_same_close_completion(self):
        class BlockingCloseApi(FakeApi):
            def __init__(self):
                super().__init__()
                self.close_started = threading.Event()
                self.release_close = threading.Event()
                self.close_finished = threading.Event()

            def close(self):
                self.close_calls += 1
                self.close_started.set()
                self.release_close.wait(2)
                self.close_finished.set()
                self.wait_release.set()

        api = BlockingCloseApi()
        runtime = self.runtime_module.Runtime(lambda: api)
        run_codes = []
        input_release = threading.Event()

        class BlockingInput:
            def read(self, _size):
                input_release.wait(10)
                return ""

        run_thread = threading.Thread(
            target=lambda: run_codes.append(runtime.run(
                self.runtime_module.parse_host_arguments([]),
                BlockingInput(),
            ))
        )
        run_thread.start()
        self.assertTrue(api.connected.wait(1))
        results = []
        callers = [threading.Thread(target=lambda index=index: results.append(runtime.stop(f"stop-{index}")))
                   for index in range(4)]
        callers[0].start()
        self.assertTrue(api.close_started.wait(1))
        for caller in callers[1:]:
            caller.start()
        time.sleep(0.05)
        self.assertTrue(all(caller.is_alive() for caller in callers))
        self.assertFalse(api.close_finished.is_set())
        api.release_close.set()
        for caller in callers:
            caller.join(2)
        run_thread.join(2)
        self.assertEqual(len(results), 4)
        self.assertTrue(all(result is results[0] for result in results))
        self.assertTrue(results[0])
        self.assertTrue(api.close_finished.is_set())
        self.assertEqual(api.close_calls, 1)
        self.assertEqual(run_codes, [0])
        input_release.set()

    def test_router_stop_failure_propagates_incomplete_shutdown_and_nonzero_run(self):
        api = FakeApi()

        class IncompleteRouter:
            def __init__(self):
                self.stop_calls = 0

            def start(self):
                return True

            def handle_run(self, _event):
                return False

            def stop(self, _timeout=None):
                self.stop_calls += 1
                return False

        router = IncompleteRouter()
        runtime = self.runtime_module.Runtime(lambda: api, router=router)
        run_codes = []
        run_thread = threading.Thread(target=lambda: run_codes.append(
            runtime.run(self.runtime_module.parse_host_arguments([]), io.StringIO(""))
        ))
        run_thread.start()
        self.assertTrue(api.connected.wait(1))
        result = runtime.stop("worker-timeout")
        run_thread.join(2)
        self.assertTrue(result.settled, result)
        self.assertFalse(result.success)
        self.assertEqual(result.reason, "worker_alive")
        self.assertFalse(runtime.stopped)
        self.assertEqual(run_codes, [1])
        self.assertEqual(router.stop_calls, 1)
        self.assertEqual(api.close_calls, 1)

    def test_progress_stop_failure_survives_external_timeout_then_finalizes_once(self):
        class BlockingDisplayApi(FakeApi):
            def __init__(self):
                super().__init__()
                self.send_entered = threading.Event()
                self.send_release = threading.Event()

            def setBaseDataIcon(self, _context, _data, _text):
                self.send_entered.set()
                self.send_release.wait(10)
                return True

        class Router:
            def __init__(self):
                from bridge_client import BridgeStateResult
                self.client = type("Client", (), {
                    "get_state": lambda _self, cancelled=None: BridgeStateResult(
                        "ok", {"available": False, "is_playing": False,
                               "timeline_available": False, "position_seconds": 0,
                               "duration_seconds": 0, "playback_rate": 0,
                               "position_updated_at": "", "updated_at":
                               "2026-08-24T00:00:00+00:00"}, 200)
                })()
                self.stop_calls = 0

            def start(self): return True
            def handle_run(self, _event): return False
            def stop(self, _timeout=None): self.stop_calls += 1; return True

        api, router = BlockingDisplayApi(), Router()
        schedulers = []

        class CountingScheduler(self.runtime_module.ProgressScheduler):
            def __init__(self, *args):
                super().__init__(*args)
                self.stop_calls = 0
                schedulers.append(self)

            def stop(self, timeout=None):
                self.stop_calls += 1
                return super().stop(timeout)

        runtime = self.runtime_module.Runtime(
            lambda: api, router=router, progress_scheduler_factory=CountingScheduler
        )
        arguments = self.runtime_module.parse_host_arguments([])
        run_codes = []
        input_release = threading.Event()

        class BlockingInput:
            def read(self, _size):
                input_release.wait(10)
                return ""

        run_thread = threading.Thread(target=lambda: run_codes.append(
            runtime.run(arguments, BlockingInput())))
        old_progress = self.runtime_module.PROGRESS_STOP_TIMEOUT_SECONDS
        old_external = self.runtime_module.EXTERNAL_STOP_WAIT_SECONDS
        self.runtime_module.PROGRESS_STOP_TIMEOUT_SECONDS = .03
        self.runtime_module.EXTERNAL_STOP_WAIT_SECONDS = .1
        try:
            run_thread.start(); self.assertTrue(api.connected.wait(1))
            api.listeners["add"][0]({
                "uuid": "com.arkamax404.ulanzi.mediacontrol.progress",
                "context": "progress",
            })
            self.assertTrue(api.send_entered.wait(1))
            results = []
            callers = [threading.Thread(target=lambda: results.append(runtime.stop("blocked")))
                       for _ in range(2)]
            callers[0].start()
            self.assertTrue(self._wait_for(lambda: runtime.cleanup_state is not None))
            published_failure = runtime.cleanup_state
            self.assertTrue(runtime.progress_scheduler.worker_alive)
            self.assertEqual(published_failure.reason, "progress_worker_alive")
            self.assertEqual((router.stop_calls, api.close_calls), (0, 0))
            callers[1].start()
            for caller in callers: caller.join(1)
            self.assertFalse(any(caller.is_alive() for caller in callers))
            self.assertEqual(len(results), 2)
            self.assertTrue(all(result is published_failure for result in results))
            self.assertEqual((router.stop_calls, api.close_calls, api.wait_calls), (0, 0, 1))
            self.assertTrue(run_thread.is_alive())
            self.assertEqual(schedulers[0].stop_calls, 1)
            finalized_at = time.monotonic()
            api.send_release.set()
            run_thread.join(1)
            self.assertLess(time.monotonic() - finalized_at, .5)
            self.assertFalse(run_thread.is_alive())
            final_result = runtime.shutdown_result
            self.assertEqual(final_result, self.runtime_module.ShutdownResult(
                True, False, "progress_worker_alive"))
            self.assertIs(runtime.stop("after-finalization"), final_result)
            self.assertEqual(schedulers[0].stop_calls, 1)
            self.assertEqual((router.stop_calls, api.close_calls, api.wait_calls), (1, 1, 1))
            self.assertEqual(run_codes, [1])
        finally:
            api.send_release.set()
            input_release.set()
            self.runtime_module.PROGRESS_STOP_TIMEOUT_SECONDS = old_progress
            self.runtime_module.EXTERNAL_STOP_WAIT_SECONDS = old_external

    def test_shutdown_budget_formula_real_and_scaled(self):
        module = self.runtime_module
        self.assertEqual(module.BOUNDED_SHUTDOWN_SECONDS, 10.0)
        self.assertEqual(module.EXTERNAL_STOP_WAIT_SECONDS, 10.5)
        self.assertEqual(module.EXTERNAL_STOP_WAIT_SECONDS,
                         module.PROGRESS_STOP_TIMEOUT_SECONDS
                         + module.ROUTER_STOP_TIMEOUT_SECONDS
                         + module.API_CLOSE_TIMEOUT_SECONDS
                         + module.API_WAIT_TIMEOUT_SECONDS
                         + module.EXTERNAL_STOP_GRACE_SECONDS)
        scale = .01
        scaled = sum(value * scale for value in (
            module.PROGRESS_STOP_TIMEOUT_SECONDS, module.ROUTER_STOP_TIMEOUT_SECONDS,
            module.API_CLOSE_TIMEOUT_SECONDS, module.API_WAIT_TIMEOUT_SECONDS,
            module.EXTERNAL_STOP_GRACE_SECONDS))
        self.assertAlmostEqual(scaled, .105)

    @staticmethod
    def _wait_for(predicate, timeout=1):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            time.sleep(.002)
        return predicate()

    def test_internal_onclose_during_api_close_returns_immediately(self):
        api = FakeApi()
        runtime = self.runtime_module.Runtime(lambda: api)
        callback_results = []
        callback_elapsed = []
        original_close = api.close

        def close_with_callback():
            started = time.monotonic()
            callback_results.append(api.emit_close())
            callback_elapsed.append(time.monotonic() - started)
            original_close()

        api.close = close_with_callback
        run_codes = []
        run_thread = threading.Thread(target=lambda: run_codes.append(
            runtime.run(self.runtime_module.parse_host_arguments([]), io.StringIO(""))
        ))
        run_thread.start()
        self.assertTrue(api.connected.wait(1))
        result = runtime.stop("external")
        run_thread.join(2)
        self.assertTrue(result)
        self.assertEqual(api.close_calls, 1)
        self.assertEqual(callback_results, [self.runtime_module.SHUTDOWN_REQUESTED])
        self.assertLess(callback_elapsed[0], 0.05)
        self.assertEqual(run_codes, [0])
        self.assertIs(runtime.stop("after"), result)

    def test_api_close_timeout_fails_and_run_returns_bounded(self):
        class BlockingCloseApi(FakeApi):
            def __init__(self):
                super().__init__()
                self.close_started = threading.Event()
                self.release_close = threading.Event()

            def close(self):
                self.close_calls += 1
                self.close_started.set()
                self.release_close.wait(10)

        api = BlockingCloseApi()
        runtime = self.runtime_module.Runtime(lambda: api)
        run_codes = []
        run_thread = threading.Thread(target=lambda: run_codes.append(
            runtime.run(self.runtime_module.parse_host_arguments([]), io.StringIO(""))
        ))
        run_thread.start()
        self.assertTrue(api.connected.wait(1))
        started = time.monotonic()
        result = runtime.stop("close-timeout")
        elapsed = time.monotonic() - started
        run_thread.join(6)
        self.assertFalse(result.success)
        self.assertEqual(result.reason, "api_close_timeout", result)
        self.assertEqual(run_codes, [1])
        self.assertLess(elapsed, 6)
        api.release_close.set()
        api.wait_release.set()
        self.assertTrue(api.close_started.wait(1))

    def test_api_wait_timeout_fails_and_run_returns_bounded(self):
        class BlockingWaitApi(FakeApi):
            def close(self):
                self.close_calls += 1

        api = BlockingWaitApi()
        runtime = self.runtime_module.Runtime(lambda: api)
        run_codes = []
        run_thread = threading.Thread(target=lambda: run_codes.append(
            runtime.run(self.runtime_module.parse_host_arguments([]), io.StringIO(""))
        ))
        run_thread.start()
        self.assertTrue(api.connected.wait(1))
        started = time.monotonic()
        result = runtime.stop("wait-timeout")
        elapsed = time.monotonic() - started
        run_thread.join(4)
        self.assertFalse(result.success)
        self.assertEqual(result.reason, "api_wait_timeout", result)
        self.assertEqual(run_codes, [1])
        self.assertLess(elapsed, 4)
        api.wait_release.set()

    def test_no_non_daemon_runtime_threads_remain(self):
        leaked = [thread.name for thread in threading.enumerate()
                  if thread.name.startswith("ulanzi-") and not thread.daemon]
        self.assertEqual(leaked, [])


if __name__ == "__main__":
    unittest.main()
