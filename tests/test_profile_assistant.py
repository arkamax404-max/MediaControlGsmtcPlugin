import copy
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "com.arkamax404.mediacontrold200.ulanziPlugin" / "runtime" / "python"
sys.path.insert(0, str(RUNTIME))

import profile_assistant  # noqa: E402
from profile_assistant import (  # noqa: E402
    ProfileAssistantError,
    ProductionRoots,
    RECEIPT_SCHEMA,
    SCHEMA,
    create_request,
    default_process_check,
    execute_request,
    load_request,
)
from setup_action import ACTION_UUID, BUILTIN_UUID, LARGEITEM_UUID  # noqa: E402


PACKAGE_ID = "10000000-0000-4000-8000-000000000000"
PAGE_ID = "20000000-0000-4000-8000-000000000000"
SETUP_ID = "30000000-0000-4000-8000-000000000000"
BUILTIN_ID = "40000000-0000-4000-8000-000000000000"
LAUNCH_ID = "50000000-0000-4000-8000-000000000000"


class Fixture:
    def __init__(self, root: Path):
        self.root = root
        self.profiles = root / "ProfilesV2"
        self.backups = root / "Backups"
        self.state = root / "State"
        self.plugin = root / "com.arkamax404.mediacontrold200.ulanziPlugin"
        self.studio = root / "Studio" / "UlanziDeck.exe"
        for directory in (self.profiles, self.backups, self.state,
                          self.plugin, self.studio.parent):
            directory.mkdir(parents=True)
        self.studio.write_bytes(b"test executable")
        self.plugin_manifest = self.plugin / "manifest.json"
        self.plugin_manifest.write_text(json.dumps({
            "Version": "1.4.0", "Name": "Media Control for D200",
            "UUID": "com.arkamax404.ulanzi.mediacontrol",
            "Actions": [{"Name": "Large Now Playing", "UUID": LARGEITEM_UUID}],
        }), "utf-8")
        self.package = self.profiles / f"{PACKAGE_ID}.ulanziProfile"
        self.page = self.package / "Profiles" / PAGE_ID
        self.page.mkdir(parents=True)
        (self.package / "manifest.json").write_text(json.dumps({
            "Device": {"Model": "D200", "UUID": "device"},
            "Name": "Test Profile", "Pages": {"Current": PAGE_ID, "Pages": [PAGE_ID]},
            "Version": 2,
        }), "utf-8")
        self.original_entry = {
            "Action": BUILTIN_UUID, "ActionID": BUILTIN_ID,
            "ActionParam": {"preserve": True}, "VendorField": "exact-original",
        }
        self.manifest = self.page / "manifest.json"
        self.write_center(self.original_entry)
        (self.package / "asset.bin").write_bytes(b"complete package backup")

    def write_center(self, center):
        self.manifest.write_text(json.dumps({
            "Name": "Page", "Unchanged": [1, {"two": 2}],
            "Controllers": [{"Actions": {}}, {"Actions": {
                "1_1": {"Action": ACTION_UUID, "ActionID": SETUP_ID},
                "3_2": copy.deepcopy(center),
            }}],
        }), "utf-8")

    def center(self):
        return json.loads(self.manifest.read_text("utf-8"))["Controllers"][1]["Actions"]["3_2"]

    def request(self, operation="install"):
        return {
            "schema": SCHEMA, "operation": operation, "action_id": SETUP_ID,
            "launch_id": LAUNCH_ID,
            "profiles_root": str(self.profiles),
            "plugin_manifest": str(self.plugin_manifest),
            "backup_root": str(self.backups), "state_root": str(self.state),
            "studio_executable": str(self.studio), "wait_timeout_seconds": 1.0,
        }

    def roots(self):
        request_root = self.root / "Requests"
        request_root.mkdir(exist_ok=True)
        return ProductionRoots(request_root, self.backups, self.state, self.profiles,
                               self.plugin_manifest, self.studio,
                               self.root / "profile-assistant.key")


class UUIDs:
    def __init__(self):
        self.value = 10

    def __call__(self):
        self.value += 1
        return uuid.UUID(f"00000000-0000-4000-8000-{self.value:012d}")


class ProfileAssistantTests(unittest.TestCase):
    def run_request(self, fixture, operation="install", process_check=lambda _path: False,
                    sleeper=lambda _seconds: None, monotonic=lambda: 0.0,
                    relaunch=lambda _path: None, readback=None):
        if not hasattr(fixture, "uuid_factory"):
            fixture.uuid_factory = UUIDs()
        return execute_request(
            fixture.request(operation), process_check=process_check, sleeper=sleeper,
            monotonic=monotonic, relaunch=relaunch, uuid_factory=fixture.uuid_factory,
            readback=readback,
        )

    def test_install_changes_only_center_and_writes_complete_backup_and_records(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            before = json.loads(fixture.manifest.read_text("utf-8"))
            result = self.run_request(fixture)
            after = json.loads(fixture.manifest.read_text("utf-8"))
            installed = after["Controllers"][1]["Actions"]["3_2"]
            self.assertEqual(installed["Action"], LARGEITEM_UUID)
            expected = copy.deepcopy(before)
            expected["Controllers"][1]["Actions"]["3_2"] = installed
            self.assertEqual(after, expected)
            backup = Path(result["backup_path"])
            self.assertEqual((backup / "asset.bin").read_bytes(), b"complete package backup")
            self.assertEqual((backup / "Profiles" / PAGE_ID / "manifest.json").read_text("utf-8"),
                             json.dumps(before))
            receipt = json.loads((fixture.state / "receipt.json").read_text("utf-8"))
            self.assertEqual((receipt["schema"], receipt["state"]),
                             (RECEIPT_SCHEMA, "succeeded"))
            self.assertEqual(receipt["target"]["controller_index"], 1)
            self.assertEqual(receipt["trigger"]["setup_key"], "1_1")
            self.assertTrue((fixture.state / f"journal-{result['operation_id']}.json").is_file())
            self.assertEqual(json.loads((fixture.state / "status.json").read_text("utf-8"))["state"],
                             "succeeded")
            inventory = receipt["backup_inventory"]
            self.assertEqual(inventory["total_bytes"], sum(item["size"] for item in inventory["files"]))
            marker = json.loads((backup.parent / "complete.json").read_text("utf-8"))
            self.assertEqual(marker["inventory_sha256"], receipt["backup_inventory_sha256"])

    def test_backup_fsyncs_payloads_and_directory_tree_before_complete_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            directory_calls = []
            original_directory_fsync = profile_assistant._fsync_directory

            def record_directory(path, required=False):
                directory_calls.append((Path(path), required))
                return original_directory_fsync(path, required)

            with patch.object(profile_assistant, "_fsync_directory",
                              side_effect=record_directory), \
                    patch.object(profile_assistant.os, "fsync",
                                 wraps=profile_assistant.os.fsync) as file_fsync:
                result = self.run_request(fixture)
            backup = Path(result["backup_path"])
            self.assertGreaterEqual(file_fsync.call_count, 3)
            required = {path for path, is_required in directory_calls if is_required}
            self.assertIn(fixture.backups, required)
            self.assertIn(backup.parent, required)
            self.assertIn(backup, required)

    def test_repair_restores_receipt_entry_only_from_builtin(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            self.run_request(fixture)
            installed = fixture.center()
            fixture.write_center(fixture.original_entry)
            self.run_request(fixture, "repair")
            self.assertEqual(fixture.center(), installed)

    def test_restore_restores_exact_original_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            self.run_request(fixture)
            self.run_request(fixture, "restore")
            self.assertEqual(fixture.center(), fixture.original_entry)

    def test_restore_accepts_only_studio_canonical_largeitem_normalization(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            self.run_request(fixture)
            installed = fixture.center()
            normalized = copy.deepcopy(installed)
            normalized["Plugin"] = {}
            normalized["ActionParam"] = {
                "showArtwork": True, "pausedArtwork": "grayscale",
                "showProgress": True, "showElapsed": False, "showRemaining": True,
                "backgroundColor": "#0B0D10", "primaryColor": "#FFFFFF",
                "secondaryColor": "#B8BEC8", "accentColor": "#1DB954",
                "fit": "contain",
            }
            fixture.write_center(normalized)
            self.run_request(fixture, "restore")
            self.assertEqual(fixture.center(), fixture.original_entry)

        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            self.run_request(fixture)
            hostile = copy.deepcopy(fixture.center())
            hostile["Plugin"] = {}
            hostile["ActionParam"] = {"showArtwork": True, "unexpected": "value"}
            fixture.write_center(hostile)
            with self.assertRaisesRegex(ProfileAssistantError, "exact installed"):
                self.run_request(fixture, "restore")

    def test_unknown_third_party_center_fails_without_backup_or_state(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            fixture.write_center({"Action": "third.party.action", "ActionID": BUILTIN_ID})
            with self.assertRaisesRegex(ProfileAssistantError, "unique target|built-in center|entry schema"):
                self.run_request(fixture)
            self.assertEqual(list(fixture.backups.iterdir()), [])
            self.assertEqual(json.loads((fixture.state / "status.json").read_text("utf-8"))["state"],
                             "failed")

    def test_wait_is_bounded_and_timeout_has_no_filesystem_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            original = fixture.manifest.read_bytes()
            ticks = iter((0.0, 0.0, 0.5, 1.0))
            with self.assertRaisesRegex(ProfileAssistantError, "Timed out"):
                self.run_request(fixture, process_check=lambda _path: True,
                                 monotonic=lambda: next(ticks))
            self.assertEqual(fixture.manifest.read_bytes(), original)
            self.assertEqual(list(fixture.backups.iterdir()), [])
            self.assertEqual(json.loads((fixture.state / "status.json").read_text("utf-8"))["state"],
                             "failed")

    def test_success_and_validated_rollback_relaunch_exact_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            launches = []
            checks = iter((True, False, False, False))
            self.run_request(fixture, process_check=lambda _path: next(checks),
                             relaunch=launches.append)
            self.assertEqual(launches, [fixture.studio])

        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            original = fixture.manifest.read_bytes()
            launches = []
            checks = iter((True, False, False, False, False, False))
            with self.assertRaisesRegex(ProfileAssistantError, "readback"):
                self.run_request(fixture, process_check=lambda _path: next(checks),
                                 relaunch=launches.append, readback=lambda _path: b"{}")
            self.assertEqual(fixture.manifest.read_bytes(), original)
            self.assertEqual(launches, [fixture.studio])
            status = json.loads((fixture.state / "status.json").read_text("utf-8"))
            self.assertEqual(status["state"], "rolled_back")

    def test_backup_rejects_links_before_manifest_write(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            link = fixture.package / "linked.bin"
            try:
                link.symlink_to(fixture.package / "asset.bin")
            except (OSError, NotImplementedError):
                self.skipTest("Symlinks are not available")
            original = fixture.manifest.read_bytes()
            with self.assertRaisesRegex(ProfileAssistantError, "links and reparse"):
                self.run_request(fixture)
            self.assertEqual(fixture.manifest.read_bytes(), original)
            self.assertEqual(json.loads((fixture.state / "status.json").read_text("utf-8"))["state"],
                             "failed")

    def test_backup_rejects_resource_excess_before_manifest_write(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            original = fixture.manifest.read_bytes()
            with patch.object(profile_assistant, "MAX_FILES", 2):
                with self.assertRaisesRegex(ProfileAssistantError, "inventory limits"):
                    self.run_request(fixture)
            self.assertEqual(fixture.manifest.read_bytes(), original)
            self.assertEqual(json.loads((fixture.state / "status.json").read_text("utf-8"))["state"],
                             "failed")

    def test_malformed_request_and_paths_have_no_real_filesystem_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = Fixture(root / "fixture")
            roots = fixture.roots()
            request_path = roots.request_root / f"{uuid.uuid4()}.json"
            request_path.write_text(json.dumps({"schema": SCHEMA, "operation": "install"}), "utf-8")
            with self.assertRaisesRegex(ProfileAssistantError, "schema"):
                load_request(request_path.resolve(), roots=roots)
            self.assertFalse(request_path.exists())

            request = fixture.request()
            request["profiles_root"] = str(root / "not-profiles")
            with self.assertRaises(ProfileAssistantError):
                execute_request(request, process_check=lambda _path: False,
                                relaunch=lambda _path: self.fail("must not relaunch"))
            self.assertEqual(list(fixture.backups.iterdir()), [])
            self.assertFalse((fixture.state / "receipt.json").exists())

    def test_request_is_authenticated_exact_root_one_shot_and_retired(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            roots = fixture.roots()
            issued = create_request(SETUP_ID, "install", lambda: roots)
            issued_bytes = issued.read_bytes()
            loaded = load_request(issued, roots=roots)
            self.assertEqual((loaded["operation"], loaded["profiles_root"]),
                             ("install", str(fixture.profiles)))
            self.assertFalse(issued.exists())
            issued.write_bytes(issued_bytes)
            with self.assertRaisesRegex(ProfileAssistantError, "already consumed"):
                load_request(issued, roots=roots)

            tampered = create_request(SETUP_ID, "install", lambda: roots)
            value = json.loads(tampered.read_text("utf-8"))
            value["operation"] = "restore"
            tampered.write_text(json.dumps(value), "utf-8")
            with self.assertRaisesRegex(ProfileAssistantError, "authentication"):
                load_request(tampered, roots=roots)
            self.assertFalse(tampered.exists())

            issued = create_request(SETUP_ID, "install", lambda: roots)
            outside = fixture.root / issued.name
            outside.write_bytes(issued.read_bytes())
            with self.assertRaisesRegex(ProfileAssistantError, "outside the exact request root"):
                load_request(outside, roots=roots)

    def test_forged_receipt_cannot_override_backed_original(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            self.run_request(fixture)
            fixture.write_center(fixture.original_entry)
            receipt_path = fixture.state / "receipt.json"
            receipt = json.loads(receipt_path.read_text("utf-8"))
            receipt["original_entry"]["VendorField"] = "forged"
            key = (fixture.root / "profile-assistant.key").read_bytes()
            receipt["auth"] = profile_assistant._authenticate(receipt, key)
            receipt_path.write_text(json.dumps(receipt), "utf-8")
            with self.assertRaisesRegex(ProfileAssistantError, "authoritative rollback"):
                self.run_request(fixture, "repair")
            self.assertEqual(fixture.center(), fixture.original_entry)

    def test_injected_exclusive_lock_rejects_concurrent_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            entered = threading.Event(); release = threading.Event()
            mutex = threading.Lock()

            @contextmanager
            def lock(_root):
                if not mutex.acquire(blocking=False):
                    raise ProfileAssistantError("Another Profile Assistant operation is active")
                try:
                    yield
                finally:
                    mutex.release()

            def process(_path):
                entered.set(); release.wait(1); return False

            errors = []
            thread = threading.Thread(target=lambda: execute_request(
                fixture.request(), process_check=process, uuid_factory=UUIDs(),
                lock_factory=lock))
            thread.start(); self.assertTrue(entered.wait(1))
            with self.assertRaisesRegex(ProfileAssistantError, "operation is active"):
                execute_request(fixture.request(), process_check=lambda _path: False,
                                uuid_factory=UUIDs(), lock_factory=lock)
            release.set(); thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_studio_restart_immediately_before_write_fails_without_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory)); original = fixture.manifest.read_bytes()
            checks = iter((False, True, True))
            with self.assertRaisesRegex(ProfileAssistantError, "restarted before"):
                self.run_request(fixture, process_check=lambda _path: next(checks))
            self.assertEqual(fixture.manifest.read_bytes(), original)

    def test_studio_restart_during_expensive_backup_validation_prevents_write(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory)); original = fixture.manifest.read_bytes()
            running = [False]
            original_validate = profile_assistant._validate_backup

            def validating(*args, **kwargs):
                result = original_validate(*args, **kwargs)
                running[0] = True
                return result

            with patch.object(profile_assistant, "_validate_backup", side_effect=validating):
                with self.assertRaisesRegex(ProfileAssistantError, "restarted before"):
                    self.run_request(fixture, process_check=lambda _path: running[0])
            self.assertEqual(fixture.manifest.read_bytes(), original)

    def test_tasklist_failure_is_fail_closed(self):
        completed = subprocess.CompletedProcess([], 1, stdout="", stderr="denied")
        with patch.object(profile_assistant.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(ProfileAssistantError, "could not be determined"):
                default_process_check(Path("UlanziDeck.exe"))

    def test_tasklist_process_check_is_always_windowless(self):
        completed = subprocess.CompletedProcess([], 0, stdout='"UlanziDeck.exe"', stderr="")
        with patch.object(profile_assistant.subprocess, "run", return_value=completed) as run:
            self.assertTrue(default_process_check(Path("UlanziDeck.exe")))
        self.assertEqual(run.call_args.kwargs["creationflags"],
                         profile_assistant.CREATE_NO_WINDOW)
        self.assertFalse(run.call_args.kwargs["shell"])

    def test_prewrite_failure_relaunches_observed_studio_and_records_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory)); launches = []
            (fixture.package / "bad-link").symlink_to(fixture.package / "asset.bin")
            checks = iter((True, False, False))
            with self.assertRaises(ProfileAssistantError):
                self.run_request(fixture, process_check=lambda _path: next(checks),
                                 relaunch=launches.append)
            self.assertEqual(launches, [fixture.studio])
            self.assertEqual(json.loads((fixture.state / "status.json").read_text("utf-8"))["state"],
                             "failed")

    def test_install_journal_recovered_during_restore_keeps_actual_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            result = self.run_request(fixture)
            receipt_path = fixture.state / "receipt.json"
            receipt = json.loads(receipt_path.read_text("utf-8")); receipt["state"] = "prepared"
            key = (fixture.root / "profile-assistant.key").read_bytes()
            receipt["auth"] = profile_assistant._authenticate(receipt, key)
            journal_path = fixture.state / f"journal-{result['operation_id']}.json"
            receipt_path.write_text(json.dumps(receipt), "utf-8")
            journal_path.write_text(json.dumps(receipt), "utf-8")
            recovery_launch_id = "90000000-0000-4000-8000-000000000000"
            request = fixture.request("restore")
            request["launch_id"] = recovery_launch_id
            recovered = execute_request(request, process_check=lambda _path: False,
                                        uuid_factory=fixture.uuid_factory)
            self.assertEqual((recovered["state"], recovered["recovered"]), ("succeeded", True))
            self.assertEqual((recovered["operation"], recovered["requested_operation"],
                              recovered["requested_operation_executed"]),
                             ("install", "restore", False))
            self.assertEqual(fixture.center()["Action"], LARGEITEM_UUID)
            self.assertEqual(len(list(fixture.backups.iterdir())), 1)
            status = json.loads((fixture.state / "status.json").read_text("utf-8"))
            self.assertEqual((status["operation"], status["launch_id"]),
                             ("install", LAUNCH_ID))
            self.assertEqual((status["request_operation"], status["request_launch_id"],
                              status["request_disposition"]),
                             ("restore", recovery_launch_id, "recovery_only"))

    def test_unknown_hash_recovery_preserves_live_manifest_for_manual_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            result = self.run_request(fixture)
            journal_path = fixture.state / f"journal-{result['operation_id']}.json"
            journal = json.loads(journal_path.read_text("utf-8"))
            journal["state"] = "prepared"
            key = (fixture.root / "profile-assistant.key").read_bytes()
            journal["auth"] = profile_assistant._authenticate(journal, key)
            journal_path.write_text(json.dumps(journal), "utf-8")
            divergent = b'{"external":"preserve exactly"}'
            fixture.manifest.write_bytes(divergent)
            with self.assertRaisesRegex(ProfileAssistantError, "manual recovery required"):
                self.run_request(fixture)
            self.assertEqual(fixture.manifest.read_bytes(), divergent)
            self.assertEqual(json.loads(journal_path.read_text("utf-8"))["state"], "prepared")
            self.assertEqual(json.loads((fixture.state / "status.json").read_text("utf-8"))["state"],
                             "manual_recovery_required")

    def test_concurrent_writer_during_rollback_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            divergent = b'{"external":"concurrent writer"}'

            def conflicting_readback(path):
                path.write_bytes(divergent)
                return b"{}"

            checks = iter((False, False, False))
            with self.assertRaisesRegex(ProfileAssistantError, "manual recovery required"):
                self.run_request(fixture, process_check=lambda _path: next(checks),
                                 readback=conflicting_readback)
            self.assertEqual(fixture.manifest.read_bytes(), divergent)
            status = json.loads((fixture.state / "status.json").read_text("utf-8"))
            self.assertEqual(status["state"], "manual_recovery_required")

    def test_studio_final_check_blocks_rollback_and_preserves_attempted_result(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            checks = iter((False, False, True, True))
            with self.assertRaisesRegex(ProfileAssistantError, "manual recovery required"):
                self.run_request(fixture, process_check=lambda _path: next(checks),
                                 readback=lambda _path: b"{}")
            self.assertEqual(fixture.center()["Action"], LARGEITEM_UUID)
            self.assertEqual(json.loads((fixture.state / "status.json").read_text("utf-8"))["state"],
                             "manual_recovery_required")

    def test_install_restore_repair_preserves_authoritative_lineage_across_plugin_update(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            self.run_request(fixture)
            installed_id = fixture.center()["ActionID"]
            authority = json.loads((fixture.state / "receipt.json").read_text("utf-8"))
            authority_backup = authority["backup_path"]
            plugin = json.loads(fixture.plugin_manifest.read_text("utf-8"))
            plugin["Version"] = "2.0.0"
            fixture.plugin_manifest.write_text(json.dumps(plugin), "utf-8")
            self.run_request(fixture, "restore")
            self.assertEqual(fixture.center(), fixture.original_entry)
            self.run_request(fixture, "repair")
            repaired = fixture.center()
            self.assertEqual((repaired["ActionID"], repaired["Plugin"]["Version"]),
                             (installed_id, "2.0.0"))
            receipt = json.loads((fixture.state / "receipt.json").read_text("utf-8"))
            self.assertEqual(receipt["backup_path"], authority_backup)
            self.assertEqual(receipt["installed_entry"], repaired)

    def test_terminal_metadata_commits_receipt_and_status_before_journal(self):
        class SimulatedCrash(BaseException):
            pass

        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            original_write = profile_assistant._write_signed_json

            def crash_on_terminal_journal(path, value, key):
                if path.name.startswith("journal-") and value.get("state") == "succeeded":
                    raise SimulatedCrash()
                return original_write(path, value, key)

            with patch.object(profile_assistant, "_write_signed_json",
                              side_effect=crash_on_terminal_journal):
                with self.assertRaises(SimulatedCrash):
                    self.run_request(fixture)
            journal_path = next(fixture.state.glob("journal-*.json"))
            self.assertEqual(json.loads(journal_path.read_text("utf-8"))["state"], "prepared")
            self.assertEqual(json.loads((fixture.state / "receipt.json").read_text("utf-8"))["state"],
                             "succeeded")
            self.assertEqual(json.loads((fixture.state / "status.json").read_text("utf-8"))["state"],
                             "succeeded")
            plugin = json.loads(fixture.plugin_manifest.read_text("utf-8"))
            plugin["Version"] = "2.1.0"
            fixture.plugin_manifest.write_text(json.dumps(plugin), "utf-8")
            recovered = self.run_request(fixture)
            self.assertTrue(recovered["recovered"])

    def test_relaunch_failure_is_observable_after_success(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            checks = iter((True, False, False, False))
            with self.assertRaisesRegex(ProfileAssistantError, "relaunch failed"):
                self.run_request(fixture, process_check=lambda _path: next(checks),
                                 relaunch=lambda _path: (_ for _ in ()).throw(OSError("blocked")))
            status = json.loads((fixture.state / "status.json").read_text("utf-8"))
            self.assertEqual((status["state"], status["profile_result"]),
                             ("failed", "succeeded"))
            self.assertIn("blocked", status["relaunch_failure"])
            self.assertEqual(fixture.center()["Action"], LARGEITEM_UUID)
            journal = json.loads(next(fixture.state.glob("journal-*.json")).read_text("utf-8"))
            self.assertEqual(journal["state"], "succeeded")

    def test_frozen_launcher_command_and_durable_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory)); roots = fixture.roots(); calls = []
            with patch.object(profile_assistant.sys, "frozen", True, create=True), \
                    patch.object(profile_assistant.sys, "executable", str(fixture.plugin / "runtime.exe")):
                self.assertTrue(profile_assistant.launch_profile_assistant(
                    SETUP_ID, "install", lambda: roots,
                    lambda *args, **kwargs: calls.append((args, kwargs))))
                self.assertFalse(profile_assistant.launch_profile_assistant(
                    SETUP_ID, "install", lambda: roots,
                    lambda *args, **kwargs: self.fail("must not launch twice")))
            command = calls[0][0][0]
            self.assertEqual(command[:2], [str(fixture.plugin / "runtime.exe"), "--profile-assistant"])
            self.assertEqual(Path(command[2]).parent, roots.request_root)

    def test_frozen_launcher_failure_publishes_durable_failed_status(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory)); roots = fixture.roots()
            with patch.object(profile_assistant.sys, "frozen", True, create=True):
                self.assertFalse(profile_assistant.launch_profile_assistant(
                    SETUP_ID, "install", lambda: roots,
                    lambda *args, **kwargs: (_ for _ in ()).throw(OSError("spawn"))))
            status = json.loads((roots.state_root / "status.json").read_text("utf-8"))
            self.assertEqual(status["state"], "failed")
            self.assertIn("could not be started", status["failure"])

    def test_stale_empty_and_authenticated_dead_launch_claims_are_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory)); roots = fixture.roots()
            roots.state_root.mkdir(exist_ok=True)
            claim = roots.state_root / "launch.claim"
            claim.write_bytes(b"")
            os.utime(claim, (100.0, 100.0))
            processes = []

            class Process:
                pid = 4321

            with patch.object(profile_assistant.sys, "frozen", True, create=True):
                self.assertTrue(profile_assistant.launch_profile_assistant(
                    SETUP_ID, "install", lambda: roots,
                    lambda *args, **kwargs: processes.append(args) or Process(),
                    wall_clock=lambda: 401.0, pid_alive=lambda _pid: False))
            self.assertEqual(len(processes), 1)

        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory)); roots = fixture.roots()
            key = profile_assistant._load_or_create_secret(roots.secret_path)
            roots.state_root.mkdir(exist_ok=True)
            claim = roots.state_root / "launch.claim"
            profile_assistant._write_signed_json(claim, {
                "schema": SCHEMA, "pid": 9999, "action_id": SETUP_ID,
                "operation": "install", "request": "dead.json", "launch_id": LAUNCH_ID,
                "created_at": 10.0,
            }, key)
            profile_assistant._status(roots.state_root / "status.json", key,
                                      state="launching", operation="install",
                                      action_id=SETUP_ID, launch_id=LAUNCH_ID,
                                      operation_id="", created_at=10.0)
            with patch.object(profile_assistant.sys, "frozen", True, create=True):
                self.assertTrue(profile_assistant.launch_profile_assistant(
                    SETUP_ID, "install", lambda: roots, lambda *args, **kwargs: Process(),
                    wall_clock=lambda: 311.0, pid_alive=lambda _pid: False))

    def test_main_preserves_manual_recovery_terminal_status(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory)); roots = fixture.roots()
            request_path = create_request(SETUP_ID, "install", lambda: roots)

            def require_manual(_request, _paths, **_dependencies):
                key = profile_assistant._load_or_create_secret(roots.secret_path)
                profile_assistant._status(
                    roots.state_root / "status.json", key,
                    state="manual_recovery_required", operation="install",
                    action_id=SETUP_ID, launch_id=request_path.stem,
                    operation_id="operation",
                    failure="Concurrent bytes were preserved")
                raise profile_assistant.ManualRecoveryRequired(
                    "Concurrent bytes were preserved")

            with patch.object(profile_assistant, "_execute_locked", side_effect=require_manual):
                with self.assertRaises(profile_assistant.ManualRecoveryRequired):
                    profile_assistant.profile_assistant_main(
                        [str(request_path)], roots_factory=lambda: roots)
            status = json.loads((roots.state_root / "status.json").read_text("utf-8"))
            self.assertEqual(status["state"], "manual_recovery_required")
            self.assertEqual(status["failure"], "Concurrent bytes were preserved")


if __name__ == "__main__":
    unittest.main()
