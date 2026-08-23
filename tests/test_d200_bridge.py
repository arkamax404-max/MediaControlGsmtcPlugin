import asyncio
import base64
import io
import json
import hashlib
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
from d200_bridge.artwork import (
    ARTWORK_CACHE_SIZE,
    MAX_BUNDLE_BYTES,
    MAX_DECODED_PIXELS,
    MOSAIC_SIZE,
    MOSAIC_TILE_SIZE,
    ArtworkProcessor,
    ArtworkVariants,
    validate_png_data_uri,
)
from d200_bridge.core_audio import AudioCommandResult
from d200_bridge.gsmtc import (
    GSMTCAdapter,
    normalize_timeline_properties,
    read_thumbnail,
    select_session,
    timespan_seconds,
)
from d200_bridge.server import BRIDGE_HOST, MAX_STATE_RESPONSE_BYTES, create_server
from d200_bridge.state import (
    MediaStateCache,
    normalize_state,
    normalize_timeline,
)

try:
    from PIL import Image, ImageOps, features
except ImportError:
    Image = None
    ImageOps = None
    features = None


PILLOW_AVAILABLE = Image is not None
SIGNATURE_ONLY_URI = "data:image/png;base64," + base64.b64encode(
    b"\x89PNG\r\n\x1a\n"
).decode("ascii")
REAL_PNG_URI = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg=="


def valid_variants():
    return ArtworkVariants(REAL_PNG_URI, REAL_PNG_URI, (REAL_PNG_URI,) * 4)


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
        self.assertIsNone(state.artwork_id)
        self.assertNotIn("thumbnail", state.public())

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

    def test_state_publishes_only_strict_artwork_id_and_never_rasters(self):
        artwork_id = "a" * 64
        state = normalize_state({
            "available": True,
            "artwork_id": artwork_id,
            "thumbnail": REAL_PNG_URI,
            "thumbnail_grayscale": REAL_PNG_URI,
            "artwork_tiles": [REAL_PNG_URI] * 4,
        })
        self.assertEqual(state.artwork_id, artwork_id)
        self.assertEqual(state.public()["artwork_id"], artwork_id)
        self.assertNotIn("thumbnail", state.public())
        self.assertNotIn("thumbnail_grayscale", state.public())
        self.assertNotIn("artwork_tiles", state.public())
        for invalid in ("A" * 64, "a" * 63, "../" + "a" * 64, SIGNATURE_ONLY_URI):
            self.assertIsNone(normalize_state({"artwork_id": invalid}).artwork_id)


class ArtworkCacheTests(unittest.TestCase):
    def test_content_hash_cache_avoids_reprocessing_and_evicts_lru(self):
        processor = ArtworkProcessor(cache_size=2)
        with patch.object(
            processor,
            "_decode_encode",
            side_effect=lambda _data: valid_variants(),
        ) as decode:
            one = processor.process(b"one")
            self.assertIs(processor.process(b"one"), one)
            processor.process(b"two")
            self.assertIs(processor.process(b"one"), one)
            processor.process(b"three")
            processor.process(b"two")
        self.assertEqual(decode.call_count, 4)
        self.assertEqual(processor.cached_entries, 2)

    def test_transient_failure_retries_then_caches_success(self):
        processor = ArtworkProcessor()
        success = valid_variants()
        with patch.object(
            processor, "_decode_encode", side_effect=[None, success]
        ) as decode:
            self.assertIsNone(processor.process(b"same-content"))
            self.assertEqual(processor.cached_entries, 0)
            result = processor.process(b"same-content")
            self.assertIsNotNone(result)
            self.assertIs(processor.process(b"same-content"), result)
        self.assertEqual(decode.call_count, 2)
        self.assertEqual(processor.cached_entries, 1)

    def test_default_cache_has_eight_entry_deterministic_lru_capacity(self):
        self.assertEqual(ARTWORK_CACHE_SIZE, 8)
        processor = ArtworkProcessor()

        def variants(data):
            return valid_variants()

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
        success = valid_variants()
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
                first_result = first.result(timeout=2)
                self.assertIs(first_result, second.result(timeout=2))
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(processor.cached_entries, 1)

    def test_endpoint_lookup_is_strict_and_does_not_promote_lru(self):
        processor = ArtworkProcessor(cache_size=2)
        with patch.object(processor, "_decode_encode", return_value=valid_variants()):
            first = processor.process(b"first")
            second = processor.process(b"second")
            self.assertIs(processor.get_cached(first.artwork_id), first)
            processor.process(b"third")
        self.assertIsNone(processor.get_cached(first.artwork_id))
        self.assertIsNotNone(processor.get_cached(second.artwork_id))
        for invalid in ("A" * 64, "a" * 63, "../" + "a" * 64):
            self.assertIsNone(processor.get_cached(invalid))


@unittest.skipUnless(PILLOW_AVAILABLE, "Pillow is not installed")
class ArtworkProcessorTests(unittest.TestCase):
    def asymmetric_png(self):
        image = Image.new("RGBA", (3, 2))
        image.putdata([
            (255, 0, 0, 10), (0, 255, 0, 40), (0, 0, 255, 70),
            (250, 120, 10, 100), (20, 40, 80, 160), (240, 30, 180, 220),
        ])
        return image_bytes(image)

    def recomposed_mosaic(self, result):
        tiles = [data_uri_image(tile).convert("RGBA") for tile in result.mosaic_tiles]
        mosaic = Image.new("RGBA", (MOSAIC_SIZE, MOSAIC_SIZE))
        for tile, position in zip(tiles, ((0, 0), (196, 0), (0, 196), (196, 196))):
            mosaic.paste(tile, position)
        return mosaic, tiles

    def marked_rectangle(self, width, height):
        image = Image.new("RGBA", (width, height), (20, 30, 40, 255))
        marker = 20
        colors = [
            (255, 0, 0, 255),
            (0, 255, 0, 255),
            (0, 0, 255, 255),
            (255, 255, 0, 255),
            (0, 255, 255, 255),
            (255, 0, 255, 255),
            (255, 128, 0, 255),
            (180, 80, 255, 255),
        ]
        boxes = [
            (0, 0, marker, marker),
            (width - marker, 0, width, marker),
            (0, height - marker, marker, height),
            (width - marker, height - marker, width, height),
            (width // 2 - marker // 2, 0, width // 2 + marker // 2, marker),
            (width // 2 - marker // 2, height - marker, width // 2 + marker // 2, height),
            (0, height // 2 - marker // 2, marker, height // 2 + marker // 2),
            (width - marker, height // 2 - marker // 2, width, height // 2 + marker // 2),
        ]
        for box, color in zip(boxes, colors):
            image.paste(color, box)
        return image, colors

    def test_outputs_matching_png_frames_with_alpha_and_pixel_positions_preserved(self):
        source_bytes = self.asymmetric_png()
        result = ArtworkProcessor().process(source_bytes)
        color = data_uri_image(result.color).convert("RGBA")
        grayscale = data_uri_image(result.grayscale).convert("RGBA")

        self.assertTrue(result.color.startswith("data:image/png;base64,"))
        self.assertTrue(result.grayscale.startswith("data:image/png;base64,"))
        self.assertEqual(result.artwork_id, hashlib.sha256(source_bytes).hexdigest())
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

    def test_wide_mosaic_contains_every_edge_with_exact_transparent_letterbox(self):
        source, markers = self.marked_rectangle(400, 200)
        data = image_bytes(source)
        processor = ArtworkProcessor()

        with patch.object(
            artwork_module.ImageOps,
            "exif_transpose",
            wraps=artwork_module.ImageOps.exif_transpose,
        ) as orient, patch.object(
            artwork_module.Image,
            "open",
            wraps=artwork_module.Image.open,
        ) as open_image:
            result = processor.process(data)
            self.assertIs(processor.process(data), result)

        self.assertEqual(orient.call_count, 1, "one oriented source decode per cache miss")
        self.assertEqual(open_image.call_count, 1, "one source open per cache miss")
        self.assertEqual(len(result.mosaic_tiles), 4)
        recomposed, tiles = self.recomposed_mosaic(result)
        self.assertTrue(all(tile.size == (MOSAIC_TILE_SIZE, MOSAIC_TILE_SIZE) for tile in tiles))
        self.assertTrue(all(tile.format == "PNG" for tile in map(data_uri_image, result.mosaic_tiles)))
        expected = Image.new("RGBA", (392, 392), (0, 0, 0, 0))
        expected.paste(source.resize((392, 196), Image.Resampling.LANCZOS), (0, 98))
        self.assertEqual(image_pixels(recomposed), image_pixels(expected))
        self.assertEqual(set(image_pixels(recomposed.crop((0, 0, 392, 98)))), {(0, 0, 0, 0)})
        self.assertEqual(set(image_pixels(recomposed.crop((0, 294, 392, 392)))), {(0, 0, 0, 0)})
        content_pixels = image_pixels(recomposed.crop((0, 98, 392, 294)))
        for marker in markers:
            self.assertIn(marker, content_pixels)
        self.assertEqual(
            [recomposed.getpixel(point) for point in (
                (8, 106), (384, 106), (8, 286), (384, 286),
                (196, 106), (196, 286), (8, 196), (384, 196),
            )],
            markers,
        )

    def test_tall_mosaic_contains_every_edge_with_exact_transparent_pillarbox(self):
        source, markers = self.marked_rectangle(200, 400)
        result = ArtworkProcessor().process(image_bytes(source))
        recomposed, _tiles = self.recomposed_mosaic(result)
        expected = Image.new("RGBA", (392, 392), (0, 0, 0, 0))
        expected.paste(source.resize((196, 392), Image.Resampling.LANCZOS), (98, 0))
        self.assertEqual(image_pixels(recomposed), image_pixels(expected))
        self.assertEqual(set(image_pixels(recomposed.crop((0, 0, 98, 392)))), {(0, 0, 0, 0)})
        self.assertEqual(set(image_pixels(recomposed.crop((294, 0, 392, 392)))), {(0, 0, 0, 0)})
        content_pixels = image_pixels(recomposed.crop((98, 0, 294, 392)))
        for marker in markers:
            self.assertIn(marker, content_pixels)
        self.assertEqual(
            [recomposed.getpixel(point) for point in (
                (106, 8), (286, 8), (106, 384), (286, 384),
                (196, 8), (196, 384), (106, 196), (286, 196),
            )],
            markers,
        )

    def test_square_mosaic_fills_canvas_without_padding_or_geometry_change(self):
        source = Image.new("RGBA", (392, 392))
        source.paste((255, 0, 0, 255), (0, 0, 196, 196))
        source.paste((0, 255, 0, 192), (196, 0, 392, 196))
        source.paste((0, 0, 255, 128), (0, 196, 196, 392))
        source.paste((255, 255, 0, 64), (196, 196, 392, 392))
        result = ArtworkProcessor().process(image_bytes(source))
        recomposed, tiles = self.recomposed_mosaic(result)
        self.assertEqual(image_pixels(recomposed), image_pixels(source))
        self.assertEqual(
            [tile.getpixel((20, 20)) for tile in tiles],
            [(255, 0, 0, 255), (0, 255, 0, 192),
             (0, 0, 255, 128), (255, 255, 0, 64)],
        )

    def test_exif_oriented_rectangle_remains_complete_and_correctly_oriented(self):
        source = Image.new("RGBA", (196, 392))
        source.paste((255, 0, 0, 255), (0, 0, 98, 196))
        source.paste((0, 255, 0, 192), (98, 0, 196, 196))
        source.paste((0, 0, 255, 128), (0, 196, 98, 392))
        source.paste((255, 255, 0, 64), (98, 196, 196, 392))
        exif = source.getexif()
        exif[274] = 6
        encoded = image_bytes(source, "PNG", exif=exif)
        oriented = ImageOps.exif_transpose(Image.open(io.BytesIO(encoded))).convert("RGBA")
        result = ArtworkProcessor().process(encoded)
        color = data_uri_image(result.color).convert("RGBA")
        self.assertEqual(image_pixels(color), image_pixels(oriented))

        expected_mosaic = Image.new("RGBA", (392, 392), (0, 0, 0, 0))
        expected_mosaic.paste(oriented, (0, 98))
        recomposed, _tiles = self.recomposed_mosaic(result)
        self.assertEqual(image_pixels(recomposed), image_pixels(expected_mosaic))
        self.assertEqual(
            [recomposed.getpixel(point) for point in (
                (20, 118), (372, 118), (20, 274), (372, 274),
            )],
            [(0, 0, 255, 128), (255, 0, 0, 255),
             (255, 255, 0, 64), (0, 255, 0, 192)],
        )
        self.assertEqual(set(image_pixels(recomposed.crop((0, 0, 392, 98)))), {(0, 0, 0, 0)})
        self.assertEqual(set(image_pixels(recomposed.crop((0, 294, 392, 392)))), {(0, 0, 0, 0)})

    def test_animated_gif_uses_first_frame(self):
        first = Image.new("RGBA", (2, 1))
        first.putdata([(255, 0, 0, 255), (0, 255, 0, 255)])
        second = Image.new("RGBA", (2, 1), (0, 0, 255, 255))
        source = image_bytes(first, "GIF", save_all=True, append_images=[second], loop=0)
        result = ArtworkProcessor().process(source)
        color = data_uri_image(result.color).convert("RGBA")
        self.assertEqual(image_pixels(color), image_pixels(first))
        self.assertEqual(len(result.mosaic_tiles), 4)

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
        self.assertEqual(len(result.mosaic_tiles), 4)

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
        aggregate_limited = ArtworkProcessor(max_aggregate_encoded_bytes=1)
        self.assertIsNone(aggregate_limited.process(valid))
        self.assertEqual(aggregate_limited.cached_entries, 0)
        with patch.object(artwork_module.Image, "MAX_IMAGE_PIXELS", 1):
            self.assertIsNone(ArtworkProcessor().process(valid))

    def test_strict_png_profile_rejects_signature_probes_structure_and_crc_failures(self):
        rgba = REAL_PNG_URI
        self.assertEqual(validate_png_data_uri(rgba), rgba)
        self.assertIsNone(validate_png_data_uri(SIGNATURE_ONLY_URI))

        encoded = base64.b64decode(rgba.split(",", 1)[1])
        invalid_bytes = [
            encoded[:-1],
            encoded + b"trailing",
            encoded[:20] + bytes((encoded[20] ^ 1,)) + encoded[21:],
            encoded[:33] + encoded[8:33] + encoded[33:],
        ]
        rgb = "data:image/png;base64," + base64.b64encode(
            image_bytes(Image.new("RGB", (1, 1), (255, 0, 0)))
        ).decode("ascii")
        for data in invalid_bytes:
            uri = "data:image/png;base64," + base64.b64encode(data).decode("ascii")
            self.assertIsNone(validate_png_data_uri(uri))
        self.assertIsNone(validate_png_data_uri(rgb))
        self.assertEqual(MAX_DECODED_PIXELS, 4_194_304)
        self.assertIsNone(ArtworkProcessor().process(
            image_bytes(Image.new("RGBA", (4097, 1), (0, 0, 0, 0)))
        ))
        processor = ArtworkProcessor()
        invalid = ArtworkVariants(
            SIGNATURE_ONLY_URI,
            REAL_PNG_URI,
            (REAL_PNG_URI,) * 4,
        )
        with patch.object(processor, "_decode_encode", return_value=invalid):
            self.assertIsNone(processor.process(b"invalid-output"))
        self.assertEqual(processor.cached_entries, 0)


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
        variants = valid_variants()
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
            thumbnail_reader=AsyncMock(return_value=ArtworkVariants(
                REAL_PNG_URI,
                REAL_PNG_URI,
                (REAL_PNG_URI,) * 4,
                artwork_id="a" * 64,
            )),
        )
        await adapter.start()
        self.assertEqual(first.media_reads, 1)
        self.assertEqual(cache.get().artwork_id, "a" * 64)
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
        self.artwork_processor = ArtworkProcessor()
        self.variants = self.artwork_processor.process(
            image_bytes(Image.new("RGBA", (2, 2), (20, 40, 60, 128)))
        )
        self.cache = MediaStateCache()
        self.cache.update({
            "available": True,
            "title": "Local Track",
            "artwork_id": self.variants.artwork_id,
        })
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
            artwork_lookup=self.artwork_processor.get_cached,
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
            state_body = response.read()
            payload = json.loads(state_body)
        self.assertEqual(payload["title"], "Local Track")
        self.assertEqual(payload["artwork_id"], self.variants.artwork_id)
        self.assertLessEqual(len(state_body), MAX_STATE_RESPONSE_BYTES)
        self.assertNotIn(b"data:image", state_body)
        self.assertNotIn(b"thumbnail", state_body)
        self.assertNotIn(b"tiles", state_body)
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

    def test_artwork_bundle_is_strict_immutable_conditional_and_read_only(self):
        artwork_id = self.variants.artwork_id
        url = f"{self.base_url}/artwork/{artwork_id}"
        with urlopen(url, timeout=2) as response:
            body = response.read()
            payload = json.loads(body)
            self.assertEqual(response.headers["ETag"], f'"{artwork_id}"')
            self.assertEqual(
                response.headers["Cache-Control"],
                "private, max-age=31536000, immutable",
            )
        self.assertLessEqual(len(body), MAX_BUNDLE_BYTES)
        self.assertEqual(payload, self.variants.public())
        self.assertEqual(payload["id"], artwork_id)
        self.assertEqual(len(payload["tiles"]), 4)
        self.assertTrue(all(validate_png_data_uri(value) for value in (
            payload["color"], payload["grayscale"], *payload["tiles"]
        )))

        conditional = Request(url, headers={"If-None-Match": f'"{artwork_id}"'})
        with self.assertRaises(HTTPError) as error:
            urlopen(conditional, timeout=2)
        self.assertEqual(error.exception.code, 304)
        self.assertEqual(error.exception.read(), b"")
        self.assertEqual(error.exception.headers["ETag"], f'"{artwork_id}"')
        error.exception.close()

        for invalid in ("f" * 64, "A" * 64, "a" * 63, f"{artwork_id}?other=1"):
            with self.assertRaises(HTTPError) as error:
                urlopen(f"{self.base_url}/artwork/{invalid}", timeout=2)
            self.assertEqual(error.exception.code, 404)
            error.exception.close()

        write = Request(url, data=b"{}", method="POST")
        with self.assertRaises(HTTPError) as error:
            urlopen(write, timeout=2)
        self.assertEqual(error.exception.code, 404)
        error.exception.close()

    def test_evicted_artwork_id_returns_404_without_exposing_other_ids(self):
        evicted_id = self.variants.artwork_id
        newest = None
        for index in range(8):
            newest = self.artwork_processor.process(
                image_bytes(Image.new("RGBA", (2, 2), (index + 1, 0, 0, 255)))
            )
        with self.assertRaises(HTTPError) as error:
            urlopen(f"{self.base_url}/artwork/{evicted_id}", timeout=2)
        self.assertEqual(error.exception.code, 404)
        self.assertEqual(json.load(error.exception), {"error": "not_found"})
        error.exception.close()
        with urlopen(f"{self.base_url}/artwork/{newest.artwork_id}", timeout=2) as response:
            self.assertEqual(json.load(response)["id"], newest.artwork_id)

if __name__ == "__main__":
    unittest.main()
