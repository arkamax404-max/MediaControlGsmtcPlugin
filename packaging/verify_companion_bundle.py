import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


NAME = "GSMTCD200Companion"
FORBIDDEN = {"tests", "test", "__pycache__", "node_modules", "openspec", ".atl",
             ".env", "bridge-token", "companion.log"}
REQUIRED_NATIVE = ("PIL/_imaging.", "PIL/_webp.", "psutil/_psutil_windows.pyd",
                   "winrt/_winrt.", "winrt/_winrt_windows_foundation.",
                   "winrt/_winrt_windows_foundation_collections.",
                   "winrt/_winrt_windows_media_control.",
                   "winrt/_winrt_windows_storage_streams.",
                   "winrt/_winrt_windows_system.")
REQUIRED_ARCHIVE = ("comtypes", "pycaw", "PIL", "winrt.windows.media.control")
EXCLUDED_MODULES = ("ssl", "_ssl", "_hashlib", "pyexpat", "_elementtree", "xml.parsers.expat",
                    "lzma", "_lzma", "compression.zstd", "compression.zstd._zstdfile", "_zstd")
EXCLUDED_FILES = {"_ssl.pyd", "_hashlib.pyd", "pyexpat.pyd", "_elementtree.pyd",
                  "_lzma.pyd", "_zstd.pyd", "libssl-3.dll", "libcrypto-3.dll"}
NATIVE_NOTICE_KEYS = {"cpython-runtime", "pillow-codecs", "psutil-native", "pywinrt-native"}
def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
def inspect_tree(bundle):
    files = []
    for path in sorted(bundle.rglob("*"), key=lambda item: item.relative_to(bundle).as_posix().lower()):
        relative = path.relative_to(bundle).as_posix()
        metadata = path.lstat()
        attributes = getattr(metadata, "st_file_attributes", 0)
        if stat.S_ISLNK(metadata.st_mode) or attributes & 0x400:
            raise RuntimeError("Bundle contains link or reparse point")
        if any(part.lower() in FORBIDDEN for part in Path(relative).parts):
            raise RuntimeError("Bundle contains forbidden path")
        if path.is_file():
            if metadata.st_size > 100 * 1024 * 1024:
                raise RuntimeError("Bundle file exceeds bound")
            files.append({"path": relative, "size": metadata.st_size, "sha256": sha256(path)})
    return files
def tree_hash(files):
    digest = hashlib.sha256()
    for item in files:
        digest.update(item["path"].encode("utf-8") + b"\0")
        digest.update(item["sha256"].encode("ascii") + b"\n")
    return digest.hexdigest()
def verify_native(files):
    names = "\n".join(item["path"].lower() for item in files)
    missing = [name for name in REQUIRED_NATIVE if name.lower() not in names]
    if missing:
        raise RuntimeError("Missing native runtime files: " + ", ".join(missing))
    if any(name in names for name in ("_avif", "_imagingcms", "_imagingft", "_imagingtk")):
        raise RuntimeError("Unused Pillow native module collected")
    if any(Path(item["path"]).name.lower() in EXCLUDED_FILES for item in files):
        raise RuntimeError("Excluded native file collected")
def verify_metadata(internal, require_ready=True):
    manifest = json.loads((internal / "build-dependencies.json").read_text("utf-8"))
    notices = json.loads((internal / "third-party-notices.json").read_text("utf-8"))
    packages = manifest.get("packages", [])
    if re.search(r"[A-Za-z]:\\|/Users/|\\\\|https?://", json.dumps((manifest, notices))): raise RuntimeError("Provenance contains private source")
    roles = [item.get("role") for item in packages]
    if (require_ready and not manifest.get("release_ready")) or len(packages) != 18 or roles.count("runtime") != 11 or roles.count("build-only") != 6 or roles.count("build+bootloader") != 1:
        raise RuntimeError("Build provenance incomplete")
    required = {"name", "version", "wheel", "sha256", "role", "source", "licenses"}
    if any(set(item) != required or item["source"] != "locked-wheelhouse" or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) for item in packages):
        raise RuntimeError("Invalid package provenance")
    license_root = internal / "licenses"
    for filename, evidence in notices.get("licenses", {}).items():
        path = license_root / filename
        if not path.is_file() or sha256(path) != evidence.get("sha256"):
            raise RuntimeError("License evidence missing or changed")
    covered = notices.get("packages", {})
    native = notices.get("native_components", {})
    if (require_ready and not notices.get("release_ready")) or set(native) != NATIVE_NOTICE_KEYS or any(not value for value in native.values()) or any(not covered.get(item["name"]) for item in packages if item["role"] != "build-only"):
        raise RuntimeError("License coverage incomplete")
    proof = manifest.get("exclusion_proof")
    if require_ready and (proof != notices.get("exclusion_proof") or set(proof or {}) != {"excluded_modules_absent", "excluded_native_files_absent", "openssl_pe_imports_absent"} or not all(proof.values())): raise RuntimeError("Exclusion proof incomplete")
    return manifest, notices
def archive_listing(executable):
    command = [sys.executable, "-m", "PyInstaller.utils.cliutils.archive_viewer",
               "-r", "-l", str(executable)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError("Archive inspection failed")
    listing = result.stdout
    missing = [name for name in REQUIRED_ARCHIVE if name.lower() not in listing.lower()]
    if missing:
        raise RuntimeError("Missing archived modules: " + ", ".join(missing))
    verify_archive_modules(listing)
    return listing
def verify_archive_modules(listing):
    for name in EXCLUDED_MODULES:
        if re.search(rf"(?<![\w.]){re.escape(name)}(?![\w.])", listing):
            raise RuntimeError("Excluded module archived: " + name)
def verify_pe_imports(bundle, pe_module=None):
    if pe_module is None:
        import pefile as pe_module
    for path in bundle.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".exe", ".dll", ".pyd"}: continue
        try:
            pe = pe_module.PE(str(path), fast_load=True)
            pe.parse_data_directories(directories=[pe_module.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
            imports = [entry.dll.decode("ascii", "replace").lower() for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", [])]
            pe.close()
        except pe_module.PEFormatError: continue
        if any(name.startswith(("libssl", "libcrypto")) for name in imports): raise RuntimeError("OpenSSL PE import remains")
def finalize(bundle):
    internal = bundle / "_internal"
    manifest, notices = verify_metadata(internal, require_ready=False)
    files = inspect_tree(bundle); verify_native(files); archive_listing(bundle / f"{NAME}.exe"); verify_pe_imports(bundle)
    proof = {"excluded_modules_absent": True, "excluded_native_files_absent": True,
             "openssl_pe_imports_absent": True}
    manifest["release_ready"] = notices["release_ready"] = True
    manifest["exclusion_proof"] = notices["exclusion_proof"] = proof
    (internal / "build-dependencies.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", "utf-8")
    (internal / "third-party-notices.json").write_text(json.dumps(notices, sort_keys=True, indent=2) + "\n", "utf-8")


def frozen_diagnostics(executable, synthetic_root):
    empty_path = synthetic_root / "empty-path"
    empty_path.mkdir(parents=True, exist_ok=True)
    local_data = synthetic_root / "LocalData"
    local_data.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update({"LOCALAPPDATA": str(local_data), "PATH": str(empty_path),
                        "GSMTC_DIAGNOSTICS_FORCE_OFFLINE": "1", "PYTHONHASHSEED": "0"})
    result = subprocess.run([str(executable), "--diagnose"], env=environment,
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError("Frozen diagnostics failed")
    outputs = list((local_data / "GSMTCD200Controller" / "diagnostics").glob("*.zip"))
    if len(outputs) != 1 or not zipfile.is_zipfile(outputs[0]):
        raise RuntimeError("Frozen diagnostics ZIP missing")
    with zipfile.ZipFile(outputs[0]) as archive:
        if archive.namelist() != ["summary.json", "runtime.json", "dependencies.json", "logs.txt"]:
            raise RuntimeError("Frozen diagnostics schema mismatch")
        content = b"".join(archive.read(name) for name in archive.namelist())
        if len(content) > 1024 * 1024 or re.search(rb"Bearer\s+|data:image|[A-Za-z0-9_-]{43}", content):
            raise RuntimeError("Frozen diagnostics privacy check failed")


def frozen_import_probe(executable, synthetic_root):
    environment = os.environ.copy()
    environment.update({"LOCALAPPDATA": str(synthetic_root / "ProbeData"), "PATH": "",
                        "GSMTC_PACKAGING_IMPORT_PROBE": "1", "PYTHONHASHSEED": "0"})
    result = subprocess.run([str(executable)], env=environment,
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError("Frozen native import probe failed")


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--finalize": finalize(Path(sys.argv[2]).resolve()); return
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    parser.add_argument("synthetic_root", type=Path)
    parser.add_argument("manifest_out", type=Path)
    parser.add_argument("--compare", type=Path)
    args = parser.parse_args()
    bundle = args.bundle.resolve()
    executable = bundle / f"{NAME}.exe"
    internal = bundle / "_internal"
    if not executable.is_file() or executable.read_bytes()[:2] != b"MZ" or not internal.is_dir():
        raise RuntimeError("Invalid one-folder layout")
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md", "build-dependencies.json",
                 "third-party-notices.json"):
        if not (internal / name).is_file():
            raise RuntimeError("Missing bundled notice or manifest")
    build_manifest, _notices = verify_metadata(internal)
    serialized = json.dumps(build_manifest)
    if re.search(r"[A-Za-z]:\\|/Users/|\\\\", serialized):
        raise RuntimeError("Build manifest contains local path")
    files = inspect_tree(bundle)
    verify_native(files)
    archive_listing(executable)
    verify_pe_imports(bundle)
    frozen_import_probe(executable, args.synthetic_root.resolve())
    frozen_diagnostics(executable, args.synthetic_root.resolve())
    manifest = {"schema_version": 1, "file_count": len(files),
                "total_size": sum(item["size"] for item in files),
                "tree_sha256": tree_hash(files), "files": files}
    args.manifest_out.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", "utf-8")
    if args.compare and manifest != json.loads(args.compare.read_text("utf-8")):
        raise RuntimeError("Bundle manifests differ")
    print(json.dumps({key: manifest[key] for key in ("file_count", "total_size", "tree_sha256")},
                     sort_keys=True))


if __name__ == "__main__":
    main()
