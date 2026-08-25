import base64
import json
import subprocess
import struct
import sys
import threading
import time
import unittest
import zlib
from pathlib import Path


ROOT = Path(__file__).parents[1]
RUNTIME = ROOT / "com.arkamax404.mediacontrold200.ulanziPlugin" / "runtime" / "python"
sys.path.insert(0, str(RUNTIME))

from artwork_bundle import (  # noqa: E402
    ArtworkBundle,
    ArtworkBundleCache,
    ArtworkFetchReservation,
    MAX_AGGREGATE_BYTES,
    PNG_DATA_URI_PREFIX,
    parse_artwork_bundle,
)
from bridge_client import (  # noqa: E402
    MAX_ARTWORK_BODY_BYTES,
    BridgeArtworkResult,
    BridgeClient,
)
from now_playing_action import (  # noqa: E402
    ACTION_UUID,
    MediaSnapshot,
    NowPlayingActionModel,
    RenderIntent,
    RenderRequest,
    normalize_media_snapshot,
    now_playing_text,
    unavailable_media_snapshot,
)


TOKEN = "A" * 43
INSTANCE_ID = "123e4567-e89b-42d3-a456-426614174000"
ARTWORK_ID = "a" * 64
OTHER_ID = "b" * 64
NOW = __import__("datetime").datetime(2026, 8, 25, 12, tzinfo=__import__("datetime").timezone.utc)


def chunk(kind, data=b""):
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def png(width=1, height=1, profile=bytes((8, 6, 0, 0, 0)), idat=b"x",
        before=(), after=()):
    header = struct.pack(">II", width, height) + profile
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + b"".join(before) \
        + chunk(b"IDAT", idat) + b"".join(after) + chunk(b"IEND")


def uri(data=None):
    return PNG_DATA_URI_PREFIX + base64.b64encode(png() if data is None else data).decode("ascii")


def payload(artwork_id=ARTWORK_ID, values=None):
    values = values or [uri(png(idat=bytes((index + 1,)))) for index in range(6)]
    return {"id": artwork_id, "color": values[0], "grayscale": values[1],
            "tiles": values[2:]}


def health():
    return {"service": "d200-gsmtc-bridge", "api_major": 1, "api_minor": 0,
            "status": "ready", "instance_id": INSTANCE_ID}


class Response:
    def __init__(self, value=None, status=200, raw=None):
        self.status = status
        self.body = raw if raw is not None else json.dumps(value).encode("utf-8")
        self.read_amounts = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, amount=-1):
        self.read_amounts.append(amount)
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


class PythonArtworkTests(unittest.TestCase):
    def test_get_uses_serial_health_auth_exact_path_and_no_instance_header(self):
        artwork = Response(payload())
        opener = Opener([Response(health()), artwork])
        result = BridgeClient(token_loader=lambda: TOKEN, opener=opener).get_artwork(ARTWORK_ID)
        self.assertTrue(result.ok)
        self.assertEqual([(call[0].full_url, call[0].method, call[1]) for call in opener.calls], [
            ("http://127.0.0.1:43821/health", "GET", 1.0),
            (f"http://127.0.0.1:43821/artwork/{ARTWORK_ID}", "GET", 1.0),
        ])
        headers = {key.lower(): value for key, value in opener.calls[1][0].header_items()}
        self.assertEqual(headers, {"authorization": f"Bearer {TOKEN}"})
        self.assertEqual(artwork.read_amounts, [MAX_ARTWORK_BODY_BYTES + 1])

    def test_invalid_ids_never_load_token_or_send_http(self):
        for value in (None, "A" * 64, "a" * 63, "a" * 65, "../" + ARTWORK_ID):
            with self.subTest(value=value):
                opener = Opener([])
                result = BridgeClient(token_loader=lambda: (_ for _ in ()).throw(AssertionError()),
                                      opener=opener).get_artwork(value)
                self.assertEqual(result, BridgeArtworkResult("invalid"))
                self.assertEqual(opener.calls, [])

    def test_get_contains_body_json_http_network_timeout_and_schema_failures(self):
        cases = (
            ([Response(health()), Response(raw=b"x" * (MAX_ARTWORK_BODY_BYTES + 1))], 200),
            ([Response(health()), Response(raw=b"{")], 200),
            ([Response(health()), Response([], status=200)], 200),
            ([Response(health()), Response({}, status=404)], 404),
            ([Response(health()), TimeoutError("slow")], None),
        )
        for responses, status in cases:
            with self.subTest(status=status):
                result = BridgeClient(token_loader=lambda: TOKEN,
                                      opener=Opener(responses)).get_artwork(ARTWORK_ID)
                self.assertEqual(result.status, "unavailable")
                self.assertEqual(result.status_code, status)

    def test_png_rejects_structure_crc_profile_dimensions_and_noncanonical_base64(self):
        good = png()
        bad_crc = bytearray(good); bad_crc[-1] ^= 1
        invalid = (
            b"", b"not-png", good[:-1], bytes(bad_crc), good + b"x",
            b"\x89PNG\r\n\x1a\n" + chunk(b"IDAT", b"x") + chunk(b"IEND"),
            png(idat=b""), png(before=(chunk(b"tEXt", b"x"),)),
            png(after=(chunk(b"IHDR", b"x"),)),
            png(width=0), png(width=4097), png(width=4096, height=1025),
            png(profile=bytes((16, 6, 0, 0, 0))), png(profile=bytes((8, 2, 0, 0, 0))),
            png(profile=bytes((8, 6, 1, 0, 0))), png(profile=bytes((8, 6, 0, 1, 0))),
            png(profile=bytes((8, 6, 0, 0, 1))),
        )
        malformed_uris = [uri(value) for value in invalid]
        malformed_uris += [
            "Data:image/png;base64," + base64.b64encode(good).decode("ascii"),
            PNG_DATA_URI_PREFIX + base64.b64encode(good).decode("ascii")[:-1],
            PNG_DATA_URI_PREFIX + base64.b64encode(good).decode("ascii") + "\n",
        ]
        for value in malformed_uris:
            with self.subTest(size=len(value)):
                values = [uri()] * 6; values[3] = value
                self.assertIsNone(parse_artwork_bundle(payload(values=values), ARTWORK_ID))

    def test_exact_schema_all_members_atomicity_and_id_match(self):
        valid = payload()
        mutations = []
        for key in valid:
            changed = dict(valid); changed.pop(key); mutations.append(changed)
        changed = dict(valid); changed["extra"] = 1; mutations.append(changed)
        changed = dict(valid); changed["id"] = OTHER_ID; mutations.append(changed)
        changed = dict(valid); changed["color"] = 1; mutations.append(changed)
        changed = dict(valid); changed["tiles"] = tuple(valid["tiles"]); mutations.append(changed)
        changed = dict(valid); changed["tiles"] = valid["tiles"][:3]; mutations.append(changed)
        for changed in mutations:
            with self.subTest(keys=changed.keys()):
                self.assertIsNone(parse_artwork_bundle(changed, ARTWORK_ID))

    def test_six_uris_are_retained_byte_exact_in_tl_tr_bl_br_order(self):
        values = [uri(png(idat=bytes((index,)))) for index in range(1, 7)]
        bundle = parse_artwork_bundle(payload(values=values), ARTWORK_ID)
        self.assertEqual((bundle.color, bundle.grayscale), tuple(values[:2]))
        self.assertEqual(bundle.tiles, tuple(values[2:]))
        self.assertIs(bundle.color, values[0])
        self.assertIs(bundle.grayscale, values[1])
        self.assertTrue(all(actual is expected for actual, expected in zip(bundle.tiles, values[2:])))

    def test_individual_and_six_image_aggregate_decoded_bounds_are_atomic(self):
        too_large = uri(png(idat=b"x" * 1_000_000))
        values = [uri()] * 6; values[0] = too_large
        self.assertIsNone(parse_artwork_bundle(payload(values=values), ARTWORK_ID))
        member_size = MAX_AGGREGATE_BYTES // 6
        values = [uri(png(idat=bytes((index,)) * member_size)) for index in range(1, 7)]
        self.assertIsNone(parse_artwork_bundle(payload(values=values), ARTWORK_ID))

    def test_one_entry_cache_evicts_before_install_and_rejects_stale_response(self):
        first = parse_artwork_bundle(payload(), ARTWORK_ID)
        second = parse_artwork_bundle(payload(OTHER_ID), OTHER_ID)
        cache = ArtworkBundleCache()
        self.assertIsNone(cache.begin(ARTWORK_ID))
        first_reservation = cache.reserve(ARTWORK_ID)
        self.assertTrue(cache.install(first_reservation, first))
        self.assertIs(cache.begin(ARTWORK_ID), first)
        self.assertIsNone(cache.begin(OTHER_ID))
        self.assertIsNone(cache.get(ARTWORK_ID))
        self.assertFalse(cache.install(first_reservation, first))
        second_reservation = cache.reserve(OTHER_ID)
        self.assertTrue(cache.install(second_reservation, second))
        self.assertIs(cache.get(OTHER_ID), second)

    def test_reservation_epoch_is_immutable_and_install_validation_is_atomic(self):
        bundle = parse_artwork_bundle(payload(), ARTWORK_ID)
        cache = ArtworkBundleCache(); cache.begin(ARTWORK_ID)
        relevance = (RenderRequest("cover", 1, 1),)
        stale = cache.reserve(ARTWORK_ID, relevance)
        self.assertEqual((stale.artwork_id, stale.relevance), (ARTWORK_ID, relevance))
        with self.assertRaises(__import__("dataclasses").FrozenInstanceError):
            stale.epoch = 99
        cache.invalidate()
        self.assertFalse(cache.install(stale, bundle))
        current = cache.reserve(ARTWORK_ID, relevance)
        self.assertTrue(cache.install(current, bundle))
        self.assertIs(cache.get(ARTWORK_ID), bundle)

    def test_cache_fetch_clears_before_external_call_and_failure_retries(self):
        bundle = parse_artwork_bundle(payload(), ARTWORK_ID)

        class Client:
            def __init__(self):
                self.calls = 0

            def get_artwork(inner, artwork_id, cancelled=None):
                inner.calls += 1
                self.assertIsNone(cache.get(artwork_id))
                return BridgeArtworkResult("unavailable") if inner.calls == 1 \
                    else BridgeArtworkResult("ok", bundle, 200)

        cache = ArtworkBundleCache(); client = Client()
        self.assertIsNone(cache.fetch(ARTWORK_ID, client))
        self.assertIs(cache.fetch(ARTWORK_ID, client), bundle)
        self.assertIs(cache.fetch(ARTWORK_ID, client), bundle)
        self.assertEqual(client.calls, 2)
        cache.close(); cache.clear()
        self.assertIsNone(cache.begin(OTHER_ID))
        self.assertFalse(cache.install(ArtworkFetchReservation(OTHER_ID, 1, ()), bundle))
        self.assertIsNone(cache.fetch(OTHER_ID, client))
        self.assertEqual(client.calls, 2, "closed cache must not revive bridge fetches")

    def test_cancelled_wait_and_cache_reentrant_validation_do_not_deadlock(self):
        client = BridgeClient(token_loader=lambda: TOKEN, opener=Opener([]))
        client._request_active = True
        client._request_available.clear()
        self.assertEqual(client.get_artwork(ARTWORK_ID, cancelled=lambda: True).status, "stopped")

        cache = ArtworkBundleCache(); cache.begin(ARTWORK_ID)
        valid = parse_artwork_bundle(payload(), ARTWORK_ID)

        class ReentrantBundle(ArtworkBundle):
            def __getattribute__(self, name):
                if name == "artwork_id":
                    cache.get(ARTWORK_ID)
                return super().__getattribute__(name)

        probe = ReentrantBundle(valid.artwork_id, valid.color, valid.grayscale, valid.tiles)
        self.assertTrue(cache.install(cache.reserve(ARTWORK_ID), probe))

    def test_model_and_cache_create_no_threads_or_external_resources(self):
        before = tuple(threading.enumerate())
        bundle = parse_artwork_bundle(payload(), ARTWORK_ID)
        cache = ArtworkBundleCache(); cache.begin(ARTWORK_ID)
        cache.install(cache.reserve(ARTWORK_ID), bundle)
        self.assertEqual(tuple(threading.enumerate()), before)

    def test_now_playing_lifecycle_recreation_activation_and_unknown_calls(self):
        model = NowPlayingActionModel()
        self.assertEqual(ACTION_UUID, "com.arkamax404.ulanzi.mediacontrol.nowplaying")
        self.assertEqual(model.add({"uuid": "other", "context": "full___key___action"}), ())
        first = model.add({"uuid": ACTION_UUID, "context": "full___key___action"})[0]
        stale_intent = self._intent(model, first)
        recreated = model.add({"action": ACTION_UUID, "context": first.context})[0]
        self.assertGreater(recreated.generation, first.generation)
        self.assertFalse(model.acknowledge(stale_intent, True))
        self.assertEqual(model.set_active({"context": "missing", "active": False}), ())
        self.assertEqual(model.set_active({"context": first.context, "active": False}), ())
        self.assertEqual(model.requests(), ())
        active = model.set_active({"context": first.context, "active": True})[0]
        self.assertGreater(active.version, recreated.version)
        self.assertFalse(model.clear({"param": [{"context": "missing"}]}))
        self.assertTrue(model.clear({"param": [{"context": first.context},
                                                {"context": first.context}]}))
        self.assertIsNone(model.context(first.context))

    def test_normalized_statuses_text_coercion_trim_and_utf16_boundaries(self):
        def state(**values):
            return {"updated_at": NOW.isoformat(), "available": True, "is_playing": True,
                    "title": " Track ", "artist": " Artist ", "artwork_id": ARTWORK_ID,
                    **values}

        current = normalize_media_snapshot(state(), lambda: NOW)
        self.assertEqual(current, MediaSnapshot(True, True, True, "Track", "Artist",
                                                ARTWORK_ID, "ready"))
        self.assertEqual(now_playing_text(current), "Track\nArtist")
        self.assertEqual(now_playing_text(MediaSnapshot(True, True, False, "", "", None,
                                                        "ready")), "Playing")
        self.assertEqual(now_playing_text(MediaSnapshot(True, True, False, "", "Artist", None,
                                                        "ready")), "Artist")
        unicode_title = "😀" * 49
        capped = normalize_media_snapshot(state(title=unicode_title, artist=""), lambda: NOW)
        self.assertEqual(capped.title, "😀" * 24)
        self.assertEqual(len(capped.title.encode("utf-16-le")) // 2, 48)
        split = normalize_media_snapshot(state(title="A" * 47 + "😀", artist=""), lambda: NOW)
        self.assertEqual(split.title, "A" * 47 + "\ud83d")
        self.assertEqual(json.loads(json.dumps({"text": split.title})), {"text": split.title})
        self.assertIn(r"\ud83d", json.dumps({"text": split.title}))
        model = NowPlayingActionModel()
        request = model.add({"uuid": ACTION_UUID, "context": "surrogate"})[0]
        intent = model.render(request, split)
        self.assertEqual(intent.text, "A" * 47 + "\ufffd")
        wire = json.dumps({"textData": intent.text}, ensure_ascii=False).encode("utf-8")
        self.assertTrue(wire.endswith(b'\xef\xbf\xbd"}'))
        whole = normalize_media_snapshot(state(title="A" * 46 + "😀X", artist=""), lambda: NOW)
        self.assertEqual(whole.title, "A" * 46 + "😀")
        coercions = (
            (False, ""), (True, "true"), (0, ""), (42, "42"),
            (100000000000000000000, "100000000000000000000"),
            (1e21, "1e+21"),
            (["Track", 2, True, None, {"x": 1}], "Track,2,true,,[object Object]"),
            ({"title": "Track"}, "[object Object]"),
            ("\u0085Track\u0085", "\u0085Track\u0085"),
            ("\ufeff\u2000 Track \u3000", "Track"),
        )
        for value, expected in coercions:
            with self.subTest(value=value):
                self.assertEqual(normalize_media_snapshot(
                    state(title=value, artist=""), lambda: NOW).title, expected)
        self.assertEqual(normalize_media_snapshot(state(available=False), lambda: NOW).status,
                         "no_session")
        self.assertEqual(normalize_media_snapshot(state(updated_at="bad"), lambda: NOW).status,
                         "offline")

    def test_text_normalization_matches_production_node_for_bounded_json_matrix(self):
        values = [None, False, True, 0, -1, 1.25, 1e-7, 1e-6, 1e20, 1e21,
                  [], ["Track", 2, True, None, {"x": 1}], {},
                  "\u0085Track\u0085", "\ufeff Track \u3000", "A" * 47 + "😀"]
        script = """
import fs from "node:fs";
import { normalizeBridgeState } from "./src/plugin.js";
const values = JSON.parse(fs.readFileSync(0, "utf8"));
const base = { updated_at: "2026-08-25T12:00:00+00:00", available: true };
const now = Date.parse(base.updated_at);
console.log(JSON.stringify(values.map((title) => normalizeBridgeState({ ...base, title }, now).title)));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "--eval", script],
            cwd=ROOT / "com.arkamax404.mediacontrold200.ulanziPlugin",
            input=json.dumps(values), text=True, encoding="utf-8", capture_output=True, check=True,
        )
        expected = json.loads(result.stdout)
        actual = [normalize_media_snapshot(
            {"updated_at": NOW.isoformat(), "available": True, "title": value},
            lambda: NOW,
        ).title for value in values]
        self.assertEqual(actual, expected)

    def test_exact_color_grayscale_resume_identity_mismatch_and_tiles_unused(self):
        values = [uri(png(idat=bytes((index,)))) for index in range(1, 7)]
        bundle = parse_artwork_bundle(payload(values=values), ARTWORK_ID)
        model = NowPlayingActionModel()
        request = model.add({"uuid": ACTION_UUID, "context": "cover"})[0]
        playing = MediaSnapshot(True, True, True, "Track", "Artist", ARTWORK_ID, "ready")
        color = model.render(request, playing, bundle)
        paused = model.render(request, MediaSnapshot(**{**playing.__dict__, "is_playing": False}),
                              bundle)
        resumed = model.render(request, playing, bundle)
        self.assertEqual((color.method, paused.method), ("setBaseDataIcon", "setBaseDataIcon"))
        self.assertIs(color.image, bundle.color)
        self.assertIs(paused.image, bundle.grayscale)
        self.assertIs(resumed.image, color.image)
        self.assertNotIn(color.image, bundle.tiles)
        mismatch = parse_artwork_bundle(payload(OTHER_ID), OTHER_ID)
        fallback = model.render(request, playing, mismatch)
        self.assertEqual((fallback.method, fallback.image, fallback.text),
                         ("setPathIcon", "./assets/music.svg", "Track\nArtist"))
        self.assertTrue(model.reserve_send(color))
        self.assertTrue(model.acknowledge(color, True))
        committed = model._contexts[request.context].committed_signature
        self.assertIs(type(committed), tuple)
        self.assertTrue(all(type(value) is str for value in committed))
        self.assertEqual(committed, color.signature)
        self.assertEqual(committed[1].encode("utf-8"), bundle.color.encode("utf-8"))

    def test_all_fallbacks_dedup_failures_and_mutation_round_trip(self):
        model = NowPlayingActionModel()
        request = model.add({"uuid": ACTION_UUID, "context": "cover"})[0]
        for reason, label in (("configuration", "Companion setup required"),
                              ("incompatible", "Incompatible companion"),
                              ("unavailable", "Offline")):
            intent = model.render(request, unavailable_media_snapshot(reason))
            self.assertEqual((intent.image, intent.text), ("./assets/offline.svg", label))
        no_session = model.render(request, MediaSnapshot(True, False, False, "", "", None,
                                                         "no_session"))
        self.assertEqual((no_session.image, no_session.text), ("./assets/offline.svg", "Offline"))
        online = MediaSnapshot(True, True, False, "", "", None, "ready")
        intent = model.render(request, online)
        self.assertTrue(model.reserve_send(intent))
        self.assertTrue(model.acknowledge(intent, False))
        self.assertIsNotNone(model.render(request, online), "failed sends retry")
        self.assertTrue(model.acknowledge(intent, True))
        self.assertIsNone(model.render(request, online), "successful sends dedup")
        model.set_active({"context": "cover", "active": False})
        current = model.set_active({"context": "cover", "active": True})[0]
        round_trip = model.render(current, online)
        self.assertEqual(round_trip.signature, intent.signature)

    def test_stale_requests_acknowledgements_reservations_and_shutdown(self):
        snapshot = MediaSnapshot(True, True, True, "", "", None, "ready")
        for invalidation in ("inactive", "recreate", "shutdown"):
            for reservation_first in (False, True):
                model = NowPlayingActionModel()
                request = model.add({"uuid": ACTION_UUID, "context": "cover"})[0]
                intent = model.render(request, snapshot)
                accepted = model.reserve_send(intent) if reservation_first else None
                if invalidation == "inactive":
                    model.set_active({"context": "cover", "active": False})
                elif invalidation == "recreate":
                    model.add({"uuid": ACTION_UUID, "context": "cover"})
                else:
                    model.shutdown()
                if reservation_first:
                    self.assertTrue(accepted)
                    self.assertFalse(model.acknowledge(intent, True))
                else:
                    self.assertFalse(model.reserve_send(intent))
        closed = NowPlayingActionModel(); closed.shutdown()
        self.assertEqual(closed.add({"uuid": ACTION_UUID, "context": "late"}), ())

    def test_hostile_reentry_concurrency_and_zero_model_resources(self):
        model = NowPlayingActionModel()

        class ReentrantMapping(dict):
            def get(self, key, default=None):
                model.requests()
                return super().get(key, default)

        class ReentrantItems:
            def __iter__(self):
                model.context("cover")
                yield {"context": "cover"}

        before = tuple(threading.enumerate())
        model.add(ReentrantMapping(uuid=ACTION_UUID, context="cover"))
        thread = threading.Thread(target=lambda: model.clear({"param": ReentrantItems()}),
                                  daemon=True)
        thread.start(); thread.join(1)
        self.assertFalse(thread.is_alive(), "caller iterable reentry deadlocked")
        errors = []

        def churn(index):
            try:
                context = f"cover-{index}"
                for _ in range(20):
                    model.add({"uuid": ACTION_UUID, "context": context})
                    model.set_active({"context": context, "active": False})
                    model.set_active({"context": context, "active": True})
                model.clear({"param": [{"context": context}]})
            except BaseException as error:
                errors.append(error)

        workers = [threading.Thread(target=churn, args=(index,)) for index in range(6)]
        for worker in workers: worker.start()
        for worker in workers: worker.join(2)
        self.assertEqual(errors, [])
        time.sleep(0)
        self.assertEqual(tuple(threading.enumerate()), before)

    def test_hostile_string_identity_never_reenters_under_model_lock(self):
        model = NowPlayingActionModel()
        calls = []

        class Hostile(str):
            def __hash__(self):
                calls.append("hash")
                model.requests()
                return str.__hash__(self)

            def __eq__(self, other):
                calls.append("eq")
                model.requests()
                return str.__eq__(self, other)

            def __str__(self):
                calls.append("str")
                model.requests()
                return str.__str__(self)

        def bounded(operation):
            outcome = []
            worker = threading.Thread(target=lambda: outcome.append(operation()), daemon=True)
            worker.start(); worker.join(1)
            self.assertFalse(worker.is_alive(), "hostile string identity reentry deadlocked")
            return outcome[0]

        context = Hostile("com.arkamax404.ulanzi.mediacontrol.nowplaying___key___action")
        self.assertEqual(hash(context), str.__hash__(context))
        self.assertTrue(context == str.__str__(context))
        self.assertEqual(str(context), str.__str__(context))
        self.assertEqual(set(calls), {"hash", "eq", "str"})
        calls.clear()
        request = bounded(lambda: model.add({"uuid": Hostile(ACTION_UUID),
                                             "context": context}))[0]
        self.assertIs(type(request.context), str)
        self.assertIs(type(model.context(Hostile(request.context)).context), str)
        active = bounded(lambda: model.set_active({"context": Hostile(request.context),
                                                   "active": True}))[0]
        hostile_request = RenderRequest(Hostile(active.context), active.generation, active.version)
        intent = bounded(lambda: model.render(
            hostile_request, MediaSnapshot(True, True, False, "", "", None, "ready")))
        hostile_intent = RenderIntent(Hostile(intent.context), intent.generation, intent.version,
                                      intent.method, intent.image, intent.text, intent.signature)
        self.assertTrue(bounded(lambda: model.reserve_send(hostile_intent)))
        self.assertTrue(bounded(lambda: model.acknowledge(hostile_intent, False)))
        self.assertTrue(bounded(lambda: model.clear(
            {"param": [{"context": Hostile(intent.context)}]})))
        self.assertIn("str", calls)
        self.assertNotIn("hash", calls)
        self.assertNotIn("eq", calls)

    def test_hostile_render_intents_are_canonicalized_or_rejected_before_lock(self):
        model = NowPlayingActionModel()
        request = model.add({"uuid": ACTION_UUID, "context": "cover"})[0]
        snapshot = MediaSnapshot(True, True, False, "", "", None, "ready")
        generated = model.render(request, snapshot)
        calls = []

        def bounded(operation):
            outcome = []
            worker = threading.Thread(target=lambda: outcome.append(operation()), daemon=True)
            worker.start(); worker.join(1)
            self.assertFalse(worker.is_alive(), "hostile render intent deadlocked")
            return outcome[0]

        def assert_rejected(intent):
            self.assertFalse(bounded(lambda: model.reserve_send(intent)))
            self.assertIsNotNone(model.render(request, snapshot))
            self.assertFalse(bounded(lambda: model.acknowledge(intent, True)))
            self.assertIsNotNone(model.render(request, snapshot))
            self.assertTrue(model.reserve_send(generated))

        class HostileIntent(RenderIntent):
            def __getattribute__(self, name):
                if name in {"context", "generation", "version", "method", "image", "text",
                            "signature"}:
                    calls.append(name)
                    model.requests()
                return super().__getattribute__(name)

        property_intent = HostileIntent(
            generated.context, generated.generation, generated.version, generated.method,
            generated.image, generated.text, generated.signature,
        )
        self.assertTrue(bounded(lambda: model.reserve_send(property_intent)))
        self.assertTrue(bounded(lambda: model.acknowledge(property_intent, False)))
        self.assertEqual(calls, [
            "context", "generation", "version", "method", "image", "text", "signature",
            "context", "generation", "version", "method", "image", "text", "signature",
        ])

        class HostileSequence(tuple):
            def __iter__(self):
                calls.append("tuple-iter")
                model.requests()
                return super().__iter__()

        class HostileList(list):
            def __iter__(self):
                calls.append("list-iter")
                model.requests()
                return super().__iter__()

        for signature in (HostileSequence(generated.signature), HostileList(generated.signature)):
            iterable_intent = RenderIntent(
                generated.context, generated.generation, generated.version, generated.method,
                generated.image, generated.text, signature,
            )
            self.assertTrue(bounded(lambda: model.reserve_send(iterable_intent)))
        self.assertIn("tuple-iter", calls)
        self.assertIn("list-iter", calls)

        class HostileString(str):
            def __hash__(self):
                calls.append("hash")
                model.requests()
                return str.__hash__(self)

            def __eq__(self, other):
                calls.append("eq")
                model.requests()
                return str.__eq__(self, other)

        class HostileInteger(int):
            def __hash__(self):
                calls.append("int-hash")
                model.requests()
                return int.__hash__(self)

            def __eq__(self, other):
                calls.append("int-eq")
                model.requests()
                return int.__eq__(self, other)

        hostile_values = tuple(HostileString(value) for value in generated.signature)
        hostile_strings = RenderIntent(
            HostileString(generated.context), HostileInteger(generated.generation),
            HostileInteger(generated.version),
            *hostile_values, hostile_values,
        )
        self.assertTrue(bounded(lambda: model.acknowledge(hostile_strings, True)))
        self.assertIsNone(model.render(request, snapshot))
        model.set_active({"context": request.context, "active": False})
        request = model.set_active({"context": request.context, "active": True})[0]
        generated = model.render(request, snapshot)

        class NestedList(list):
            def __iter__(self):
                calls.append("nested-iter")
                model.requests()
                return super().__iter__()

        malformed = (
            RenderIntent(generated.context, generated.generation, generated.version,
                         generated.method, generated.image, generated.text,
                         (generated.method, NestedList([generated.image]), generated.text)),
            RenderIntent(generated.context, generated.generation, generated.version,
                         generated.method, generated.image, generated.text,
                         generated.signature[:2]),
            RenderIntent(generated.context, generated.generation, generated.version,
                         generated.method, generated.image, generated.text,
                         (generated.method, generated.image, "different")),
        )
        for intent in malformed:
            assert_rejected(intent)

        class HostileTruth:
            def __bool__(self):
                calls.append("bool")
                model.requests()
                return True

        self.assertTrue(bounded(lambda: model.acknowledge(generated, HostileTruth())))
        self.assertIsNone(model.render(request, snapshot))
        self.assertIn("bool", calls)

    def test_event_identity_uses_truthy_uuid_action_then_context_segment(self):
        model = NowPlayingActionModel()
        action = model.add({"uuid": "", "action": ACTION_UUID, "context": "action"})[0]
        context = model.add({"uuid": "", "action": "",
                             "context": ACTION_UUID + "___key___action"})[0]
        self.assertEqual((action.context, context.context),
                         ("action", ACTION_UUID + "___key___action"))
        self.assertEqual(model.add({"uuid": 1, "action": ACTION_UUID, "context": "bad"}), ())
        self.assertEqual(model.add({"uuid": "", "action": 1,
                                    "context": ACTION_UUID + "___key___bad"}), ())

        class ShortCircuit(dict):
            def get(self, key, default=None):
                if key == "action":
                    raise AssertionError("truthy uuid evaluated action fallback")
                return super().get(key, default)

        short = model.add(ShortCircuit(uuid=ACTION_UUID, context="short-circuit"))[0]
        self.assertEqual(short.context, "short-circuit")

    @staticmethod
    def _intent(model, request):
        return model.render(request, MediaSnapshot(True, True, False, "", "", None, "ready"))


if __name__ == "__main__":
    unittest.main()
