import json
import math
import sys
import base64
import subprocess
import threading
import time
import unittest
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).parents[1]
RUNTIME = ROOT / "com.arkamax404.mediacontrold200.ulanziPlugin" / "runtime" / "python"
sys.path.insert(0, str(RUNTIME))

from artwork_bundle import ArtworkBundle, ArtworkBundleCache  # noqa: E402
from bridge_client import (BridgeArtworkResult, BridgeClient, BridgeResult,
                           BridgeStateResult)  # noqa: E402
from now_playing_action import (ACTION_UUID as NOW_PLAYING_UUID,
                                AUDIO_ACTIONS,
                                MOSAIC_ACTIONS,
                                MUTE_TOGGLE_UUID,
                                PREVIOUS_UUID,
                                TOGGLE_UUID,
                                TRANSPORT_DISPLAY,
                                MediaSnapshot, NowPlayingActionModel,
                                mute_toggle_data_uri)  # noqa: E402
from progress_action import (  # noqa: E402
    ACTION_UUID,
    ProgressActionModel,
    ProgressSettings,
    _fixed_three,
    normalize_progress_settings,
    progress_settings_payload,
    render_progress_svg,
)
from progress_state import (  # noqa: E402
    extrapolate_position,
    format_progress_time,
    next_progress_mode,
    normalize_progress_state,
    unavailable_progress_state,
)
from progress_scheduler import ProgressScheduler, register_progress_handlers  # noqa: E402
from transport_actions import TransportRouter  # noqa: E402


TOKEN = "A" * 43
INSTANCE_ID = "123e4567-e89b-42d3-a456-426614174000"
NOW = datetime(2026, 8, 23, 12, 0, 15, tzinfo=timezone.utc)


def health(**overrides):
    return {"service": "d200-gsmtc-bridge", "api_major": 1, "api_minor": 0,
            "status": "ready", "instance_id": INSTANCE_ID, **overrides}


def state(**overrides):
    return {"available": True, "is_playing": True, "timeline_available": True,
            "position_seconds": 0, "duration_seconds": 180, "playback_rate": 1,
            "position_updated_at": "2026-08-23T12:00:10Z",
            "updated_at": "2026-08-23T12:00:00+00:00", **overrides}


class Response:
    def __init__(self, payload=None, status=200, raw=None):
        self.status = status
        self.body = raw if raw is not None else json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, amount=-1):
        return self.body if amount < 0 else self.body[:amount]


class Opener:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response


class PythonProgressTests(unittest.TestCase):
    @staticmethod
    def _wait_for(predicate, timeout=1):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.002)
        return predicate()

    def test_state_get_reuses_health_snapshot_auth_and_timeout(self):
        payload = state()
        opener = Opener([Response(health()), Response(payload)])
        result = BridgeClient(token_loader=lambda: TOKEN, opener=opener).get_state()
        self.assertEqual(result, BridgeStateResult("ok", payload, 200))
        self.assertEqual([(call[0].full_url, call[0].method, call[1]) for call in opener.calls], [
            ("http://127.0.0.1:43821/health", "GET", 1.0),
            ("http://127.0.0.1:43821/state", "GET", 1.0),
        ])
        headers = {key.lower(): value for key, value in opener.calls[1][0].header_items()}
        self.assertEqual(headers, {"authorization": f"Bearer {TOKEN}",
                                   "x-companion-instance": INSTANCE_ID})

    def test_state_get_contains_auth_errors_invalid_json_size_and_schema(self):
        cases = [
            (lambda: "bad", [], "configuration"),
            (lambda: TOKEN, [Response(health(api_major=2))], "incompatible"),
            (lambda: TOKEN, [Response(health()), TimeoutError()], "unavailable"),
            (lambda: TOKEN, [Response(health()), Response(status=503, payload={})], "unavailable"),
            (lambda: TOKEN, [Response(health()), Response(raw=b"{")], "unavailable"),
            (lambda: TOKEN, [Response(health()), Response(payload=[])], "unavailable"),
            (lambda: TOKEN, [Response(health()), Response(raw=b"x" * 4097)], "unavailable"),
        ]
        for loader, responses, expected in cases:
            with self.subTest(expected=expected, count=len(responses)):
                result = BridgeClient(token_loader=loader, opener=Opener(responses)).get_state()
                self.assertEqual(result.status, expected)

    def test_state_get_cancellation_stops_before_waiting_for_serial_turn(self):
        self.assertEqual(BridgeClient(token_loader=lambda: TOKEN, opener=Opener([]))
                         .get_state(lambda: True).status, "stopped")
        occupied = threading.Event()
        release = threading.Event()

        class Probe(BridgeClient):
            def _execute(self, command, cancelled=None):
                occupied.set(); release.wait(.5); return BridgeResult(command, "ok")

        client = Probe(token_loader=lambda: TOKEN)
        request = threading.Thread(target=client.execute, args=("next",))
        request.start(); self.assertTrue(occupied.wait(.5))
        self.assertEqual(client.get_state(lambda: True).status, "stopped")
        release.set(); request.join(.5)

    def test_reentrant_cancellation_and_request_callbacks_run_without_request_lock(self):
        lock_checks = []

        class Probe(BridgeClient):
            def _execute(self, command, cancelled=None):
                acquired = self._request_lock.acquire(blocking=False)
                lock_checks.append(acquired)
                if acquired:
                    self._request_lock.release()
                return BridgeResult(command, "ok")

            def _get_state(self, cancelled=None):
                acquired = self._request_lock.acquire(blocking=False)
                lock_checks.append(acquired)
                if acquired:
                    self._request_lock.release()
                return BridgeStateResult("configuration")

        client = Probe(token_loader=lambda: TOKEN)
        reentered = []

        def cancelled():
            reentered.append(client.get_state())
            return True

        results = []
        thread = threading.Thread(target=lambda: results.append(client.execute("next", cancelled)),
                                  daemon=True)
        thread.start(); thread.join(1)
        self.assertFalse(thread.is_alive(), "reentrant cancellation deadlocked")
        self.assertEqual(results, [BridgeResult("next", "stopped")])
        self.assertEqual(reentered, [BridgeStateResult("configuration")])
        self.assertEqual(lock_checks, [True])

        callback_lock_checks = []
        client_ref = []

        def assert_lock_free():
            acquired = client_ref[0]._request_lock.acquire(blocking=False)
            callback_lock_checks.append(acquired)
            if acquired:
                client_ref[0]._request_lock.release()

        def token_loader():
            assert_lock_free()
            return TOKEN

        class LockCheckingOpener(Opener):
            def __call__(self, request, timeout):
                assert_lock_free()
                return super().__call__(request, timeout)

        client_ref.append(BridgeClient(
            token_loader=token_loader,
            opener=LockCheckingOpener([Response(health()), Response(state())]),
        ))
        self.assertTrue(client_ref[0].get_state().ok)
        self.assertEqual(callback_lock_checks, [True, True, True])

    def test_normalizes_fresh_schema_zero_position_and_status_labels(self):
        normalized = normalize_progress_state(state(), lambda: NOW)
        self.assertTrue(normalized.timeline_available)
        self.assertEqual(normalized.position_seconds, 0)
        self.assertEqual(normalized.status, "ready")
        for reason, label in (("configuration", "Companion setup required"),
                              ("incompatible", "Incompatible companion"),
                              ("unavailable", "Offline")):
            self.assertEqual(unavailable_progress_state(reason).label, label)
        self.assertEqual(normalize_progress_state(
            state(available=False), lambda: NOW).label, "No timeline")
        self.assertEqual(normalize_progress_state(
            state(timeline_available=False, position_updated_at=""), lambda: NOW).label,
            "No timeline")

    def test_rejects_stale_future_non_utc_wrong_types_and_nonfinite_numbers(self):
        invalid = [
            state(updated_at=(NOW - timedelta(seconds=15, microseconds=1)).isoformat()),
            state(updated_at=(NOW + timedelta(microseconds=1)).isoformat()),
            state(updated_at="2026-08-23T14:00:15+02:00"),
            state(available=1), state(position_seconds="0"),
            state(duration_seconds=math.inf), state(playback_rate=math.nan),
            state(position_updated_at="2026-08-23T14:00:10+02:00"),
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assertEqual(normalize_progress_state(payload, lambda: NOW).status, "offline")
        self.assertTrue(normalize_progress_state(
            state(updated_at=(NOW - timedelta(seconds=15)).isoformat()),
            lambda: NOW).online)

    def test_position_anchor_schema_is_strict_for_available_and_unavailable_timelines(self):
        omitted = state(timeline_available=False)
        omitted.pop("position_updated_at")
        cases = [
            ("omitted unavailable", omitted, "offline"),
            ("null unavailable", state(timeline_available=False,
                                       position_updated_at=None), "offline"),
            ("numeric unavailable", state(timeline_available=False,
                                          position_updated_at=0), "offline"),
            ("malformed unavailable", state(timeline_available=False,
                                            position_updated_at="invalid"), "offline"),
            ("empty unavailable", state(timeline_available=False,
                                        position_updated_at=""), "no_timeline"),
            ("malformed available", state(position_updated_at="invalid"), "offline"),
            ("valid available", state(position_updated_at="2026-08-23T12:00:10Z"),
             "ready"),
        ]
        for name, payload, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(normalize_progress_state(payload, lambda: NOW).status,
                                 expected)

    def test_extrapolates_only_playing_from_anchor_and_clamps_both_bounds(self):
        playing = normalize_progress_state(state(position_seconds=175, playback_rate=2), lambda: NOW)
        self.assertEqual(extrapolate_position(playing, lambda: NOW), 180)
        paused = normalize_progress_state(state(is_playing=False, position_seconds=20), lambda: NOW)
        self.assertEqual(extrapolate_position(paused, lambda: NOW + timedelta(hours=1)), 20)
        future_anchor = normalize_progress_state(
            state(position_seconds=5, position_updated_at="2026-08-23T12:01:00Z"), lambda: NOW)
        self.assertEqual(extrapolate_position(future_anchor, lambda: NOW), 5)
        self.assertEqual(normalize_progress_state(
            state(position_seconds=999), lambda: NOW).position_seconds, 180)

    def test_modes_and_format_boundaries_match_node_rounding(self):
        self.assertEqual([next_progress_mode(value) for value in
                          ("remaining", "elapsed", "total", None)],
                         ["elapsed", "total", "remaining", "elapsed"])
        cases = [
            (None, 0, 0, "0:00"), ("remaining", 64.99, 65, "0:01"),
            ("elapsed", 60.9, 3661.2, "1:00"),
            ("total", 60.9, 3661.2, "1:01:02"),
            ("elapsed", 4000, 3661.2, "1:01:01"),
            ("remaining", -1, 65, "1:05"),
        ]
        for mode, position, duration, expected in cases:
            self.assertEqual(format_progress_time(mode, position, duration), expected)

    def test_progress_context_lifecycle_modes_settings_and_recreation(self):
        model = ProgressActionModel()
        self.assertEqual(ACTION_UUID, "com.arkamax404.ulanzi.mediacontrol.progress")
        first = model.add({"uuid": ACTION_UUID, "context": "full___key___action",
                           "param": {"progressColor": "#abcdef"}})[0]
        second = model.add({"uuid": ACTION_UUID, "context": "two", "param": {}})[0]
        self.assertEqual(model.context(first.context).settings.progress_color, "#ABCDEF")
        model.run({"context": first.context})
        model.receive_settings({"context": "two", "settings": {"strokeWidth": "30"}})
        self.assertEqual(model.context(first.context).mode, "elapsed")
        self.assertEqual(model.context("two").mode, "remaining")
        self.assertEqual(model.context("two").settings.stroke_width, 30)
        self.assertEqual(model.set_active({"context": first.context, "active": False}), ())
        self.assertFalse(model.context(first.context).active)
        self.assertEqual(len(model.set_active({"context": first.context, "active": True})), 1)
        model.clear({"param": [{"context": first.context}]})
        self.assertIsNone(model.context(first.context))
        recreated = model.add({"uuid": ACTION_UUID, "context": first.context})[0]
        self.assertGreater(recreated.generation, second.generation)
        self.assertEqual(model.context(first.context).mode, "remaining")

    def test_progress_settings_match_node_boundaries_bool_and_nonfinite(self):
        defaults = ProgressSettings()
        self.assertEqual(normalize_progress_settings(), defaults)
        normalized = normalize_progress_settings({
            "progressColor": "#abcdef", "trackColor": "bad",
            "textColor": "#123456", "backgroundColor": "#654321",
            "strokeWidth": "5.6",
        })
        self.assertEqual(normalized, ProgressSettings("#ABCDEF", "#333333", "#123456",
                                                       "#654321", 6))
        for value, expected in [(5, 6), (30.4, 30), (99, 30), (7.5, 8),
                                (False, 6), (True, 6), (None, 6), ("", 6),
                                (math.nan, 14), (math.inf, 14), ("bad", 14)]:
            with self.subTest(value=value):
                self.assertEqual(normalize_progress_settings({"strokeWidth": value}).stroke_width,
                                 expected)

    def test_svg_three_decimal_ties_match_node_for_complete_arc_domain(self):
        circumference = 2 * math.pi * 70
        values = [(2 * index + 1) / 16
                  for index in range(math.floor(circumference * 8) + 1)]
        script = ("let input=''; process.stdin.setEncoding('utf8'); "
                  "process.stdin.on('data', chunk => input += chunk); "
                  "process.stdin.on('end', () => process.stdout.write(JSON.stringify("
                  "JSON.parse(input).map(value => value.toFixed(3)))));")
        completed = subprocess.run(
            ["node", "-e", script], input=json.dumps(values), text=True,
            capture_output=True, check=True, timeout=10,
        )
        expected = json.loads(completed.stdout)
        self.assertEqual(len(values), 3519)
        self.assertEqual([_fixed_three(value) for value in values], expected)

    def test_exact_deterministic_svg_and_data_uri_for_progress_and_statuses(self):
        model = ProgressActionModel()
        request = model.add({"uuid": ACTION_UUID, "context": "progress"})[0]
        cases = [
            (state(position_seconds=0, is_playing=False), "3:00", "0.000 439.823"),
            (state(position_seconds=90, is_playing=False), "1:30", "219.911 439.823"),
            (state(position_seconds=180, is_playing=False), "0:00", "439.823 439.823"),
        ]
        for payload, text, dash in cases:
            snapshot = normalize_progress_state(payload, lambda: NOW)
            intent = model.render(request, snapshot, lambda: NOW)
            svg = base64.b64decode(intent.data_uri.split(",", 1)[1]).decode()
            self.assertEqual(svg, render_progress_svg(
                snapshot.position_seconds / snapshot.duration_seconds, text, ProgressSettings()))
            self.assertIn('width="196" height="196" viewBox="0 0 196 196"', svg)
            self.assertIn('transform="rotate(-90 98 98)"', svg)
            self.assertIn(f'stroke-dasharray="{dash}"', svg)
            self.assertNotIn("image", svg)
        for reason, label in [("configuration", "Companion setup required"),
                              ("incompatible", "Incompatible companion"),
                              ("unavailable", "Offline")]:
            svg = model.render(request, unavailable_progress_state(reason), lambda: NOW).signature
            self.assertIn(f">{label}</text>", svg)
        no_timeline = normalize_progress_state(
            state(timeline_available=False, position_updated_at=""), lambda: NOW)
        self.assertIn(">No timeline</text>", model.render(request, no_timeline, lambda: NOW).signature)
        self.assertEqual(model.render(request, normalize_progress_state(cases[1][0], lambda: NOW),
                                      lambda: NOW).data_uri,
                         model.render(request, normalize_progress_state(cases[1][0], lambda: NOW),
                                      lambda: NOW).data_uri)

    def test_time_modes_dedup_retry_and_stale_acknowledgements(self):
        model = ProgressActionModel()
        request = model.add({"uuid": ACTION_UUID, "context": "progress"})[0]
        snapshot = normalize_progress_state(state(position_seconds=60.9, duration_seconds=125.4,
                                                   is_playing=False), lambda: NOW)
        expected = ["1:05", "1:00", "2:06"]
        intents = []
        for text in expected:
            current = model.requests()[0]
            intent = model.render(current, snapshot, lambda: NOW)
            self.assertIn(f">{text}</text>", intent.signature)
            intents.append(intent)
            if text != expected[-1]:
                model.run({"context": "progress"})
        self.assertFalse(model.acknowledge(intents[0], True), "old mode acknowledgement is stale")
        self.assertTrue(model.acknowledge(intents[-1], False))
        self.assertIsNotNone(model.render(model.requests()[0], snapshot, lambda: NOW))
        retry = model.render(model.requests()[0], snapshot, lambda: NOW)
        self.assertTrue(model.acknowledge(retry, True))
        self.assertIsNone(model.render(model.requests()[0], snapshot, lambda: NOW))
        model.clear({"param": [{"context": "progress"}]})
        recreated = model.add({"uuid": ACTION_UUID, "context": "progress"})[0]
        self.assertFalse(model.acknowledge(retry, True))
        model.set_active({"context": "progress", "active": False})
        self.assertIsNone(model.render(recreated, snapshot, lambda: NOW))

    def test_mode_settings_and_activation_round_trips_invalidate_dedup_per_version(self):
        snapshot = normalize_progress_state(state(position_seconds=60, duration_seconds=120,
                                                   is_playing=False), lambda: NOW)

        def seed(model):
            request = model.add({"uuid": ACTION_UUID, "context": "progress"})[0]
            intent = model.render(request, snapshot, lambda: NOW)
            self.assertTrue(model.acknowledge(intent, True))
            self.assertIsNone(model.render(request, snapshot, lambda: NOW))
            return intent.signature

        model = ProgressActionModel()
        original = seed(model)
        for _ in range(3):
            request = model.run({"context": "progress"})[0]
        intent = model.render(request, snapshot, lambda: NOW)
        self.assertEqual(intent.signature, original)
        self.assertTrue(model.acknowledge(intent, True))
        self.assertIsNone(model.render(request, snapshot, lambda: NOW))

        model = ProgressActionModel()
        original = seed(model)
        model.receive_settings({"context": "progress", "param": {"progressColor": "#ABCDEF"}})
        request = model.receive_settings({"context": "progress", "param": {}})[0]
        intent = model.render(request, snapshot, lambda: NOW)
        self.assertEqual(intent.signature, original)
        self.assertTrue(model.acknowledge(intent, True))
        self.assertIsNone(model.render(request, snapshot, lambda: NOW))

        model = ProgressActionModel()
        original = seed(model)
        self.assertEqual(model.set_active({"context": "progress", "active": False}), ())
        request = model.set_active({"context": "progress", "active": True})[0]
        intent = model.render(request, snapshot, lambda: NOW)
        self.assertEqual(intent.signature, original)
        self.assertTrue(model.acknowledge(intent, True))
        self.assertIsNone(model.render(request, snapshot, lambda: NOW))

    def test_send_reservation_is_the_linearization_point_for_every_invalidation(self):
        snapshot = normalize_progress_state(state(is_playing=False), lambda: NOW)

        def mutate(model, name):
            if name == "clear/recreate":
                model.clear({"param": [{"context": "progress"}]})
                model.add({"uuid": ACTION_UUID, "context": "progress"})
            elif name == "inactive":
                model.set_active({"context": "progress", "active": False})
            elif name == "mode":
                model.run({"context": "progress"})
            elif name == "settings":
                model.receive_settings({"context": "progress", "settings": {
                    "progressColor": "#ABCDEF",
                }})
            else:
                model.shutdown()

        for invalidation in ("clear/recreate", "inactive", "mode", "settings", "shutdown"):
            with self.subTest(invalidation=invalidation, order="invalidation-first"):
                model = ProgressActionModel()
                request = model.add({"uuid": ACTION_UUID, "context": "progress"})[0]
                intent = model.render(request, snapshot, lambda: NOW)
                persistence = model.persistence_requests()[0]
                mutate(model, invalidation)
                self.assertFalse(model.reserve_display_send(intent))
                self.assertFalse(model.reserve_persistence_send(persistence))

            with self.subTest(invalidation=invalidation, order="reservation-first"):
                model = ProgressActionModel()
                request = model.add({"uuid": ACTION_UUID, "context": "progress"})[0]
                intent = model.render(request, snapshot, lambda: NOW)
                persistence = model.persistence_requests()[0]
                self.assertTrue(model.reserve_display_send(intent))
                self.assertTrue(model.reserve_persistence_send(persistence))
                mutate(model, invalidation)
                self.assertFalse(model.acknowledge(intent, True))
                self.assertFalse(model.acknowledge_persistence(persistence, True, 3))

    def test_reentrant_mapping_and_iterable_callbacks_run_outside_model_lock(self):
        class ReentrantMapping(Mapping):
            def __init__(self, values, callback):
                self.values = values
                self.callback = callback
                self.reentered = False

            def __getitem__(self, key):
                return self.values[key]

            def __iter__(self):
                return iter(self.values)

            def __len__(self):
                return len(self.values)

            def get(self, key, default=None):
                if not self.reentered:
                    self.reentered = True
                    self.callback()
                return self.values.get(key, default)

        class ReentrantItems:
            def __iter__(self):
                model.context("progress")
                yield ReentrantMapping({"context": "progress"}, model.requests)

        def completes(operation):
            errors = []
            thread = threading.Thread(target=lambda: self._capture(operation, errors), daemon=True)
            thread.start()
            thread.join(1)
            self.assertFalse(thread.is_alive(), "caller callback re-entered while lock was held")
            self.assertEqual(errors, [])

        model = ProgressActionModel()
        add_event = {"uuid": ACTION_UUID, "context": "progress",
                     "param": ReentrantMapping({"strokeWidth": True}, model.requests)}
        completes(lambda: model.add(add_event))
        self.assertEqual(model.context("progress").settings.stroke_width, 6)

        settings = ReentrantMapping({"progressColor": "#ABCDEF"},
                                    lambda: model.run({"context": "progress"}))
        completes(lambda: model.receive_settings({"context": "progress", "param": settings}))
        view = model.context("progress")
        self.assertEqual((view.mode, view.settings.progress_color), ("elapsed", "#ABCDEF"))

        completes(lambda: model.clear({"param": ReentrantItems()}))
        self.assertIsNone(model.context("progress"))

    @staticmethod
    def _capture(operation, errors):
        try:
            operation()
        except BaseException as error:
            errors.append(error)

    def test_concurrent_context_mutations_are_safe_and_create_no_resources(self):
        before = {(thread.ident, thread.name) for thread in threading.enumerate()}
        model = ProgressActionModel()
        errors = []
        barrier = threading.Barrier(9)

        def mutate(index):
            try:
                barrier.wait()
                context = f"context-{index}"
                model.add({"uuid": ACTION_UUID, "context": context})
                for _ in range(30):
                    model.run({"context": context})
                    model.set_active({"context": context, "active": True})
                model.clear({"param": [{"context": context}]})
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=mutate, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(2)
        self.assertFalse(errors)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(model.requests(), ())
        self.assertEqual({(thread.ident, thread.name) for thread in threading.enumerate()}, before)

    def test_scheduler_registers_exact_callbacks_and_separates_progress_run(self):
        class Api:
            def __init__(self): self.handlers = {}
            def __getattr__(self, name):
                def register(callback): self.handlers.setdefault(name, []).append(callback); return self
                return register
            def setBaseDataIcon(self, context, data, text): return True
            def setSettings(self, settings, context): return True
        class Client:
            def get_state(self, cancelled=None): return BridgeStateResult("stopped")

        api, model = Api(), ProgressActionModel()
        scheduler = ProgressScheduler(api, Client(), model)
        register_progress_handlers(api, scheduler)
        self.assertEqual({name: len(callbacks) for name, callbacks in api.handlers.items()}, {
            "onAdd": 1, "onClear": 1, "onSetActive": 1,
            "onParamFromPlugin": 1, "onDidReceiveSettings": 1,
        })
        api.handlers["onAdd"][0]({"uuid": ACTION_UUID, "context": "progress"})
        self.assertFalse(scheduler.handle_run({"uuid": "com.other.next", "context": "progress"}))
        self.assertTrue(scheduler.handle_run({"uuid": ACTION_UUID, "context": "progress"}))
        self.assertEqual(model.context("progress").mode, "elapsed")
        self.assertFalse(scheduler.handle_run({"uuid": ACTION_UUID, "context": "missing"}))
        self.assertFalse(api.handlers["onClear"][0]({"param": [{"context": "missing"}]}))

    def test_one_poll_drives_progress_and_nowplaying_fallback_then_exact_artwork(self):
        artwork_id = "a" * 64
        bundle = ArtworkBundle(artwork_id, "color-image", "gray-image",
                               ("tl", "tr", "bl", "br"))
        payload = state(updated_at=NOW.isoformat(), position_updated_at=NOW.isoformat(),
                        title="Track", artist="Artist", artwork_id=artwork_id)

        class Client:
            def __init__(self): self.state_calls = 0; self.artwork_calls = 0
            def get_state(self, cancelled=None):
                self.state_calls += 1
                return BridgeStateResult("ok", payload, 200)
            def get_artwork(self, requested, cancelled=None):
                self.artwork_calls += 1
                return BridgeArtworkResult("ok", bundle, 200)

        class Api:
            def __init__(self): self.sends = []
            def setSettings(self, settings, context): return True
            def setBaseDataIcon(self, context, image, text):
                self.sends.append((threading.get_ident(), "data", context, image, text)); return True
            def setPathIcon(self, context, image, text):
                self.sends.append((threading.get_ident(), "path", context, image, text)); return True

        client, api = Client(), Api()
        scheduler = ProgressScheduler(api, client, ProgressActionModel(),
                                      NowPlayingActionModel(), ArtworkBundleCache(),
                                      clock=lambda: NOW, poll_interval=.2)
        callback_thread = threading.get_ident()
        self.assertTrue(scheduler.handle_add({"uuid": ACTION_UUID, "context": "progress",
                                              "param": progress_settings_payload(ProgressSettings())}))
        self.assertTrue(scheduler.handle_add({"uuid": NOW_PLAYING_UUID, "context": "cover"}))
        scheduler.start()
        self.assertTrue(self._wait_for(lambda: any(send[3] == "color-image"
                                                   for send in api.sends)))
        self.assertEqual((client.state_calls, client.artwork_calls), (1, 1))
        cover = [send for send in api.sends if send[2] == "cover"]
        self.assertEqual([(send[1], send[3], send[4]) for send in cover], [
            ("path", "./assets/music.svg", "Track\nArtist"),
            ("data", "color-image", "Track\nArtist"),
        ])
        self.assertTrue(all(send[0] == cover[0][0] != callback_thread for send in api.sends))
        self.assertTrue(scheduler.stop(.5))

    def test_one_poll_and_fetch_drive_nowplaying_and_all_mosaic_tiles(self):
        artwork_id = "6" * 64
        bundle = ArtworkBundle(artwork_id, "color", "gray", ("tl", "tr", "bl", "br"))
        payload = state(updated_at=NOW.isoformat(), position_updated_at=NOW.isoformat(),
                        artwork_id=artwork_id)

        class Client:
            def __init__(self): self.state_calls = self.artwork_calls = 0
            def get_state(self, cancelled=None):
                self.state_calls += 1; return BridgeStateResult("ok", payload, 200)
            def get_artwork(self, requested, cancelled=None):
                self.artwork_calls += 1; return BridgeArtworkResult("ok", bundle, 200)
        class Api:
            def __init__(self): self.sends = []
            def setPathIcon(self, context, image, text):
                self.sends.append((threading.get_ident(), context, image, text)); return True
            def setBaseDataIcon(self, context, image, text):
                self.sends.append((threading.get_ident(), context, image, text)); return True

        client, api = Client(), Api()
        scheduler = ProgressScheduler(api, client, ProgressActionModel(),
                                      NowPlayingActionModel(), ArtworkBundleCache(),
                                      clock=lambda: NOW, poll_interval=.2)
        callback_thread = threading.get_ident(); scheduler.start()
        scheduler.handle_add({"uuid": NOW_PLAYING_UUID, "context": "cover"})
        for index, action in enumerate(MOSAIC_ACTIONS):
            scheduler.handle_add({"uuid": action, "context": f"tile-{index}"})
        self.assertTrue(self._wait_for(lambda: all(any(send[2] == image for send in api.sends)
                                                   for image in ("color", *bundle.tiles))))
        self.assertEqual((client.state_calls, client.artwork_calls), (1, 1))
        self.assertTrue(all(send[0] != callback_thread for send in api.sends))
        self.assertFalse(scheduler.handle_run({"uuid": next(iter(MOSAIC_ACTIONS)),
                                               "context": "tile-0"}))
        self.assertTrue(scheduler.stop(.5))

    def test_shared_poll_renders_audio_actions_and_mute_icon_transition(self):
        payload = state(updated_at=NOW.isoformat(), position_updated_at=NOW.isoformat(),
                        audio_available=True, volume_percent=55)

        class Client:
            def __init__(self): self.state_calls = 0
            def get_state(self, cancelled=None):
                self.state_calls += 1
                muted = self.state_calls >= 2
                return BridgeStateResult("ok", dict(payload, is_muted=muted), 200)

        class Api:
            def __init__(self): self.sends = []
            def setPathIcon(self, context, image, text):
                self.sends.append((context, image, text)); return True
            def setBaseDataIcon(self, context, image, text):
                self.sends.append((context, image, text)); return True

        client, api = Client(), Api()
        scheduler = ProgressScheduler(api, client, ProgressActionModel(),
                                      NowPlayingActionModel(), ArtworkBundleCache(),
                                      clock=lambda: NOW, poll_interval=.05)
        scheduler.start()
        for action in AUDIO_ACTIONS:
            scheduler.handle_add({"uuid": action, "context": action})
        self.assertTrue(self._wait_for(lambda: all(
            any(send == (action, icon, "55%") for send in api.sends)
            for action, icon in AUDIO_ACTIONS.items()
            if action != MUTE_TOGGLE_UUID), 1))
        self.assertTrue(self._wait_for(lambda: any(
            send == (MUTE_TOGGLE_UUID, mute_toggle_data_uri("55%", False), "")
            for send in api.sends), 1))
        self.assertTrue(self._wait_for(lambda: any(
            send == (MUTE_TOGGLE_UUID, mute_toggle_data_uri("Muted", True), "")
            for send in api.sends), 1))
        self.assertEqual([send for send in api.sends if send[0] == MUTE_TOGGLE_UUID], [
            (MUTE_TOGGLE_UUID, mute_toggle_data_uri("55%", False), ""),
            (MUTE_TOGGLE_UUID, mute_toggle_data_uri("Muted", True), ""),
        ])
        for action, icon in AUDIO_ACTIONS.items():
            if action == MUTE_TOGGLE_UUID:
                continue
            self.assertEqual([send for send in api.sends if send[0] == action], [
                (action, icon, "55%"), (action, icon, "Muted"),
            ])
        self.assertTrue(scheduler.stop(.5))

    def test_mute_command_polls_immediately_and_updates_display(self):
        payload = state(updated_at=NOW.isoformat(), position_updated_at=NOW.isoformat(),
                        audio_available=True, volume_percent=55)

        class Client:
            def __init__(self): self.commands = []; self.state_calls = 0
            def execute(self, command, cancelled=None):
                self.commands.append(command)
                return BridgeResult(command, "ok", 200)
            def get_state(self, cancelled=None):
                self.state_calls += 1
                return BridgeStateResult(
                    "ok", dict(payload, is_muted=bool(self.commands)), 200)

        class Api:
            def __init__(self): self.sends = []
            def setPathIcon(self, context, image, text):
                self.sends.append((context, image, text)); return True
            def setBaseDataIcon(self, context, image, text):
                self.sends.append((context, image, text)); return True

        client, api = Client(), Api()
        router = TransportRouter(client)
        scheduler = ProgressScheduler(api, client, ProgressActionModel(),
                                      NowPlayingActionModel(), ArtworkBundleCache(),
                                      clock=lambda: NOW, poll_interval=5)
        router.configure_runtime(scheduler.handle_run, scheduler.request_poll)
        router.start(); scheduler.start()
        scheduler.handle_add({"uuid": MUTE_TOGGLE_UUID, "context": "mute"})
        self.assertTrue(self._wait_for(lambda: api.sends == [
            ("mute", mute_toggle_data_uri("55%", False), "")], 1))
        self.assertTrue(router.handle_run({"uuid": MUTE_TOGGLE_UUID, "context": "mute"}))
        self.assertTrue(self._wait_for(lambda: api.sends == [
            ("mute", mute_toggle_data_uri("55%", False), ""),
            ("mute", mute_toggle_data_uri("Muted", True), ""),
        ], 1))
        self.assertEqual(client.commands, ["mute-toggle"])
        self.assertGreaterEqual(client.state_calls, 2,
                                "successful mute command must trigger an immediate poll")
        self.assertTrue(router.stop(.5))
        self.assertTrue(scheduler.stop(.5))

    def test_audio_clear_and_inactive_contexts_never_render(self):
        payload = state(updated_at=NOW.isoformat(), position_updated_at=NOW.isoformat(),
                        audio_available=True, volume_percent=55)

        class Client:
            def __init__(self): self.state_calls = 0; self.gate = threading.Event()
            def get_state(self, cancelled=None):
                self.state_calls += 1
                muted = self.state_calls >= 2
                if self.state_calls == 2:
                    self.gate.wait(1)
                return BridgeStateResult("ok", dict(payload, is_muted=muted), 200)

        class Api:
            def __init__(self): self.sends = []
            def setPathIcon(self, context, image, text):
                self.sends.append((context, image, text)); return True
            def setBaseDataIcon(self, context, image, text):
                self.sends.append((context, image, text)); return True

        client, api = Client(), Api()
        scheduler = ProgressScheduler(api, client, ProgressActionModel(),
                                      NowPlayingActionModel(), ArtworkBundleCache(),
                                      clock=lambda: NOW, poll_interval=.04)
        scheduler.start()
        scheduler.handle_add({"uuid": MUTE_TOGGLE_UUID, "context": "gone"})
        scheduler.handle_add({"uuid": MUTE_TOGGLE_UUID, "context": "sleeping"})
        scheduler.handle_add({"uuid": "com.arkamax404.ulanzi.mediacontrol.volume-up",
                              "context": "kept"})
        self.assertTrue(self._wait_for(lambda: len(api.sends) == 3, 1))
        scheduler.handle_set_active({"context": "sleeping", "active": False})
        self.assertTrue(scheduler.handle_clear({"param": [{"context": "gone"}]}))
        client.gate.set()
        self.assertTrue(self._wait_for(lambda: any(
            send == ("kept", "./assets/volume-up.svg", "Muted") for send in api.sends), 1))
        self.assertEqual([send for send in api.sends if send[0] != "kept"], [
            ("gone", mute_toggle_data_uri("55%", False), ""),
            ("sleeping", mute_toggle_data_uri("55%", False), ""),
        ], "cleared and inactive audio contexts must not re-render on later polls")
        self.assertTrue(scheduler.stop(.5))

    def test_shared_poll_renders_transport_actions_and_toggle_transition(self):
        payload = state(updated_at=NOW.isoformat(), position_updated_at=NOW.isoformat())

        class Client:
            def __init__(self): self.state_calls = 0
            def get_state(self, cancelled=None):
                self.state_calls += 1
                return BridgeStateResult(
                    "ok", dict(payload, is_playing=self.state_calls >= 2), 200)

        class Api:
            def __init__(self): self.sends = []
            def setPathIcon(self, context, image, text):
                self.sends.append((context, image, text)); return True
            def setBaseDataIcon(self, context, image, text):
                self.sends.append((context, image, text)); return True

        client, api = Client(), Api()
        scheduler = ProgressScheduler(api, client, ProgressActionModel(),
                                      NowPlayingActionModel(), ArtworkBundleCache(),
                                      clock=lambda: NOW, poll_interval=.05)
        scheduler.start()
        for action in TRANSPORT_DISPLAY:
            scheduler.handle_add({"uuid": action, "context": action})
        self.assertTrue(self._wait_for(lambda: all(
            any(send == (action, TRANSPORT_DISPLAY[action],
                         "Previous" if action == PREVIOUS_UUID else "Next")
                for send in api.sends)
            for action in TRANSPORT_DISPLAY if action != TOGGLE_UUID), 1))
        self.assertTrue(self._wait_for(lambda: any(
            send == (TOGGLE_UUID, "./assets/play.svg", "Play")
            for send in api.sends), 1))
        self.assertTrue(self._wait_for(lambda: any(
            send == (TOGGLE_UUID, "./assets/pause.svg", "Pause")
            for send in api.sends), 1))
        self.assertEqual([send for send in api.sends if send[0] == TOGGLE_UUID], [
            (TOGGLE_UUID, "./assets/play.svg", "Play"),
            (TOGGLE_UUID, "./assets/pause.svg", "Pause"),
        ], "a play/pause transition re-renders the dedicated toggle button exactly once")
        for action in TRANSPORT_DISPLAY:
            if action == TOGGLE_UUID:
                continue
            self.assertEqual(len([send for send in api.sends if send[0] == action]), 1,
                             "static transport labels dedup across polls")
        self.assertFalse(scheduler.handle_run({"uuid": TOGGLE_UUID, "context": TOGGLE_UUID}))
        self.assertTrue(scheduler.stop(.5))

    def test_toggle_command_polls_immediately_and_updates_display(self):
        payload = state(updated_at=NOW.isoformat(), position_updated_at=NOW.isoformat(),
                        is_playing=False)

        class Client:
            def __init__(self): self.commands = []; self.state_calls = 0
            def execute(self, command, cancelled=None):
                self.commands.append(command)
                return BridgeResult(command, "ok", 200)
            def get_state(self, cancelled=None):
                self.state_calls += 1
                return BridgeStateResult(
                    "ok", dict(payload, is_playing=bool(self.commands)), 200)

        class Api:
            def __init__(self): self.sends = []
            def setPathIcon(self, context, image, text):
                self.sends.append((context, image, text)); return True
            def setBaseDataIcon(self, context, image, text):
                self.sends.append((context, image, text)); return True

        client, api = Client(), Api()
        router = TransportRouter(client)
        scheduler = ProgressScheduler(api, client, ProgressActionModel(),
                                      NowPlayingActionModel(), ArtworkBundleCache(),
                                      clock=lambda: NOW, poll_interval=5)
        router.configure_runtime(scheduler.handle_run, scheduler.request_poll)
        router.start(); scheduler.start()
        scheduler.handle_add({"uuid": TOGGLE_UUID, "context": "toggle"})
        self.assertTrue(self._wait_for(lambda: api.sends == [
            ("toggle", "./assets/play.svg", "Play")], 1))
        self.assertTrue(router.handle_run({"uuid": TOGGLE_UUID, "context": "toggle"}))
        self.assertTrue(self._wait_for(lambda: api.sends == [
            ("toggle", "./assets/play.svg", "Play"),
            ("toggle", "./assets/pause.svg", "Pause"),
        ], 1))
        self.assertEqual(client.commands, ["toggle"])
        self.assertGreaterEqual(client.state_calls, 2,
                                "successful toggle command must trigger an immediate poll")
        self.assertTrue(router.stop(.5))
        self.assertTrue(scheduler.stop(.5))

    def test_transport_clear_and_inactive_contexts_never_render(self):
        payload = state(updated_at=NOW.isoformat(), position_updated_at=NOW.isoformat(),
                        is_playing=False)

        class Client:
            def __init__(self): self.state_calls = 0; self.gate = threading.Event()
            def get_state(self, cancelled=None):
                self.state_calls += 1
                playing = self.state_calls >= 2
                if self.state_calls == 2:
                    self.gate.wait(1)
                return BridgeStateResult("ok", dict(payload, is_playing=playing), 200)

        class Api:
            def __init__(self): self.sends = []
            def setPathIcon(self, context, image, text):
                self.sends.append((context, image, text)); return True
            def setBaseDataIcon(self, context, image, text):
                self.sends.append((context, image, text)); return True

        client, api = Client(), Api()
        scheduler = ProgressScheduler(api, client, ProgressActionModel(),
                                      NowPlayingActionModel(), ArtworkBundleCache(),
                                      clock=lambda: NOW, poll_interval=.04)
        scheduler.start()
        scheduler.handle_add({"uuid": TOGGLE_UUID, "context": "gone"})
        scheduler.handle_add({"uuid": PREVIOUS_UUID, "context": "sleeping"})
        scheduler.handle_add({"uuid": TOGGLE_UUID, "context": "kept"})
        self.assertTrue(self._wait_for(lambda: len(api.sends) == 3, 1))
        scheduler.handle_set_active({"context": "sleeping", "active": False})
        self.assertTrue(scheduler.handle_clear({"param": [{"context": "gone"}]}))
        client.gate.set()
        self.assertTrue(self._wait_for(lambda: any(
            send == ("kept", "./assets/pause.svg", "Pause") for send in api.sends), 1))
        self.assertEqual([send for send in api.sends if send[0] != "kept"], [
            ("gone", "./assets/play.svg", "Play"),
            ("sleeping", "./assets/previous.svg", "Previous"),
        ], "cleared and inactive transport contexts must not re-render on later polls")
        self.assertTrue(scheduler.stop(.5))

    def test_nowplaying_failed_fetch_retries_on_poll_and_playback_reuses_bundle(self):
        artwork_id = "b" * 64
        bundle = ArtworkBundle(artwork_id, "color", "gray", ("1", "2", "3", "4"))
        playing = state(updated_at=NOW.isoformat(), position_updated_at=NOW.isoformat(),
                        title="Track", artist="Artist", artwork_id=artwork_id)

        class Client:
            def __init__(self): self.polls = 0; self.fetches = []
            def get_state(self, cancelled=None):
                self.polls += 1
                current = dict(playing, is_playing=self.polls != 2)
                return BridgeStateResult("ok", current, 200)
            def get_artwork(self, requested, cancelled=None):
                self.fetches.append(time.monotonic())
                return (BridgeArtworkResult("unavailable") if len(self.fetches) == 1
                        else BridgeArtworkResult("ok", bundle, 200))

        class Api:
            def __init__(self): self.images = []
            def setPathIcon(self, context, image, text): self.images.append(image); return True
            def setBaseDataIcon(self, context, image, text): self.images.append(image); return True

        client, api = Client(), Api()
        scheduler = ProgressScheduler(api, client, ProgressActionModel(),
                                      NowPlayingActionModel(), ArtworkBundleCache(),
                                      clock=lambda: NOW, poll_interval=.04)
        scheduler.start(); scheduler.handle_add({"uuid": NOW_PLAYING_UUID, "context": "cover"})
        self.assertTrue(self._wait_for(lambda: api.images[-2:] == ["gray", "color"], 1))
        self.assertEqual(api.images[0], "./assets/music.svg")
        self.assertGreaterEqual(client.fetches[1] - client.fetches[0], .03)
        self.assertEqual(api.images.count("color"), 1)
        self.assertTrue(scheduler.stop(.5))

    def test_stale_artwork_response_cannot_install_after_context_recreation(self):
        artwork_id = "c" * 64
        bundle = ArtworkBundle(artwork_id, "stale-color", "stale-gray",
                               ("1", "2", "3", "4"))
        payload = state(updated_at=NOW.isoformat(), position_updated_at=NOW.isoformat(),
                        artwork_id=artwork_id)

        class Client:
            def __init__(self):
                self.entered, self.release, self.fetches = threading.Event(), threading.Event(), 0
            def get_state(self, cancelled=None): return BridgeStateResult("ok", payload, 200)
            def get_artwork(self, requested, cancelled=None):
                self.fetches += 1
                if self.fetches > 1:
                    return BridgeArtworkResult("unavailable")
                self.entered.set(); self.release.wait(1)
                return BridgeArtworkResult("ok", bundle, 200)

        class Api:
            def __init__(self): self.images = []
            def setPathIcon(self, context, image, text): self.images.append(image); return True
            def setBaseDataIcon(self, context, image, text): self.images.append(image); return True

        client, api, cache = Client(), Api(), ArtworkBundleCache()
        scheduler = ProgressScheduler(api, client, ProgressActionModel(),
                                      NowPlayingActionModel(), cache,
                                      clock=lambda: NOW, poll_interval=.5)
        mosaic_action = next(iter(MOSAIC_ACTIONS))
        scheduler.start(); scheduler.handle_add({"uuid": mosaic_action, "context": "cover"})
        self.assertTrue(client.entered.wait(.5))
        started = time.monotonic()
        self.assertTrue(scheduler.handle_clear({"param": [{"context": "cover"}]}))
        self.assertTrue(scheduler.handle_add({"uuid": mosaic_action, "context": "cover"}))
        self.assertLess(time.monotonic() - started, .05)
        client.release.set(); time.sleep(.04)
        self.assertEqual(client.fetches, 2, "recreated context receives its own poll attempt")
        self.assertIsNone(cache.get(artwork_id))
        self.assertNotIn("1", api.images)
        self.assertTrue(scheduler.stop(.5))

    def test_artwork_install_reservation_linearizes_all_invalidations(self):
        artwork_id = "d" * 64
        other_id = "e" * 64
        bundle = ArtworkBundle(artwork_id, "old-color", "old-gray",
                               ("1", "2", "3", "4"))
        snapshot = MediaSnapshot(
            True, True, True, "Track", "Artist", artwork_id, "ready")

        for invalidation in ("clear", "inactive", "recreate", "artwork-id",
                             "shutdown", "two-context"):
            for install_first in (False, True):
                with self.subTest(invalidation=invalidation, install_first=install_first):
                    model = NowPlayingActionModel()

                    class GatedCache(ArtworkBundleCache):
                        def __init__(inner):
                            super().__init__()
                            inner.entered = threading.Event()
                            inner.release = threading.Event()
                            inner.installs = []

                        def install(inner, reservation, candidate):
                            if install_first:
                                accepted = super().install(reservation, candidate)
                                inner.installs.append(accepted)
                                inner.entered.set(); inner.release.wait(1)
                                return accepted
                            inner.entered.set(); inner.release.wait(1)
                            accepted = super().install(reservation, candidate)
                            inner.installs.append(accepted)
                            return accepted

                    class Client:
                        def get_artwork(inner, requested, cancelled=None):
                            return BridgeArtworkResult("ok", bundle, 200)

                    class Api:
                        def __init__(inner): inner.sends = []
                        def setBaseDataIcon(inner, context, image, text):
                            inner.sends.append((context, image)); return True
                        def setPathIcon(inner, context, image, text):
                            inner.sends.append((context, image)); return True

                    cache, api = GatedCache(), Api()
                    scheduler = ProgressScheduler(api, Client(), ProgressActionModel(),
                                                  model, cache, clock=lambda: NOW)
                    display_action = (next(iter(MOSAIC_ACTIONS))
                                      if invalidation == "two-context" else NOW_PLAYING_UUID)
                    model.add({"uuid": display_action, "context": "changing"})
                    if invalidation == "two-context":
                        model.add({"uuid": display_action, "context": "steady"})
                    scheduler._media_state = snapshot
                    cache.begin(artwork_id)

                    class HostileRelevance(tuple):
                        def __iter__(inner):
                            cache.get(artwork_id)
                            model.requests()
                            return super().__iter__()

                    relevance = HostileRelevance(model.requests())
                    reservation = cache.reserve(artwork_id, relevance)

                    class ReentrantBundle(ArtworkBundle):
                        def __getattribute__(inner, name):
                            if name == "artwork_id":
                                cache.get(artwork_id)
                                model.requests()
                            return super().__getattribute__(name)

                    hostile_bundle = ReentrantBundle(
                        bundle.artwork_id, bundle.color, bundle.grayscale, bundle.tiles)
                    client_bundle = hostile_bundle
                    scheduler.client.get_artwork = lambda requested, cancelled=None: \
                        BridgeArtworkResult("ok", client_bundle, 200)
                    worker = threading.Thread(
                        target=scheduler._fetch_artwork,
                        args=(snapshot, reservation), daemon=True)
                    worker.start(); self.assertTrue(cache.entered.wait(.5))

                    started = time.monotonic()
                    if invalidation == "clear":
                        scheduler.handle_clear({"param": [{"context": "changing"}]})
                    elif invalidation == "inactive":
                        scheduler.handle_set_active({"context": "changing", "active": False})
                    elif invalidation in ("recreate", "two-context"):
                        scheduler.handle_clear({"param": [{"context": "changing"}]})
                        scheduler.handle_add({"uuid": display_action,
                                              "context": "changing"})
                    elif invalidation == "artwork-id":
                        cache.begin(other_id)
                        scheduler._media_state = MediaSnapshot(
                            True, True, True, "New", "Artist", other_id, "ready")
                    else:
                        scheduler.stop(.01)
                    self.assertLess(time.monotonic() - started, .05)
                    cache.release.set(); worker.join(1)
                    self.assertFalse(worker.is_alive(), "cache/model reentry deadlocked")
                    self.assertEqual(cache.installs, [install_first])
                    self.assertIsNone(cache.get(artwork_id))

                    if invalidation == "two-context" and install_first:
                        self.assertEqual(api.sends, [("steady", "1")])
                    else:
                        self.assertEqual(api.sends, [])
                    current = model.requests()
                    scheduler._render_now_all(current, scheduler._media_state,
                                              cache.get(artwork_id))
                    self.assertNotIn(("changing", "old-color"), api.sends)

    def test_settings_persist_canonically_without_echo_loop_and_fail_closed(self):
        payload = state(updated_at=NOW.isoformat(), position_updated_at=NOW.isoformat(),
                        is_playing=False)

        class Client:
            def get_state(self, cancelled=None):
                return BridgeStateResult("ok", payload, 200)

        class Api:
            def __init__(self):
                self.settings, self.displays = [], []

            def setSettings(self, settings, context):
                self.settings.append((threading.get_ident(), context, settings))
                return True

            def setBaseDataIcon(self, context, data, text):
                self.displays.append((threading.get_ident(), context, data))
                return True

        api, model = Api(), ProgressActionModel()
        scheduler = ProgressScheduler(api, Client(), model, clock=lambda: NOW,
                                      poll_interval=.05)
        scheduler.start()
        callback_thread = threading.get_ident()
        self.assertTrue(scheduler.handle_add({"uuid": ACTION_UUID, "context": "one"}))
        self.assertTrue(self._wait_for(lambda: len(api.settings) == 1 and api.displays))
        canonical = progress_settings_payload(ProgressSettings())
        self.assertEqual(api.settings[0][1:], ("one", canonical))
        self.assertNotEqual(api.settings[0][0], callback_thread)
        self.assertEqual(api.settings[0][0], api.displays[0][0])

        self.assertTrue(scheduler.handle_settings({"context": "one", "settings": canonical}))
        time.sleep(.07)
        self.assertEqual(len(api.settings), 1, "canonical host echo must not persist")
        self.assertTrue(scheduler.handle_settings({
            "context": "one", "settings": {"progressColor": "bad", "strokeWidth": "6.4"}
        }))
        self.assertTrue(self._wait_for(lambda: len(api.settings) == 2))
        self.assertEqual(api.settings[-1][2], {
            **canonical, "strokeWidth": 6,
        })
        before = model.context("one")
        for malformed in (
            None, {}, {"context": "one"}, {"settings": canonical},
            {"context": "one", "settings": []},
        ):
            self.assertFalse(scheduler.handle_settings(malformed))
        self.assertEqual(model.context("one"), before)
        self.assertTrue(scheduler.stop(.5))

    def test_property_settings_coalesce_and_persistence_revalidates_context(self):
        payload = state(updated_at=NOW.isoformat(), position_updated_at=NOW.isoformat(),
                        is_playing=False)

        class Client:
            def get_state(self, cancelled=None):
                return BridgeStateResult("ok", payload, 200)

        class Api:
            def __init__(self):
                self.settings = []

            def setSettings(self, settings, context):
                self.settings.append((context, settings)); return True

            def setBaseDataIcon(self, context, data, text): return True

        api, model = Api(), ProgressActionModel()
        scheduler = ProgressScheduler(api, Client(), model, clock=lambda: NOW,
                                      poll_interval=.03)
        scheduler.start()
        canonical = progress_settings_payload(ProgressSettings())
        scheduler.handle_add({"uuid": ACTION_UUID, "context": "race", "param": canonical})
        self.assertTrue(self._wait_for(lambda: model.context("race") is not None))
        for _ in range(20):
            scheduler.handle_property_settings({"context": "race", "param": canonical})
        scheduler.handle_clear({"param": [{"context": "race"}]})
        scheduler.handle_add({"uuid": ACTION_UUID, "context": "race", "param": canonical})
        time.sleep(.07)
        self.assertEqual(api.settings, [], "cleared generation must not receive stale settings")
        scheduler.handle_set_active({"context": "race", "active": False})
        scheduler.handle_property_settings({"context": "race", "param": {
            **canonical, "textColor": "#ABCDEF",
        }})
        time.sleep(.05)
        self.assertEqual(api.settings, [])
        scheduler.handle_set_active({"context": "race", "active": True})
        self.assertTrue(self._wait_for(lambda: len(api.settings) == 1))
        self.assertEqual(api.settings[0][1]["textColor"], "#ABCDEF")
        self.assertTrue(scheduler.stop(.5))

    def test_display_race_is_closed_on_both_sides_of_send_reservation(self):
        payload = state(updated_at=NOW.isoformat(), position_updated_at=NOW.isoformat(),
                        is_playing=False)

        for reservation_first in (False, True):
            with self.subTest(reservation_first=reservation_first):
                class Client:
                    def get_state(self, cancelled=None):
                        return BridgeStateResult("ok", payload, 200)

                class GatedModel(ProgressActionModel):
                    def __init__(self):
                        super().__init__()
                        self.entered, self.release = threading.Event(), threading.Event()
                        self.acknowledgements = []
                        self.gated = False

                    def reserve_display_send(self, intent):
                        if not self.gated:
                            self.gated = True
                            if reservation_first:
                                accepted = super().reserve_display_send(intent)
                                self.entered.set(); self.release.wait(1)
                                return accepted
                            self.entered.set(); self.release.wait(1)
                        return super().reserve_display_send(intent)

                    def acknowledge(self, intent, success):
                        accepted = super().acknowledge(intent, success)
                        self.acknowledgements.append(accepted)
                        return accepted

                class Api:
                    def __init__(self): self.sends = []
                    def setBaseDataIcon(self, context, data, text):
                        svg = base64.b64decode(data.split(",", 1)[1]).decode()
                        self.sends.append(svg); return True

                canonical = progress_settings_payload(ProgressSettings())
                old = {**canonical, "progressColor": "#AA0000"}
                new = {**canonical, "progressColor": "#0000AA"}
                api, model = Api(), GatedModel()
                scheduler = ProgressScheduler(api, Client(), model, clock=lambda: NOW,
                                              poll_interval=.03)
                scheduler.start(); scheduler.handle_add({
                    "uuid": ACTION_UUID, "context": "race", "param": old,
                })
                self.assertTrue(model.entered.wait(.5))
                started = time.monotonic()
                self.assertTrue(scheduler.handle_clear({"param": [{"context": "race"}]}))
                self.assertTrue(scheduler.handle_add({
                    "uuid": ACTION_UUID, "context": "race", "param": new,
                }))
                self.assertLess(time.monotonic() - started, .05)
                model.release.set()
                self.assertTrue(self._wait_for(lambda: any("#0000AA" in svg for svg in api.sends)))
                time.sleep(.07)
                old_sends = [svg for svg in api.sends if "#AA0000" in svg]
                self.assertEqual(len(old_sends), 1 if reservation_first else 0)
                if reservation_first:
                    self.assertEqual(model.acknowledgements[:2], [False, True])
                self.assertTrue(scheduler.stop(.5))

    def test_settings_race_is_closed_on_both_sides_of_send_reservation(self):
        payload = state(updated_at=NOW.isoformat(), position_updated_at=NOW.isoformat(),
                        is_playing=False)

        for reservation_first in (False, True):
            with self.subTest(reservation_first=reservation_first):
                class Client:
                    def get_state(self, cancelled=None):
                        return BridgeStateResult("ok", payload, 200)

                class GatedModel(ProgressActionModel):
                    def __init__(self):
                        super().__init__()
                        self.entered, self.release = threading.Event(), threading.Event()
                        self.acknowledgements = []
                        self.gated = False

                    def reserve_persistence_send(self, request):
                        if not self.gated:
                            self.gated = True
                            if reservation_first:
                                accepted = super().reserve_persistence_send(request)
                                self.entered.set(); self.release.wait(1)
                                return accepted
                            self.entered.set(); self.release.wait(1)
                        return super().reserve_persistence_send(request)

                    def acknowledge_persistence(self, request, success, max_attempts):
                        accepted = super().acknowledge_persistence(
                            request, success, max_attempts)
                        self.acknowledgements.append(accepted)
                        return accepted

                class Api:
                    def __init__(self): self.settings = []
                    def setSettings(self, settings, context):
                        self.settings.append(settings); return True
                    def setBaseDataIcon(self, context, data, text): return True

                api, model = Api(), GatedModel()
                scheduler = ProgressScheduler(api, Client(), model, clock=lambda: NOW,
                                              poll_interval=.03)
                scheduler.start(); scheduler.handle_add({
                    "uuid": ACTION_UUID, "context": "race",
                    "param": {"progressColor": "#AA0000"},
                })
                self.assertTrue(model.entered.wait(.5))
                started = time.monotonic()
                scheduler.handle_clear({"param": [{"context": "race"}]})
                scheduler.handle_add({
                    "uuid": ACTION_UUID, "context": "race",
                    "param": {"progressColor": "#0000AA"},
                })
                self.assertLess(time.monotonic() - started, .05)
                model.release.set()
                self.assertTrue(self._wait_for(lambda: any(
                    item["progressColor"] == "#0000AA" for item in api.settings)))
                time.sleep(.07)
                old_sends = [item for item in api.settings
                             if item["progressColor"] == "#AA0000"]
                self.assertEqual(len(old_sends), 1 if reservation_first else 0)
                if reservation_first:
                    self.assertEqual(model.acknowledgements[:2], [False, True])
                self.assertTrue(scheduler.stop(.5))

    def test_failed_settings_send_recovers_on_bounded_scheduler_cycles(self):
        payload = state(updated_at=NOW.isoformat(), position_updated_at=NOW.isoformat(),
                        is_playing=False)

        class Client:
            def get_state(self, cancelled=None):
                return BridgeStateResult("ok", payload, 200)

        class Api:
            def __init__(self): self.attempts, self.displays = [], 0
            def setSettings(self, settings, context):
                self.attempts.append(time.monotonic())
                if len(self.attempts) == 1:
                    time.sleep(.05)
                return len(self.attempts) > 1
            def setBaseDataIcon(self, context, data, text):
                self.displays += 1; return True

        api, model = Api(), ProgressActionModel()
        scheduler = ProgressScheduler(api, Client(), model, clock=lambda: NOW,
                                      poll_interval=.04)
        scheduler.start()
        started = time.monotonic()
        scheduler.handle_add({"uuid": ACTION_UUID, "context": "retry"})
        self.assertLess(time.monotonic() - started, .02, "callback must not wait for SDK send")
        self.assertTrue(self._wait_for(lambda: len(api.attempts) == 2 and api.displays))
        self.assertGreaterEqual(api.attempts[1] - api.attempts[0], .03)
        time.sleep(.06)
        self.assertEqual(len(api.attempts), 2)
        self.assertTrue(scheduler.stop(.5))

    def test_shutdown_rejects_acknowledgement_for_each_inflight_send_type(self):
        payload = state(updated_at=NOW.isoformat(), position_updated_at=NOW.isoformat(),
                        is_playing=False)

        for send_type in ("display", "settings"):
            with self.subTest(send_type=send_type):
                class Client:
                    def get_state(self, cancelled=None):
                        return BridgeStateResult("ok", payload, 200)

                class TrackingModel(ProgressActionModel):
                    def __init__(self):
                        super().__init__()
                        self.acknowledgements = []

                    def acknowledge(self, intent, success):
                        accepted = super().acknowledge(intent, success)
                        self.acknowledgements.append(accepted)
                        return accepted

                    def acknowledge_persistence(self, request, success, max_attempts):
                        accepted = super().acknowledge_persistence(
                            request, success, max_attempts)
                        self.acknowledgements.append(accepted)
                        return accepted

                class Api:
                    def __init__(self):
                        self.entered, self.release = threading.Event(), threading.Event()
                        self.calls = 0

                    def _send(self):
                        self.calls += 1; self.entered.set(); self.release.wait(1)
                        return True

                    def setSettings(self, settings, context):
                        return self._send() if send_type == "settings" else True

                    def setBaseDataIcon(self, context, data, text):
                        return self._send() if send_type == "display" else True

                api, model = Api(), TrackingModel()
                scheduler = ProgressScheduler(api, Client(), model, clock=lambda: NOW,
                                              poll_interval=.03)
                settings = (progress_settings_payload(ProgressSettings())
                            if send_type == "display" else {})
                scheduler.start(); scheduler.handle_add({
                    "uuid": ACTION_UUID, "context": "shutdown", "param": settings,
                })
                self.assertTrue(api.entered.wait(.5))
                started = time.monotonic()
                self.assertFalse(scheduler.stop(.02))
                self.assertLess(time.monotonic() - started, .1)
                self.assertIsNone(model.context("shutdown"))
                api.release.set()
                self.assertTrue(scheduler.wait_stopped(.5))
                self.assertEqual(model.acknowledgements, [False])
                self.assertEqual(api.calls, 1)

    def test_scheduler_immediate_poll_cadence_ticks_idle_coalescing_and_contexts(self):
        started = time.monotonic()
        clock = lambda: NOW + timedelta(seconds=time.monotonic() - started)
        payload = state(updated_at=NOW.isoformat(), position_updated_at=NOW.isoformat(),
                        position_seconds=10, duration_seconds=60)

        class Client:
            def __init__(self): self.calls, self.active, self.overlap = [], 0, False
            def get_state(self, cancelled=None):
                self.active += 1; self.overlap |= self.active > 1; self.calls.append(time.monotonic())
                time.sleep(0.003); self.active -= 1
                return BridgeStateResult("ok", payload, 200)
        class Api:
            def __init__(self): self.sends = []
            def setBaseDataIcon(self, context, data, text):
                self.sends.append((context, data, time.monotonic())); return True

        client, api, model = Client(), Api(), ProgressActionModel()
        scheduler = ProgressScheduler(api, client, model, clock=clock,
                                      poll_interval=.05, tick_interval=.015)
        scheduler.start()
        before = time.monotonic()
        scheduler.handle_add({"uuid": ACTION_UUID, "context": "one"})
        self.assertTrue(self._wait_for(lambda: len(client.calls) == 1))
        scheduler.handle_add({"uuid": ACTION_UUID, "context": "two",
                              "param": {"progressColor": "#ABCDEF"}})
        self.assertTrue(self._wait_for(lambda: len(client.calls) >= 2 and len(api.sends) >= 4))
        self.assertLess(client.calls[0] - before, .04)
        self.assertGreaterEqual(client.calls[1] - client.calls[0], .045)
        self.assertFalse(client.overlap)
        self.assertEqual({item[0] for item in api.sends}, {"one", "two"})
        for value in range(40):
            scheduler.handle_settings({"context": "one", "settings": {"strokeWidth": value}})
        self.assertTrue(self._wait_for(lambda: model.context("one").settings.stroke_width == 30))
        self.assertLess(len(api.sends), 40, "wakeup storm must coalesce")
        self.assertTrue(scheduler.stop(.5))

        paused = dict(payload, is_playing=False)
        client, api, model = Client(), Api(), ProgressActionModel()
        payload = paused
        scheduler = ProgressScheduler(api, client, model, clock=clock,
                                      poll_interval=.025, tick_interval=.01)
        scheduler.start(); scheduler.handle_add({"uuid": ACTION_UUID, "context": "paused"})
        self.assertTrue(self._wait_for(lambda: len(client.calls) >= 3))
        self.assertEqual(len(api.sends), 1, "unchanged paused polls must remain idle")
        self.assertTrue(scheduler.stop(.5))

    def test_scheduler_status_recovery_failed_send_stale_render_and_poll_stop(self):
        payload = state(updated_at=NOW.isoformat(), position_updated_at=NOW.isoformat(),
                        is_playing=False)

        class Client:
            def __init__(self): self.results = [BridgeStateResult("configuration"),
                                                BridgeStateResult("configuration"),
                                                BridgeStateResult("incompatible"),
                                                BridgeStateResult("ok", payload, 200)]
            def get_state(self, cancelled=None):
                return self.results.pop(0) if self.results else BridgeStateResult("ok", payload, 200)
        class Api:
            def __init__(self): self.sends, self.fail = [], True
            def setBaseDataIcon(self, context, data, text):
                self.sends.append((context, data)); result = not self.fail; self.fail = False; return result

        api, model = Api(), ProgressActionModel()
        scheduler = ProgressScheduler(api, Client(), model, clock=lambda: NOW,
                                      poll_interval=.02, tick_interval=.01)
        scheduler.start(); scheduler.handle_add({"uuid": ACTION_UUID, "context": "recover"})
        self.assertTrue(self._wait_for(lambda: len(api.sends) >= 4))
        decoded = [base64.b64decode(data.split(",", 1)[1]).decode() for _, data in api.sends]
        self.assertIn("Companion setup required", decoded[0])
        self.assertTrue(any("Incompatible companion" in svg for svg in decoded))
        self.assertTrue(any("3:00" in svg for svg in decoded))
        self.assertTrue(scheduler.stop(.5))

        class BlockingModel(ProgressActionModel):
            entered, release = threading.Event(), threading.Event()
            def render(self, request, snapshot, clock):
                intent = super().render(request, snapshot, clock)
                self.entered.set(); self.release.wait(.5); return intent
        model, api = BlockingModel(), Api(); api.fail = False
        scheduler = ProgressScheduler(api, Client(), model, clock=lambda: NOW, poll_interval=1)
        scheduler.start(); scheduler.handle_add({"uuid": ACTION_UUID, "context": "race"})
        self.assertTrue(model.entered.wait(.5))
        scheduler.handle_clear({"param": [{"context": "race"}]}); model.release.set()
        time.sleep(.03)
        self.assertEqual(api.sends, [])
        self.assertTrue(scheduler.stop(.5))

        class BlockingClient:
            entered = threading.Event()
            def get_state(self, cancelled=None):
                self.entered.set()
                while not cancelled(): time.sleep(.002)
                return BridgeStateResult("stopped")
        scheduler = ProgressScheduler(Api(), BlockingClient(), ProgressActionModel())
        scheduler.start(); scheduler.handle_add({"uuid": ACTION_UUID, "context": "close"})
        self.assertTrue(BlockingClient.entered.wait(.5))
        self.assertTrue(scheduler.stop(.5))
        self.assertFalse(scheduler.worker_alive)

        class BlockingApi:
            entered, release = threading.Event(), threading.Event()
            def setBaseDataIcon(self, context, data, text):
                self.entered.set(); self.release.wait(.5); return True
        blocking_api = BlockingApi()
        scheduler = ProgressScheduler(blocking_api, Client(), ProgressActionModel(),
                                      clock=lambda: NOW)
        scheduler.start(); scheduler.handle_add({"uuid": ACTION_UUID, "context": "send"})
        self.assertTrue(blocking_api.entered.wait(.5))
        started = time.monotonic(); self.assertFalse(scheduler.stop(.02))
        self.assertLess(time.monotonic() - started, .1)
        blocking_api.release.set(); self.assertTrue(scheduler.stop(.5))

    def test_reentrant_sdk_sends_do_not_hold_the_model_lock_or_echo_forever(self):
        payload = state(updated_at=NOW.isoformat(), position_updated_at=NOW.isoformat(),
                        is_playing=False)

        class Client:
            def get_state(self, cancelled=None):
                return BridgeStateResult("ok", payload, 200)

        class Api:
            def __init__(self):
                self.scheduler = None
                self.settings_calls = 0
                self.display_calls = 0
                self.completed = threading.Event()

            def setSettings(self, settings, context):
                self.settings_calls += 1
                if self.settings_calls == 1:
                    self.scheduler.handle_settings({"context": context,
                                                    "settings": settings})
                return True

            def setBaseDataIcon(self, context, data, text):
                self.display_calls += 1
                if self.display_calls == 1:
                    self.scheduler.handle_run({"uuid": ACTION_UUID,
                                               "context": context})
                else:
                    self.completed.set()
                return True

        api, model = Api(), ProgressActionModel()
        scheduler = ProgressScheduler(api, Client(), model, clock=lambda: NOW,
                                      poll_interval=.03)
        api.scheduler = scheduler
        scheduler.start(); scheduler.handle_add({
            "uuid": ACTION_UUID, "context": "reentrant", "param": {},
        })
        self.assertTrue(api.completed.wait(1), "reentrant SDK callback deadlocked")
        time.sleep(.07)
        self.assertEqual(api.settings_calls, 1, "canonical reentrant echo retried")
        self.assertEqual(api.display_calls, 2)
        self.assertTrue(scheduler.stop(.5))

    def test_shared_bridge_client_serializes_command_and_state_requests(self):
        class Probe(BridgeClient):
            def __init__(self):
                super().__init__(token_loader=lambda: TOKEN); self.active = 0; self.overlap = False
            def enter(self, result):
                self.active += 1; self.overlap |= self.active > 1
                time.sleep(.02); self.active -= 1; return result
            def _execute(self, command, cancelled=None):
                return self.enter(BridgeResult(command, "ok"))
            def _get_state(self, cancelled=None):
                return self.enter(BridgeStateResult("configuration"))
        client = Probe()
        threads = [threading.Thread(target=client.execute, args=("next",)),
                   threading.Thread(target=client.get_state)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(.2)
        self.assertFalse(client.overlap)


if __name__ == "__main__":
    unittest.main()
