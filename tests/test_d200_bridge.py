import asyncio
import base64
import io
import json
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import d200_bridge.__main__ as bridge_main
import d200_bridge.artwork as artwork_module
import d200_bridge.gsmtc as gsmtc_module
from d200_bridge.artwork import ARTWORK_CACHE_SIZE, ArtworkProcessor, ArtworkVariants
from d200_bridge.core_audio import AudioCommandResult
from d200_bridge.gsmtc import (
    GSMTCAdapter,
    normalize_timeline_properties,
    read_thumbnail,
    select_session,
    timespan_seconds,
)
from d200_bridge.server import BRIDGE_HOST, create_server
from d200_bridge.state import (
    MediaStateCache,
    normalize_state,
    normalize_timeline,
)

try:
    from PIL import Image, features
except ImportError:
    Image = None
    features = None


PILLOW_AVAILABLE = Image is not None
PNG_HEADER_URI = "data:image/png;base64," + base64.b64encode(
    b"\x89PNG\r\n\x1a\n"
).decode("ascii")


def image_bytes(image, image_format="PNG", **save_options):
    output = io.BytesIO()
    image.save(output, format=image_format, **save_options)
    return output.getvalue()


def data_uri_image(value):
    encoded = value.split(",", 1)[1]
    return Image.open(io.BytesIO(base64.b64decode(encoded)))


def image_pixels(image):
    return [
        image.getpixel((x, y))
        for y in range(image.height)
        for x in range(image.width)
    ]


class StateTests(unittest.TestCase):
    def test_normalizes_and_bounds_public_state(self):
        state = normalize_state(
            {
                "available": False,
                "is_playing": True,
                "title": " x " * 200,
                "artist": " Artist ",
                "thumbnail": "https://remote.invalid/cover.jpg",
                "source": "Spotify.exe",
            }
        )
        self.assertFalse(state.is_playing)
        self.assertLessEqual(len(state.title), 160)
        self.assertEqual(state.artist, "Artist")
        self.assertIsNone(state.thumbnail)

    def test_revision_changes_only_with_state(self):
        cache = MediaStateCache()
        first = cache.update({"available": True, "title": "Track"})
        second = cache.update({"available": True, "title": "Track"})
        stale = cache.unavailable()
        self.assertEqual(first.revision, second.revision)
        self.assertGreater(stale.revision, second.revision)
        self.assertFalse(stale.available)
        self.assertEqual(stale.title, "")

    def test_media_and_audio_updates_preserve_each_other(self):
        cache = MediaStateCache()
        cache.update({"available": True, "title": "Track"})
        cache.update_audio(
            {
                "audio_available": True,
                "volume_percent": 65,
                "is_muted": False,
                "audio_session_count": 2,
                "audio_mixed": True,
            }
        )
        cache.update({"available": True, "title": "Next Track"})
        self.assertEqual(cache.get().volume_percent, 65)
        cache.audio_unavailable()
        self.assertEqual(cache.get().title, "Next Track")

    def test_timeline_partial_update_preserves_media_and_audio(self):
        cache = MediaStateCache()
        cache.update({"available": True, "title": "Track"})
        cache.update_audio({"audio_available": True, "volume_percent": 65})
        state = cache.update_timeline(
            {
                "timeline_available": True,
                "position_seconds": 12,
                "duration_seconds": 90,
                "playback_rate": 1.25,
                "position_updated_at": "2026-08-23T12:00:00+00:00",
            }
        )
        self.assertEqual(state.title, "Track")
        self.assertEqual(state.volume_percent, 65)
        self.assertEqual(state.position_seconds, 12)

    def test_rejects_non_finite_timeline_and_keeps_fingerprint_coherent(self):
        invalid = normalize_timeline(
            {
                "timeline_available": True,
                "position_seconds": float("nan"),
                "duration_seconds": float("inf"),
                "playback_rate": float("nan"),
                "position_updated_at": "2026-08-23T12:00:00+00:00",
            }
        )
        self.assertFalse(invalid["timeline_available"])
        cache = MediaStateCache()
        first = cache.update({"available": True, "title": "Track"})
        fingerprint = cache.fingerprint()
        second = cache.update({"available": True, "title": "Track"})
        self.assertEqual(first.revision, second.revision)
        self.assertEqual(fingerprint, cache.fingerprint())
        self.assertEqual(json.loads(json.dumps(second.public()))["title"], "Track")

    def test_artwork_contract_includes_both_variants_and_falls_back_safely(self):
        grayscale = "data:image/png;base64," + base64.b64encode(
            b"\x89PNG\r\n\x1a\ngray"
        ).decode("ascii")
        state = normalize_state({
            "available": True,
            "thumbnail": PNG_HEADER_URI,
            "thumbnail_grayscale": grayscale,
        })
        self.assertEqual(state.thumbnail, PNG_HEADER_URI)
        self.assertEqual(state.thumbnail_grayscale, grayscale)
        self.assertEqual(state.public()["thumbnail_grayscale"], grayscale)

        missing_gray = normalize_state({
            "available": True,
            "thumbnail": PNG_HEADER_URI,
            "thumbnail_grayscale": "data:image/svg+xml;base64,PHN2Zy8+",
        })
        self.assertEqual(missing_gray.thumbnail, PNG_HEADER_URI)
        self.assertIsNone(missing_gray.thumbnail_grayscale)

        malformed_color = normalize_state({
            "available": True,
            "thumbnail": "data:image/png;base64,not-canonical",
            "thumbnail_grayscale": grayscale,
        })
        self.assertIsNone(malformed_color.thumbnail)
        self.assertIsNone(malformed_color.thumbnail_grayscale)


class ArtworkCacheTests(unittest.TestCase):
    def test_content_hash_cache_avoids_reprocessing_and_evicts_lru(self):
        processor = ArtworkProcessor(cache_size=2)
        variants = {
            data: ArtworkVariants(f"color-{data!r}", f"gray-{data!r}")
            for data in (b"one", b"two", b"three")
        }
        with patch.object(
            processor,
            "_decode_encode",
            side_effect=lambda data: variants[data],
        ) as decode:
            self.assertEqual(processor.process(b"one"), variants[b"one"])
            self.assertEqual(processor.process(b"one"), variants[b"one"])
            self.assertEqual(processor.process(b"two"), variants[b"two"])
            self.assertEqual(processor.process(b"one"), variants[b"one"])
            self.assertEqual(processor.process(b"three"), variants[b"three"])
            self.assertEqual(processor.process(b"two"), variants[b"two"])
        self.assertEqual(decode.call_count, 4)
        self.assertEqual(processor.cached_entries, 2)

    def test_transient_failure_retries_then_caches_success(self):
        processor = ArtworkProcessor()
        success = ArtworkVariants("color", "grayscale")
        with patch.object(
            processor, "_decode_encode", side_effect=[None, success]
        ) as decode:
            self.assertIsNone(processor.process(b"same-content"))
            self.assertEqual(processor.cached_entries, 0)
            self.assertEqual(processor.process(b"same-content"), success)
            self.assertEqual(processor.process(b"same-content"), success)
        self.assertEqual(decode.call_count, 2)
        self.assertEqual(processor.cached_entries, 1)

    def test_default_cache_has_eight_entry_deterministic_lru_capacity(self):
        self.assertEqual(ARTWORK_CACHE_SIZE, 8)
        processor = ArtworkProcessor()

        def variants(data):
            return ArtworkVariants(f"color-{data!r}", f"gray-{data!r}")

        with patch.object(processor, "_decode_encode", side_effect=variants) as decode:
            for index in range(8):
                processor.process(str(index).encode("ascii"))
            self.assertEqual(processor.cached_entries, 8)
            processor.process(b"0")
            processor.process(b"8")
            processor.process(b"0")
            processor.process(b"1")
        self.assertEqual(processor.cached_entries, 8)
        self.assertEqual(decode.call_count, 10)

    def test_same_content_concurrency_decodes_once_inside_lock(self):
        processor = ArtworkProcessor()
        success = ArtworkVariants("color", "grayscale")
        decode_started = threading.Event()
        second_attempting = threading.Event()
        release_decode = threading.Event()

        def decode(_data):
            decode_started.set()
            self.assertTrue(release_decode.wait(timeout=2))
            return success

        def second_call():
            second_attempting.set()
            return processor.process(b"same-content")

        with patch.object(processor, "_decode_encode", side_effect=decode) as mocked:
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(processor.process, b"same-content")
                self.assertTrue(decode_started.wait(timeout=2))
                second = executor.submit(second_call)
                self.assertTrue(second_attempting.wait(timeout=2))
                release_decode.set()
                self.assertEqual(first.result(timeout=2), success)
                self.assertEqual(second.result(timeout=2), success)
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(processor.cached_entries, 1)


@unittest.skipUnless(PILLOW_AVAILABLE, "Pillow is not installed")
class ArtworkProcessorTests(unittest.TestCase):
    def asymmetric_png(self):
        image = Image.new("RGBA", (3, 2))
        image.putdata([
            (255, 0, 0, 10), (0, 255, 0, 40), (0, 0, 255, 70),
            (250, 120, 10, 100), (20, 40, 80, 160), (240, 30, 180, 220),
        ])
        return image_bytes(image)

    def test_outputs_matching_png_frames_with_alpha_and_pixel_positions_preserved(self):
        result = ArtworkProcessor().process(self.asymmetric_png())
        color = data_uri_image(result.color).convert("RGBA")
        grayscale = data_uri_image(result.grayscale).convert("RGBA")

        self.assertTrue(result.color.startswith("data:image/png;base64,"))
        self.assertTrue(result.grayscale.startswith("data:image/png;base64,"))
        self.assertEqual(color.size, (3, 2))
        self.assertEqual(grayscale.size, color.size)
        expected = image_pixels(
            Image.open(io.BytesIO(self.asymmetric_png())).convert("RGBA")
        )
        self.assertEqual(image_pixels(color), expected)
        self.assertEqual(
            [pixel[3] for pixel in image_pixels(grayscale)],
            [pixel[3] for pixel in image_pixels(color)],
        )
        self.assertNotEqual(color.getpixel((0, 0))[0], color.getpixel((0, 0))[1])
        for pixel in image_pixels(grayscale):
            self.assertEqual(pixel[0], pixel[1])
            self.assertEqual(pixel[1], pixel[2])
        self.assertNotEqual(color.getpixel((0, 0)), color.getpixel((2, 1)))

    def test_applies_exif_orientation_identically(self):
        source = Image.new("RGB", (2, 3))
        source.putdata([
            (255, 0, 0), (0, 255, 0),
            (0, 0, 255), (255, 255, 0),
            (255, 0, 255), (0, 255, 255),
        ])
        exif = source.getexif()
        exif[274] = 6
        result = ArtworkProcessor().process(
            image_bytes(source, "JPEG", exif=exif, quality=100, subsampling=0)
        )
        color = data_uri_image(result.color)
        grayscale = data_uri_image(result.grayscale)
        self.assertEqual(color.size, (3, 2))
        self.assertEqual(grayscale.size, color.size)

    def test_animated_gif_uses_first_frame(self):
        first = Image.new("RGBA", (2, 1))
        first.putdata([(255, 0, 0, 255), (0, 255, 0, 255)])
        second = Image.new("RGBA", (2, 1), (0, 0, 255, 255))
        source = image_bytes(first, "GIF", save_all=True, append_images=[second], loop=0)
        result = ArtworkProcessor().process(source)
        color = data_uri_image(result.color).convert("RGBA")
        self.assertEqual(image_pixels(color), image_pixels(first))

    @unittest.skipUnless(
        PILLOW_AVAILABLE and features.check("webp"), "Pillow WebP codec is unavailable"
    )
    def test_animated_webp_uses_first_frame(self):
        first = Image.new("RGBA", (2, 1))
        first.putdata([(255, 0, 0, 255), (0, 255, 0, 255)])
        second = Image.new("RGBA", (2, 1), (0, 0, 255, 255))
        try:
            source = image_bytes(
                first, "WEBP", save_all=True, append_images=[second], loop=0,
                lossless=True,
            )
        except OSError as error:
            self.skipTest(f"Pillow WebP animation codec is unavailable: {error}")
        result = ArtworkProcessor().process(source)
        color = data_uri_image(result.color).convert("RGBA")
        self.assertEqual(image_pixels(color), image_pixels(first))

    def test_rejects_malformed_truncated_svg_disguised_and_resource_excesses(self):
        valid = self.asymmetric_png()
        bmp = image_bytes(Image.new("RGB", (1, 1)), "BMP")
        rejected = [
            b"", valid[:-10], b"<svg xmlns='http://www.w3.org/2000/svg'/>",
            b"\x89PNG\r\n\x1a\n<svg/>", bmp,
        ]
        processor = ArtworkProcessor()
        for source in rejected:
            self.assertIsNone(processor.process(source))
        self.assertIsNone(
            ArtworkProcessor(max_source_bytes=len(valid) - 1).process(valid)
        )
        self.assertIsNone(ArtworkProcessor(max_decoded_pixels=5).process(valid))
        self.assertIsNone(ArtworkProcessor(max_encoded_bytes=10).process(valid))
        with patch.object(artwork_module.Image, "MAX_IMAGE_PIXELS", 1):
            self.assertIsNone(ArtworkProcessor().process(valid))


class FakeStream:
    size = 5
    content_type = "image/png"

    def close(self):
        pass


class FakeStreamReference:
    async def open_read_async(self):
        return FakeStream()


class SessionSelectionTests(unittest.TestCase):
    def test_prefers_spotify_desktop_then_current_session(self):
        browser = SimpleNamespace(source_app_user_model_id="browser.exe")
        spotify = SimpleNamespace(source_app_user_model_id="Spotify.exe")
        self.assertIs(select_session([browser, spotify], browser), spotify)
        self.assertIs(select_session([browser], browser), browser)


class TimelineTests(unittest.TestCase):
    def test_converts_timedelta_and_winrt_timespan_ticks(self):
        self.assertEqual(timespan_seconds(timedelta(seconds=2.5)), 2.5)
        self.assertEqual(timespan_seconds(SimpleNamespace(duration=25_000_000)), 2.5)

    def test_normalizes_relative_position_clamp_rate_and_last_updated_time(self):
        now = datetime(2026, 8, 23, 12, 0, 5, tzinfo=timezone.utc)
        timeline = SimpleNamespace(
            start_time=timedelta(seconds=10),
            end_time=timedelta(seconds=110),
            position=timedelta(seconds=108),
            last_updated_time=now - timedelta(seconds=4),
        )
        playback = SimpleNamespace(playback_status=4, playback_rate=2.0)
        result = normalize_timeline_properties(timeline, playback, now)
        self.assertEqual(result["duration_seconds"], 100)
        self.assertEqual(result["position_seconds"], 100)
        self.assertEqual(result["playback_rate"], 2.0)
        self.assertEqual(result["position_updated_at"], now.isoformat())

    def test_paused_timeline_does_not_advance_and_invalid_rate_defaults(self):
        now = datetime(2026, 8, 23, 12, 0, 5, tzinfo=timezone.utc)
        timeline = SimpleNamespace(
            start_time=0,
            end_time=100,
            position=30,
            last_updated_time=now - timedelta(seconds=20),
        )
        playback = SimpleNamespace(playback_status=3, playback_rate=float("nan"))
        result = normalize_timeline_properties(timeline, playback, now)
        self.assertEqual(result["position_seconds"], 30)
        self.assertEqual(result["playback_rate"], 1.0)

    def test_invalid_duration_is_unavailable(self):
        timeline = SimpleNamespace(
            start_time=0, end_time=float("nan"), position=0, last_updated_time=None
        )
        result = normalize_timeline_properties(
            timeline, SimpleNamespace(playback_status=4, playback_rate=1)
        )
        self.assertFalse(result["timeline_available"])


class ThumbnailStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_winrt_stream_into_bounded_data_uri(self):
        class Reader:
            def __init__(self, _stream):
                pass

            async def load_async(self, size):
                return size

            def read_bytes(self, target):
                target[:] = b"cover"

            def close(self):
                pass

        streams = SimpleNamespace(DataReader=Reader)
        windows = SimpleNamespace(storage=SimpleNamespace(streams=streams))
        winrt = SimpleNamespace(windows=windows)
        modules = {
            "winrt": winrt,
            "winrt.windows": windows,
            "winrt.windows.storage": windows.storage,
            "winrt.windows.storage.streams": streams,
        }
        variants = ArtworkVariants(PNG_HEADER_URI, PNG_HEADER_URI)
        with patch.dict(sys.modules, modules), patch.object(
            gsmtc_module.artwork_processor, "process", return_value=variants
        ) as process:
            result = await read_thumbnail(FakeStreamReference())
        self.assertEqual(result, variants)
        process.assert_called_once_with(b"cover")


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_mapping_and_refresh_failure_invalidate_state(self):
        class Session:
            source_app_user_model_id = "Spotify.exe"

            async def try_skip_previous_async(self):
                return True

            async def try_toggle_play_pause_async(self):
                return True

            async def try_skip_next_async(self):
                return True

        cache = MediaStateCache()
        cache.update({"available": True, "title": "Old"})
        adapter = GSMTCAdapter(cache)
        adapter._session = Session()
        calls = []

        async def refresh():
            calls.append("refresh")

        adapter.refresh = refresh
        for command in ("previous", "toggle", "next"):
            self.assertTrue(await adapter.command(command))
        self.assertEqual(calls, ["refresh", "refresh", "refresh"])
        with self.assertRaises(ValueError):
            await adapter.command("volume")

        adapter.refresh = GSMTCAdapter.refresh.__get__(adapter)
        adapter._manager = SimpleNamespace(
            get_sessions=lambda: (_ for _ in ()).throw(RuntimeError("failed"))
        )
        await adapter.refresh()
        self.assertFalse(cache.get().available)

    async def test_timeline_event_refreshes_only_timeline_and_session_change_unsubscribes(self):
        class Event:
            def __init__(self):
                self.handlers = []

            def __iadd__(self, handler):
                self.handlers.append(handler)
                return self

            def __isub__(self, handler):
                self.handlers.remove(handler)
                return self

            def fire(self):
                for handler in list(self.handlers):
                    handler(None, None)

        class Session:
            source_app_user_model_id = "Spotify.exe"

            def __init__(self, title):
                self.title = title
                self.media_properties_changed = Event()
                self.playback_info_changed = Event()
                self.timeline_properties_changed = Event()
                self.media_reads = 0

            async def try_get_media_properties_async(self):
                self.media_reads += 1
                return SimpleNamespace(title=self.title, artist="Artist", thumbnail=None)

            def get_playback_info(self):
                return SimpleNamespace(playback_status=4, playback_rate=1.0)

            def get_timeline_properties(self):
                return SimpleNamespace(
                    start_time=0,
                    end_time=100,
                    position=20,
                    last_updated_time=datetime.now(timezone.utc),
                )

        first = Session("First")
        second = Session("Second")
        selected = [first]
        manager = SimpleNamespace(
            sessions_changed=Event(),
            current_session_changed=Event(),
            get_sessions=lambda: selected,
            get_current_session=lambda: selected[0],
        )
        cache = MediaStateCache()
        adapter = GSMTCAdapter(
            cache,
            manager_factory=AsyncMock(return_value=manager),
            thumbnail_reader=AsyncMock(return_value=None),
        )
        await adapter.start()
        self.assertEqual(first.media_reads, 1)
        first.timeline_properties_changed.fire()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(first.media_reads, 1)
        self.assertTrue(cache.get().timeline_available)

        selected[0] = second
        await adapter.refresh()
        self.assertEqual(first.timeline_properties_changed.handlers, [])
        self.assertEqual(len(second.timeline_properties_changed.handlers), 1)
        await adapter.stop()


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    def test_signal_handlers_notify_loop_and_can_be_restored(self):
        loop = Mock()
        stop_event = Mock()
        registered = {}

        def register(signal_name, handler):
            registered[signal_name] = handler
            return f"previous-{signal_name}"

        with patch.object(bridge_main.signal, "signal", side_effect=register) as setter:
            previous = bridge_main.install_signal_handlers(loop, stop_event)
            for signal_name, handler in registered.items():
                handler(signal_name, None)
            bridge_main.restore_signal_handlers(previous)

        expected_signals = bridge_main.shutdown_signals()
        self.assertEqual(list(registered), expected_signals)
        self.assertEqual(
            loop.call_soon_threadsafe.call_args_list,
            [call(stop_event.set)] * len(expected_signals),
        )
        self.assertEqual(setter.call_count, len(expected_signals) * 2)
        for signal_name in expected_signals:
            setter.assert_any_call(signal_name, f"previous-{signal_name}")

    async def test_run_bridge_cleans_up_after_shutdown_notification(self):
        adapter = SimpleNamespace(
            start=AsyncMock(), stop=AsyncMock(), command=AsyncMock()
        )
        audio = SimpleNamespace(refresh=Mock(return_value=True), command=Mock(), stop=Mock())
        server = SimpleNamespace(
            serve_forever=Mock(), shutdown=Mock(), server_close=Mock()
        )
        server_thread = SimpleNamespace(start=Mock(), join=Mock())

        def install(loop, stop_event):
            loop.call_soon(stop_event.set)
            return {bridge_main.signal.SIGINT: "previous"}

        with patch.object(bridge_main, "GSMTCAdapter", return_value=adapter), patch.object(
            bridge_main, "CoreAudioController", return_value=audio
        ), patch.object(
            bridge_main.asyncio, "to_thread", new=AsyncMock(side_effect=lambda fn: fn())
        ), patch.object(
            bridge_main, "create_server", return_value=server
        ), patch.object(
            bridge_main.threading, "Thread", return_value=server_thread
        ), patch.object(
            bridge_main, "install_signal_handlers", side_effect=install
        ), patch.object(bridge_main, "restore_signal_handlers") as restore:
            await bridge_main.run_bridge()

        adapter.start.assert_awaited_once_with()
        server_thread.start.assert_called_once_with()
        server.shutdown.assert_called_once_with()
        server.server_close.assert_called_once_with()
        adapter.stop.assert_awaited_once_with()
        audio.stop.assert_called_once_with()
        server_thread.join.assert_called_once_with(timeout=2)
        restore.assert_called_once_with({bridge_main.signal.SIGINT: "previous"})


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.cache = MediaStateCache()
        self.cache.update({"available": True, "title": "Local Track"})
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self.loop.run_forever)
        self.loop_thread.start()
        self.commands = []

        async def commander(action):
            self.commands.append(action)
            return True

        def audio_commander(action):
            if action == "volume-up":
                return AudioCommandResult(
                    "ok", 1, 0,
                    {"audio_available": True, "volume_percent": 55,
                     "is_muted": False, "audio_session_count": 1,
                     "audio_mixed": False},
                )
            if action == "volume-down":
                return AudioCommandResult(
                    "no_audio", 0, 0,
                    {"audio_available": False, "volume_percent": None,
                     "is_muted": False, "audio_session_count": 0,
                     "audio_mixed": False},
                )
            return AudioCommandResult(
                "partial_failure", 1, 1,
                {"audio_available": True, "volume_percent": 55,
                 "is_muted": False, "audio_session_count": 1,
                 "audio_mixed": False},
            )

        self.server = create_server(
            self.cache, commander, self.loop, port=0,
            audio_commander=audio_commander,
        )
        self.server_thread = threading.Thread(target=self.server.serve_forever)
        self.server_thread.start()
        self.base_url = f"http://{BRIDGE_HOST}:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.loop_thread.join()
        self.loop.close()

    def test_local_only_configuration_and_explicit_routes(self):
        with self.assertRaises(ValueError):
            create_server(self.cache, None, self.loop, host="0.0.0.0", port=0)

        with urlopen(f"{self.base_url}/state", timeout=2) as response:
            payload = json.load(response)
        self.assertEqual(payload["title"], "Local Track")
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)

        request = Request(f"{self.base_url}/command/next", data=b"{}", method="POST")
        with urlopen(request, timeout=2) as response:
            self.assertEqual(json.load(response), {"ok": True})
        self.assertEqual(self.commands, ["next"])

        for path, status, expected in (
            ("volume-up", 200, "ok"),
            ("volume-down", 409, "no_audio"),
            ("mute-toggle", 503, "partial_failure"),
        ):
            request = Request(
                f"{self.base_url}/command/{path}", data=b"{}", method="POST"
            )
            if status == 200:
                with urlopen(request, timeout=2) as response:
                    payload = json.load(response)
            else:
                with self.assertRaises(HTTPError) as error:
                    urlopen(request, timeout=2)
                self.assertEqual(error.exception.code, status)
                payload = json.load(error.exception)
                error.exception.close()
            self.assertEqual(payload["status"], expected)

        request = Request(f"{self.base_url}/state", method="PUT")
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=2)
        self.assertEqual(error.exception.code, 405)
        self.assertEqual(json.load(error.exception), {"error": "method_not_allowed"})
        error.exception.close()

        with self.assertRaises(HTTPError) as error:
            urlopen(f"{self.base_url}/unknown", timeout=2)
        self.assertEqual(error.exception.code, 404)
        error.exception.close()

if __name__ == "__main__":
    unittest.main()
