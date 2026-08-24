import io
import json
import os
import platform
import re
import secrets
import stat
import sys
import zipfile
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from urllib.request import Request, urlopen

from .paths import CompanionPaths, REPARSE_ATTRIBUTE, load_token, validate_token
from . import BRIDGE_HOST, BRIDGE_PORT
from .version import API_MAJOR, API_MINOR, COMPANION_VERSION


SCHEMA_VERSION = 1
ENTRY_NAMES = ("summary.json", "runtime.json", "dependencies.json", "logs.txt")
DEPENDENCIES = ("Pillow", "comtypes", "psutil", "pycaw", "winrt-Windows-Media-Control")
LOG_FILES = ("companion.log.4", "companion.log.3", "companion.log.2",
             "companion.log.1", "companion.log")
SAFE_EVENTS = {"companion_listening", "startup_failed", "redacted_event"}
MAX_HTTP_BYTES = 64 * 1024
MAX_LOG_BYTES = 512 * 1024
MAX_LOG_FILE_BYTES = 2 * 1024 * 1024
MAX_ZIP_BYTES = 1024 * 1024
HTTP_TIMEOUT = 1


def bounded_http_get(url, headers, timeout):
    request = Request(url, headers=headers, method="GET")
    with urlopen(request, timeout=timeout) as response:
        return response.status, response.read(MAX_HTTP_BYTES + 1)


def _dependency_version(name):
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _safe_text(value, limit=64):
    value = str(value or "")
    return value[:limit] if re.fullmatch(r"[A-Za-z0-9_.+ -]*", value[:limit]) else "unavailable"


def _file_identity(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_nlink,
            stat.S_IFMT(value.st_mode), getattr(value, "st_file_attributes", 0))


def _safe_log_metadata(value):
    return (stat.S_ISREG(value.st_mode) and not stat.S_ISLNK(value.st_mode)
            and value.st_nlink == 1 and 0 <= value.st_size <= MAX_LOG_FILE_BYTES
            and not getattr(value, "st_file_attributes", 0) & REPARSE_ATTRIBUTE)


def read_log_tail(path, limit, fs=os):
    try:
        before_path = fs.lstat(path)
        if not _safe_log_metadata(before_path):
            return b""
        if getattr(fs.path, "isjunction", lambda _path: False)(path):
            return b""
        descriptor = fs.open(path, fs.O_RDONLY | getattr(fs, "O_BINARY", 0))
        try:
            before = fs.fstat(descriptor)
            if not _safe_log_metadata(before) or _file_identity(before_path) != _file_identity(before):
                return b""
            fs.lseek(descriptor, max(0, before.st_size - limit), fs.SEEK_SET)
            data = fs.read(descriptor, limit)
            after = fs.fstat(descriptor)
            if not _safe_log_metadata(after) or _file_identity(before) != _file_identity(after):
                return b""
            return data
        finally:
            fs.close(descriptor)
    except (OSError, AttributeError):
        return b""


def _sanitized_logs(paths, log_reader):
    output = []
    for name in LOG_FILES:
        data = log_reader(paths.logs / name, MAX_LOG_BYTES // len(LOG_FILES))
        for line in data.decode("utf-8", "replace").splitlines():
            if len(line) > 1024:
                continue
            match = re.search(r"\b(INFO|ERROR) (companion_listening|startup_failed|redacted_event)\s*$", line)
            if match:
                output.append(f"{match.group(1)} {match.group(2)}")
    return ("\n".join(output) + ("\n" if output else ""))[:MAX_LOG_BYTES].encode("utf-8")


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def build_zip(entries):
    if tuple(name for name, _value in entries) != ENTRY_NAMES:
        raise ValueError("Invalid diagnostics entries")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6) as archive:
        for name, value in entries:
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100600 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, value, compress_type=zipfile.ZIP_DEFLATED,
                             compresslevel=6)
    return buffer.getvalue()


def _collect_runtime(http_get, token_loader, paths, health_status, health_reason):
    if health_status not in {"ready", "degraded"}:
        return {"online": False, "reason": health_reason}
    try:
        token = validate_token(token_loader(paths))
    except (OSError, RuntimeError, ValueError, UnicodeError):
        return {"online": False, "reason": "token_unavailable"}
    try:
        status, body = http_get(f"http://{BRIDGE_HOST}:{BRIDGE_PORT}/state",
            {"Authorization": f"Bearer {token}"}, HTTP_TIMEOUT)
        if status != 200 or len(body) > MAX_HTTP_BYTES:
            return {"online": False, "reason": "state_unavailable"}
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError
        return {"online": True, "available": payload.get("available") is True,
            "timeline_available": payload.get("timeline_available") is True,
            "audio_available": payload.get("audio_available") is True,
            "artwork_id_present": isinstance(payload.get("artwork_id"), str)
                and bool(payload["artwork_id"]), "payload_size": len(body)}
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError):
        return {"online": False, "reason": "state_unavailable"}


def create_diagnostics(paths=None, clock=None, http_get=None, token_loader=None,
                       dependency_provider=None, log_reader=read_log_tail,
                       name_source=secrets.token_hex):
    paths = paths or CompanionPaths.from_environment()
    now = (clock or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)
    http_get = http_get or bounded_http_get
    token_loader = token_loader or load_token
    dependency_provider = dependency_provider or _dependency_version
    reachable, health_status, error_code = False, "unavailable", "health_unreachable"
    try:
        status, body = http_get(f"http://{BRIDGE_HOST}:{BRIDGE_PORT}/health", {}, HTTP_TIMEOUT)
        reachable = True
        if status == 200 and len(body) <= MAX_HTTP_BYTES:
            payload = json.loads(body)
            candidate = payload.get("status") if isinstance(payload, dict) else None
            if candidate in {"starting", "ready", "degraded", "stopping"}:
                health_status, error_code = candidate, "none"
            else:
                error_code = "health_invalid"
        else:
            error_code = "health_http"
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError):
        pass
    generated_at = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    summary = {"schema_version": SCHEMA_VERSION, "generated_at": generated_at,
        "companion_version": COMPANION_VERSION, "api_major": API_MAJOR, "api_minor": API_MINOR,
        "os_family": _safe_text(platform.system(), 32), "architecture": _safe_text(platform.machine(), 32),
        "python_version": _safe_text(platform.python_version(), 32),
        "mode": "frozen" if getattr(sys, "frozen", False) else "source",
        "reachable": reachable, "health_status": health_status, "error_code": error_code}
    health_reason = "health_unreachable" if not reachable else "health_unavailable"
    runtime = _collect_runtime(http_get, token_loader, paths, health_status, health_reason)
    dependencies = {}
    for name in DEPENDENCIES:
        value = dependency_provider(name)
        dependencies[name] = _safe_text(value) if value else "unavailable"
    values = (summary, runtime, dependencies, _sanitized_logs(paths, log_reader))
    content = build_zip(tuple((name, value if isinstance(value, bytes) else _json_bytes(value))
                              for name, value in zip(ENTRY_NAMES, values)))
    if len(content) > MAX_ZIP_BYTES:
        raise OSError("Diagnostics bundle exceeds size limit")
    paths.security.validate_chain(paths.diagnostics)
    paths.diagnostics.mkdir(parents=True, exist_ok=True)
    paths.security.validate_chain(paths.diagnostics, "directory")
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    for _attempt in range(3):
        suffix = name_source(6)
        if not re.fullmatch(r"[0-9a-f]{12}", suffix):
            raise ValueError("Invalid diagnostics name")
        destination = paths.diagnostics / f"diagnostics-{stamp}-{suffix}.zip"
        temporary = destination.with_suffix(".tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, destination)
        except FileExistsError:
            continue
        except BaseException:
            raise
        finally:
            temporary.unlink(missing_ok=True)
        return destination
    raise FileExistsError("Diagnostics name collision")
