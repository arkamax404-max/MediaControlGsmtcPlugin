import base64
import sys
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "com.arkamax404.mediacontrold200.ulanziPlugin" / "runtime" / "python"
sys.path.insert(0, str(RUNTIME))

from largeitem_renderer import (  # noqa: E402
    HEIGHT,
    WIDTH,
    LargeItemSettings,
    LargeItemView,
    render_largeitem,
    render_largeitem_data_uri,
)
from artwork_bundle import ArtworkBundle  # noqa: E402
from largeitem_action import (  # noqa: E402
    ACTION_UUID,
    LargeItemActionModel,
    normalize_largeitem_settings,
)
from now_playing_action import MediaSnapshot  # noqa: E402
from progress_state import ProgressState  # noqa: E402
from bridge_client import BridgeStateResult  # noqa: E402
from progress_action import ProgressActionModel  # noqa: E402
from progress_scheduler import ProgressScheduler  # noqa: E402


PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"


def view(**overrides):
    values = {
        "status": "ready",
        "title": "Track",
        "artist": "Artist",
        "is_playing": True,
        "artwork_data_uri": PNG,
        "progress_ratio": 0.25,
        "elapsed_text": "0:30",
        "remaining_text": "-1:30",
        "settings": LargeItemSettings(),
    }
    values.update(overrides)
    return LargeItemView(**values)


class LargeItemRendererTests(unittest.TestCase):
    def test_exact_canvas_valid_xml_and_deterministic_data_uri(self):
        svg = render_largeitem(view())
        root = ET.fromstring(svg)
        self.assertEqual((WIDTH, HEIGHT), (458, 196))
        self.assertEqual(root.attrib, {
            "width": "458", "height": "196", "viewBox": "0 0 458 196",
        })
        first = render_largeitem_data_uri(view())
        self.assertEqual(first, render_largeitem_data_uri(view()))
        self.assertEqual(base64.b64decode(first.split(",", 1)[1]).decode("utf-8"), svg)

    def test_metadata_is_xml_safe_and_unicode_survives_base64(self):
        unsafe = '😀 <script a="1">&\' canción</script>'
        svg = render_largeitem(view(title=unsafe, artist="艺术家 e\u0301"))
        ET.fromstring(svg)
        self.assertNotIn("<script", svg)
        self.assertIn("&lt;script", svg)
        self.assertIn("😀", svg)
        self.assertIn("艺术家 é", svg)

    def test_long_unbroken_metadata_is_bounded_and_ellipsized(self):
        root = ET.fromstring(render_largeitem(view(title="X" * 500, artist="Y" * 500)))
        texts = [node.text or "" for node in root.findall("{*}text")]
        title = [text for text in texts if text.startswith("X")]
        artist = [text for text in texts if text.startswith("Y")]
        self.assertEqual(len(title), 2)
        self.assertTrue(title[-1].endswith("…"))
        self.assertEqual(len(artist), 1)
        self.assertTrue(artist[0].endswith("…"))

    def test_invalid_artwork_colors_and_progress_fail_to_safe_values(self):
        settings = LargeItemSettings(background_color='red"/><script>', accent_color="#xyzxyz")
        svg = render_largeitem(view(
            artwork_data_uri='data:image/svg+xml,<script/>',
            progress_ratio=float("nan"),
            settings=settings,
        ))
        ET.fromstring(svg)
        self.assertNotIn("script", svg)
        self.assertIn('fill="#0B0D10"', svg)
        self.assertIn('width="0" height="8"', svg)
        self.assertIn("M70 70", svg)

    def test_progress_clamps_and_optional_labels_obey_settings(self):
        settings = LargeItemSettings(show_elapsed=True, show_remaining=False)
        svg = render_largeitem(view(progress_ratio=2, settings=settings))
        self.assertIn('width="246" height="8"', svg)
        self.assertIn("0:30", svg)
        self.assertNotIn("-1:30", svg)
        hidden = render_largeitem(view(settings=LargeItemSettings(show_progress=False)))
        self.assertNotIn('y="170"', hidden)

    def test_status_and_missing_artwork_have_deterministic_fallbacks(self):
        offline = render_largeitem(view(status="offline", artwork_data_uri=None))
        self.assertIn("Media service offline", offline)
        self.assertNotIn("Track", offline)
        self.assertNotIn('<circle cx="430"', offline)
        missing = render_largeitem(view(artwork_data_uri=None))
        self.assertIn("M70 70", missing)

    def test_title_reserves_space_for_playback_status(self):
        root = ET.fromstring(render_largeitem(view(title="W" * 100)))
        title_nodes = [node for node in root.findall("{*}text")
                       if node.attrib.get("font-size") == "25"]
        title = [node.text for node in title_nodes]
        self.assertEqual(len(title), 2)
        self.assertLessEqual(len(title[0]), 14)
        self.assertEqual(title_nodes[0].attrib.get("textLength"), "204")

    def test_artist_uses_larger_single_line_type(self):
        root = ET.fromstring(render_largeitem(view(artist="Visible Artist")))
        artists = [node for node in root.findall("{*}text")
                   if node.text == "Visible Artist"]
        self.assertEqual(len(artists), 1)
        self.assertEqual(artists[0].attrib.get("font-size"), "22")


class LargeItemActionModelTests(unittest.TestCase):
    CONTEXT = f"{ACTION_UUID}___3_2___instance"
    NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)

    @staticmethod
    def media(**overrides):
        values = dict(online=True, available=True, is_playing=True, title="Track",
                      artist="Artist", artwork_id="a" * 64, status="ready")
        values.update(overrides)
        return MediaSnapshot(**values)

    @classmethod
    def progress(cls, **overrides):
        values = dict(online=True, available=True, timeline_available=True,
                      is_playing=True, position_seconds=30, duration_seconds=120,
                      playback_rate=1, position_updated_at=cls.NOW,
                      status="ready", label="")
        values.update(overrides)
        return ProgressState(**values)

    def test_settings_normalize_to_canonical_bounded_contract(self):
        settings = normalize_largeitem_settings({
            "showArtwork": False, "pausedArtwork": "invalid", "showProgress": "yes",
            "backgroundColor": "#abcdef", "primaryColor": "red", "fit": "cover",
        })
        self.assertFalse(settings.show_artwork)
        self.assertEqual(settings.paused_artwork, "grayscale")
        self.assertTrue(settings.show_progress)
        self.assertEqual(settings.background_color, "#ABCDEF")
        self.assertEqual(settings.primary_color, "#FFFFFF")
        self.assertEqual(settings.fit, "cover")

    def test_accepts_only_exact_action_and_large_context_key(self):
        model = LargeItemActionModel()
        self.assertEqual(model.add({"uuid": ACTION_UUID, "context": "ordinary___1_1___id"}), ())
        self.assertEqual(model.add({"uuid": ACTION_UUID + "-bad", "context": self.CONTEXT}), ())
        request = model.add({"uuid": ACTION_UUID, "context": self.CONTEXT})[0]
        self.assertEqual(request.context, self.CONTEXT)
        self.assertTrue(model.context(self.CONTEXT).active)

    def test_generation_lifecycle_rejects_stale_intents_and_deduplicates_success(self):
        model = LargeItemActionModel()
        first = model.add({"uuid": ACTION_UUID, "context": self.CONTEXT})[0]
        bundle = ArtworkBundle("a" * 64, PNG, PNG, ("1", "2", "3", "4"))
        intent = model.render(first, self.media(), self.progress(), bundle, lambda: self.NOW)
        self.assertTrue(model.reserve_send(intent))
        self.assertTrue(model.acknowledge(intent, True))
        self.assertIsNone(model.render(first, self.media(), self.progress(), bundle, lambda: self.NOW))
        model.set_active({"context": self.CONTEXT, "active": False})
        self.assertFalse(model.reserve_send(intent))
        current = model.set_active({"context": self.CONTEXT, "active": True})[0]
        self.assertGreater(current.version, first.version)
        self.assertIsNotNone(model.render(current, self.media(), self.progress(), bundle, lambda: self.NOW))
        model.clear({"param": [{"context": self.CONTEXT}]})
        self.assertEqual(model.requests(), ())

    def test_scheduler_refreshes_requests_when_mutated_during_blocked_poll(self):
        second = f"{ACTION_UUID}___3_2___second"
        entered = threading.Event()
        release = threading.Event()

        class Client:
            def get_state(inner, cancelled=None):
                entered.set()
                release.wait(1)
                return BridgeStateResult("ok", {
                    "available": True, "is_playing": False, "title": "Paused",
                    "artist": "Artist", "artwork_id": None,
                    "timeline_available": False, "position_seconds": 0,
                    "duration_seconds": 0, "playback_rate": 1,
                    "position_updated_at": "", "updated_at": self.NOW.isoformat(),
                }, 200)

        class Api:
            def __init__(inner):
                inner.displays = []
            def setSettings(inner, settings, context):
                return True
            def setBaseDataIcon(inner, context, data, text):
                inner.displays.append((context, data))
                return True

        api = Api()
        scheduler = ProgressScheduler(
            api, Client(), ProgressActionModel(), clock=lambda: self.NOW,
            poll_interval=10,
        )
        scheduler.handle_add({"uuid": ACTION_UUID, "context": self.CONTEXT})
        scheduler.start()
        self.assertTrue(entered.wait(1))
        self.assertTrue(scheduler.handle_add({"uuid": ACTION_UUID, "context": second}))
        self.assertTrue(scheduler.handle_property_settings({
            "context": self.CONTEXT, "param": {"accentColor": "#ABCDEF"},
        }))
        release.set()
        deadline = time.monotonic() + 1
        while {item[0] for item in api.displays} != {self.CONTEXT, second} \
                and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertEqual({item[0] for item in api.displays}, {self.CONTEXT, second})
        first_svg = base64.b64decode(
            next(data for context, data in api.displays if context == self.CONTEXT).split(",", 1)[1]
        ).decode()
        self.assertIn("#ABCDEF", first_svg)
        self.assertTrue(scheduler.stop(.5))

    def test_paused_artwork_progress_and_context_local_settings(self):
        model = LargeItemActionModel()
        first = model.add({"uuid": ACTION_UUID, "context": self.CONTEXT,
                           "param": {"pausedArtwork": "grayscale"}})[0]
        second_context = f"{ACTION_UUID}___3_2___second"
        model.add({"uuid": ACTION_UUID, "context": second_context,
                   "param": {"pausedArtwork": "color"}})
        bundle = ArtworkBundle("a" * 64, PNG + "A", PNG + "B", ("1", "2", "3", "4"))
        paused = self.media(is_playing=False)
        first_svg = base64.b64decode(model.render(
            first, paused, self.progress(is_playing=False), bundle, lambda: self.NOW
        ).data_uri.split(",", 1)[1]).decode()
        self.assertIn(PNG + "B", first_svg)
        self.assertEqual(model.context(second_context).settings.paused_artwork, "color")

    def test_persistence_is_bounded_and_failed_display_remains_retryable(self):
        model = LargeItemActionModel()
        request = model.add({"uuid": ACTION_UUID, "context": self.CONTEXT, "param": {}})[0]
        persistence = model.persistence_requests()[0]
        self.assertTrue(model.reserve_persistence_send(persistence))
        self.assertTrue(model.acknowledge_persistence(persistence, False, 3))
        self.assertTrue(model.acknowledge_persistence(persistence, False, 3))
        self.assertTrue(model.acknowledge_persistence(persistence, False, 3))
        self.assertEqual(model.persistence_requests(), ())
        intent = model.render(request, self.media(), self.progress(), None, lambda: self.NOW)
        self.assertTrue(model.acknowledge(intent, False))
        self.assertTrue(model.reserve_send(intent))


if __name__ == "__main__":
    unittest.main()
