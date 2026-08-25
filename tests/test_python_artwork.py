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
from xml.etree import ElementTree


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
    AUDIO_ACTIONS,
    DISPLAY_ACTION_UUIDS,
    MOSAIC_ACTIONS,
    MUTE_TOGGLE_UUID,
    NEXT_UUID,
    PREVIOUS_UUID,
    TOGGLE_UUID,
    TRANSPORT_DISPLAY,
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


MUTE_CROSS_GLYPH = ('<path fill="none" stroke="#1db954" stroke-width="8" stroke-linecap="round" '
                    'd="m64 39 22 22m0-22L64 61"/>')
MUTE_WAVES_GLYPH = ('<path fill="none" stroke="#1db954" stroke-width="7" stroke-linecap="round" '
                    'd="M61 37a19 19 0 0 1 0 26M72 27a33 33 0 0 1 0 46"/>')


def mute_svg(label, waves=False):
    glyph = MUTE_WAVES_GLYPH if waves else MUTE_CROSS_GLYPH
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="196" height="196" viewBox="0 0 196 196">'
            '<rect width="196" height="196" rx="35.28" fill="#121212"/>'
            f'<text x="98" y="44" fill="#ffffff" font-family="Arial, sans-serif" font-size="38" '
            f'font-weight="700" text-anchor="middle">{label}</text>'
            '<g transform="translate(-5 28) scale(2)">'
            f'<path fill="#1db954" d="M17 42h15l19-16v48L32 58H17z"/>{glyph}</g></svg>')


def mute_uri(label, waves=False):
    return ("data:image/svg+xml;base64,"
            + base64.b64encode(mute_svg(label, waves).encode("utf-8")).decode("ascii"))


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

    def test_mosaic_exact_mapping_bytes_fallbacks_and_pause_dedup(self):
        values = [uri(png(idat=bytes((index,)))) for index in range(1, 7)]
        bundle = parse_artwork_bundle(payload(values=values), ARTWORK_ID)
        model = NowPlayingActionModel()
        playing = MediaSnapshot(True, True, True, "Track", "Artist", ARTWORK_ID, "ready")
        requests = [model.add({"uuid": action, "context": f"tile-{index}"})[0]
                    for index, action in enumerate(MOSAIC_ACTIONS)]
        intents = [model.render(request, playing, bundle) for request in requests]
        self.assertEqual([intent.image for intent in intents], list(bundle.tiles))
        for intent, tile in zip(intents, bundle.tiles):
            self.assertIs(intent.image, tile)
            self.assertEqual((intent.method, intent.text), ("setBaseDataIcon", ""))
            self.assertEqual(intent.image.encode(), tile.encode())
            self.assertTrue(model.acknowledge(intent, True))
        paused = MediaSnapshot(**{**playing.__dict__, "is_playing": False})
        self.assertEqual([model.render(request, paused, bundle) for request in requests],
                         [None] * 4)

        mismatch = parse_artwork_bundle(payload(OTHER_ID), OTHER_ID)
        model = NowPlayingActionModel()
        fallback_requests = [model.add({"uuid": action, "context": action})[0]
                             for action in MOSAIC_ACTIONS]
        fallbacks = [model.render(request, playing, mismatch) for request in fallback_requests]
        self.assertEqual([(item.method, item.image, item.text) for item in fallbacks], [
            ("setPathIcon", path, title) for _, path, title in MOSAIC_ACTIONS.values()
        ])
        for status, text in (("configuration", "Companion setup required"),
                             ("incompatible", "Incompatible companion"),
                             ("offline", "Offline")):
            offline = [model.render(request, unavailable_media_snapshot(status))
                       for request in fallback_requests]
            self.assertEqual([(item.image, item.text) for item in offline],
                             [("./assets/offline.svg", text)] * 4)

    def test_mosaic_lifecycle_copies_and_unknown_identity_are_independent(self):
        model = NowPlayingActionModel()
        action = next(iter(MOSAIC_ACTIONS))
        first = model.add({"uuid": action, "context": "one"})[0]
        second = model.add({"uuid": action, "context": "two"})[0]
        self.assertEqual(model.add({"uuid": action + "-unknown", "context": "bad"}), ())
        self.assertEqual(len(model.requests()), 2)
        self.assertEqual(model.set_active({"context": "one", "active": False}), ())
        self.assertEqual(model.requests(), (second,))
        self.assertTrue(model.clear({"param": [{"context": "one"}]}))
        recreated = model.add({"uuid": action, "context": "one"})[0]
        self.assertGreater(recreated.generation, first.generation)
        self.assertEqual(set(request.context for request in model.requests()), {"one", "two"})

    def test_audio_actions_exact_mapping_and_identity_routing(self):
        self.assertEqual(AUDIO_ACTIONS, {
            "com.arkamax404.ulanzi.mediacontrol.volume-up": "./assets/volume-up.svg",
            "com.arkamax404.ulanzi.mediacontrol.volume-down": "./assets/volume-down.svg",
            "com.arkamax404.ulanzi.mediacontrol.mute-toggle": "./assets/mute.svg",
        })
        self.assertEqual(DISPLAY_ACTION_UUIDS,
                         frozenset((ACTION_UUID, *MOSAIC_ACTIONS, *AUDIO_ACTIONS,
                                    *TRANSPORT_DISPLAY)))
        model = NowPlayingActionModel()
        requests = [model.add({"uuid": action, "context": f"audio-{index}"})[0]
                    for index, action in enumerate(AUDIO_ACTIONS)]
        self.assertEqual(len(model.requests()), 3)
        self.assertEqual(model.add({"uuid": MUTE_TOGGLE_UUID + "-unknown", "context": "bad"}), ())
        self.assertEqual(model.add({"uuid": f"{ACTION_UUID}.volume-up", "context": "bad"}), ())
        self.assertEqual({request.context for request in model.requests()},
                         {request.context for request in requests})

    def test_audio_render_states_match_node_block_exactly(self):
        model = NowPlayingActionModel()
        requests = {action: model.add({"uuid": action, "context": action})[0]
                    for action in AUDIO_ACTIONS}
        base = {"online": True, "available": True, "is_playing": True, "title": "Track",
                "artist": "Artist", "artwork_id": None, "status": "ready",
                "audio_available": True, "volume_percent": 55}

        def snapshot(**overrides):
            return MediaSnapshot(**{**base, **overrides})

        for action in (item for item in AUDIO_ACTIONS if item != MUTE_TOGGLE_UUID):
            icon = AUDIO_ACTIONS[action]
            intent = model.render(requests[action], snapshot())
            self.assertEqual((intent.method, intent.image, intent.text),
                             ("setPathIcon", icon, "55%"))
            intent = model.render(requests[action], snapshot(volume_percent=None))
            self.assertEqual((intent.image, intent.text), (icon, "null%"))
            intent = model.render(requests[action], snapshot(volume_percent=0))
            self.assertEqual((intent.image, intent.text), (icon, "0%"))
            intent = model.render(requests[action], snapshot(audio_available=False))
            self.assertEqual((intent.image, intent.text), (icon, "No audio"))
            intent = model.render(requests[action], snapshot(available=False,
                                                             status="no_session"))
            self.assertEqual((intent.image, intent.text), (icon, "55%"),
                             "online audio actions ignore media availability")
        mute = requests[MUTE_TOGGLE_UUID]
        for overrides, label, waves in (
                ({}, "55%", False),
                ({"volume_percent": None}, "null%", False),
                ({"volume_percent": 0}, "0%", False),
                ({"volume_percent": 100}, "100%", False),
                ({"audio_available": False}, "No audio", False),
                ({"audio_available": False, "is_muted": True}, "No audio", False),
                ({"is_muted": True}, "Muted", True),
                ({"is_muted": True, "audio_mixed": True}, "Mixed", True),
                ({"audio_mixed": True}, "Mixed", False)):
            with self.subTest(overrides=overrides):
                intent = model.render(mute, snapshot(**overrides))
                self.assertEqual((intent.method, intent.image, intent.text),
                                 ("setBaseDataIcon", mute_uri(label, waves), ""))
        no_session = model.render(mute, snapshot(available=False, status="no_session"))
        self.assertEqual((no_session.method, no_session.text), ("setBaseDataIcon", ""))
        self.assertEqual(no_session.image, mute_uri("55%"),
                         "online mute toggle ignores media availability")
        muted = [model.render(requests[action], snapshot(is_muted=True))
                 for action in AUDIO_ACTIONS]
        self.assertEqual([(intent.method, intent.image, intent.text) for intent in muted[:2]], [
            ("setPathIcon", "./assets/volume-up.svg", "Muted"),
            ("setPathIcon", "./assets/volume-down.svg", "Muted"),
        ])
        self.assertEqual((muted[2].method, muted[2].image, muted[2].text),
                         ("setBaseDataIcon", mute_uri("Muted", waves=True), ""))
        mixed = [model.render(requests[action], snapshot(is_muted=True, audio_mixed=True))
                 for action in AUDIO_ACTIONS]
        self.assertEqual([(intent.method, intent.image, intent.text) for intent in mixed[:2]], [
            ("setPathIcon", "./assets/volume-up.svg", "Mixed"),
            ("setPathIcon", "./assets/volume-down.svg", "Mixed"),
        ])
        self.assertEqual((mixed[2].method, mixed[2].image, mixed[2].text),
                         ("setBaseDataIcon", mute_uri("Mixed", waves=True), ""))
        for status, label in (("configuration", "Companion setup required"),
                              ("incompatible", "Incompatible companion"),
                              ("offline", "Offline")):
            offline = [model.render(requests[action], unavailable_media_snapshot(status))
                       for action in AUDIO_ACTIONS]
            self.assertEqual([(intent.method, intent.image, intent.text) for intent in offline],
                             [("setPathIcon", "./assets/offline.svg", label)] * 3)

    def test_mute_toggle_composite_determinism_dedup_and_xml_structure(self):
        model = NowPlayingActionModel()
        request = model.add({"uuid": MUTE_TOGGLE_UUID, "context": "mute"})[0]
        base = {"online": True, "available": True, "is_playing": True, "title": "Track",
                "artist": "Artist", "artwork_id": None, "status": "ready",
                "audio_available": True, "volume_percent": 55}

        def snapshot(**overrides):
            return MediaSnapshot(**{**base, **overrides})

        first = model.render(request, snapshot())
        second = model.render(request, snapshot())
        self.assertEqual((first.method, first.text), ("setBaseDataIcon", ""))
        self.assertEqual(first.image, mute_uri("55%"))
        self.assertEqual((first.image, first.signature[1]), (second.image, second.signature[1]))
        self.assertTrue(model.reserve_send(first))
        self.assertTrue(model.acknowledge(first, True))
        self.assertIsNone(model.render(request, snapshot()), "unchanged audio state dedups")
        louder = model.render(request, snapshot(volume_percent=56))
        self.assertEqual((louder.method, louder.image, louder.text),
                         ("setBaseDataIcon", mute_uri("56%"), ""))
        self.assertNotEqual(louder.image, first.image)
        self.assertTrue(model.reserve_send(louder))
        self.assertTrue(model.acknowledge(louder, True))
        self.assertIsNone(model.render(request, snapshot(volume_percent=56)),
                          "a volume change sends exactly one new intent")
        muted = model.render(request, snapshot(is_muted=True))
        self.assertEqual(muted.image, mute_uri("Muted", waves=True))
        decoded = base64.b64decode(muted.image.split(",", 1)[1]).decode("utf-8")
        self.assertIn("Muted", decoded)
        self.assertIn('d="M61 37a19 19 0 0 1 0 26M72 27a33 33 0 0 1 0 46"', decoded)
        produced = base64.b64decode(first.image.split(",", 1)[1]).decode("utf-8")
        root = ElementTree.fromstring(produced)
        namespace = "{http://www.w3.org/2000/svg}"
        text = root.find(namespace + "text")
        group = root.find(namespace + "g")
        self.assertIsNotNone(text)
        self.assertIsNotNone(group)
        self.assertEqual(text.text, "55%")
        self.assertLess(float(text.get("y")), 60)
        self.assertEqual([path.get("d") for path in group.findall(namespace + "path")],
                         ["M17 42h15l19-16v48L32 58H17z", "m64 39 22 22m0-22L64 61"])

    def test_audio_snapshot_fields_parse_strictly_and_clamp(self):
        def state(**values):
            return {"updated_at": NOW.isoformat(), "available": True, "is_playing": True,
                    "title": "Track", "artist": "Artist", "artwork_id": ARTWORK_ID,
                    **values}

        current = normalize_media_snapshot(
            state(audio_available=True, volume_percent=55, is_muted=True, audio_mixed=True),
            lambda: NOW)
        self.assertEqual(current, MediaSnapshot(True, True, True, "Track", "Artist",
                                                ARTWORK_ID, "ready", True, 55, True, True))
        self.assertEqual(normalize_media_snapshot(state(), lambda: NOW),
                         MediaSnapshot(True, True, True, "Track", "Artist", ARTWORK_ID,
                                       "ready", False, None, False, False))
        self.assertEqual(unavailable_media_snapshot("configuration"),
                         MediaSnapshot(False, False, False, "", "", None, "configuration",
                                       False, None, False, False))
        for value, expected in ((0, 0), (100, 100), (-5, 0), (105, 100), (55, 55)):
            with self.subTest(value=value):
                self.assertEqual(normalize_media_snapshot(
                    state(volume_percent=value), lambda: NOW).volume_percent, expected)
        for value in (True, False, 5.5, "55", None, [55]):
            with self.subTest(value=value):
                self.assertIsNone(normalize_media_snapshot(
                    state(volume_percent=value), lambda: NOW).volume_percent,
                    "only true integers are accepted, bools included")
        for field in ("audio_available", "is_muted", "audio_mixed"):
            for value in (1, "true", "yes", None, [], False):
                with self.subTest(field=field, value=value):
                    snapshot = normalize_media_snapshot(state(**{field: value}), lambda: NOW)
                    self.assertFalse(getattr(snapshot, field))
            with self.subTest(field=field):
                self.assertTrue(getattr(normalize_media_snapshot(
                    state(**{field: True}), lambda: NOW), field))

    def test_audio_lifecycle_copies_dedup_and_unknown_identity(self):
        model = NowPlayingActionModel()
        action = MUTE_TOGGLE_UUID
        first = model.add({"uuid": action, "context": "one"})[0]
        second = model.add({"uuid": action, "context": "two"})[0]
        self.assertEqual(model.add({"uuid": action + "-unknown", "context": "bad"}), ())
        self.assertEqual(len(model.requests()), 2)
        self.assertEqual(model.set_active({"context": "one", "active": False}), ())
        self.assertEqual(model.requests(), (second,))
        self.assertTrue(model.clear({"param": [{"context": "one"}]}))
        recreated = model.add({"uuid": action, "context": "one"})[0]
        self.assertGreater(recreated.generation, first.generation)
        self.assertEqual(set(request.context for request in model.requests()), {"one", "two"})
        online = MediaSnapshot(True, True, False, "", "", None, "ready", True, 40, False, False)
        intent = model.render(second, online)
        self.assertEqual((intent.method, intent.image, intent.text),
                         ("setBaseDataIcon", mute_uri("40%"), ""))
        self.assertTrue(model.reserve_send(intent))
        self.assertTrue(model.acknowledge(intent, False))
        self.assertIsNotNone(model.render(second, online), "failed sends retry")
        self.assertTrue(model.acknowledge(intent, True))
        self.assertIsNone(model.render(second, online), "successful sends dedup")

    def test_transport_actions_exact_mapping_and_identity_routing(self):
        self.assertEqual(TRANSPORT_DISPLAY, {
            TOGGLE_UUID: "./assets/play.svg",
            PREVIOUS_UUID: "./assets/previous.svg",
            NEXT_UUID: "./assets/next.svg",
        })
        self.assertEqual(DISPLAY_ACTION_UUIDS,
                         frozenset((ACTION_UUID, *MOSAIC_ACTIONS, *AUDIO_ACTIONS,
                                    *TRANSPORT_DISPLAY)))
        model = NowPlayingActionModel()
        requests = [model.add({"uuid": action, "context": f"transport-{index}"})[0]
                    for index, action in enumerate(TRANSPORT_DISPLAY)]
        self.assertEqual(len(model.requests()), 3)
        self.assertEqual(model.add({"uuid": TOGGLE_UUID + "-unknown", "context": "bad"}), ())
        self.assertEqual(model.add({"uuid": f"{MUTE_TOGGLE_UUID}.toggle", "context": "bad"}), ())
        self.assertEqual(model.add({"uuid": NEXT_UUID, "context": ""}), ())
        self.assertEqual({request.context for request in model.requests()},
                         {request.context for request in requests})

    def test_transport_render_states_match_node_chain_exactly(self):
        model = NowPlayingActionModel()
        requests = {action: model.add({"uuid": action, "context": action})[0]
                    for action in TRANSPORT_DISPLAY}
        base = {"online": True, "available": True, "is_playing": True, "title": "Track",
                "artist": "Artist", "artwork_id": None, "status": "ready"}

        def snapshot(**overrides):
            return MediaSnapshot(**{**base, **overrides})

        expected = {
            TOGGLE_UUID: [("./assets/pause.svg", "Pause"), ("./assets/play.svg", "Play")],
            PREVIOUS_UUID: [("./assets/previous.svg", "Previous")],
            NEXT_UUID: [("./assets/next.svg", "Next")],
        }
        for action, variants in expected.items():
            for is_playing, (icon, label) in zip((True, False), variants):
                with self.subTest(action=action, is_playing=is_playing):
                    intent = model.render(requests[action], snapshot(is_playing=is_playing))
                    self.assertEqual((intent.method, intent.image, intent.text),
                                     ("setPathIcon", icon, label))
        for action in TRANSPORT_DISPLAY:
            no_session = model.render(requests[action],
                                       snapshot(available=False, status="no_session"))
            self.assertEqual((no_session.method, no_session.image, no_session.text),
                             ("setPathIcon", "./assets/offline.svg", "Offline"))
        for status, label in (("configuration", "Companion setup required"),
                              ("incompatible", "Incompatible companion"),
                              ("offline", "Offline")):
            offline = [model.render(requests[action], unavailable_media_snapshot(status))
                       for action in TRANSPORT_DISPLAY]
            self.assertEqual([(item.method, item.image, item.text) for item in offline],
                             [("setPathIcon", "./assets/offline.svg", label)] * 3)

    def test_transport_toggle_play_pause_flip_dedups_exactly_once(self):
        model = NowPlayingActionModel()
        request = model.add({"uuid": TOGGLE_UUID, "context": "toggle"})[0]
        playing = MediaSnapshot(True, True, True, "", "", None, "ready")
        paused = MediaSnapshot(True, True, False, "", "", None, "ready")
        first = model.render(request, playing)
        self.assertEqual((first.method, first.image, first.text),
                         ("setPathIcon", "./assets/pause.svg", "Pause"))
        self.assertTrue(model.reserve_send(first))
        self.assertTrue(model.acknowledge(first, True))
        self.assertIsNone(model.render(request, playing), "unchanged playing state dedups")
        flip = model.render(request, paused)
        self.assertEqual((flip.method, flip.image, flip.text),
                         ("setPathIcon", "./assets/play.svg", "Play"))
        self.assertTrue(model.reserve_send(flip))
        self.assertTrue(model.acknowledge(flip, True))
        self.assertIsNone(model.render(request, paused),
                          "an is_playing flip sends exactly one new intent")
        resumed = model.render(request, playing)
        self.assertEqual((resumed.image, resumed.text), ("./assets/pause.svg", "Pause"))
        self.assertTrue(model.acknowledge(resumed, True))
        self.assertIsNone(model.render(request, playing))

    def test_transport_lifecycle_copies_and_reservation_for_every_action(self):
        playing = MediaSnapshot(True, True, True, "", "", None, "ready")
        for action in TRANSPORT_DISPLAY:
            with self.subTest(action=action):
                model = NowPlayingActionModel()
                first = model.add({"uuid": action, "context": "one"})[0]
                second = model.add({"uuid": action, "context": "two"})[0]
                self.assertEqual(model.add({"uuid": action + "-unknown", "context": "bad"}), ())
                self.assertEqual(len(model.requests()), 2)
                intent = model.render(first, playing)
                icon, label = (("./assets/pause.svg", "Pause") if action == TOGGLE_UUID
                               else (TRANSPORT_DISPLAY[action],
                                     "Previous" if action == PREVIOUS_UUID else "Next"))
                self.assertEqual((intent.method, intent.image, intent.text),
                                 ("setPathIcon", icon, label))
                self.assertTrue(model.reserve_send(intent))
                self.assertTrue(model.acknowledge(intent, True))
                self.assertIsNone(model.render(first, playing), "successful sends dedup")
                self.assertEqual(model.set_active({"context": "two", "active": False}), ())
                self.assertIsNone(model.render(second, playing),
                                  "inactive copies never render")
                self.assertTrue(model.clear({"param": [{"context": "one"}]}))
                self.assertIsNone(model.render(first, playing),
                                  "cleared contexts never render")
                self.assertFalse(model.reserve_send(intent),
                                 "cleared reservations are rejected")
                recreated = model.add({"uuid": action, "context": "one"})[0]
                self.assertGreater(recreated.generation, first.generation)
                self.assertEqual(model.render(recreated, playing).signature, intent.signature)
                self.assertEqual({request.context for request in model.requests()},
                                 {"one"}, "inactive copies stay out of polls")
                self.assertGreater(
                    model.set_active({"context": "two", "active": True})[0].version,
                    second.version)
                self.assertEqual(set(request.context for request in model.requests()),
                                 {"one", "two"})

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
