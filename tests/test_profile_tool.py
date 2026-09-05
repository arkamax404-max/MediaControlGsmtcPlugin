import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = ROOT / "tools" / "profile_tool.py"
MANIFEST = ROOT / "com.arkamax404.mediacontrold200.ulanziPlugin" / "manifest.json"
spec = importlib.util.spec_from_file_location("profile_tool", TOOL_PATH)
profile_tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(profile_tool)


PACKAGE_ID = "10000000-0000-4000-8000-000000000000"
PAGE_A = "20000000-0000-4000-8000-000000000000"
PAGE_B = "30000000-0000-4000-8000-000000000000"
ACTION_A = "40000000-0000-4000-8000-000000000000"
ACTION_B = "50000000-0000-4000-8000-000000000000"


def source_profile(path: Path, center_action=profile_tool.BUILTIN_ACTION,
                   package_id=PACKAGE_ID, device_model="D200", reference=PAGE_A,
                   action_id=ACTION_A, extra_members=()):
    root = f"{package_id}.ulanziProfile"
    root_manifest = {
        "Name": "Source", "Device": {"UUID": "device", "Model": device_model},
        "Pages": {"Current": PAGE_A, "Pages": [PAGE_A, PAGE_B]},
    }
    center = {
        "Action": center_action, "ActionID": action_id,
        "ActionParam": {"SmallViewMode": 1}, "LinkedTitle": True,
        "Name": "Background setting", "Plugin": {}, "State": 0,
        "ViewParam": [{"Icon": "Images/background.png", "IconRel": "Images/background.png"}],
    }
    page_a = {"Name": "Page A", "Controllers": [{}, {"Actions": {"3_2": center}}]}
    page_b = {"Name": "Page B", "Controllers": [{"Actions": {
        "1_1": {"Action": "other", "ActionID": ACTION_B,
                "ActionParam": {"ProfileUUID": reference}},
    }}]}
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(f"{root}/manifest.json", json.dumps(root_manifest))
        archive.writestr(f"{root}/Profiles/{PAGE_A}/manifest.json", json.dumps(page_a))
        archive.writestr(f"{root}/Profiles/{PAGE_B}/manifest.json", json.dumps(page_b))
        archive.writestr(f"{root}/Images/background.png", b"asset")
        archive.writestr(f"{root}/Profiles/{PAGE_A}/Images/local.png", b"local-asset")
        for name, body in extra_members:
            archive.writestr(name, body)
    path.write_bytes(profile_tool.HEADER + payload.getvalue())


def identity(path: Path):
    _, archive = profile_tool.read_archive(path)
    return profile_tool.package_identity(archive)


class ProfileToolTests(unittest.TestCase):
    def clone(self, directory: str, seed="clone"):
        source = Path(directory) / "source.ulanziDeckProfile"
        output = Path(directory) / "clone.ulanziDeckProfile"
        source_profile(source)
        with contextlib.redirect_stdout(io.StringIO()):
            receipt = profile_tool.patch(
                source, output, PAGE_A, MANIFEST,
                uuid_factory=profile_tool.deterministic_uuid_factory(seed),
                running_check=lambda: False,
            )
        return source, output, receipt

    def test_clone_remaps_complete_identity_graph_and_patches_only_selected_center(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output, receipt = self.clone(directory)
            old_package, _, _, old_root, old_pages, old_docs = identity(source)
            new_package, _, _, new_root, new_pages, new_docs = identity(output)
            self.assertNotEqual(old_package, new_package)
            self.assertFalse(old_pages & new_pages)
            self.assertEqual(old_root["Device"], new_root["Device"])
            self.assertEqual(new_root["Name"], "Source Media Control")
            self.assertEqual(set(receipt["profile_id_map"].values()), new_pages)
            self.assertEqual(len(profile_tool.collect_key_values(new_docs.values(), "ActionID")), 2)
            references = profile_tool.collect_key_values(new_docs.values(), "ProfileUUID")
            self.assertEqual(references, [receipt["profile_id_map"][PAGE_A]])
            selected = [item for item in profile_tool.candidates(profile_tool.read_archive(output)[1])
                        if item["profile_id"] == receipt["clone_profile_id"]][0]
            manifest = json.loads(MANIFEST.read_text("utf-8"))
            self.assertEqual(selected["action"], manifest["UUID"] + ".largeitem-nowplaying")
            self.assertEqual(selected["entry"]["Plugin"]["Version"], manifest["Version"])
            self.assertEqual(receipt["validation"]["assets"], "byte-identical")
            self.assertEqual(receipt["validation"]["unrelated_semantics"], "preserved")

    def test_duplicate_source_action_ids_are_regenerated_uniquely(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.ulanziDeckProfile"
            output = Path(directory) / "clone.ulanziDeckProfile"
            source_profile(source, action_id=ACTION_B)
            with contextlib.redirect_stdout(io.StringIO()):
                profile_tool.patch(
                    source, output, PAGE_A, MANIFEST,
                    uuid_factory=profile_tool.deterministic_uuid_factory("duplicates"),
                    running_check=lambda: False,
                )
            _, _, _, _, _, documents = identity(output)
            action_ids = profile_tool.collect_key_values(documents.values(), "ActionID")
            self.assertEqual(len(action_ids), len(set(action_ids)))
            self.assertNotIn(ACTION_B, action_ids)

    def test_output_and_receipt_are_readable_hashed_and_deterministic(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            _, output_a, receipt_a = self.clone(first, "same")
            _, output_b, receipt_b = self.clone(second, "same")
            self.assertTrue(output_a.read_bytes().startswith(profile_tool.HEADER))
            self.assertEqual(receipt_a["profile_id_map"], receipt_b["profile_id_map"])
            self.assertEqual(receipt_a["clone_package_id"], receipt_b["clone_package_id"])
            stored = json.loads(Path(str(output_a) + ".receipt.json").read_text("utf-8"))
            self.assertEqual(stored["output_sha256"], profile_tool.sha256(output_a.read_bytes()))
            self.assertEqual(stored["plugin_manifest_sha256"],
                             profile_tool.sha256(MANIFEST.read_bytes()))

    def test_inspect_lists_candidate_without_modifying_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.ulanziDeckProfile"
            source_profile(source)
            before = source.read_bytes()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                profile_tool.inspect_profile(source)
            item = json.loads(output.getvalue())
            self.assertEqual((item["profile_id"], item["controller_index"]), (PAGE_A, 1))
            self.assertEqual(source.read_bytes(), before)

    def test_fails_closed_for_running_studio_wrong_action_paths_and_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.ulanziDeckProfile"
            output = Path(directory) / "clone.ulanziDeckProfile"
            source_profile(source)
            with self.assertRaisesRegex(profile_tool.ProfileError, "Close Ulanzi Studio"):
                profile_tool.patch(source, output, PAGE_A, MANIFEST, running_check=lambda: True)
            with self.assertRaisesRegex(profile_tool.ProfileError, "In-place"):
                profile_tool.patch(source, source, PAGE_A, MANIFEST, running_check=lambda: False)
            with self.assertRaisesRegex(profile_tool.ProfileError, "matched 0"):
                profile_tool.patch(source, output, str(uuid.uuid4()), MANIFEST,
                                   running_check=lambda: False)
            source_profile(source, center_action="third.party.action")
            with self.assertRaisesRegex(profile_tool.ProfileError, "not the built-in"):
                profile_tool.patch(source, output, PAGE_A, MANIFEST, running_check=lambda: False)
            bad_manifest = Path(directory) / "manifest.json"
            bad_manifest.write_text('{"UUID":"wrong","Version":"1","Name":"Wrong","Actions":[]}', "utf-8")
            with self.assertRaisesRegex(profile_tool.ProfileError, "Large Now Playing"):
                profile_tool.patch(source, output, PAGE_A, bad_manifest,
                                   running_check=lambda: False)

    def test_rejects_header_overwrite_unsafe_paths_device_and_invalid_identities(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.ulanziDeckProfile"
            output = Path(directory) / "clone.ulanziDeckProfile"
            source.write_bytes(b"PK bad")
            with self.assertRaisesRegex(profile_tool.ProfileError, "exact"):
                profile_tool.patch(source, output, PAGE_A, MANIFEST, running_check=lambda: False)
            source_profile(source)
            output.write_bytes(b"existing")
            with self.assertRaisesRegex(profile_tool.ProfileError, "already exists"):
                profile_tool.patch(source, output, PAGE_A, MANIFEST, running_check=lambda: False)
            output.unlink()
            source_profile(source, extra_members=(("../outside.txt", b"unsafe"),))
            with self.assertRaisesRegex(profile_tool.ProfileError, "Unsafe ZIP member"):
                profile_tool.patch(source, output, PAGE_A, MANIFEST, running_check=lambda: False)
            source_profile(source, device_model="Other")
            with self.assertRaisesRegex(profile_tool.ProfileError, "D200"):
                profile_tool.patch(source, output, PAGE_A, MANIFEST, running_check=lambda: False)
            source_profile(source, package_id="not-a-uuid")
            with self.assertRaisesRegex(profile_tool.ProfileError, "package UUID"):
                profile_tool.patch(source, output, PAGE_A, MANIFEST, running_check=lambda: False)
            source_profile(source, action_id="not-a-uuid")
            with self.assertRaisesRegex(profile_tool.ProfileError, "action UUID"):
                profile_tool.patch(source, output, PAGE_A, MANIFEST, running_check=lambda: False)
            source_profile(source, reference=str(uuid.uuid4()))
            with self.assertRaisesRegex(profile_tool.ProfileError, "Unresolved"):
                profile_tool.patch(source, output, PAGE_A, MANIFEST, running_check=lambda: False)

    def test_publish_race_never_overwrites_or_deletes_competing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.ulanziDeckProfile"
            output = Path(directory) / "clone.ulanziDeckProfile"
            source_profile(source)
            real_link = profile_tool.os.link

            def racing_link(source_path, destination):
                destination = Path(destination)
                if destination == output:
                    destination.write_bytes(b"created-by-other-process")
                return real_link(source_path, destination)

            profile_tool.os.link = racing_link
            try:
                with self.assertRaises(FileExistsError):
                    profile_tool.patch(source, output, PAGE_A, MANIFEST,
                                       running_check=lambda: False)
            finally:
                profile_tool.os.link = real_link
            self.assertEqual(output.read_bytes(), b"created-by-other-process")
            self.assertFalse(Path(str(output) + ".receipt.json").exists())


if __name__ == "__main__":
    unittest.main()
