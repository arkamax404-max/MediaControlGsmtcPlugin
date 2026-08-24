import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path


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

if __name__ == "__main__":
    unittest.main()
