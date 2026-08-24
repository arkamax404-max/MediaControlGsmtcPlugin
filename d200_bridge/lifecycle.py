import ctypes
import threading
import time
import uuid
from datetime import datetime, timezone

from .version import API_MAJOR, API_MINOR, COMPANION_VERSION


MUTEX_NAME = "Global\\GSMTCD200Controller.Companion"
ERROR_ALREADY_EXISTS = 183
TRANSITIONS = {
    "starting": {"starting", "ready", "degraded", "stopping"},
    "ready": {"ready", "degraded", "stopping"},
    "degraded": {"degraded", "ready", "stopping"},
    "stopping": {"stopping"},
}


class WindowsMutexAdapter:
    def __init__(self):
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool,
                                                ctypes.c_wchar_p]
        self.kernel32.CreateMutexW.restype = ctypes.c_void_p
        self.kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        self.kernel32.CloseHandle.restype = ctypes.c_bool

    def create(self, name):
        ctypes.set_last_error(0)
        handle = self.kernel32.CreateMutexW(None, False, name)
        error = ctypes.get_last_error()
        if not handle:
            raise ctypes.WinError(error)
        return handle, error

    def close(self, handle):
        if not self.kernel32.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())


class NamedMutex:
    def __init__(self, name=MUTEX_NAME, adapter=None):
        self.name = name
        self.adapter = adapter or WindowsMutexAdapter()
        self.handle = None
        self.unavailable = False

    def acquire(self):
        try:
            self.handle, error = self.adapter.create(self.name)
        except OSError:
            self.unavailable = True
            return False
        if error not in (0, ERROR_ALREADY_EXISTS):
            self.unavailable = True
            self.close()
            return False
        return error != ERROR_ALREADY_EXISTS

    def close(self):
        if self.handle is not None:
            handle, self.handle = self.handle, None
            self.adapter.close(handle)


class CompanionLifecycle:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._started = clock()
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._status = "starting"
        self._lock = threading.Lock()
        self.instance_id = str(uuid.uuid4())

    @property
    def status(self):
        with self._lock:
            return self._status

    def set_status(self, status):
        with self._lock:
            if status not in TRANSITIONS[self._status]:
                raise ValueError("Invalid lifecycle transition")
            self._status = status

    def health(self):
        return {
            "service": "d200-gsmtc-bridge",
            "companion_version": COMPANION_VERSION,
            "api_major": API_MAJOR,
            "api_minor": API_MINOR,
            "status": self.status,
            "instance_id": self.instance_id,
            "started_at": self._started_at,
            "uptime_seconds": round(max(0, self._clock() - self._started), 3),
        }
