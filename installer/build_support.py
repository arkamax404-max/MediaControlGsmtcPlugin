import argparse, hashlib, json, re, shutil, stat, subprocess
from pathlib import Path, PurePosixPath

TREE_HASH = "78a5a8537bc4183a3461d3894c955b76e47a5268ca721282ccfe117a41556dcb"
EXE_HASH = "b851211d2617566375189cda1e41bbe4ce489fcb6efbe2cd6b0e9ce2917784d3"
LOCAL = re.compile(r'^[A-Z]:\\[^\x00-\x1f\\/:*?"<>|]+(?:\\[^\x00-\x1f\\/:*?"<>|]+)*$')
VERSION = re.compile(r'^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$')

def companion_version(repo):
    source = (Path(repo) / "d200_bridge" / "version.py").read_text("utf-8")
    matches = re.findall(r'^COMPANION_VERSION\s*=\s*["\']([^"\']+)["\']\s*$', source, re.MULTILINE)
    if len(matches) != 1 or not VERSION.fullmatch(matches[0]): raise ValueError("invalid companion version source")
    return matches[0]

def sha256(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def canonical(raw):
    parts = raw[3:].split("\\") if isinstance(raw, str) else []
    aliases = re.compile(r"^(con|prn|aux|nul|clock\$|com[1-9]|lpt[1-9])(\.|$)", re.I)
    if not LOCAL.fullmatch(raw or "") or any(part in (".", "..") or part.endswith((".", " ")) or aliases.match(part) for part in parts): raise ValueError("noncanonical path")
    return Path(raw)
def reject_reparse(path):
    current = path
    while True:
        if current.exists():
            data = current.lstat()
            if stat.S_ISLNK(data.st_mode) or getattr(data, "st_file_attributes", 0) & 0x400: raise ValueError("reparse path")
        if current.parent == current: break
        current = current.parent
def validate_roots(repo, roots, managed=()):
    repo, roots, managed = canonical(repo), [canonical(value) for value in roots], [canonical(value) for value in managed]
    if len({str(path).lower() for path in roots}) != len(roots): raise ValueError("equal roots")
    def within(left, right):
        try: left.relative_to(right); return True
        except ValueError: return False
    for root in [repo] + roots:
        if root.parts[1].lower() in {"$recycle.bin", "recovery", "system volume information"} or any(within(root, value) for value in managed): raise ValueError("system-managed root")
    for root in roots:
        if root.parent == root or within(root, repo) or within(repo, root): raise ValueError("unsafe root")
        reject_reparse(root)
    for index, left in enumerate(roots):
        for right in roots[index + 1:]:
            if within(left, right) or within(right, left): raise ValueError("overlapping roots")
    return roots
def generate_include(files, app_version):
    if not VERSION.fullmatch(app_version): raise ValueError("invalid app version")
    lines = []
    for value in sorted(files, key=str.lower):
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or any(part.lower() in {"tests", "plugin", "node_modules", "openspec", ".atl"} for part in path.parts): raise ValueError("unsafe bundle path")
        source = str(path).replace("/", "\\"); parent = str(path.parent).replace("/", "\\")
        destination = "{app}\\versions\\" + app_version + "\\bridge" + ("" if parent == "." else "\\" + parent)
        lines.append(f'Source: "{{#BundleRoot}}\\{source}"; DestDir: "{destination}"; Flags: ignoreversion')
    return "\n".join(lines) + "\n"
def enumerate_bundle(root):
    files = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix().lower()):
        data = path.lstat()
        if stat.S_ISLNK(data.st_mode) or getattr(data, "st_file_attributes", 0) & 0x400: raise ValueError("bundle reparse path")
        if not (stat.S_ISREG(data.st_mode) or stat.S_ISDIR(data.st_mode)): raise ValueError("nonregular bundle path")
        if path.is_file(): files.append({"path": path.relative_to(root).as_posix(), "size": data.st_size, "sha256": sha256(path)})
    digest = hashlib.sha256()
    for item in files: digest.update(item["path"].encode() + b"\0" + item["sha256"].encode() + b"\n")
    return {"files": files, "file_count": len(files), "total_size": sum(item["size"] for item in files), "tree_sha256": digest.hexdigest()}
def same_bundle(left, right): return all(left.get(key) == right.get(key) for key in ("files", "file_count", "total_size", "tree_sha256"))
def verify_bundle(root, expected):
    actual = enumerate_bundle(root)
    if not same_bundle(actual, expected): raise ValueError("bundle snapshot changed")
    return actual
def create_bundle_snapshot(source, destination, expected):
    destination.mkdir()
    for item in expected["files"]:
        source_file = source / PurePosixPath(item["path"]); target = destination / PurePosixPath(item["path"])
        data = source_file.lstat()
        if not stat.S_ISREG(data.st_mode) or getattr(data, "st_file_attributes", 0) & 0x400 or data.st_size != item["size"] or sha256(source_file) != item["sha256"]: raise ValueError("source changed during snapshot")
        target.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(source_file, target)
    return verify_bundle(destination, expected)
def source_snapshot(source_root, include):
    names = ("build_installer.ps1", "build_support.py", "companion.iss", "manage_companion.ps1", "tooling.lock.json")
    files = [{"path": name, "sha256": sha256(source_root / name)} for name in names]
    files.append({"path": "generated/bundle-files.iss", "sha256": sha256(include)})
    digest = hashlib.sha256()
    for item in files: digest.update(item["path"].encode() + b"\0" + item["sha256"].encode() + b"\n")
    return {"sha256": digest.hexdigest(), "files": files}
def build_receipt(companion_commit, snapshot, app_version, inno, installer_hash, installer_size, source_bundle, compiled_bundle):
    source_executable = next(item for item in source_bundle["files"] if item["path"] == "GSMTCD200Companion.exe")
    executable = next(item for item in compiled_bundle["files"] if item["path"] == "GSMTCD200Companion.exe")
    return {"schema_version": 3, "companion_source_commit": companion_commit,
        "installer_source_snapshot_sha256": snapshot["sha256"], "installer_source_files": snapshot["files"], "app_version": app_version,
        "inno_version": inno, "source_bundle_tree_sha256": source_bundle["tree_sha256"],
        "source_bundle_exe_sha256": source_executable["sha256"], "snapshot_bundle_tree_sha256": compiled_bundle["tree_sha256"], "snapshot_bundle_exe_sha256": executable["sha256"],
        "source_bundle_file_count": source_bundle["file_count"], "source_bundle_total_size": source_bundle["total_size"],
        "snapshot_bundle_file_count": compiled_bundle["file_count"], "snapshot_bundle_total_size": compiled_bundle["total_size"], "installer_sha256": installer_hash,
        "installer_size": installer_size, "signed": False}
def prepare(args):
    bundle, output, metadata = validate_roots(args.repo, [args.bundle, args.output, args.metadata], args.managed_root)
    for root in (output, metadata):
        if not root.parent.is_dir(): raise ValueError("missing output parent")
        root.mkdir(exist_ok=True)
        if any(root.iterdir()): raise ValueError("output root not empty")
    manifest = metadata / "bundle-manifest.json"; synthetic = metadata / "synthetic"
    result = subprocess.run([args.python, "-I", "-s", args.verifier, str(bundle), str(synthetic), str(manifest)], capture_output=True, text=True, timeout=180)
    if result.returncode: raise RuntimeError("bundle verification failed")
    bundle_data = json.loads(manifest.read_text("utf-8")); actual = enumerate_bundle(bundle)
    if not same_bundle(bundle_data, actual): raise ValueError("forged or stale bundle manifest")
    if actual["tree_sha256"] != TREE_HASH or actual["file_count"] != 85: raise ValueError("unexpected bundle identity")
    exe = next(item for item in actual["files"] if item["path"] == "GSMTCD200Companion.exe")
    if exe["sha256"] != EXE_HASH: raise ValueError("unexpected executable identity")
    compiled = create_bundle_snapshot(bundle, metadata / "bundle-snapshot", actual)
    (metadata / "bundle-snapshot-manifest.json").write_text(json.dumps(compiled, sort_keys=True, indent=2) + "\n", "utf-8")
    include = metadata / "bundle-files.iss"; include.write_text(generate_include((item["path"] for item in compiled["files"]), companion_version(args.repo)), "utf-8")
    (metadata / "installer-source-snapshot.json").write_text(json.dumps(source_snapshot(Path(args.source_root), include), sort_keys=True, indent=2) + "\n", "utf-8")
def receipt(args):
    source = json.loads(Path(args.bundle_manifest).read_text("utf-8")); compiled = json.loads(Path(args.snapshot_manifest).read_text("utf-8")); installer = Path(args.installer)
    verify_bundle(Path(args.snapshot_root), compiled)
    if not same_bundle(source, compiled) or compiled["tree_sha256"] != TREE_HASH or compiled["file_count"] != 85 or next(item for item in compiled["files"] if item["path"] == "GSMTCD200Companion.exe")["sha256"] != EXE_HASH: raise ValueError("unexpected compiled snapshot identity")
    app_version = companion_version(args.repo)
    if Path(args.include).read_text("utf-8") != generate_include((item["path"] for item in compiled["files"]), app_version): raise ValueError("generated include version mismatch")
    snapshot = json.loads(Path(args.source_snapshot).read_text("utf-8"))
    if snapshot != source_snapshot(Path(args.source_root), Path(args.include)): raise ValueError("installer source changed during build")
    data = build_receipt(args.companion_commit, snapshot, app_version, args.inno_version, sha256(installer), installer.stat().st_size, source, compiled)
    Path(args.output).write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", "utf-8")
def main():
    parser = argparse.ArgumentParser(); sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    for name in ("repo", "bundle", "output", "metadata", "python", "verifier", "source_root"): prep.add_argument("--" + name.replace("_", "-"), required=True)
    prep.add_argument("--managed-root", action="append", required=True)
    rec = sub.add_parser("receipt")
    for name in ("repo", "companion_commit", "source_snapshot", "source_root", "include", "inno_version", "installer", "bundle_manifest", "snapshot_manifest", "snapshot_root", "output"): rec.add_argument("--" + name.replace("_", "-"), required=True)
    args = parser.parse_args(); prepare(args) if args.command == "prepare" else receipt(args)
if __name__ == "__main__": main()
