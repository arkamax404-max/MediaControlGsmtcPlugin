import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).parents[1]
RUNTIME = ROOT / "com.arkamax404.mediacontrold200.ulanziPlugin" / "runtime" / "python"
sys.path.insert(0, str(RUNTIME))

from bridge_client import BridgeHealthResult  # noqa: E402
from companion_supervisor import (  # noqa: E402
    CompanionStartResult,
    CompanionSupervisor,
    PREFER_EMBEDDED_MARKER,
    prefer_embedded_from_environment,
)


INSTANCE_ID = "123e4567-e89b-42d3-a456-426614174000"


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def sleep(self, amount):
        self.value += amount


class CompanionSupervisorTests(unittest.TestCase):
    @staticmethod
    def health(status="compatible", version="1.4.0"):
        return BridgeHealthResult(status, INSTANCE_ID if status == "compatible" else None,
                                  version if status == "compatible" else None)

    def test_reuses_compatible_external_companion_by_default(self):
        client = Mock(probe_health=Mock(return_value=self.health()))
        spawn = Mock()
        supervisor = CompanionSupervisor(client_factory=lambda: client, popen=spawn,
                                         prefer_embedded=False)

        self.assertEqual(supervisor.ensure_ready(), CompanionStartResult("ready", False))
        self.assertTrue(supervisor.shutdown())
        spawn.assert_not_called()
        client.stop_owned.assert_not_called()

    def test_prefer_embedded_stops_external_then_owns_bundled_process(self):
        clock = Clock()
        child = Mock(stderr=io.BytesIO())
        child.poll.side_effect = [None, None, 0]
        client = Mock()
        client.probe_health.side_effect = [
            self.health(), self.health("unavailable"), self.health(),
        ]
        client.stop_owned.return_value = True
        spawn = Mock(return_value=child)
        executable = str(Path("C:/plugin/runtime/companion/GSMTCD200Companion.exe"))
        supervisor = CompanionSupervisor(
            client_factory=lambda: client, popen=spawn, clock=clock, sleep=clock.sleep,
            executable=executable, prefer_embedded=True,
        )

        self.assertEqual(supervisor.ensure_ready(), CompanionStartResult("ready", True))
        client.stop_owned.assert_called_once_with(INSTANCE_ID)
        self.assertTrue(supervisor.shutdown())
        self.assertEqual(client.stop_owned.call_count, 2)
        spawn.assert_called_once_with(
            [str(Path(executable).resolve()), "--parent-pid", str(os.getpid())], shell=False,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def test_older_external_is_replaced_without_test_marker(self):
        clock = Clock()
        child = Mock(stderr=io.BytesIO())
        child.poll.side_effect = [None, None, 0]
        client = Mock()
        client.probe_health.side_effect = [
            self.health(version="1.3.0"), self.health("unavailable"), self.health(),
        ]
        client.stop_owned.return_value = True
        supervisor = CompanionSupervisor(
            client_factory=lambda: client, popen=Mock(return_value=child),
            clock=clock, sleep=clock.sleep, prefer_embedded=False,
        )
        self.assertEqual(supervisor.ensure_ready(), CompanionStartResult("ready", True))
        client.stop_owned.assert_called_once_with(INSTANCE_ID)
        self.assertTrue(supervisor.shutdown())

    def test_refuses_spawn_when_external_cannot_stop(self):
        client = Mock(probe_health=Mock(return_value=self.health()),
                      stop_owned=Mock(return_value=False))
        spawn = Mock()
        result = CompanionSupervisor(client_factory=lambda: client, popen=spawn,
                                     prefer_embedded=True).ensure_ready()
        self.assertEqual(result.stage, "external-stop")
        spawn.assert_not_called()

    def test_timeout_and_exit_never_kill_child(self):
        clock = Clock()
        child = Mock(stderr=io.BytesIO(), poll=Mock(return_value=None))
        client = Mock(probe_health=Mock(return_value=self.health("unavailable")))
        supervisor = CompanionSupervisor(
            client_factory=lambda: client, popen=Mock(return_value=child),
            clock=clock, sleep=clock.sleep, prefer_embedded=True,
            readiness_timeout=.2, readiness_poll=.1,
        )
        self.assertEqual(supervisor.ensure_ready().stage, "health-timeout")
        self.assertTrue(supervisor.shutdown())
        child.kill.assert_not_called()
        child.terminate.assert_not_called()

    def test_prefer_embedded_marker_is_exact_regular_file(self):
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory)
            root = local / "GSMTCD200Controller"
            root.mkdir()
            marker = root / PREFER_EMBEDDED_MARKER
            with patch.dict(os.environ, {"LOCALAPPDATA": str(local)}):
                self.assertFalse(prefer_embedded_from_environment())
                marker.write_bytes(b"1\n")
                self.assertTrue(prefer_embedded_from_environment())
                marker.write_bytes(b"1")
                self.assertFalse(prefer_embedded_from_environment())


if __name__ == "__main__":
    unittest.main()
