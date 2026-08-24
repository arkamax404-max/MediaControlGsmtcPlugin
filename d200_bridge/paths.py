import base64
import os
import re
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path


PRODUCT_DIRECTORY = "GSMTCD200Controller"
TOKEN_BYTES = 32
TOKEN_LENGTH = 43
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
REPARSE_ATTRIBUTE = 0x400
LOCAL_DRIVE = re.compile(r'^[A-Z]:\\[^\x00-\x1f\\/:*?"<>|]+(?:\\[^\x00-\x1f\\/:*?"<>|]+)*\Z')
THREAT_MODEL = "Blocks accidental loopback callers; same-user malicious processes are out of scope."
class PathSecurity:
    # Best-effort misconfiguration defense, not race-free against same-user mutation.
    def validate_metadata(self, metadata, expected):
        attributes = getattr(metadata, "st_file_attributes", 0)
        if attributes & REPARSE_ATTRIBUTE or stat.S_ISLNK(metadata.st_mode):
            raise OSError("Unsafe reparse point")
        if expected == "directory" and not stat.S_ISDIR(metadata.st_mode):
            raise OSError("Expected directory")
        if expected == "file":
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise OSError("Unsafe token file type")
            if metadata.st_size != TOKEN_LENGTH:
                raise OSError("Invalid token size")
    def validate_chain(self, path, expected=None):
        path = Path(path)
        existing = []
        for candidate in (path, *path.parents):
            try:
                metadata = os.lstat(candidate)
            except FileNotFoundError:
                continue
            existing.append((candidate, metadata))
        for candidate, metadata in reversed(existing):
            if getattr(os.path, "isjunction", lambda _path: False)(candidate):
                raise OSError("Unsafe junction")
            kind = expected if candidate == path and expected else "directory"
            self.validate_metadata(metadata, kind)
        return next((metadata for candidate, metadata in existing if candidate == path), None)
def _local_root(raw):
    if not isinstance(raw, str) or not LOCAL_DRIVE.fullmatch(raw) or any(
        segment in (".", "..") for segment in raw[3:].split("\\")
    ):
        raise ValueError("Root must be a canonical local drive path")
    return Path(raw)
@dataclass(frozen=True)
class CompanionPaths:
    root: Path
    security: PathSecurity = PathSecurity()

    def __post_init__(self):
        root = _local_root(os.fspath(self.root))
        object.__setattr__(self, "root", root)
        self.security.validate_chain(root)

    @classmethod
    def from_environment(cls, environment=None, security=None):
        local_app_data = (os.environ if environment is None else environment).get("LOCALAPPDATA")
        if not local_app_data:
            raise RuntimeError("LOCALAPPDATA is required")
        return cls(_local_root(local_app_data) / PRODUCT_DIRECTORY, security or PathSecurity())

    @property
    def config(self): return self.root / "config"
    @property
    def logs(self): return self.root / "logs"
    @property
    def cache(self): return self.root / "cache"
    @property
    def diagnostics(self): return self.root / "diagnostics"
    @property
    def token(self): return self.config / "bridge-token"
def validate_token(token):
    if not TOKEN_PATTERN.fullmatch(token):
        raise ValueError("Invalid companion token")
    return token
def _identity(metadata):
    return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_nlink,
            stat.S_IFMT(metadata.st_mode), getattr(metadata, "st_file_attributes", 0))


def load_token_file(path, security=None):
    path = Path(path)
    if not path.is_absolute(): raise ValueError("Token path must be absolute")
    security = security or PathSecurity()
    path_metadata = security.validate_chain(path, "file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        before = os.fstat(descriptor)
        security.validate_metadata(before, "file")
        if path_metadata and _identity(path_metadata) != _identity(before):
            raise OSError("Token target changed")
        data = os.read(descriptor, TOKEN_LENGTH + 1)
        after = os.fstat(descriptor)
        security.validate_metadata(after, "file")
        if _identity(before) != _identity(after):
            raise OSError("Token changed while reading")
    finally:
        os.close(descriptor)
    path_metadata = security.validate_chain(path, "file")
    if path_metadata and _identity(path_metadata) != _identity(after):
        raise OSError("Token target changed")
    return validate_token(data.decode("ascii"))


def ensure_token(path, security=None):
    path = Path(path)
    if not path.is_absolute(): raise ValueError("Token path must be absolute")
    security = security or PathSecurity()
    security.validate_chain(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    security.validate_chain(path.parent, "directory")
    try:
        return load_token_file(path, security)
    except FileNotFoundError:
        token = base64.urlsafe_b64encode(secrets.token_bytes(TOKEN_BYTES)).rstrip(b"=").decode("ascii")
        validate_token(token)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="ascii", newline="") as stream:
                stream.write(token)
                stream.flush()
                os.fsync(stream.fileno())
            security.validate_chain(temporary, "file")
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
        return load_token_file(path, security)


def load_token(paths=None):
    selected = paths or CompanionPaths.from_environment()
    try:
        return load_token_file(selected.token, selected.security)
    except (FileNotFoundError, OSError):
        time.sleep(0.02)
        return load_token_file(selected.token, selected.security)
