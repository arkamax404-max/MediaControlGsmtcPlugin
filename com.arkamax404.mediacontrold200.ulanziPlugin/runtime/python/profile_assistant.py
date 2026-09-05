"""Offline, fail-closed transaction core for managed ProfilesV2 edits."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import secrets
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from setup_action import BUILTIN_UUID, LARGEITEM_UUID, probe_setup_action
from largeitem_action import (largeitem_settings_payload,
                              normalize_largeitem_settings)


SCHEMA = "com.arkamax404.ulanzi.mediacontrol.profile-assistant/v2"
RECEIPT_SCHEMA = "com.arkamax404.ulanzi.mediacontrol.profile-assistant-receipt/v2"
BACKUP_SCHEMA = "com.arkamax404.ulanzi.mediacontrol.profile-assistant-backup/v1"
WIRE_REQUEST_KEYS = {"schema", "operation", "action_id", "nonce", "auth"}
REQUEST_KEYS = {
    "schema", "operation", "action_id", "launch_id", "profiles_root", "plugin_manifest",
    "backup_root", "state_root", "studio_executable", "wait_timeout_seconds",
}
OPERATIONS = {"install", "repair", "restore"}
MAX_REQUEST_BYTES = 64 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_FILES = 10_000
MAX_DIRECTORIES = 10_000
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_WAIT_SECONDS = 600.0
POLL_SECONDS = 0.1
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
MUTEX_NAME = "Local\\com.arkamax404.ulanzi.mediacontrol.ProfileAssistant"
LAUNCH_CLAIM_STALE_SECONDS = 300.0


@dataclass(frozen=True)
class ProductionRoots:
    request_root: Path
    backup_root: Path
    state_root: Path
    profiles_root: Path
    plugin_manifest: Path
    studio_executable: Path
    secret_path: Path


def production_roots() -> ProductionRoots:
    appdata = os.environ.get("APPDATA")
    local_appdata = os.environ.get("LOCALAPPDATA")
    program_files = os.environ.get("ProgramFiles(x86)")
    if not all((appdata, local_appdata, program_files)):
        raise ProfileAssistantError("Required Windows profile paths are unavailable")
    base = Path(local_appdata) / "GSMTCD200Controller"
    executable = Path(sys.executable)
    return ProductionRoots(
        base / "profile-assistant-requests",
        base / "profile-assistant-backups",
        base / "profile-assistant-state",
        Path(appdata) / "Ulanzi" / "UlanziDeck" / "ProfilesV2",
        executable.parent.parent / "manifest.json",
        Path(program_files) / "UlanziDeck" / "UlanziDeck.exe",
        base / "profile-assistant.key",
    )


class ProfileAssistantError(RuntimeError):
    """A request was rejected or its transaction could not be completed safely."""


class ManualRecoveryRequired(ProfileAssistantError):
    """The live manifest is divergent and must be preserved for manual resolution."""


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _load_or_create_secret(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    _safe_chain(path.parent)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | getattr(os, "O_BINARY", 0), 0o600)
    except FileExistsError:
        descriptor = None
    if descriptor is not None:
        try:
            value = secrets.token_bytes(32)
            written = 0
            while written < len(value):
                count = os.write(descriptor, value[written:])
                if count <= 0:
                    raise ProfileAssistantError("Profile Assistant secret could not be written")
                written += count
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            path.chmod(stat.S_IREAD | stat.S_IWRITE)
        except OSError:
            pass
        _fsync_directory(path.parent)
    _safe_existing(path, False)
    value = path.read_bytes()
    if len(value) != 32:
        raise ProfileAssistantError("Profile Assistant secret is invalid")
    return value


def _authenticate(value: Mapping[str, object], key: bytes) -> str:
    unsigned = {name: item for name, item in value.items() if name != "auth"}
    return hmac.new(key, _canonical_json(unsigned), hashlib.sha256).hexdigest()


def create_request(action_id: str, operation: str,
                   roots_factory: Callable[[], ProductionRoots] = production_roots,
                   launch_id: str | None = None) -> Path:
    action_id = _canonical_uuid(action_id, "action_id")
    if operation not in OPERATIONS:
        raise ProfileAssistantError("Request operation is invalid")
    roots = roots_factory()
    for path in (roots.request_root, roots.backup_root, roots.state_root):
        path.mkdir(parents=True, exist_ok=True)
        _safe_chain(path)
    key = _load_or_create_secret(roots.secret_path)
    nonce = (_canonical_uuid(launch_id, "launch_id") if launch_id is not None
             else str(uuid.uuid4()))
    request = {
        "schema": SCHEMA,
        "operation": operation,
        "action_id": action_id,
        "nonce": nonce,
    }
    request["auth"] = _authenticate(request, key)
    target = roots.request_root / f"{nonce}.json"
    _atomic_json(target, request)
    return target


def launch_profile_assistant(action_id: str, operation: str,
                             roots_factory: Callable[[], ProductionRoots] = production_roots,
                             popen: Callable[..., object] = subprocess.Popen,
                             wall_clock: Callable[[], float] = time.time,
                             pid_alive: Callable[[int], bool] = lambda pid: _process_id_alive(pid),
                             launch_id: str | None = None) -> bool:
    if not getattr(sys, "frozen", False):
        return False
    roots = roots_factory()
    launch_id = (_canonical_uuid(launch_id, "launch_id") if launch_id is not None
                 else str(uuid.uuid4()))
    roots.state_root.mkdir(parents=True, exist_ok=True)
    key = _load_or_create_secret(roots.secret_path)
    claim = roots.state_root / "launch.claim"
    try:
        descriptor = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            if not _stale_launch_claim(claim, key, wall_clock(), pid_alive):
                return False
            claim.unlink()
            descriptor = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except (OSError, ProfileAssistantError):
            return False
    os.close(descriptor)
    created_at = wall_clock()
    _write_signed_json(claim, {"schema": SCHEMA, "pid": 0, "action_id": action_id,
                               "operation": operation, "request": "pending",
                               "launch_id": launch_id, "created_at": created_at}, key)
    request_path = None
    launched = False
    try:
        request_path = create_request(action_id, operation, roots_factory, launch_id)
        _status(roots.state_root / "status.json", key, state="launching",
                operation=operation, action_id=action_id, operation_id="",
                launch_id=launch_id, created_at=created_at)
        process = popen(
            [sys.executable, "--profile-assistant", str(request_path)],
            cwd=str(Path(sys.executable).parent),
            shell=False,
            close_fds=True,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
        )
        launched = True
        pid = getattr(process, "pid", None)
        if isinstance(pid, int) and pid > 0 and claim.exists():
            _write_signed_json(claim, {"schema": SCHEMA, "pid": pid,
                                       "action_id": action_id, "operation": operation,
                                       "request": request_path.name,
                                       "launch_id": launch_id, "created_at": created_at}, key)
        return True
    except (OSError, ProfileAssistantError):
        if request_path is not None and not launched:
            request_path.unlink(missing_ok=True)
        if not launched:
            claim.unlink(missing_ok=True)
            try:
                _status(roots.state_root / "status.json", key, state="failed",
                        operation=operation, action_id=action_id, operation_id="",
                        launch_id=launch_id,
                        failure="Profile Assistant process could not be started")
            except Exception:
                pass
        return False


def _stale_launch_claim(path: Path, key: bytes, now: float,
                        pid_alive: Callable[[int], bool]) -> bool:
    try:
        info = path.lstat()
        value = _signed_json(path, "Launch claim", key)
        expected = {"schema", "pid", "action_id", "operation", "request", "launch_id",
                    "created_at", "auth"}
        created_at = value.get("created_at")
        pid = value.get("pid")
        if (set(value) != expected or value.get("schema") != SCHEMA
                or value.get("operation") not in OPERATIONS
                or not isinstance(created_at, (int, float)) or isinstance(created_at, bool)
                or created_at > now or now - float(created_at) < LAUNCH_CLAIM_STALE_SECONDS
                or not isinstance(pid, int) or pid < 0):
            return False
        _canonical_uuid(value.get("action_id"), "launch action_id")
        _canonical_uuid(value.get("launch_id"), "launch_id")
        return pid == 0 or not pid_alive(pid)
    except ProfileAssistantError:
        # Empty/truncated claims can only be retired after their filesystem age is bounded.
        try:
            return now >= info.st_mtime and now - info.st_mtime >= LAUNCH_CLAIM_STALE_SECONDS
        except (OSError, UnboundLocalError):
            return False


def _process_id_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_uuid(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ProfileAssistantError(f"{label} must be a canonical UUID")
    try:
        canonical = str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ProfileAssistantError(f"{label} must be a canonical UUID") from exc
    if value != canonical:
        raise ProfileAssistantError(f"{label} must be a canonical UUID")
    return canonical


def _is_reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _safe_existing(path: Path, directory: bool) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ProfileAssistantError(f"Required path is unavailable: {path}") from exc
    wanted = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not wanted or path.is_symlink() or _is_reparse(info):
        raise ProfileAssistantError(f"Links, reparse points, and special files are forbidden: {path}")


def _safe_chain(path: Path) -> None:
    current = path
    while True:
        _safe_existing(current, True)
        if current.parent == current:
            return
        current = current.parent


def _strict_path(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ProfileAssistantError(f"{label} must be an absolute path string")
    path = Path(value)
    if not path.is_absolute() or any(part == ".." for part in path.parts):
        raise ProfileAssistantError(f"{label} must be an absolute normalized path")
    return path


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _load_json_file(path: Path, limit: int, label: str) -> tuple[dict, bytes]:
    _safe_existing(path, False)
    try:
        size = path.stat().st_size
        if not 1 <= size <= limit:
            raise ProfileAssistantError(f"{label} size is invalid")
        data = path.read_bytes()
        value = json.loads(data.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProfileAssistantError(f"{label} is unavailable or invalid") from exc
    if not isinstance(value, dict):
        raise ProfileAssistantError(f"{label} must contain a JSON object")
    return value, data


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def load_request(request_path: Path, *, roots: ProductionRoots | None = None,
                 retire: bool = True) -> dict[str, object]:
    roots = roots or production_roots()
    request_path = _strict_path(str(request_path), "request path")
    expected_root = roots.request_root
    _safe_chain(expected_root)
    try:
        nonce = _canonical_uuid(request_path.stem, "request nonce")
    except ProfileAssistantError as exc:
        raise ProfileAssistantError("Request path is not an issued request") from exc
    expected = expected_root / f"{nonce}.json"
    if not _same_path(request_path, expected) or not _same_path(request_path.parent, expected_root):
        raise ProfileAssistantError("Request path is outside the exact request root")
    consumed = expected_root / f"{nonce}.used"
    try:
        descriptor = os.open(consumed, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        _fsync_directory(expected_root)
    except FileExistsError as exc:
        raise ProfileAssistantError("Request was already consumed") from exc
    claimed = expected_root / f"{nonce}.claimed"
    try:
        os.replace(request_path, claimed)
    except OSError as exc:
        raise ProfileAssistantError("Request was already claimed or is unavailable") from exc
    try:
        value, _ = _load_json_file(claimed, MAX_REQUEST_BYTES, "Request")
    except Exception:
        claimed.unlink(missing_ok=True)
        raise
    if set(value) != WIRE_REQUEST_KEYS or value.get("schema") != SCHEMA:
        claimed.unlink(missing_ok=True)
        raise ProfileAssistantError("Request schema or fields are invalid")
    if value.get("operation") not in OPERATIONS:
        claimed.unlink(missing_ok=True)
        raise ProfileAssistantError("Request operation is invalid")
    try:
        _canonical_uuid(value.get("action_id"), "action_id")
    except Exception:
        claimed.unlink(missing_ok=True)
        raise
    if value.get("nonce") != nonce:
        claimed.unlink(missing_ok=True)
        raise ProfileAssistantError("Request nonce does not match its file name")
    key = _load_or_create_secret(roots.secret_path)
    supplied = value.get("auth")
    if not isinstance(supplied, str) or not hmac.compare_digest(supplied, _authenticate(value, key)):
        claimed.unlink(missing_ok=True)
        raise ProfileAssistantError("Request authentication failed")
    operational = {
        "schema": SCHEMA, "operation": value["operation"], "action_id": value["action_id"],
        "launch_id": nonce,
        "profiles_root": str(roots.profiles_root),
        "plugin_manifest": str(roots.plugin_manifest),
        "backup_root": str(roots.backup_root), "state_root": str(roots.state_root),
        "studio_executable": str(roots.studio_executable),
        "wait_timeout_seconds": MAX_WAIT_SECONDS,
    }
    if retire:
        claimed.unlink(missing_ok=True)
    else:
        operational["_claimed_request"] = str(claimed)
    return operational


@contextmanager
def default_operation_lock(_state_root: Path):
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.ReleaseMutex.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if not handle:
            raise ProfileAssistantError("Profile Assistant mutex could not be created")
        acquired = False
        try:
            result = kernel32.WaitForSingleObject(handle, 0)
            if result not in (0, 0x80):
                raise ProfileAssistantError("Another Profile Assistant operation is active")
            acquired = True
            yield
        finally:
            if acquired:
                kernel32.ReleaseMutex(handle)
            kernel32.CloseHandle(handle)
        return
    lock_path = _state_root / "operation.lock"
    try:
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ProfileAssistantError("Another Profile Assistant operation is active") from exc
    try:
        yield
    finally:
        os.close(descriptor)
        lock_path.unlink(missing_ok=True)


def _validate_paths(request: Mapping[str, object]) -> dict[str, Path]:
    paths = {name: _strict_path(request[name], name) for name in (
        "profiles_root", "plugin_manifest", "backup_root", "state_root",
        "studio_executable")}
    profiles = paths["profiles_root"]
    if profiles.name != "ProfilesV2":
        raise ProfileAssistantError("profiles_root must name ProfilesV2")
    for name in ("profiles_root", "backup_root", "state_root"):
        _safe_chain(paths[name])
    manifest = paths["plugin_manifest"]
    _safe_existing(manifest, False)
    _safe_chain(manifest.parent)
    if manifest.name != "manifest.json" or not manifest.parent.name.endswith(".ulanziPlugin"):
        raise ProfileAssistantError("plugin_manifest must be an installed plugin manifest.json")
    executable = paths["studio_executable"]
    _safe_existing(executable, False)
    _safe_chain(executable.parent)
    if executable.name.casefold() != "ulanzideck.exe":
        raise ProfileAssistantError("studio_executable must name UlanziDeck.exe")
    for name in ("backup_root", "state_root", "plugin_manifest", "studio_executable"):
        if _inside(paths[name], profiles):
            raise ProfileAssistantError(f"{name} must be outside ProfilesV2")
    if _inside(paths["backup_root"], paths["state_root"]) or _inside(
            paths["state_root"], paths["backup_root"]):
        raise ProfileAssistantError("backup_root and state_root must be separate")
    return paths


def _plugin_contract(path: Path) -> tuple[dict[str, str], str]:
    manifest, data = _load_json_file(path, MAX_MANIFEST_BYTES, "Plugin manifest")
    plugin_uuid = manifest.get("UUID")
    version = manifest.get("Version")
    name = manifest.get("Name")
    action_uuid = f"{plugin_uuid}.largeitem-nowplaying"
    actions = manifest.get("Actions")
    matches = ([item for item in actions if isinstance(item, dict)
                and item.get("UUID") == action_uuid] if isinstance(actions, list) else [])
    if (not all(isinstance(item, str) and item for item in (plugin_uuid, version, name))
            or action_uuid != LARGEITEM_UUID or len(matches) != 1):
        raise ProfileAssistantError("Plugin manifest does not expose the exact Large Now Playing action")
    action_name = matches[0].get("Name")
    if not isinstance(action_name, str) or not action_name:
        raise ProfileAssistantError("Large Now Playing action name is invalid")
    return {"uuid": plugin_uuid, "version": version, "name": name,
            "action_uuid": action_uuid, "action_name": action_name}, _sha256(data)


def _new_entry(action_id: str, contract: Mapping[str, str]) -> dict[str, object]:
    name = contract["action_name"]
    return {
        "Action": contract["action_uuid"], "ActionID": action_id,
        "ActionParam": {"SmallViewMode": 2},
        "LinkedTitle": True, "Name": name,
        "Plugin": {"Name": contract["name"], "UUID": contract["uuid"],
                   "Version": contract["version"]},
        "State": 0, "ViewParam": [{"Icon": "", "IconRel": "", "Name": name}],
    }


def default_process_check(executable: Path) -> bool:
    try:
        completed = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {executable.name}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=2.0, check=False, shell=False,
            creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProfileAssistantError("Studio process state could not be determined") from exc
    if completed.returncode != 0:
        raise ProfileAssistantError("Studio process state could not be determined")
    return executable.name.casefold() in completed.stdout.casefold()


def default_relaunch(executable: Path) -> None:
    subprocess.Popen([str(executable)], cwd=str(executable.parent), shell=False,
                     close_fds=True)


def _wait_for_exit(executable: Path, timeout: float,
                   process_check: Callable[[Path], bool],
                   sleeper: Callable[[float], None], monotonic: Callable[[], float],
                   observed: list[bool] | None = None) -> bool:
    deadline = monotonic() + timeout
    observed_running = False
    while process_check(executable):
        observed_running = True
        if observed is not None:
            observed[0] = True
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise ProfileAssistantError("Timed out waiting for UlanziDeck.exe to exit")
        sleeper(min(POLL_SECONDS, remaining))
    return observed_running


def _inventory(root: Path) -> dict[str, object]:
    _safe_existing(root, True)
    directories: list[str] = ["."]
    files: list[dict[str, object]] = []
    total = 0
    seen: set[str] = set()
    stack = [root]
    while stack:
        directory = stack.pop()
        _safe_existing(directory, True)
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name.casefold())
        except OSError as exc:
            raise ProfileAssistantError("Package inventory could not be read") from exc
        local: set[str] = set()
        for child in children:
            folded = child.name.casefold()
            if folded in local:
                raise ProfileAssistantError("Package contains case-colliding entries")
            local.add(folded)
            path = Path(child.path)
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            if path.is_symlink() or _is_reparse(info):
                raise ProfileAssistantError("Package links and reparse points are forbidden")
            key = relative.casefold()
            if key in seen:
                raise ProfileAssistantError("Package contains colliding paths")
            seen.add(key)
            if stat.S_ISDIR(info.st_mode):
                directories.append(relative)
                if len(directories) > MAX_DIRECTORIES:
                    raise ProfileAssistantError("Package contains too many directories")
                stack.append(path)
            elif stat.S_ISREG(info.st_mode):
                if not 0 <= info.st_size <= MAX_FILE_BYTES:
                    raise ProfileAssistantError("Package file exceeds the size limit")
                total += info.st_size
                if total > MAX_TOTAL_BYTES or len(files) >= MAX_FILES:
                    raise ProfileAssistantError("Package exceeds inventory limits")
                data = path.read_bytes()
                if len(data) != info.st_size:
                    raise ProfileAssistantError("Package changed while inventory was read")
                files.append({"path": relative, "size": len(data), "sha256": _sha256(data)})
            else:
                raise ProfileAssistantError("Package contains a special file")
    directories.sort(key=str.casefold)
    files.sort(key=lambda item: str(item["path"]).casefold())
    return {"directories": directories, "files": files, "total_bytes": total}


def _copy_backup(package: Path, backup_root: Path, operation_id: str,
                 key: bytes) -> tuple[Path, dict[str, object], str]:
    source = _inventory(package)
    operation_root = backup_root / operation_id
    try:
        operation_root.mkdir()
        _fsync_directory(backup_root, required=True)
        destination = operation_root / package.name
        destination.mkdir()
        _fsync_directory(operation_root, required=True)
        for relative in source["directories"]:
            if relative != ".":
                created = destination / str(relative)
                created.mkdir()
                _fsync_directory(created.parent, required=True)
        for item in source["files"]:
            relative = str(item["path"])
            source_path = package / relative
            target_path = destination / relative
            data = source_path.read_bytes()
            if len(data) != item["size"] or _sha256(data) != item["sha256"]:
                raise ProfileAssistantError("Package changed during backup")
            with target_path.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        for relative in sorted(source["directories"], key=lambda value: value.count("/"),
                               reverse=True):
            directory = destination if relative == "." else destination / str(relative)
            _fsync_directory(directory, required=True)
        source_after = _inventory(package)
        copied = _inventory(destination)
        if source_after != source or copied != source:
            raise ProfileAssistantError("Backup inventory does not match the package")
        inventory_hash = _sha256(_canonical_json(source))
        marker_value = {
            "schema": BACKUP_SCHEMA, "operation_id": operation_id,
            "package_id": package.name.removesuffix(".ulanziProfile"),
            "inventory_sha256": inventory_hash, "inventory": source,
        }
        marker_value["auth"] = _authenticate(marker_value, key)
        marker = operation_root / "complete.json"
        _atomic_json(marker, marker_value)
        _fsync_directory(operation_root, required=True)
        verified, _ = _load_json_file(marker, MAX_MANIFEST_BYTES, "Backup marker")
        if verified != marker_value:
            raise ProfileAssistantError("Backup completion marker verification failed")
        for item in copied["files"]:
            (destination / str(item["path"])).chmod(stat.S_IREAD)
        marker.chmod(stat.S_IREAD)
        return destination, source, inventory_hash
    except Exception:
        # An incomplete exclusive operation directory is intentionally retained as evidence.
        raise


def _atomic_bytes(path: Path, data: bytes,
                  pre_replace: Callable[[], None] | None = None) -> None:
    _safe_existing(path.parent, True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if pre_replace is not None:
            pre_replace()
        _durable_replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _durable_replace(source: Path, destination: Path) -> None:
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.MoveFileExW.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p,
                                         ctypes.c_uint32)
        kernel32.MoveFileExW.restype = ctypes.c_int
        # REPLACE_EXISTING | WRITE_THROUGH makes publication atomic and durable.
        if not kernel32.MoveFileExW(str(source), str(destination), 0x1 | 0x8):
            raise OSError(ctypes.get_last_error(), "Durable atomic replace failed")
        return
    os.replace(source, destination)
    _fsync_directory(destination.parent)


def _fsync_directory(path: Path, required: bool = False) -> None:
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32,
                                         ctypes.c_uint32, ctypes.c_void_p,
                                         ctypes.c_uint32, ctypes.c_uint32,
                                         ctypes.c_void_p)
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.FlushFileBuffers.argtypes = (ctypes.c_void_p,)
        kernel32.FlushFileBuffers.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel32.CreateFileW(str(path), 0, 0x1 | 0x2 | 0x4, None,
                                      3, 0x02000000, None)
        invalid = ctypes.c_void_p(-1).value
        if handle == invalid:
            if required:
                raise ProfileAssistantError("Backup directory could not be opened for fsync")
            return
        try:
            if not kernel32.FlushFileBuffers(handle) and required:
                error = ctypes.get_last_error()
                # Windows commonly rejects FlushFileBuffers for directory handles.
                # Every payload is fsynced and the final marker uses WRITE_THROUGH,
                # which is the supported durability barrier for their directory entries.
                if error not in (5, 6):  # ERROR_ACCESS_DENIED / ERROR_INVALID_HANDLE
                    raise ProfileAssistantError("Backup directory fsync failed")
        finally:
            kernel32.CloseHandle(handle)
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        # The file itself is fsynced; some filesystems do not permit directory fsync.
        if required:
            raise ProfileAssistantError("Backup directory fsync failed") from exc


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    _atomic_bytes(path, data)
    readback, raw = _load_json_file(path, MAX_MANIFEST_BYTES, path.name)
    if raw != data or readback != value:
        raise ProfileAssistantError(f"Atomic metadata readback failed: {path.name}")


def _write_signed_json(path: Path, value: Mapping[str, object], key: bytes) -> dict[str, object]:
    signed = copy.deepcopy(dict(value))
    signed["auth"] = _authenticate(signed, key)
    _atomic_json(path, signed)
    return signed


def _signed_json(path: Path, label: str, key: bytes) -> dict[str, object]:
    value, _ = _load_json_file(path, MAX_MANIFEST_BYTES, label)
    supplied = value.get("auth")
    if not isinstance(supplied, str) or not hmac.compare_digest(supplied, _authenticate(value, key)):
        raise ProfileAssistantError(f"{label} authentication failed")
    return value


def _valid_inventory(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"directories", "files", "total_bytes"}:
        return False
    directories, files, total = value["directories"], value["files"], value["total_bytes"]
    if (not isinstance(directories, list) or directories[:1] != ["."]
            or not all(isinstance(item, str) and item for item in directories)
            or not isinstance(files, list) or not isinstance(total, int) or total < 0):
        return False
    size_total = 0
    for item in files:
        if (not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}
                or not isinstance(item["path"], str) or not item["path"]
                or not isinstance(item["size"], int) or item["size"] < 0
                or not isinstance(item["sha256"], str) or len(item["sha256"]) != 64):
            return False
        size_total += item["size"]
    return size_total == total


def _validate_backup(metadata: Mapping[str, object], paths: Mapping[str, Path],
                     key: bytes) -> tuple[Path, dict[str, object]]:
    operation_id = _canonical_uuid(metadata.get("operation_id"), "operation_id")
    target = metadata.get("target")
    if not isinstance(target, dict):
        raise ProfileAssistantError("Receipt target is invalid")
    package_id = _canonical_uuid(target.get("package_id"), "package_id")
    backup = paths["backup_root"] / operation_id / f"{package_id}.ulanziProfile"
    if metadata.get("backup_path") != str(backup):
        raise ProfileAssistantError("Receipt backup path is not authoritative")
    marker = _signed_json(backup.parent / "complete.json", "Backup marker", key)
    expected_marker_keys = {"schema", "operation_id", "package_id", "inventory_sha256",
                            "inventory", "auth"}
    if (set(marker) != expected_marker_keys or marker.get("schema") != BACKUP_SCHEMA
            or marker.get("operation_id") != operation_id or marker.get("package_id") != package_id
            or not _valid_inventory(marker.get("inventory"))):
        raise ProfileAssistantError("Backup completion marker schema is invalid")
    inventory = marker["inventory"]
    inventory_hash = _sha256(_canonical_json(inventory))
    if (marker.get("inventory_sha256") != inventory_hash
            or metadata.get("backup_inventory_sha256") != inventory_hash
            or metadata.get("backup_inventory") != inventory
            or _inventory(backup) != inventory):
        raise ProfileAssistantError("Receipt is not bound to the immutable backup inventory")
    return backup, inventory


def _valid_json_value(value: object, depth: int = 0) -> bool:
    if depth > 20:
        return False
    if value is None or isinstance(value, (bool, int, float, str)):
        return not isinstance(value, float) or value == value
    if isinstance(value, list):
        return len(value) <= 1000 and all(_valid_json_value(item, depth + 1) for item in value)
    if isinstance(value, dict):
        return (len(value) <= 1000 and all(isinstance(key, str) and key for key in value)
                and all(_valid_json_value(item, depth + 1) for item in value.values()))
    return False


def _validate_entry(entry: object, action: str, label: str) -> dict[str, object]:
    if (not isinstance(entry, dict) or entry.get("Action") != action
            or not _valid_json_value(entry)):
        raise ProfileAssistantError(f"{label} entry schema is invalid")
    _canonical_uuid(entry.get("ActionID"), f"{label} ActionID")
    return entry


def _validate_installed_entry(entry: object) -> dict[str, object]:
    value = _validate_entry(entry, LARGEITEM_UUID, "installed")
    if set(value) != {"Action", "ActionID", "ActionParam", "LinkedTitle", "Name",
                      "Plugin", "State", "ViewParam"}:
        raise ProfileAssistantError("Installed receipt entry schema is invalid")
    plugin = value.get("Plugin")
    view = value.get("ViewParam")
    if (value.get("ActionParam") not in ({}, {"SmallViewMode": 2})
            or value.get("LinkedTitle") is not True
            or not isinstance(value.get("Name"), str) or not value["Name"]
            or value.get("State") != 0 or not isinstance(plugin, dict)
            or set(plugin) != {"Name", "UUID", "Version"}
            or plugin.get("UUID") != LARGEITEM_UUID.removesuffix(".largeitem-nowplaying")
            or not all(isinstance(plugin.get(name), str) and plugin[name]
                       for name in ("Name", "UUID", "Version"))
            or not isinstance(view, list) or len(view) != 1
            or not isinstance(view[0], dict)
            or set(view[0]) != {"Icon", "IconRel", "Name"}
            or view[0].get("Icon") != "" or view[0].get("IconRel") != ""
            or view[0].get("Name") != value["Name"]):
        raise ProfileAssistantError("Installed receipt entry schema is invalid")
    return value


def _installed_equivalent(current: object, installed: Mapping[str, object]) -> bool:
    if not isinstance(current, dict) or set(current) != set(installed):
        return False
    if current.get("Action") != LARGEITEM_UUID or current.get("ActionID") != installed.get("ActionID"):
        return False
    plugin = current.get("Plugin")
    if plugin not in ({}, installed.get("Plugin")):
        return False
    parameters = current.get("ActionParam")
    if parameters not in ({}, {"SmallViewMode": 2}):
        if not isinstance(parameters, dict):
            return False
        canonical = largeitem_settings_payload(normalize_largeitem_settings(parameters))
        legacy_canonical = dict(canonical)
        legacy_canonical.pop("SmallViewMode", None)
        if parameters not in (canonical, legacy_canonical):
            return False
    normalized = copy.deepcopy(current)
    normalized["Plugin"] = copy.deepcopy(installed["Plugin"])
    normalized["ActionParam"] = copy.deepcopy(installed["ActionParam"])
    return normalized == installed


def _target(document: Mapping[str, object], controller_index: int) -> tuple[dict, dict]:
    controllers = document.get("Controllers")
    if not isinstance(controllers, list) or not 0 <= controller_index < len(controllers):
        raise ProfileAssistantError("Located controller no longer exists")
    controller = controllers[controller_index]
    actions = controller.get("Actions") if isinstance(controller, dict) else None
    center = actions.get("3_2") if isinstance(actions, dict) else None
    if not isinstance(actions, dict) or not isinstance(center, dict):
        raise ProfileAssistantError("Located center action no longer exists")
    return actions, center


def _hash_field(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ProfileAssistantError(f"{label} is invalid")
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise ProfileAssistantError(f"{label} is invalid") from exc
    return value


def _validate_metadata(value: Mapping[str, object], paths: Mapping[str, Path],
                       key: bytes) -> tuple[Path, bytes, dict[str, object], dict[str, object]]:
    allowed = {
        "schema", "state", "operation", "operation_id", "target", "trigger",
        "plugin_manifest_sha256", "original_manifest_sha256", "result_manifest_sha256",
        "original_entry", "installed_entry", "backup_path", "backup_inventory",
        "backup_inventory_sha256", "rollback_entry", "failure", "relaunch_failure", "auth",
    }
    required = allowed - {"rollback_entry", "failure", "relaunch_failure"}
    if (not required.issubset(value) or not set(value).issubset(allowed)
            or value.get("schema") != RECEIPT_SCHEMA
            or value.get("state") not in {"prepared", "succeeded", "rolled_back"}
            or value.get("operation") not in OPERATIONS):
        raise ProfileAssistantError("Receipt schema is invalid")
    _canonical_uuid(value.get("operation_id"), "operation_id")
    _hash_field(value.get("original_manifest_sha256"), "original manifest hash")
    _hash_field(value.get("result_manifest_sha256"), "result manifest hash")
    _hash_field(value.get("plugin_manifest_sha256"), "plugin manifest hash")
    target = value.get("target")
    trigger = value.get("trigger")
    if (not isinstance(target, dict)
            or set(target) != {"package_id", "page_id", "controller_index", "key"}
            or not isinstance(target.get("controller_index"), int)
            or target.get("controller_index") < 0 or target.get("key") != "3_2"
            or not isinstance(trigger, dict)
            or set(trigger) != {"action_id", "setup_key", "launch_id"}
            or not isinstance(trigger.get("setup_key"), str) or not trigger["setup_key"]):
        raise ProfileAssistantError("Receipt target or trigger schema is invalid")
    package_id = _canonical_uuid(target.get("package_id"), "package_id")
    page_id = _canonical_uuid(target.get("page_id"), "page_id")
    _canonical_uuid(trigger.get("action_id"), "trigger action_id")
    _canonical_uuid(trigger.get("launch_id"), "launch_id")
    installed = _validate_installed_entry(value.get("installed_entry"))
    original = _validate_entry(value.get("original_entry"), BUILTIN_UUID, "original")
    backup, _inventory_value = _validate_backup(value, paths, key)
    backed_manifest = backup / "Profiles" / page_id / "manifest.json"
    backed_document, backed_bytes = _load_json_file(
        backed_manifest, MAX_MANIFEST_BYTES, "Backed target manifest")
    _, backed_entry = _target(backed_document, target["controller_index"])
    rollback_entry = value.get("rollback_entry", original)
    if rollback_entry is not original:
        if (not isinstance(rollback_entry, dict) or not _valid_json_value(rollback_entry)
                or rollback_entry.get("Action") not in {BUILTIN_UUID, LARGEITEM_UUID}):
            raise ProfileAssistantError("Transaction rollback entry schema is invalid")
        _canonical_uuid(rollback_entry.get("ActionID"), "rollback ActionID")
    if (backed_entry != rollback_entry
            or _sha256(backed_bytes) != value["original_manifest_sha256"]):
        raise ProfileAssistantError("Transaction backup is not authoritative rollback data")
    expected_backup = paths["backup_root"] / str(value["operation_id"]) / f"{package_id}.ulanziProfile"
    if not _same_path(backup, expected_backup):
        raise ProfileAssistantError("Receipt backup location is invalid")
    return backed_manifest, backed_bytes, original, installed


def _status(path: Path, key: bytes, **fields: object) -> None:
    _write_signed_json(path, {"schema": SCHEMA, **fields}, key)


def _authority_receipt(paths: Mapping[str, Path], key: bytes) -> dict[str, object]:
    receipt = _signed_json(paths["state_root"] / "receipt.json", "Receipt", key)
    if "rollback_entry" in receipt or receipt.get("state") != "succeeded":
        raise ProfileAssistantError("Authoritative receipt is not a succeeded install lineage")
    _validate_metadata(receipt, paths, key)
    return receipt


def _terminal_receipt(journal: Mapping[str, object], paths: Mapping[str, Path],
                      key: bytes, final_state: str) -> dict[str, object]:
    operation = journal["operation"]
    if operation == "install":
        receipt = {name: copy.deepcopy(item) for name, item in journal.items()
                   if name not in {"auth", "rollback_entry", "failure", "relaunch_failure"}}
        receipt["state"] = final_state
        if final_state != "succeeded":
            receipt["failure"] = str(journal.get("failure") or "Install rolled back")
        return receipt
    receipt = _authority_receipt(paths, key)
    receipt.pop("auth", None)
    if operation == "repair" and final_state == "succeeded":
        receipt["installed_entry"] = copy.deepcopy(journal["installed_entry"])
        receipt["plugin_manifest_sha256"] = journal["plugin_manifest_sha256"]
        receipt["result_manifest_sha256"] = journal["result_manifest_sha256"]
    return receipt


def _commit_terminal_metadata(journal_path: Path, journal: dict[str, object],
                              paths: Mapping[str, Path], key: bytes,
                              final_state: str, recovered: bool = False,
                              request_identity: Mapping[str, str] | None = None) -> None:
    journal["state"] = final_state
    receipt = _terminal_receipt(journal, paths, key, final_state)
    _write_signed_json(paths["state_root"] / "receipt.json", receipt, key)
    identity = {
        "operation": str(journal["operation"]),
        "action_id": str(journal["trigger"]["action_id"]),
        "launch_id": str(journal["trigger"]["launch_id"]),
    }
    status = {
        "state": final_state, "operation": identity["operation"],
        "operation_id": journal["operation_id"], "action_id": identity["action_id"],
        "launch_id": identity["launch_id"],
        "original_manifest_sha256": journal["original_manifest_sha256"],
        "result_manifest_sha256": journal["result_manifest_sha256"],
        "plugin_manifest_sha256": journal["plugin_manifest_sha256"], "recovered": recovered,
    }
    if request_identity is not None and dict(request_identity) != identity:
        status.update({
            "request_action_id": request_identity["action_id"],
            "request_operation": request_identity["operation"],
            "request_launch_id": request_identity["launch_id"],
            "request_disposition": "recovery_only",
        })
    _status(paths["state_root"] / "status.json", key, **status)
    journal.pop("auth", None)
    _write_signed_json(journal_path, journal, key)


def _manual_recovery_status(paths: Mapping[str, Path], key: bytes,
                            journal: Mapping[str, object], failure: str,
                            request_identity: Mapping[str, str] | None = None) -> None:
    identity = {
        "operation": str(journal["operation"]),
        "action_id": str(journal["trigger"]["action_id"]),
        "launch_id": str(journal["trigger"]["launch_id"]),
    }
    status = {
        "state": "manual_recovery_required", "operation": identity["operation"],
        "operation_id": journal["operation_id"], "action_id": identity["action_id"],
        "failure": failure, "launch_id": identity["launch_id"],
        "original_manifest_sha256": journal["original_manifest_sha256"],
        "result_manifest_sha256": journal["result_manifest_sha256"],
        "plugin_manifest_sha256": journal["plugin_manifest_sha256"],
    }
    if request_identity is not None and dict(request_identity) != identity:
        status.update({
            "request_action_id": request_identity["action_id"],
            "request_operation": request_identity["operation"],
            "request_launch_id": request_identity["launch_id"],
            "request_disposition": "recovery_only",
        })
    _status(paths["state_root"] / "status.json", key, **status)


def _recover_prepared(paths: Mapping[str, Path], key: bytes,
                      process_check: Callable[[Path], bool],
                      request_identity: Mapping[str, str]) -> dict[str, object] | None:
    recovered = None
    for journal_path in sorted(paths["state_root"].glob("journal-*.json")):
        journal = _signed_json(journal_path, "Journal", key)
        if journal.get("state") != "prepared":
            continue
        _backed_manifest, original_bytes, _original, _installed = _validate_metadata(
            journal, paths, key)
        target = journal["target"]
        live_manifest = (paths["profiles_root"] /
                         f"{target['package_id']}.ulanziProfile" / "Profiles" /
                         str(target["page_id"]) / "manifest.json")
        _safe_existing(live_manifest, False)
        current = live_manifest.read_bytes()
        current_hash = _sha256(current)
        if current_hash == journal["result_manifest_sha256"]:
            final_state = "succeeded"
        elif current_hash == journal["original_manifest_sha256"]:
            final_state = "rolled_back"
        else:
            failure = "Live manifest hash conflicts with the prepared transaction; manual recovery required"
            _manual_recovery_status(paths, key, journal, failure, request_identity)
            raise ManualRecoveryRequired(failure)
        _commit_terminal_metadata(journal_path, journal, paths, key, final_state,
                                  recovered=True, request_identity=request_identity)
        recovered = {"state": final_state, "operation": journal["operation"],
                     "operation_id": journal["operation_id"], "recovered": True,
                     "requested_operation": request_identity["operation"],
                     "requested_operation_executed": False}
    return recovered


def execute_request(request: Mapping[str, object], *,
                    process_check: Callable[[Path], bool] = default_process_check,
                    sleeper: Callable[[float], None] = time.sleep,
                    monotonic: Callable[[], float] = time.monotonic,
                    relaunch: Callable[[Path], None] = default_relaunch,
                    uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
                    readback: Callable[[Path], bytes] | None = None,
                    lock_factory: Callable[[Path], object] = default_operation_lock) -> dict[str, object]:
    if set(request) != REQUEST_KEYS or request.get("schema") != SCHEMA:
        raise ProfileAssistantError("Request schema or fields are invalid")
    operation = request.get("operation")
    if operation not in OPERATIONS:
        raise ProfileAssistantError("Request operation is invalid")
    action_id = _canonical_uuid(request.get("action_id"), "action_id")
    timeout = request.get("wait_timeout_seconds")
    if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            or not 0.1 <= float(timeout) <= MAX_WAIT_SECONDS):
        raise ProfileAssistantError("wait_timeout_seconds is invalid")
    paths = _validate_paths(request)
    with lock_factory(paths["state_root"]):
        return _execute_locked(request, paths, process_check=process_check, sleeper=sleeper,
                               monotonic=monotonic, relaunch=relaunch,
                               uuid_factory=uuid_factory, readback=readback)


def _execute_locked(request: Mapping[str, object], paths: Mapping[str, Path], *,
                    process_check: Callable[[Path], bool], sleeper: Callable[[float], None],
                    monotonic: Callable[[], float], relaunch: Callable[[Path], None],
                    uuid_factory: Callable[[], uuid.UUID],
                    readback: Callable[[Path], bytes] | None) -> dict[str, object]:
    operation = str(request["operation"])
    action_id = _canonical_uuid(request.get("action_id"), "action_id")
    launch_id = _canonical_uuid(request.get("launch_id"), "launch_id")
    timeout = float(request["wait_timeout_seconds"])
    key = _load_or_create_secret(paths["state_root"].parent / "profile-assistant.key")
    contract, plugin_hash = _plugin_contract(paths["plugin_manifest"])
    status_path = paths["state_root"] / "status.json"
    observed_running = False
    terminal_state = "failed"
    operation_id: str | None = None
    observed = [False]
    try:
        _status(status_path, key, state="waiting", operation=operation,
                action_id=action_id, launch_id=launch_id, operation_id="")
        observed_running = _wait_for_exit(paths["studio_executable"], timeout,
                                          process_check, sleeper, monotonic, observed)
        recovered = _recover_prepared(paths, key, process_check, {
            "operation": operation, "action_id": action_id, "launch_id": launch_id,
        })
        if recovered is not None:
            if observed_running and not process_check(paths["studio_executable"]):
                relaunch(paths["studio_executable"])
            return recovered
        probe = probe_setup_action(paths["profiles_root"], action_id)
        if (probe.package_id is None or probe.profile_id is None
                or probe.controller_index is None or probe.setup_key is None):
            raise ProfileAssistantError(f"Setup action could not locate a unique target: {probe.reason}")
        package = paths["profiles_root"] / f"{probe.package_id}.ulanziProfile"
        manifest_path = package / "Profiles" / probe.profile_id / "manifest.json"
        _safe_existing(manifest_path, False)
        document, original_bytes = _load_json_file(manifest_path, MAX_MANIFEST_BYTES, "Target manifest")
        _actions, current = _target(document, probe.controller_index)
        receipt_path = paths["state_root"] / "receipt.json"
        target_identity = {"package_id": probe.package_id, "page_id": probe.profile_id,
                           "controller_index": probe.controller_index, "key": "3_2"}
        if operation in {"repair", "restore"}:
            existing = _authority_receipt(paths, key)
            _backed, _bytes, original_entry, historical_installed = _validate_metadata(
                existing, paths, key)
            if existing.get("target") != target_identity:
                raise ProfileAssistantError("Receipt does not match the exact succeeded target")
            installed_entry = (historical_installed if operation == "restore" else
                               _new_entry(str(historical_installed["ActionID"]), contract))
        else:
            original_entry = copy.deepcopy(_validate_entry(current, BUILTIN_UUID, "original"))
            installed_entry = _new_entry(str(uuid_factory()), contract)
        if operation in {"install", "repair"}:
            if current.get("Action") != BUILTIN_UUID:
                raise ProfileAssistantError(f"{operation.title()} requires the built-in center action")
            desired = installed_entry
        else:
            if not _installed_equivalent(current, installed_entry):
                raise ProfileAssistantError("Restore requires the exact installed LargeItem entry")
            desired = original_entry
        if current.get("Action") not in {BUILTIN_UUID, LARGEITEM_UUID}:
            raise ProfileAssistantError("The center contains an unknown third-party action")

        operation_id = str(uuid_factory())
        backup_path, inventory, inventory_hash = _copy_backup(
            package, paths["backup_root"], operation_id, key)
        backed_manifest_item = next((item for item in inventory["files"]
                                     if item["path"] == f"Profiles/{probe.profile_id}/manifest.json"), None)
        if (backed_manifest_item is None or backed_manifest_item["size"] != len(original_bytes)
                or backed_manifest_item["sha256"] != _sha256(original_bytes)):
            raise ProfileAssistantError("Backup does not contain the exact target manifest snapshot")
        changed = copy.deepcopy(document)
        changed_actions, _ = _target(changed, probe.controller_index)
        changed_actions["3_2"] = copy.deepcopy(desired)
        changed_bytes = (json.dumps(changed, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        receipt_value = {
        "schema": RECEIPT_SCHEMA, "state": "prepared", "operation": operation,
        "operation_id": operation_id, "target": target_identity,
        "trigger": {"action_id": action_id, "setup_key": probe.setup_key,
                    "launch_id": launch_id},
        "plugin_manifest_sha256": plugin_hash,
        "original_manifest_sha256": _sha256(original_bytes),
        "result_manifest_sha256": _sha256(changed_bytes),
        "original_entry": original_entry, "installed_entry": installed_entry,
        "rollback_entry": copy.deepcopy(current),
        "backup_path": str(backup_path), "backup_inventory": inventory,
        "backup_inventory_sha256": inventory_hash,
        }
        journal_path = paths["state_root"] / f"journal-{operation_id}.json"
        journal = _write_signed_json(journal_path, receipt_value, key)
        _status(status_path, key, state="prepared", operation=operation,
                operation_id=operation_id, action_id=action_id,
                launch_id=launch_id,
                original_manifest_sha256=_sha256(original_bytes),
                result_manifest_sha256=_sha256(changed_bytes), plugin_manifest_sha256=plugin_hash)

        wrote_manifest = False
        reader = readback or (lambda path: path.read_bytes())
        try:
            _validate_backup(journal, paths, key)

            def forward_guard() -> None:
                if process_check(paths["studio_executable"]):
                    raise ProfileAssistantError("Studio restarted before the manifest write")
                if manifest_path.read_bytes() != original_bytes:
                    raise ProfileAssistantError("Target manifest changed after backup and before replace")

            _atomic_bytes(manifest_path, changed_bytes, pre_replace=forward_guard)
            wrote_manifest = True
            actual_bytes = reader(manifest_path)
            actual = json.loads(actual_bytes.decode("utf-8"))
            if actual_bytes != changed_bytes or actual != changed:
                raise ProfileAssistantError("Target manifest readback or semantic delta validation failed")
            journal.update({"state": "succeeded", "result_manifest_sha256": _sha256(actual_bytes)})
            _commit_terminal_metadata(journal_path, journal, paths, key, "succeeded")
            terminal_state = "succeeded"
        except Exception as exc:
            if wrote_manifest:
                try:
                    _validate_backup(journal, paths, key)
                except Exception as backup_exc:
                    rollback_conflict = (f"Rollback backup validation failed ({backup_exc}); "
                                         "manual recovery required")
                    terminal_state = "manual_recovery_required"
                    _manual_recovery_status(paths, key, journal, rollback_conflict)
                    raise ManualRecoveryRequired(rollback_conflict) from exc

                def rollback_guard() -> None:
                    try:
                        running = process_check(paths["studio_executable"])
                    except Exception as check_exc:
                        raise ManualRecoveryRequired(
                            "Studio process state is unknown immediately before rollback; "
                            "manual recovery required") from check_exc
                    if running:
                        raise ManualRecoveryRequired(
                            "Studio restarted immediately before rollback; manual recovery required")
                    if manifest_path.read_bytes() != changed_bytes:
                        raise ManualRecoveryRequired(
                            "Live manifest changed after the attempted write; manual recovery required")

                try:
                    _atomic_bytes(manifest_path, original_bytes, pre_replace=rollback_guard)
                    rolled = manifest_path.read_bytes()
                    parsed = json.loads(rolled.decode("utf-8"))
                    if rolled != original_bytes or parsed != document:
                        raise ProfileAssistantError("Rollback readback validation failed")
                except Exception as rollback_exc:
                    rollback_conflict = (f"Rollback could not be validated ({rollback_exc}); "
                                         "manual recovery required")
                    terminal_state = "manual_recovery_required"
                    _manual_recovery_status(paths, key, journal, rollback_conflict)
                    raise ManualRecoveryRequired(rollback_conflict) from exc
                terminal_state = "rolled_back"
                receipt_value["state"] = journal["state"] = "rolled_back"
                journal["failure"] = str(exc)
                _commit_terminal_metadata(journal_path, journal, paths, key, "rolled_back")
            raise
        result = {"state": "succeeded", "operation": operation, "operation_id": operation_id,
                  "target": target_identity, "backup_path": str(backup_path),
                  "manifest_sha256": _sha256(changed_bytes)}
    except Exception as exc:
        observed_running = observed_running or observed[0]
        if isinstance(exc, ManualRecoveryRequired):
            terminal_state = "manual_recovery_required"
        else:
            try:
                _status(status_path, key, state=terminal_state, operation=operation,
                        operation_id=operation_id or "", action_id=action_id,
                        launch_id=launch_id, failure=str(exc),
                        plugin_manifest_sha256=plugin_hash)
            except Exception:
                pass
        if observed_running:
            try:
                if not process_check(paths["studio_executable"]):
                    relaunch(paths["studio_executable"])
            except Exception as relaunch_exc:
                try:
                    _status(status_path, key, state=terminal_state, operation=operation,
                            operation_id=operation_id or "", action_id=action_id,
                            launch_id=launch_id, failure=str(exc),
                            relaunch_failure=str(relaunch_exc), plugin_manifest_sha256=plugin_hash)
                except Exception:
                    pass
        raise
    if observed_running:
        try:
            if not process_check(paths["studio_executable"]):
                relaunch(paths["studio_executable"])
        except Exception as exc:
            _status(status_path, key, state="failed", profile_result="succeeded",
                    operation=operation,
                    operation_id=operation_id or "", action_id=action_id,
                    launch_id=launch_id,
                    failure="Profile update committed, but Studio restart failed",
                    relaunch_failure=str(exc), plugin_manifest_sha256=plugin_hash)
            raise ProfileAssistantError("Transaction succeeded but Studio relaunch failed") from exc
    return result


def profile_assistant_main(argv: Sequence[str] | None = None, **dependencies) -> int:
    arguments = list(argv if argv is not None else os.sys.argv[1:])
    if len(arguments) != 1:
        raise ProfileAssistantError("Expected exactly one request.json path")
    roots_factory = dependencies.pop("roots_factory", production_roots)
    lock_factory = dependencies.pop("lock_factory", default_operation_lock)
    roots = roots_factory()
    claimed: Path | None = None
    try:
        with lock_factory(roots.state_root):
            request = load_request(Path(arguments[0]), roots=roots, retire=False)
            claimed_value = request.pop("_claimed_request")
            claimed = Path(str(claimed_value))
            paths = _validate_paths(request)
            result = _execute_locked(
                request, paths,
                process_check=dependencies.pop("process_check", default_process_check),
                sleeper=dependencies.pop("sleeper", time.sleep),
                monotonic=dependencies.pop("monotonic", time.monotonic),
                relaunch=dependencies.pop("relaunch", default_relaunch),
                uuid_factory=dependencies.pop("uuid_factory", uuid.uuid4),
                readback=dependencies.pop("readback", None),
            )
            if dependencies:
                raise TypeError(f"Unexpected dependencies: {sorted(dependencies)}")
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        try:
            current, _ = _load_json_file(roots.state_root / "status.json",
                                         MAX_REQUEST_BYTES, "Assistant status")
        except Exception:
            current = {}
        if current.get("state") not in {
                "failed", "rolled_back", "manual_recovery_required", "succeeded"}:
            try:
                key = _load_or_create_secret(roots.secret_path)
                _status(roots.state_root / "status.json", key, state="failed",
                        operation=current.get("operation", "unknown"),
                        action_id=current.get("action_id", ""),
                        launch_id=current.get("launch_id", ""), operation_id="",
                        failure=str(exc))
            except Exception:
                pass
        raise
    finally:
        if claimed is not None:
            claimed.unlink(missing_ok=True)
        (roots.state_root / "launch.claim").unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(profile_assistant_main())
