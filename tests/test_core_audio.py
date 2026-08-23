import inspect
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import d200_bridge.core_audio as core_audio
from d200_bridge.core_audio import CoreAudioController
from d200_bridge.state import MediaStateCache


class FakeSession:
    def __init__(self, name="Spotify.exe", volume=0.5, muted=False):
        self.name = name
        self.volume = volume
        self.muted = muted
        self.volume_sets = []
        self.mute_sets = []
        self.fail_name = False
        self.fail_get_volume = False
        self.fail_set_volume = False
        self.fail_get_mute = False
        self.fail_set_mute = False

    @property
    def process_name(self):
        if self.fail_name:
            raise RuntimeError("process disappeared")
        return self.name

    def get_volume(self):
        if self.fail_get_volume:
            raise RuntimeError("session disappeared")
        return self.volume

    def set_volume(self, value):
        self.volume_sets.append(value)
        if self.fail_set_volume:
            raise RuntimeError("set failed")
        self.volume = value

    def get_mute(self):
        if self.fail_get_mute:
            raise RuntimeError("session disappeared")
        return self.muted

    def set_mute(self, value):
        self.mute_sets.append(value)
        if self.fail_set_mute:
            raise RuntimeError("set failed")
        self.muted = value


class FakeEnumerator:
    def __init__(self, batches):
        self.batches = list(batches)
        self.calls = 0

    @contextmanager
    def sessions(self):
        batch = self.batches[min(self.calls, len(self.batches) - 1)]
        self.calls += 1
        if isinstance(batch, Exception):
            raise batch
        yield batch


class ConcurrentReadSession(FakeSession):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.volume_reads = threading.Barrier(2)
        self.mute_reads = threading.Barrier(2)

    @staticmethod
    def _meet_concurrent_reader(barrier):
        try:
            barrier.wait(timeout=0.5)
        except threading.BrokenBarrierError:
            pass

    def get_volume(self):
        self._meet_concurrent_reader(self.volume_reads)
        return super().get_volume()

    def get_mute(self):
        self._meet_concurrent_reader(self.mute_reads)
        return super().get_mute()


class TransactionProbe:
    def __init__(self):
        self.active = False

    def __enter__(self):
        if self.active:
            raise AssertionError("transaction lock was re-entered")
        self.active = True

    def __exit__(self, exc_type, exc_value, traceback):
        self.active = False


class LockAwareEnumerator(FakeEnumerator):
    def __init__(self, batches, probe):
        super().__init__(batches)
        self.probe = probe

    @contextmanager
    def sessions(self):
        if not self.probe.active:
            raise AssertionError("sessions enumerated outside transaction lock")
        with super().sessions() as sessions:
            yield sessions


class LockAwareCache(MediaStateCache):
    def __init__(self, probe):
        super().__init__()
        self.probe = probe

    def update_audio(self, state):
        if not self.probe.active:
            raise AssertionError("audio state published outside transaction lock")
        return super().update_audio(state)


class CoreAudioTests(unittest.TestCase):
    def controller(self, batches):
        cache = MediaStateCache()
        enumerator = FakeEnumerator(batches)
        return CoreAudioController(cache, enumerator), cache, enumerator

    @staticmethod
    def run_concurrently(operation):
        start = threading.Barrier(3)

        def invoke():
            start.wait()
            return operation()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(invoke) for _ in range(2)]
            start.wait()
            return [future.result(timeout=5) for future in futures]

    def test_filters_spotify_case_insensitively_and_skips_other_or_invalid_processes(self):
        spotify = FakeSession("sPoTiFy.ExE", 0.4)
        other = FakeSession("browser.exe", 0.8)
        invalid = FakeSession()
        invalid.fail_name = True
        controller, cache, _ = self.controller([[other, invalid, spotify]])

        self.assertTrue(controller.refresh())
        state = cache.get()
        self.assertEqual(state.audio_session_count, 1)
        self.assertEqual(state.volume_percent, 40)

    def test_refresh_holds_transaction_lock_until_state_is_published(self):
        probe = TransactionProbe()
        cache = LockAwareCache(probe)
        enumerator = LockAwareEnumerator([[FakeSession(volume=0.4)]], probe)
        controller = CoreAudioController(cache, enumerator)
        controller._transaction_lock = probe

        self.assertTrue(controller.refresh())
        self.assertFalse(probe.active)
        self.assertEqual(cache.get().volume_percent, 40)

    def test_adjusts_each_session_once_by_five_percent_clamps_and_does_not_touch_mute(self):
        low = FakeSession(volume=0.02, muted=True)
        high = FakeSession(volume=0.98, muted=False)
        controller, _cache, enumerator = self.controller([[low, high]] * 3)

        down = controller.command("volume-down")
        self.assertEqual((low.volume, high.volume), (0.0, 0.9299999999999999))
        up = controller.command("volume-up")
        up_again = controller.command("volume-up")

        self.assertTrue(down.ok)
        self.assertTrue(up.ok)
        self.assertTrue(up_again.ok)
        self.assertEqual(high.volume, 1.0)
        self.assertEqual(len(low.volume_sets), 3)
        self.assertEqual(len(high.volume_sets), 3)
        self.assertEqual(low.mute_sets + high.mute_sets, [])
        self.assertEqual(enumerator.calls, 3)

    def test_mute_toggle_mutes_all_if_any_unmuted_then_unmutes_if_all_muted(self):
        first = FakeSession(muted=True)
        second = FakeSession(muted=False)
        controller, cache, _ = self.controller([[first, second]] * 2)

        muted = controller.command("mute-toggle")
        self.assertTrue(muted.ok)
        self.assertEqual((first.muted, second.muted), (True, True))
        self.assertTrue(cache.get().is_muted)

        unmuted = controller.command("mute-toggle")
        self.assertTrue(unmuted.ok)
        self.assertEqual((first.muted, second.muted), (False, False))

    def test_concurrent_volume_up_commands_apply_both_increments(self):
        session = ConcurrentReadSession(volume=0.5)
        controller, _cache, _ = self.controller([[session]] * 2)

        results = self.run_concurrently(lambda: controller.command("volume-up"))

        self.assertAlmostEqual(session.volume, 0.6)
        self.assertEqual([result.status for result in results], ["ok", "ok"])
        self.assertEqual([result.applied_count for result in results], [1, 1])
        self.assertEqual([result.failed_count for result in results], [0, 0])
        self.assertEqual(sorted(result.state["volume_percent"] for result in results), [55, 60])

    def test_concurrent_mute_toggles_restore_original_state(self):
        session = ConcurrentReadSession(muted=False)
        controller, _cache, _ = self.controller([[session]] * 2)

        results = self.run_concurrently(lambda: controller.command("mute-toggle"))

        self.assertFalse(session.muted)
        self.assertEqual([result.status for result in results], ["ok", "ok"])
        self.assertEqual([result.applied_count for result in results], [1, 1])
        self.assertEqual([result.failed_count for result in results], [0, 0])
        self.assertEqual(sorted(result.state["is_muted"] for result in results), [False, True])

    def test_reports_mixed_multi_session_state(self):
        controller, cache, _ = self.controller(
            [[FakeSession(volume=0.4, muted=True), FakeSession(volume=0.6, muted=False)]]
        )
        controller.refresh()
        state = cache.get()
        self.assertTrue(state.audio_available)
        self.assertTrue(state.audio_mixed)
        self.assertEqual(state.volume_percent, 50)
        self.assertFalse(state.is_muted)
        self.assertEqual(state.audio_session_count, 2)

    def test_absence_returns_no_audio_and_clears_only_audio_state(self):
        controller, cache, _ = self.controller([[]])
        cache.update({"available": True, "title": "Track"})
        result = controller.command("volume-up")
        self.assertEqual(result.status, "no_audio")
        self.assertTrue(cache.get().available)
        self.assertFalse(cache.get().audio_available)

    def test_disappearance_and_partial_failure_do_not_retry_applied_increment(self):
        applied = FakeSession(volume=0.5)
        failed = FakeSession(volume=0.5)
        failed.fail_set_volume = True
        controller, cache, _ = self.controller([[applied, failed]])

        result = controller.command("volume-up")
        self.assertEqual(result.status, "partial_failure")
        self.assertEqual(result.applied_count, 1)
        self.assertEqual(result.failed_count, 1)
        self.assertEqual(applied.volume_sets, [0.55])
        self.assertEqual(failed.volume_sets, [0.55])
        self.assertTrue(cache.get().audio_available)

    def test_total_enumeration_failure_returns_failed(self):
        controller, cache, _ = self.controller([[FakeSession()], RuntimeError("COM")])
        controller.refresh()
        result = controller.command("volume-down")
        self.assertEqual(result.status, "failed")
        self.assertFalse(cache.get().audio_available)

    def test_mute_read_failure_makes_no_changes(self):
        healthy = FakeSession(muted=False)
        vanished = FakeSession(muted=False)
        vanished.fail_get_mute = True
        controller, _cache, _ = self.controller([[healthy, vanished]])
        result = controller.command("mute-toggle")
        self.assertEqual(result.status, "failed")
        self.assertEqual(healthy.mute_sets, [])
        self.assertEqual(vanished.mute_sets, [])

    def test_implementation_never_references_endpoint_volume(self):
        self.assertNotIn("IAudiEndpointVolume", inspect.getsource(core_audio))


if __name__ == "__main__":
    unittest.main()
