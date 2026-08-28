import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).parents[1]
PACKAGING = ROOT / "packaging"


def read(name):
    return PACKAGING.joinpath(name).read_text("utf-8")


def load_module(name):
    path = PACKAGING / name
    spec = importlib.util.spec_from_file_location(name.replace(".", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PackagingContractTests(unittest.TestCase):
    def test_approved_plugin_uses_store_root_layout(self):
        preparer = load_module("prepare_ulanzi_spike.py")
        plugin = ROOT / preparer.PLUGIN_FOLDER
        self.assertTrue(plugin.is_dir())
        self.assertTrue((plugin / "manifest.json").is_file())
        self.assertFalse((ROOT / "plugin" / preparer.PLUGIN_FOLDER).exists())
        root_plugins = {path.name for path in ROOT.glob("*.ulanziPlugin") if path.is_dir()}
        self.assertEqual(
            root_plugins,
            {preparer.PLUGIN_FOLDER},
            f"Expected exactly the approved root plugin folder, found: {sorted(root_plugins)}",
        )

    def test_ulanzi_runtime_spec_is_isolated_and_import_complete(self):
        source = read("ulanzi_runtime.spec")
        lock = read("requirements-ulanzi-runtime.lock")
        bootstrap = read("requirements-ulanzi-bootstrap.lock")
        self.assertIn("mediacontrol_runtime.py", source)
        self.assertIn('collect_submodules("ulanzi_api")', source)
        self.assertIn('+ ["websocket"]', source)
        self.assertIn('"websocket.tests"', source)
        self.assertIn("MediaControlRuntime", source)
        self.assertNotIn("companion_entry.py", source)
        self.assertIn('pathex=[str(root), str(entry.parent)]', source)
        self.assertNotIn('"d200_bridge"', source)
        for destination in ("licenses/project", "licenses/cpython", "licenses/pyinstaller",
                            "licenses/plugin-common-python", "licenses/websocket-client"):
            self.assertIn(f'"{destination}"', source)
        self.assertIn("9158324b777dd1f1643a0a7107528ffc506984f7", lock)
        self.assertIn("c4d985d49295657e3e60a006f9e6d5e1757e4e03c6de9f0971d422266c1acf8c", lock)
        self.assertIn("setuptools==84.0.0", bootstrap)
        self.assertIn("51a52592b3b99e102b609654876bd65f19f999935166d1352678931132b0c670", bootstrap)

    def test_prepares_external_launcher_package_without_mutating_source(self):
        preparer = load_module("prepare_ulanzi_spike.py")
        plugin = ROOT / preparer.PLUGIN_FOLDER
        protected = ("manifest.json", "package.json", "package-lock.json", "src/app.js",
                     "src/plugin.js")
        protected_before = {name: (plugin / name).read_bytes() for name in protected}
        manifest_before = protected_before["manifest.json"]
        package_before = protected_before["package.json"]
        with tempfile.TemporaryDirectory() as runtime_dir, tempfile.TemporaryDirectory() as output_dir:
            runtime = Path(runtime_dir)
            for relative in preparer.REQUIRED_RUNTIME_FILES:
                path = runtime / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"runtime" if path.suffix == ".exe" else b"license")
            target = preparer.prepare_package(plugin, runtime, output_dir, ROOT)
            prepared = json.loads((target / "manifest.json").read_text("utf-8"))
            prepared_package = json.loads((target / "package.json").read_text("utf-8"))
            source_package = json.loads(package_before)
            self.assertEqual(prepared["CodePath"], "src/launcher.js")
            self.assertEqual(
                [action["UUID"] for action in prepared["Actions"]],
                [f"{prepared['UUID']}.{suffix}" for suffix in (
                    "nowplaying", "previous", "toggle", "next", "volume-up", "volume-down",
                    "mute-toggle", "progress", "artwork-top-left", "artwork-top-right",
                    "artwork-bottom-left", "artwork-bottom-right")],
            )
            self.assertEqual(len(prepared["Actions"]), 12)
            self.assertEqual([action["Name"] for action in prepared["Actions"]], [
                "Now Playing", "Previous", "Play/Pause", "Next", "Volume Up",
                "Volume Down", "Mute Toggle", "Track Progress", "Artwork Top Left",
                "Artwork Top Right", "Artwork Bottom Left", "Artwork Bottom Right",
            ])
            progress_index = next(
                index for index, action in enumerate(prepared["Actions"])
                if action["UUID"].endswith(".progress")
            )
            self.assertTrue(all("PropertyInspectorPath" not in action
                                for index, action in enumerate(prepared["Actions"])
                                if index != progress_index))
            self.assertEqual(prepared["Actions"][progress_index]["PropertyInspectorPath"],
                             preparer.PROPERTY_INSPECTOR_FILES[0])
            referenced_assets = {
                prepared["Icon"],
                prepared["CategoryIcon"],
                *preparer.RUNTIME_ASSET_FILES,
                *(action["Icon"] for action in prepared["Actions"]),
                *(state["Image"] for action in prepared["Actions"]
                  for state in action["States"]),
            }
            copied_assets = {
                "assets/" + path.relative_to(target / "assets").as_posix()
                for path in (target / "assets").rglob("*") if path.is_file()
            }
            self.assertEqual(copied_assets, referenced_assets)
            self.assertEqual({asset for asset in copied_assets if "artwork-" in asset},
                             {"assets/artwork-top-left.svg", "assets/artwork-top-right.svg",
                              "assets/artwork-bottom-left.svg",
                              "assets/artwork-bottom-right.svg"})
            self.assertEqual(copied_assets, {
                "assets/music.svg", "assets/offline.svg", "assets/previous.svg",
                "assets/play.svg", "assets/pause.svg", "assets/next.svg",
                "assets/volume-up.svg", "assets/volume-down.svg", "assets/mute.svg",
                "assets/unmute.svg", "assets/progress.svg", "assets/artwork-top-left.svg",
                "assets/artwork-top-right.svg", "assets/artwork-bottom-left.svg",
                "assets/artwork-bottom-right.svg",
            })
            self.assertEqual((plugin / "manifest.json").read_bytes(), manifest_before)
            self.assertEqual((plugin / "package.json").read_bytes(), package_before)
            self.assertEqual({name: (plugin / name).read_bytes() for name in protected},
                             protected_before)
            self.assertEqual(json.loads(manifest_before)["CodePath"], "src/app.js")
            self.assertEqual(prepared_package["name"], "media-control-for-d200")
            self.assertEqual(prepared_package["name"], source_package["name"])
            self.assertEqual(prepared_package["version"], source_package["version"])
            self.assertEqual(prepared_package["type"], "module")
            self.assertEqual(prepared_package["engines"], source_package["engines"])
            self.assertNotIn("dependencies", prepared_package)
            self.assertNotIn("devDependencies", prepared_package)
            self.assertNotIn("scripts", prepared_package)
            self.assertTrue((target / "src" / "launcher.js").is_file())
            self.assertTrue(all((target / "runtime" / name).is_file()
                                for name in preparer.REQUIRED_RUNTIME_FILES))
            self.assertFalse((target / "src" / "app.js").exists())
            inspector_files = {
                path.relative_to(target).as_posix()
                for root in (target / "property-inspector", target / "vendor")
                for path in root.rglob("*") if path.is_file()
            }
            self.assertEqual(inspector_files, set(
                (*preparer.PROPERTY_INSPECTOR_FILES,
                 *preparer.PROPERTY_INSPECTOR_VENDOR_FILES)
            ))
            html = (target / preparer.PROPERTY_INSPECTOR_FILES[0]).read_text("utf-8")
            sources = re.findall(r'<script(?:\s+type="module")?\s+src="([^"]+)"', html)
            parent = Path(preparer.PROPERTY_INSPECTOR_FILES[0]).parent
            self.assertEqual(
                {(target / parent / source).resolve().relative_to(target.resolve()).as_posix()
                 for source in sources}, inspector_files - {preparer.PROPERTY_INSPECTOR_FILES[0]},
            )
            self.assertFalse((target / "package-lock.json").exists())
            self.assertEqual(set(path.name for path in target.iterdir()),
                              {"assets", "manifest.json", "package.json", "property-inspector",
                               "runtime", "src", "vendor"})

    def test_external_launcher_package_rejects_missing_runtime_license(self):
        preparer = load_module("prepare_ulanzi_spike.py")
        plugin = ROOT / preparer.PLUGIN_FOLDER
        with tempfile.TemporaryDirectory() as runtime_dir, tempfile.TemporaryDirectory() as output_dir:
            runtime = Path(runtime_dir)
            (runtime / "MediaControlRuntime.exe").write_bytes(b"runtime")
            with self.assertRaisesRegex(ValueError, "_internal/licenses/project/LICENSE"):
                preparer.prepare_package(plugin, runtime, output_dir, ROOT)

    def test_spike_preparer_resolves_root_plugin_from_external_cwd(self):
        preparer = load_module("prepare_ulanzi_spike.py")
        with (tempfile.TemporaryDirectory() as runtime_dir,
              tempfile.TemporaryDirectory() as output_dir,
              tempfile.TemporaryDirectory() as cwd):
            runtime = Path(runtime_dir)
            for relative in preparer.REQUIRED_RUNTIME_FILES:
                path = runtime / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"runtime" if path.suffix == ".exe" else b"license")
            result = subprocess.run(
                [sys.executable, "-B", str(PACKAGING / "prepare_ulanzi_spike.py"),
                 "--runtime-bundle", str(runtime), "--output-root", output_dir],
                cwd=cwd, capture_output=True, text=True, timeout=10, check=True,
            )
            target = Path(result.stdout.strip())
            self.assertEqual(target.parent, Path(output_dir).resolve())
            self.assertEqual(target.name, preparer.PLUGIN_FOLDER)
            self.assertTrue((target / "package.json").is_file())

    def test_spec_applies_exact_exclusions(self):
        source = read("companion.spec")
        for name in ("ssl", "_ssl", "_hashlib", "pyexpat", "_elementtree",
                     "xml.parsers.expat", "lzma", "_lzma", "compression.zstd",
                     "compression.zstd._zstdfile", "_zstd"):
            self.assertIn(f'"{name}"', source)
        self.assertIn("a.pure =", source); self.assertIn("a.binaries =", source)

    def test_root_and_venv_validation_are_fail_closed(self):
        builder = load_module("build_metadata.py")
        repo = "D:\\Development\\Project"
        good = [f"C:\\Build\\r{i}" for i in range(7)]
        builder.validate_roots(repo, good)
        for roots in (["C:\\"], [repo], [repo + "\\out"], ["D:\\Development"],
                      good[:1] * 2, ["C:\\Build\\a", "C:\\Build\\a\\b"], ["relative"]):
            with self.assertRaises(ValueError): builder.validate_roots(repo, roots)
        lock = {"one": {"version": "1"}}
        builder.validate_environment("include-system-site-packages = false", True, True, False, True, {"one": "1", "pip": "2"}, lock)
        for values in (("include-system-site-packages = true", True, True, False, True, {"one": "1", "pip": "2"}),
                       ("include-system-site-packages = false", False, True, False, True, {"one": "1", "pip": "2"}),
                       ("include-system-site-packages = false", True, True, False, True, {"one": "1", "extra": "1", "pip": "2"}),
                       ("include-system-site-packages = false", True, True, False, True, {"one": "9", "pip": "2"})):
            with self.assertRaises(ValueError): builder.validate_environment(*values, lock)

    def test_verifier_hashes_only_relative_deterministic_files(self):
        verifier = load_module("verify_companion_bundle.py")
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            bundle.joinpath("_internal").mkdir()
            bundle.joinpath("a.bin").write_bytes(b"a")
            bundle.joinpath("_internal", "b.bin").write_bytes(b"b")
            first = verifier.inspect_tree(bundle)
            second = verifier.inspect_tree(bundle)
            self.assertEqual(first, second)
            self.assertEqual(verifier.tree_hash(first), verifier.tree_hash(second))
            self.assertTrue(all(not Path(item["path"]).is_absolute() for item in first))
            bundle.joinpath("tests").mkdir()
            bundle.joinpath("tests", "bad.py").write_text("bad", "utf-8")
            with self.assertRaises(RuntimeError):
                verifier.inspect_tree(bundle)

    def test_manifest_roles_privacy_and_license_evidence(self):
        verifier = load_module("verify_companion_bundle.py")
        with tempfile.TemporaryDirectory() as directory:
            internal = Path(directory); licenses = internal / "licenses"; licenses.mkdir()
            evidence = b"license"; (licenses / "e.txt").write_bytes(evidence)
            roles = ["runtime"] * 11 + ["build-only"] * 6 + ["build+bootloader"]
            packages = [{"name": f"p{i}", "version": "1", "wheel": f"p{i}.whl",
                         "sha256": "a" * 64, "role": role, "source": "locked-wheelhouse",
                         "licenses": [] if role == "build-only" else ["e.txt"]}
                        for i, role in enumerate(roles)]
            proof = {"excluded_modules_absent": True, "excluded_native_files_absent": True, "openssl_pe_imports_absent": True}
            (internal / "build-dependencies.json").write_text(json.dumps({"release_ready": True, "packages": packages, "exclusion_proof": proof}), "utf-8")
            covered = {p["name"]: p["licenses"] for p in packages if p["role"] != "build-only"}
            notices = {"release_ready": True, "exclusion_proof": proof, "licenses": {"e.txt": {"sha256": verifier.sha256(licenses / "e.txt"), "source": "locked-wheel"}}, "packages": covered, "native_components": {"cpython-runtime": ["e.txt"], "pillow-codecs": ["e.txt"], "psutil-native": ["e.txt"], "pywinrt-native": ["e.txt"]}}
            (internal / "third-party-notices.json").write_text(json.dumps(notices), "utf-8")
            verifier.verify_metadata(internal)
            notices["native_components"] = {}
            (internal / "third-party-notices.json").write_text(json.dumps(notices), "utf-8")
            with self.assertRaises(RuntimeError): verifier.verify_metadata(internal)
            notices["native_components"] = {"cpython-runtime": ["e.txt"], "pillow-codecs": ["e.txt"], "psutil-native": ["e.txt"], "pywinrt-native": ["e.txt"]}
            (internal / "third-party-notices.json").write_text(json.dumps(notices), "utf-8")
            (licenses / "e.txt").unlink()
            with self.assertRaises(RuntimeError): verifier.verify_metadata(internal)

    def test_exclusion_gates_reject_modules_files_and_openssl_imports(self):
        verifier = load_module("verify_companion_bundle.py")
        for module in verifier.EXCLUDED_MODULES:
            with self.assertRaises(RuntimeError): verifier.verify_archive_modules(f"'{module}',")
        required = [{"path": name, "size": 1, "sha256": "a" * 64}
                    for name in verifier.REQUIRED_NATIVE]
        for filename in verifier.EXCLUDED_FILES:
            with self.assertRaises(RuntimeError): verifier.verify_native(required + [{"path": filename, "size": 1, "sha256": "b" * 64}])
        class PE:
            DIRECTORY_ENTRY = {"IMAGE_DIRECTORY_ENTRY_IMPORT": 1}
            PEFormatError = ValueError
            class PE:
                def __init__(self, *_args, **_kwargs): self.DIRECTORY_ENTRY_IMPORT = [type("I", (), {"dll": b"libcrypto-3.dll"})()]
                def parse_data_directories(self, **_kwargs): pass
                def close(self): pass
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "bad.dll").write_bytes(b"MZ")
            with self.assertRaises(RuntimeError): verifier.verify_pe_imports(Path(directory), PE)

    def test_frozen_diagnostics_rejects_invalid_runtime_versions_before_manifest(self):
        verifier = load_module("verify_companion_bundle.py")
        entries = (("summary.json", b"{}"), ("dependencies.json", b"{}"), ("logs.txt", b""))
        expected_version = verifier.expected_companion_version()
        valid = {"companion_version": expected_version, "online": False,
                 "reason": "health_unreachable"}
        cases = (("matching", valid, None),
                 ("missing", {"online": False, "reason": "health_unreachable"},
                  "runtime schema mismatch"),
                 ("malformed", b"{", "runtime payload malformed"),
                 ("mismatched", {**valid, "companion_version": "0.0.0"},
                  "companion version mismatch"))
        for name, runtime, error in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                output = root / "LocalData" / "GSMTCD200Controller" / "diagnostics"
                output.mkdir(parents=True)
                with zipfile.ZipFile(output / "diagnostics.zip", "w") as archive:
                    archive.writestr("summary.json", b"{}")
                    archive.writestr("runtime.json", runtime if isinstance(runtime, bytes)
                                     else json.dumps(runtime).encode("utf-8"))
                    for entry, content in entries[1:]: archive.writestr(entry, content)
                with patch.object(verifier.subprocess, "run",
                                  return_value=type("Result", (), {"returncode": 0})()):
                    if error:
                        with self.assertRaisesRegex(RuntimeError, error):
                            verifier.frozen_diagnostics(Path("companion.exe"), root)
                    else:
                        verifier.frozen_diagnostics(Path("companion.exe"), root)

if __name__ == "__main__":
    unittest.main()
