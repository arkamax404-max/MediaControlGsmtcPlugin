from __future__ import annotations


import os
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from bridge_client import BridgeClient, BridgeHealthResult
from d200_bridge.paths import CompanionPaths
from d200_bridge.version import COMPANION_VERSION


READINESS_TIMEOUT_SECONDS = 8.0
READINESS_POLL_SECONDS = 0.1
EXTERNAL_STOP_TIMEOUT_SECONDS = 4.0
STOP_WAIT_SECONDS = 3.0
MAX_STDERR_BYTES = 512
PREFER_EMBEDDED_MARKER = "prefer-embedded-companion"


@dataclass(frozen=True)
class CompanionStartResult:
    status: str
    owned: bool
    stage: str | None = None
    exit_code: int | None = None

    @property
    def ready(self) -> bool:
        return self.status == "ready"


def _version_tuple(value: object) -> tuple[int, int, int]:
    if not isinstance(value, str):
        return (-1, -1, -1)
    parts = value.split(".")
    if len(parts) != 3 or any(not part.isdigit() or str(int(part)) != part for part in parts):
        return (-1, -1, -1)
    return tuple(int(part) for part in parts)


def prefer_embedded_from_environment() -> bool:
    try:
        marker = CompanionPaths.from_environment().root / PREFER_EMBEDDED_MARKER
        info = marker.lstat()
        if not stat.S_ISREG(info.st_mode) or marker.is_symlink() or info.st_size != 2:
            return False
        return marker.read_bytes() == b"1\n"
    except (OSError, RuntimeError, ValueError):
        return False


class CompanionSupervisor:
    """Own one bundled companion without disturbing compatible external instances."""

    def __init__(
        self,
        client_factory: Callable[[], BridgeClient] = BridgeClient,
        popen: Callable = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        executable: str | None = None,
        prefer_embedded: bool | None = None,
        readiness_timeout: float = READINESS_TIMEOUT_SECONDS,
        readiness_poll: float = READINESS_POLL_SECONDS,
        external_stop_timeout: float = EXTERNAL_STOP_TIMEOUT_SECONDS,
        stop_wait: float = STOP_WAIT_SECONDS,
        minimum_version: str = COMPANION_VERSION,
    ) -> None:
        self._client_factory = client_factory
        self._popen = popen
        self._clock = clock
        self._sleep = sleep
        default = Path(sys.executable).resolve().parent / "companion" / "GSMTCD200Companion.exe"
        self._executable = str(Path(executable).resolve()) if executable else str(default)
        self._prefer_embedded = (prefer_embedded_from_environment()
                                 if prefer_embedded is None else bool(prefer_embedded))
        self._readiness_timeout = readiness_timeout
        self._readiness_poll = readiness_poll
        self._external_stop_timeout = external_stop_timeout
        self._stop_wait = stop_wait
        self._minimum_version = _version_tuple(minimum_version)
        self._child = None
        self._owned_instance_id: str | None = None

    def ensure_ready(self) -> CompanionStartResult:
        health = self._probe()
        if health.status == "compatible" and health.instance_id:
            external_current = (_version_tuple(health.companion_version)
                                >= self._minimum_version)
            if not self._prefer_embedded and external_current:
                return CompanionStartResult("ready", False)
            if not self._client_factory().stop_owned(health.instance_id):
                return CompanionStartResult("companion_start_failed", False,
                                            "external-stop")
            deadline = self._clock() + self._external_stop_timeout
            while self._clock() < deadline:
                if self._probe().status != "compatible":
                    break
                self._sleep(self._readiness_poll)
            else:
                return CompanionStartResult("companion_start_failed", False,
                                            "external-stop-timeout")
        try:
            child = self._popen(
                [self._executable, "--parent-pid", str(os.getpid())], shell=False,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, ValueError):
            return CompanionStartResult("companion_start_failed", False, "spawn")
        stderr = _RedactedStderrBuffer()
        reader = threading.Thread(target=stderr.drain, args=(child.stderr,),
                                  name="embedded-companion-stderr", daemon=True)
        reader.start()
        deadline = self._clock() + self._readiness_timeout
        while self._clock() < deadline:
            exit_code = child.poll()
            if exit_code is not None:
                reader.join(timeout=0.05)
                return CompanionStartResult("companion_start_failed", False,
                                            "exit", _safe_exit_code(exit_code))
            health = self._probe()
            if health.status == "compatible" and health.instance_id:
                if _version_tuple(health.companion_version) < self._minimum_version:
                    self._client_factory().stop_owned(health.instance_id)
                    try:
                        child.wait(timeout=self._stop_wait)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                    return CompanionStartResult("companion_start_failed", False,
                                                "embedded-version")
                if child.poll() is not None:
                    return CompanionStartResult("companion_start_failed", False, "ownership")
                self._child = child
                self._owned_instance_id = health.instance_id
                return CompanionStartResult("ready", True)
            self._sleep(self._readiness_poll)
        return CompanionStartResult("companion_start_failed", False, "health-timeout")

    def shutdown(self) -> bool:
        child, instance_id = self._child, self._owned_instance_id
        self._child = self._owned_instance_id = None
        if child is None or instance_id is None:
            return True
        try:
            if not self._client_factory().stop_owned(instance_id):
                return False
            child.wait(timeout=self._stop_wait)
            return child.poll() is not None
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _probe(self) -> BridgeHealthResult:
        try:
            return self._client_factory().probe_health()
        except Exception:
            return BridgeHealthResult("unavailable")


def _safe_exit_code(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) \
        and 0 <= value <= 255 else None


class _RedactedStderrBuffer:
    def __init__(self, maximum: int = MAX_STDERR_BYTES) -> None:
        self._maximum = maximum
        self._seen = 0
        self._lock = threading.Lock()

    def drain(self, stream) -> None:
        while stream is not None:
            try:
                chunk = stream.read(256)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            with self._lock:
                self._seen += min(len(chunk), max(0, self._maximum - self._seen))
