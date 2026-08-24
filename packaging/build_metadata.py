import argparse, hashlib, json, os, re, shutil, site, sys, zipfile
from importlib import metadata
from pathlib import Path, PurePosixPath

def canonicalize_name(value): return re.sub(r"[-_.]+", "-", value).lower()

RUNTIME = {canonicalize_name(n) for n in ("comtypes", "Pillow", "psutil", "pycaw", "typing-extensions", "winrt-runtime", "winrt-Windows.Foundation", "winrt-Windows.Foundation.Collections", "winrt-Windows.Media.Control", "winrt-Windows.Storage.Streams", "winrt-Windows.System")}
BUILD = {canonicalize_name(n) for n in ("altgraph", "packaging", "pefile", "pyinstaller-hooks-contrib", "pywin32-ctypes", "setuptools")}
LOCAL = re.compile(r'^[A-Z]:\\[^\x00-\x1f\\/:*?"<>|]+(?:\\[^\x00-\x1f\\/:*?"<>|]+)*$')
LOCK = re.compile(r"([^=]+)==([^ ]+) --hash=sha256:([0-9a-f]{64})")

def digest(data): return hashlib.sha256(data).hexdigest()
def canonical(raw):
    if not LOCAL.fullmatch(raw) or any(p in (".", "..") for p in raw[3:].split("\\")): raise ValueError("noncanonical root")
    return Path(raw)
def validate_roots(repo, values):
    repo, roots = canonical(repo), [canonical(v) for v in values]
    if len(set(map(str.lower, map(str, roots)))) != len(roots): raise ValueError("equal roots")
    def within(a, b):
        try: a.relative_to(b); return True
        except ValueError: return False
    for root in roots:
        if root.parent == root or within(root, repo) or within(repo, root): raise ValueError("unsafe repository/root overlap")
    for i, left in enumerate(roots):
        for right in roots[i + 1:]:
            if within(left, right) or within(right, left): raise ValueError("overlapping roots")
    return roots
def parse_lock(path):
    result = {}
    for line in path.read_text("utf-8").splitlines():
        if not line or line.startswith("#"): continue
        match = LOCK.fullmatch(line)
        if not match: raise ValueError("invalid lock")
        result[canonicalize_name(match[1])] = {"name": match[1], "version": match[2], "sha256": match[3]}
    return result
def wheel_map(root):
    from packaging.utils import parse_wheel_filename
    files = list(root.iterdir())
    if any(not p.is_file() or p.suffix != ".whl" for p in files): raise ValueError("unexpected wheelhouse entry")
    result = {}
    for path in files:
        name, version, _build, _tags = parse_wheel_filename(path.name)
        result[canonicalize_name(name)] = (path, str(version), digest(path.read_bytes()))
    if len(result) != len(files): raise ValueError("duplicate wheel")
    return result
def validate_environment(config, isolated, no_user_site, user_site, prefix_diff, installed, lock):
    if "include-system-site-packages = false" not in config.lower() or not isolated or not no_user_site or user_site or not prefix_diff: raise ValueError("venv not isolated")
    expected = {name: item["version"] for name, item in lock.items()} | {"pip": installed.get("pip")}
    if installed != expected or not installed.get("pip"): raise ValueError("dirty or mismatched venv")
def validate_venv(lock):
    cfg = Path(sys.prefix) / "pyvenv.cfg"
    if not cfg.is_file(): raise ValueError("invalid venv config")
    installed = {canonicalize_name(d.metadata["Name"]): d.version for d in metadata.distributions()}
    validate_environment(cfg.read_text("utf-8"), sys.flags.isolated, sys.flags.no_user_site,
                         site.ENABLE_USER_SITE, sys.prefix != sys.base_prefix, installed, lock)
def license_members(wheel):
    with zipfile.ZipFile(wheel) as archive:
        return [n for n in archive.namelist() if any(k in PurePosixPath(n).name.lower() for k in ("license", "copying", "notice"))]
def prepare(args):
    lock, wheels = parse_lock(Path(args.lock)), wheel_map(Path(args.wheelhouse))
    if set(lock) != set(wheels): raise ValueError("wheelhouse mismatch")
    validate_venv(lock)
    out, licenses = Path(args.output), Path(args.output) / "licenses"
    out.mkdir(parents=True, exist_ok=True); licenses.mkdir()
    evidence = json.loads(Path(args.evidence_lock).read_text("utf-8"))["pywinrt"]
    external = Path(args.evidence_root) / evidence["filename"]
    if not external.is_file() or digest(external.read_bytes()) != evidence["sha256"]: raise ValueError("missing license evidence")
    refs, copied = {}, {}
    def store(key, data, source):
        filename = re.sub(r"[^a-z0-9_.-]", "_", key.lower()) + ".txt"
        target = licenses / filename; target.write_bytes(data)
        copied[filename] = {"sha256": digest(data), "source": source}; return filename
    pywinrt_license = store("pywinrt-3.2.1", external.read_bytes(), evidence["source"])
    packages = []
    for name, item in sorted(lock.items()):
        wheel, version, wheel_hash = wheels[name]
        if (version, wheel_hash) != (item["version"], item["sha256"]): raise ValueError("wheel provenance mismatch")
        role = "runtime" if name in RUNTIME else "build-only" if name in BUILD else "build+bootloader"
        package_refs = []
        if name in RUNTIME:
            if name.startswith("winrt-"): package_refs = [pywinrt_license]
            else:
                members = license_members(wheel)
                if not members: raise ValueError("missing wheel license")
                with zipfile.ZipFile(wheel) as archive:
                    package_refs = [store(f"{name}-{i}", archive.read(member), f"locked-wheel:{wheel.name}!{member}") for i, member in enumerate(members)]
                if name == "pillow" and not all(marker in b"\n".join((licenses / ref).read_bytes().lower() for ref in package_refs) for marker in (b"jpeg", b"libpng", b"zlib", b"webp", b"libtiff", b"openjpeg", b"lcms")): raise ValueError("incomplete Pillow codec evidence")
        if name == "pyinstaller":
            with zipfile.ZipFile(wheel) as archive:
                member = next((n for n in license_members(wheel) if n.endswith("COPYING.txt")), None)
                if not member: raise ValueError("missing bootloader license")
                data = archive.read(member)
                if b"bootloader" not in data.lower() or b"exception" not in data.lower(): raise ValueError("missing bootloader exception")
                package_refs = [store("pyinstaller-bootloader", data, f"locked-wheel:{wheel.name}!{member}")]
        packages.append({"name": name, "version": version, "wheel": wheel.name, "sha256": wheel_hash, "role": role, "source": "locked-wheelhouse", "licenses": package_refs})
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file(): raise ValueError("missing CPython license")
    python_ref = store("cpython-runtime", python_license.read_bytes(), "cpython-runtime:LICENSE.txt")
    native = {"cpython-runtime": [python_ref], "pillow-codecs": next(p["licenses"] for p in packages if p["name"] == "pillow"), "psutil-native": next(p["licenses"] for p in packages if p["name"] == "psutil"), "pywinrt-native": [pywinrt_license]}
    manifest = {"schema_version": 2, "release_ready": False, "python_version": sys.version.split()[0], "python_sha256": digest(Path(sys.executable).read_bytes()), "architecture": "AMD64", "source_date_epoch": args.epoch, "pyinstaller_version": metadata.version("PyInstaller"), "lock_sha256": digest(Path(args.lock).read_bytes()), "packages": packages}
    (out / "build-dependencies.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", "utf-8")
    (out / "third-party-notices.json").write_text(json.dumps({"schema_version": 1, "release_ready": False, "licenses": copied, "packages": refs | {p["name"]: p["licenses"] for p in packages if p["role"] != "build-only"}, "native_components": native}, sort_keys=True, indent=2) + "\n", "utf-8")
def normalize_zip(path):
    path, temporary = Path(path), Path(str(path) + ".normalized")
    with zipfile.ZipFile(path) as source: entries = [(name, source.read(name)) for name in sorted(source.namelist())]
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as target:
        for name, data in entries:
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0)); info.create_system = 3; info.external_attr = 0o100644 << 16; info.compress_type = zipfile.ZIP_DEFLATED
            target.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    os.replace(temporary, path)
def main():
    parser = argparse.ArgumentParser(); sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("roots"); check.add_argument("repo"); check.add_argument("roots", nargs="+")
    prep = sub.add_parser("prepare")
    for name in ("lock", "wheelhouse", "evidence_lock", "evidence_root", "output"): prep.add_argument("--" + name.replace("_", "-"), required=True)
    prep.add_argument("--epoch", type=int, required=True)
    norm = sub.add_parser("normalize-zip"); norm.add_argument("path"); args = parser.parse_args()
    validate_roots(args.repo, args.roots) if args.command == "roots" else normalize_zip(args.path) if args.command == "normalize-zip" else prepare(args)
if __name__ == "__main__": main()
