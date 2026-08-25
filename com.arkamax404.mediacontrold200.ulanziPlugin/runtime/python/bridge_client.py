from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from typing import Callable
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from d200_bridge.paths import load_token, validate_token
from artwork_bundle import ARTWORK_ID_PATTERN, ArtworkBundle, parse_artwork_bundle


BRIDGE_ORIGIN = "http://127.0.0.1:43821"
BRIDGE_TIMEOUT_SECONDS = 1.0
REQUEST_ACQUIRE_POLL_SECONDS = 0.05
API_MAJOR = 1
MIN_API_MINOR = 0
MAX_ARTWORK_BODY_BYTES = 4_001_000
INSTANCE_ID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
COMMAND_PATHS = {
    "previous": "/command/previous",
    "toggle": "/command/toggle",
    "next": "/command/next",
    "volume-up": "/command/volume-up",
    "volume-down": "/command/volume-down",
    "mute-toggle": "/command/mute-toggle",
}
BRIDGE_ORIGIN_ARGUMENT = "--bridge-origin="


def _strict_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def bridge_origin_from_future(arguments) -> str:
    matches = [value[len(BRIDGE_ORIGIN_ARGUMENT):] for value in arguments
               if isinstance(value, str) and value.startswith(BRIDGE_ORIGIN_ARGUMENT)]
    if len(matches) > 1:
        raise ValueError("Bridge origin may be provided only once")
    return matches[0] if matches else BRIDGE_ORIGIN


@dataclass(frozen=True)
class BridgeResult:
    command: str
    status: str
    status_code: int | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass(frozen=True)
class BridgeStateResult:
    status: str
    state: dict | None = None
    status_code: int | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass(frozen=True)
class BridgeArtworkResult:
    status: str
    bundle: ArtworkBundle | None = None
    status_code: int | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class BridgeClient:
    def __init__(
        self,
        token_loader: Callable[[], str] = load_token,
        opener: Callable = urlopen,
        origin: str = BRIDGE_ORIGIN,
        timeout: float = BRIDGE_TIMEOUT_SECONDS,
    ) -> None:
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.port is None
        ):
            raise ValueError("Bridge origin must be an HTTP 127.0.0.1 origin")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise ValueError("Bridge timeout must be positive")
        self.origin = f"http://127.0.0.1:{parsed.port}"
        self.timeout = float(timeout)
        self._token_loader = token_loader
        self._opener = opener
        self._request_lock = threading.Lock()
        self._request_available = threading.Event()
        self._request_available.set()
        self._request_active = False

    def execute(self, command: str, cancelled: Callable[[], bool] | None = None) -> BridgeResult:
        if not self._claim_request(cancelled):
            return BridgeResult(command, "stopped")
        try:
            return self._execute(command)
        finally:
            self._release_request()

    def _execute(self, command: str, cancelled: Callable[[], bool] | None = None) -> BridgeResult:
        if command not in COMMAND_PATHS:
            return BridgeResult(str(command), "unsupported")
        compatibility, token, instance_id, status_code = self._compatibility()
        if compatibility != "compatible":
            return BridgeResult(command, compatibility, status_code)
        request = Request(
            self.origin + COMMAND_PATHS[command],
            data=b"{}",
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-Companion-Instance": instance_id,
            },
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                status = response.status
                response.read(1025)
        except HTTPError as error:
            status_code = error.code
            error.close()
            return BridgeResult(command, "rejected", status_code)
        except Exception:
            return BridgeResult(command, "unavailable")
        return BridgeResult(command, "ok" if 200 <= status < 300 else "rejected", status)

    def get_state(self, cancelled: Callable[[], bool] | None = None) -> BridgeStateResult:
        if not self._claim_request(cancelled):
            return BridgeStateResult("stopped")
        try:
            return self._get_state()
        finally:
            self._release_request()

    def _get_state(self, cancelled: Callable[[], bool] | None = None) -> BridgeStateResult:
        compatibility, token, instance_id, status_code = self._compatibility()
        if compatibility != "compatible":
            return BridgeStateResult(compatibility, status_code=status_code)
        request = Request(
            self.origin + "/state",
            method="GET",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Companion-Instance": instance_id,
            },
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                status = response.status
                body = response.read(4097)
        except HTTPError as error:
            status_code = error.code
            error.close()
            return BridgeStateResult("unavailable", status_code=status_code)
        except Exception:
            return BridgeStateResult("unavailable")
        if not 200 <= status < 300 or len(body) > 4096:
            return BridgeStateResult("unavailable", status_code=status)
        try:
            state = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return BridgeStateResult("unavailable", status_code=status)
        if not isinstance(state, dict):
            return BridgeStateResult("unavailable", status_code=status)
        return BridgeStateResult("ok", state, status)

    def get_artwork(
        self, artwork_id: str, cancelled: Callable[[], bool] | None = None
    ) -> BridgeArtworkResult:
        if not isinstance(artwork_id, str) or not ARTWORK_ID_PATTERN.fullmatch(artwork_id):
            return BridgeArtworkResult("invalid")
        if not self._claim_request(cancelled):
            return BridgeArtworkResult("stopped")
        try:
            return self._get_artwork(artwork_id)
        finally:
            self._release_request()

    def _get_artwork(self, artwork_id: str) -> BridgeArtworkResult:
        compatibility, token, _instance_id, status_code = self._compatibility()
        if compatibility != "compatible":
            return BridgeArtworkResult(compatibility, status_code=status_code)
        request = Request(
            self.origin + "/artwork/" + artwork_id,
            method="GET",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                status = response.status
                body = response.read(MAX_ARTWORK_BODY_BYTES + 1)
        except HTTPError as error:
            status_code = error.code
            error.close()
            return BridgeArtworkResult("unavailable", status_code=status_code)
        except Exception:
            return BridgeArtworkResult("unavailable")
        if not 200 <= status < 300 or len(body) > MAX_ARTWORK_BODY_BYTES:
            return BridgeArtworkResult("unavailable", status_code=status)
        try:
            payload = json.loads(body.decode("utf-8"), object_pairs_hook=_strict_json_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return BridgeArtworkResult("unavailable", status_code=status)
        bundle = parse_artwork_bundle(payload, artwork_id)
        if bundle is None:
            return BridgeArtworkResult("unavailable", status_code=status)
        return BridgeArtworkResult("ok", bundle, status)

    def _claim_request(self, cancelled: Callable[[], bool] | None) -> bool:
        while True:
            if cancelled is not None and cancelled():
                return False
            if not self._request_available.wait(REQUEST_ACQUIRE_POLL_SECONDS):
                continue
            with self._request_lock:
                if self._request_active:
                    continue
                self._request_active = True
                self._request_available.clear()
                return True

    def _release_request(self) -> None:
        with self._request_lock:
            self._request_active = False
            self._request_available.set()

    def _compatibility(self) -> tuple[str, str | None, str | None, int | None]:
        try:
            token = validate_token(self._token_loader())
        except Exception:
            return "configuration", None, None, None
        request = Request(
            self.origin + "/health",
            method="GET",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                if not 200 <= response.status < 300:
                    return "unavailable", token, None, response.status
                body = response.read(513)
            if len(body) > 512:
                return "unavailable", token, None, None
            health = json.loads(body.decode("utf-8"))
        except HTTPError as error:
            status_code = error.code
            error.close()
            return "unavailable", token, None, status_code
        except Exception:
            return "unavailable", token, None, None
        if not isinstance(health, dict) or health.get("service") != "d200-gsmtc-bridge":
            return "unavailable", token, None, None
        major = health.get("api_major")
        minor = health.get("api_minor")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 65535
            for value in (major, minor)
        ):
            return "unavailable", token, None, None
        instance_id = health.get("instance_id")
        if not isinstance(instance_id, str) or not INSTANCE_ID_PATTERN.fullmatch(instance_id):
            return "unavailable", token, None, None
        if major != API_MAJOR or minor < MIN_API_MINOR:
            return "incompatible", token, instance_id, None
        if health.get("status") not in ("ready", "degraded"):
            return "unavailable", token, instance_id, None
        return "compatible", token, instance_id, None
