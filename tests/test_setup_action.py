import json
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "com.arkamax404.mediacontrold200.ulanziPlugin" / "runtime" / "python"
sys.path.insert(0, str(RUNTIME))

from setup_action import (  # noqa: E402
    ACTION_UUID,
    BUILTIN_UUID,
    LARGEITEM_UUID,
    SetupActionController,
    probe_setup_action,
)
from progress_action import ProgressActionModel  # noqa: E402
from progress_scheduler import ProgressScheduler  # noqa: E402


PACKAGE_ID = "10000000-0000-4000-8000-000000000000"
PAGE_ID = "20000000-0000-4000-8000-000000000000"
SETUP_ID = "30000000-0000-4000-8000-000000000000"
CONTEXT = f"{ACTION_UUID}___1_1___{SETUP_ID}"


def live_profile(root: Path, center=BUILTIN_UUID, setup_id=SETUP_ID,
                 second=False):
    package = root / f"{PACKAGE_ID}.ulanziProfile"
    page = package / "Profiles" / PAGE_ID
    page.mkdir(parents=True)
    (package / "manifest.json").write_text(json.dumps({
        "Device": {"Model": "D200", "UUID": "device"},
        "Name": "Test Profile", "Pages": {"Current": PAGE_ID, "Pages": [PAGE_ID]},
        "Version": 2,
    }), "utf-8")
    (page / "manifest.json").write_text(json.dumps({
        "Controllers": [{"Actions": {}}, {"Actions": {
            "1_1": {"Action": ACTION_UUID, "ActionID": setup_id},
            "3_2": {"Action": center, "ActionID": "40000000-0000-4000-8000-000000000000"},
        }}],
    }), "utf-8")
    if second:
        other_package = root / "50000000-0000-4000-8000-000000000000.ulanziProfile"
        other_page = other_package / "Profiles" / "60000000-0000-4000-8000-000000000000"
        other_page.mkdir(parents=True)
        (other_package / "manifest.json").write_text(json.dumps({
            "Name": "Other", "Device": {"Model": "D200", "UUID": "other-device"},
            "Pages": {"Current": other_page.name, "Pages": [other_page.name]},
        }), "utf-8")
        (other_page / "manifest.json").write_text(json.dumps({
            "Controllers": [{"Actions": {"0_0": {
                "Action": ACTION_UUID, "ActionID": setup_id,
            }}}],
        }), "utf-8")


class SetupProbeTests(unittest.TestCase):
    def test_finds_unique_setup_action_and_classifies_center_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            live_profile(root)
            ready = probe_setup_action(root, SETUP_ID)
            self.assertEqual((ready.status, ready.reason),
                             ("Ready", "Live page identity verified"))
            self.assertEqual((ready.package_id, ready.profile_id, ready.profile_name),
                             (PACKAGE_ID, PAGE_ID, "Test Profile"))

    def test_installed_conflict_missing_and_duplicate_fail_closed(self):
        for center, expected in ((LARGEITEM_UUID, "Installed"), ("other.action", "Failed")):
            with self.subTest(center=center), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); live_profile(root, center=center)
                self.assertEqual(probe_setup_action(root, SETUP_ID).status, expected)
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(probe_setup_action(Path(directory), SETUP_ID).status, "Failed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); live_profile(root, second=True)
            result = probe_setup_action(root, SETUP_ID)
            self.assertEqual((result.status, result.reason),
                             ("Failed", "Setup action is not unique"))

    def test_rejects_invalid_action_identity_and_unsafe_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); live_profile(root)
            with self.assertRaises(ValueError):
                probe_setup_action(root, "not-a-uuid")
            self.assertEqual(probe_setup_action(root / "missing", SETUP_ID).status, "Failed")
            cancelled = probe_setup_action(root, SETUP_ID, cancelled=lambda: True)
            self.assertEqual(cancelled.reason, "Profile scan was cancelled")

    def test_requires_reachable_page_and_center_on_setup_controller(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); live_profile(root)
            package = root / f"{PACKAGE_ID}.ulanziProfile"
            page_path = package / "Profiles" / PAGE_ID / "manifest.json"
            document = json.loads(page_path.read_text("utf-8"))
            setup = document["Controllers"][1]["Actions"].pop("1_1")
            document["Controllers"][0]["Actions"]["1_1"] = setup
            page_path.write_text(json.dumps(document), "utf-8")
            self.assertEqual(probe_setup_action(root, SETUP_ID).reason,
                             "The page does not contain one center display")

            root_manifest = json.loads((package / "manifest.json").read_text("utf-8"))
            other_page = "70000000-0000-4000-8000-000000000000"
            other = package / "Profiles" / other_page
            other.mkdir()
            (other / "manifest.json").write_text(json.dumps({
                "Controllers": [{"Actions": {}}, {"Actions": {
                    "3_2": {"Action": BUILTIN_UUID,
                            "ActionID": "80000000-0000-4000-8000-000000000000"},
                }}],
            }), "utf-8")
            root_manifest["Pages"] = {"Current": other_page, "Pages": [other_page]}
            (package / "manifest.json").write_text(json.dumps(root_manifest), "utf-8")
            self.assertEqual(probe_setup_action(root, SETUP_ID).reason,
                             "Setup action was not found")

    def test_follows_reachable_profile_uuid_folder_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); live_profile(root)
            package = root / f"{PACKAGE_ID}.ulanziProfile"
            folder_page = "70000000-0000-4000-8000-000000000000"
            folder = package / "Profiles" / folder_page
            folder.mkdir()
            original = json.loads((package / "Profiles" / PAGE_ID /
                                   "manifest.json").read_text("utf-8"))
            (folder / "manifest.json").write_text(json.dumps(original), "utf-8")
            source_path = package / "Profiles" / PAGE_ID / "manifest.json"
            source = json.loads(source_path.read_text("utf-8"))
            source["Controllers"][1]["Actions"].pop("1_1")
            source["Controllers"][1]["Actions"]["0_0"] = {
                "Action": "com.ulanzi.deck.page.folder",
                "ActionID": "80000000-0000-4000-8000-000000000000",
                "ActionParam": {"ProfileUUID": folder_page},
            }
            source_path.write_text(json.dumps(source), "utf-8")
            result = probe_setup_action(root, SETUP_ID)
            self.assertEqual((result.status, result.profile_id), ("Ready", folder_page))


class SetupControllerTests(unittest.TestCase):
    def test_lifecycle_probe_and_inspector_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); live_profile(root)

            class Api:
                def __init__(self): self.paths, self.inspectors = [], []
                def setPathIcon(self, context, icon, text):
                    self.paths.append((context, icon, text)); return True
                def sendToPropertyInspector(self, payload, context):
                    self.inspectors.append((context, payload)); return True

            api = Api()
            controller = SetupActionController(api, lambda: root, assistant_launcher=None)
            self.assertTrue(controller.add({"uuid": ACTION_UUID, "context": CONTEXT}))
            self.assertEqual(api.paths[-1][2], "Ready")
            self.assertTrue(controller.run({"uuid": ACTION_UUID, "context": CONTEXT}))
            deadline = time.monotonic() + 1
            while api.inspectors[-1][1]["setupStatus"]["reason"] \
                    != "Live page identity verified" and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertEqual(api.inspectors[-1][1]["setupStatus"]["reason"],
                             "Live page identity verified")
            self.assertTrue(controller.inspector_message({
                "context": CONTEXT, "payload": {"type": "requestSetupStatus"},
            }))
            self.assertTrue(controller.set_active({"context": CONTEXT, "active": False}))
            self.assertFalse(controller.run({"uuid": ACTION_UUID, "context": CONTEXT}))
            self.assertTrue(controller.clear({"param": [{"context": CONTEXT}]}))
            controller.shutdown()
            self.assertFalse(controller.add({"uuid": ACTION_UUID, "context": CONTEXT}))

    def test_ignores_unknown_or_malformed_events(self):
        class Api: pass
        controller = SetupActionController(Api(), lambda: Path("missing"), assistant_launcher=None)
        self.assertFalse(controller.add({"uuid": ACTION_UUID, "context": "bad"}))
        self.assertFalse(controller.add({"uuid": ACTION_UUID + ".bad", "context": CONTEXT}))
        self.assertFalse(controller.run({"uuid": ACTION_UUID, "context": CONTEXT}))
        self.assertFalse(controller.add({
            "uuid": ACTION_UUID, "context": CONTEXT,
            "actionid": "90000000-0000-4000-8000-000000000000",
        }))

    def test_probe_runs_off_callback_thread(self):
        entered = threading.Event()
        release = threading.Event()

        def blocked_root():
            entered.set()
            release.wait(1)
            return Path("missing")

        class Api:
            def __init__(self): self.paths = []
            def setPathIcon(self, *args): self.paths.append(args); return True
            def sendToPropertyInspector(self, *args): return True

        api = Api()
        controller = SetupActionController(api, blocked_root, assistant_launcher=None)
        self.assertTrue(controller.add({"uuid": ACTION_UUID, "context": CONTEXT}))
        started = time.monotonic()
        self.assertTrue(controller.run({"uuid": ACTION_UUID, "context": CONTEXT}))
        self.assertLess(time.monotonic() - started, .05)
        self.assertTrue(entered.wait(1))
        other_context = f"{ACTION_UUID}___2_1___90000000-0000-4000-8000-000000000000"
        self.assertTrue(controller.add({"uuid": ACTION_UUID, "context": other_context}))
        self.assertFalse(controller.run({"uuid": ACTION_UUID, "context": other_context}))
        self.assertEqual(api.paths[-1][-1], "Failed")
        release.set()
        controller.shutdown()

    def test_ready_probe_launches_install_helper_and_requests_studio_close(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); live_profile(root)
            launches = []

            class Api:
                def __init__(self): self.paths = []
                def setPathIcon(self, context, icon, text):
                    self.paths.append((context, icon, text)); return True
                def sendToPropertyInspector(self, payload, context): return True

            api = Api()
            controller = SetupActionController(
                api, lambda: root,
                assistant_launcher=lambda action_id, operation, launch_id:
                    launches.append((action_id, operation, launch_id)) or True,
            )
            self.assertTrue(controller.add({"uuid": ACTION_UUID, "context": CONTEXT}))
            self.assertTrue(controller.run({"uuid": ACTION_UUID, "context": CONTEXT}))
            deadline = time.monotonic() + 1
            while not launches and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertEqual([(item[0], item[1]) for item in launches], [(SETUP_ID, "install")])
            self.assertEqual(api.paths[-1][-1], "Waiting for Studio to close")
            controller.shutdown()

    def test_operation_change_during_probe_invalidates_launch_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); live_profile(root)
            entered = threading.Event(); release = threading.Event(); launches = []

            def blocked_root():
                entered.set(); release.wait(1); return root

            class Api:
                def setPathIcon(self, *args): return True
                def sendToPropertyInspector(self, *args): return True
                def setSettings(self, *args): return True

            controller = SetupActionController(
                Api(), blocked_root,
                assistant_launcher=lambda *args: launches.append(args) or True,
                assistant_status=lambda: None,
            )
            self.assertTrue(controller.add({"uuid": ACTION_UUID, "context": CONTEXT}))
            self.assertTrue(controller.run({"uuid": ACTION_UUID, "context": CONTEXT}))
            self.assertTrue(entered.wait(1))
            self.assertTrue(controller.receive_settings({
                "context": CONTEXT, "settings": {"operation": "restore"},
            }))
            release.set(); controller.shutdown()
            self.assertEqual(launches, [])

    def test_operation_change_is_refused_after_immutable_launch_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); live_profile(root)
            entered = threading.Event(); release = threading.Event(); launches = []

            def launcher(action_id, operation, launch_id):
                launches.append((action_id, operation, launch_id))
                entered.set(); release.wait(1); return True

            class Api:
                def setPathIcon(self, *args): return True
                def sendToPropertyInspector(self, *args): return True

            controller = SetupActionController(
                Api(), lambda: root, assistant_launcher=launcher,
                assistant_status=lambda: None)
            self.assertTrue(controller.add({"uuid": ACTION_UUID, "context": CONTEXT}))
            self.assertTrue(controller.run({"uuid": ACTION_UUID, "context": CONTEXT}))
            self.assertTrue(entered.wait(1))
            self.assertFalse(controller.receive_settings({
                "context": CONTEXT, "settings": {"operation": "restore"},
            }))
            release.set(); controller.shutdown()
            self.assertEqual([(item[0], item[1]) for item in launches], [(SETUP_ID, "install")])

    def test_restart_and_inspector_surface_detached_failure_status(self):
        reasons = []

        class Api:
            def setPathIcon(self, *args): return True
            def sendToPropertyInspector(self, payload, _context):
                reasons.append(payload["setupStatus"]["reason"]); return True

        controller = SetupActionController(
            Api(), lambda: Path("missing"), assistant_launcher=None,
            assistant_status=lambda: {
                "state": "failed", "action_id": SETUP_ID,
                "failure": "Detached helper failed safely",
            },
        )
        self.assertTrue(controller.add({"uuid": ACTION_UUID, "context": CONTEXT}))
        self.assertIn("Detached helper failed safely", reasons)
        self.assertTrue(controller.inspector_message({
            "context": CONTEXT, "payload": {"type": "requestSetupStatus"},
        }))
        self.assertEqual(reasons[-1], "Detached helper failed safely")

    def test_terminal_status_releases_reservation_for_same_controller_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); live_profile(root)
            launches = []
            status = [None]

            class Api:
                def setPathIcon(self, *args): return True
                def sendToPropertyInspector(self, *args): return True

            controller = SetupActionController(
                Api(), lambda: root,
                assistant_launcher=lambda *args: launches.append(args) or True,
                assistant_status=lambda: status[0],
            )
            event = {"uuid": ACTION_UUID, "context": CONTEXT}
            self.assertTrue(controller.add(event)); self.assertTrue(controller.run(event))
            deadline = time.monotonic() + 1
            while len(launches) < 1 and time.monotonic() < deadline:
                time.sleep(.005)
            status[0] = {"state": "failed", "operation": "install",
                         "action_id": SETUP_ID, "launch_id": launches[0][2],
                         "failure": "Retry safely"}
            self.assertTrue(controller.inspector_message({
                "context": CONTEXT, "payload": {"type": "requestSetupStatus"},
            }))
            self.assertTrue(controller.receive_settings({
                "context": CONTEXT, "settings": {"operation": "repair"},
            }))
            status[0] = None
            self.assertTrue(controller.run(event))
            deadline = time.monotonic() + 1
            while len(launches) < 2 and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertEqual([(item[0], item[1]) for item in launches],
                             [(SETUP_ID, "install"), (SETUP_ID, "repair")])
            controller.shutdown()

    def test_old_terminal_poll_cannot_release_current_launch_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); live_profile(root)
            entered = threading.Event(); release = threading.Event(); launches = []
            old_id = "60000000-0000-4000-8000-000000000000"
            new_id = uuid.UUID("70000000-0000-4000-8000-000000000000")
            status = [{"state": "failed", "operation": "install", "action_id": SETUP_ID,
                       "launch_id": old_id, "failure": "Old launch failed"}]

            def launcher(action_id, operation, launch_id):
                launches.append((action_id, operation, launch_id))
                entered.set(); release.wait(1); return True

            class Api:
                def setPathIcon(self, *args): return True
                def sendToPropertyInspector(self, *args): return True

            controller = SetupActionController(
                Api(), lambda: root, assistant_launcher=launcher,
                assistant_status=lambda: status[0], launch_id_factory=lambda: new_id)
            event = {"uuid": ACTION_UUID, "context": CONTEXT}
            self.assertTrue(controller.add(event)); self.assertTrue(controller.run(event))
            self.assertTrue(entered.wait(1))
            self.assertTrue(controller.inspector_message({
                "context": CONTEXT, "payload": {"type": "requestSetupStatus"},
            }))
            self.assertFalse(controller.receive_settings({
                "context": CONTEXT, "settings": {"operation": "restore"},
            }))
            self.assertEqual(launches, [(SETUP_ID, "install", str(new_id))])
            release.set(); controller.shutdown()

    def test_successful_restore_is_projected_as_restored(self):
        labels = []

        class Api:
            def setPathIcon(self, _context, _icon, label): labels.append(label); return True
            def sendToPropertyInspector(self, *args): return True

        controller = SetupActionController(
            Api(), lambda: Path("missing"), assistant_launcher=None,
            assistant_status=lambda: {"state": "succeeded", "operation": "restore",
                                      "action_id": SETUP_ID},
        )
        self.assertTrue(controller.add({"uuid": ACTION_UUID, "context": CONTEXT}))
        self.assertEqual(labels[-1], "Restored")
        controller.shutdown()

    def test_cross_operation_recovery_projects_actual_operation_and_requires_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); live_profile(root, center=LARGEITEM_UUID)
            paths, reasons, launches = [], [], []
            status = [None]

            class Api:
                def setPathIcon(self, _context, _icon, label): paths.append(label); return True
                def sendToPropertyInspector(self, payload, _context):
                    reasons.append(payload["setupStatus"]["reason"]); return True

            controller = SetupActionController(
                Api(), lambda: root,
                assistant_launcher=lambda *args: launches.append(args) or True,
                assistant_status=lambda: status[0],
            )
            event = {"uuid": ACTION_UUID, "context": CONTEXT,
                     "param": {"operation": "restore"}}
            self.assertTrue(controller.add(event)); self.assertTrue(controller.run(event))
            deadline = time.monotonic() + 1
            while not launches and time.monotonic() < deadline:
                time.sleep(.005)
            current_launch = launches[0][2]
            status[0] = {
                "state": "succeeded", "operation": "install", "action_id": SETUP_ID,
                "launch_id": "80000000-0000-4000-8000-000000000000",
                "request_disposition": "recovery_only", "request_action_id": SETUP_ID,
                "request_operation": "restore", "request_launch_id": current_launch,
            }
            self.assertTrue(controller.inspector_message({
                "context": CONTEXT, "payload": {"type": "requestSetupStatus"},
            }))
            self.assertEqual(paths[-1], "Installed")
            self.assertIn("Recovered prior Install", reasons[-1])
            self.assertIn("run Restore", reasons[-1])
            self.assertTrue(controller.receive_settings({
                "context": CONTEXT, "settings": {"operation": "install"},
            }))
            controller.shutdown()

    def test_committed_profile_with_relaunch_failure_projects_failed(self):
        labels, reasons = [], []

        class Api:
            def setPathIcon(self, _context, _icon, label): labels.append(label); return True
            def sendToPropertyInspector(self, payload, _context):
                reasons.append(payload["setupStatus"]["reason"]); return True

        controller = SetupActionController(
            Api(), lambda: Path("missing"), assistant_launcher=None,
            assistant_status=lambda: {
                "state": "failed", "profile_result": "succeeded", "operation": "install",
                "action_id": SETUP_ID, "relaunch_failure": "access denied",
            },
        )
        self.assertTrue(controller.add({"uuid": ACTION_UUID, "context": CONTEXT}))
        self.assertEqual(labels[-1], "Failed")
        self.assertIn("Profile updated, but Studio restart failed", reasons[-1])
        self.assertIn("access denied", reasons[-1])
        controller.shutdown()

    def test_shared_scheduler_routes_setup_without_polling_media(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); live_profile(root)

            class Api:
                def __init__(self): self.paths = []
                def setPathIcon(self, context, icon, text):
                    self.paths.append((context, icon, text)); return True
                def sendToPropertyInspector(self, payload, context): return True

            class Client:
                def __init__(self): self.calls = 0
                def get_state(self, cancelled=None): self.calls += 1

            api, client = Api(), Client()
            controller = SetupActionController(api, lambda: root, assistant_launcher=None)
            scheduler = ProgressScheduler(
                api, client, ProgressActionModel(), setup_controller=controller)
            event = {"uuid": ACTION_UUID, "context": CONTEXT}
            self.assertTrue(scheduler.handle_add(event))
            self.assertTrue(scheduler.handle_run(event))
            self.assertEqual(client.calls, 0)
            deadline = time.monotonic() + 1
            while len(api.paths) < 3 and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertEqual(api.paths[-1][2], "Ready")
            self.assertTrue(scheduler.handle_clear({"param": [{"context": CONTEXT}]}))
            self.assertTrue(scheduler.stop(.1))


if __name__ == "__main__":
    unittest.main()
