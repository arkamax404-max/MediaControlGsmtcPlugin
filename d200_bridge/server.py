import asyncio
import hmac
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .artwork import MAX_BUNDLE_BYTES
from .lifecycle import CompanionLifecycle
from .paths import validate_token


BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 43821
MAX_REQUEST_BODY = 256
MAX_STATE_RESPONSE_BYTES = 4096
ARTWORK_PATH_PATTERN = re.compile(r"^/artwork/([0-9a-f]{64})$")
COMMAND_PATHS = {
    "/command/previous": "previous",
    "/command/toggle": "toggle",
    "/command/next": "next",
}
AUDIO_COMMAND_PATHS = {
    "/command/volume-up": "volume-up",
    "/command/volume-down": "volume-down",
    "/command/mute-toggle": "mute-toggle",
}


def create_server(
    cache, commander, loop, host=BRIDGE_HOST, port=BRIDGE_PORT, audio_commander=None,
    artwork_lookup=None, token=None, lifecycle=None, request_stop=None,
):
    if host != BRIDGE_HOST:
        raise ValueError("The D200 bridge may only bind to 127.0.0.1")

    validate_token(token)
    lifecycle = lifecycle or CompanionLifecycle()

    class BridgeHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                self._json(200, lifecycle.health(), max_bytes=512)
                return
            if self._protected() and not self._authorized():
                return
            if self.path == "/state":
                self._json(200, cache.get().public(), max_bytes=MAX_STATE_RESPONSE_BYTES)
            else:
                match = ARTWORK_PATH_PATTERN.fullmatch(self.path)
                if match and artwork_lookup is not None:
                    self._artwork(match.group(1))
                else:
                    self._json(404, {"error": "not_found"})

        def do_POST(self):
            if self._protected() and not self._authorized():
                return
            if self.path == "/lifecycle/stop":
                lifecycle.set_status("stopping")
                if request_stop is not None:
                    request_stop()
                self._json(200, {"ok": True})
                return
            if self.path.startswith("/command/") and not self._instance_matches():
                return
            action = COMMAND_PATHS.get(self.path)
            audio_action = AUDIO_COMMAND_PATHS.get(self.path)
            if action is None and audio_action is None:
                self._json(404, {"error": "not_found"})
                return
            if lifecycle.status == "stopping":
                self._json(503, {"ok": False, "error": "stopping"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._json(400, {"error": "invalid_content_length"})
                return
            if length < 0 or length > MAX_REQUEST_BODY:
                self._json(413, {"error": "request_too_large"})
                return
            if length:
                self.rfile.read(length)
            if audio_action is not None:
                self._audio_command(audio_action)
                return
            future = asyncio.run_coroutine_threadsafe(commander(action), loop)
            try:
                accepted = bool(future.result(timeout=2))
            except Exception:
                self._json(503, {"ok": False, "error": "command_failed"})
                return
            self._json(200 if accepted else 409, {"ok": accepted})

        def _audio_command(self, action):
            if audio_commander is None:
                self._json(503, {"ok": False, "status": "failed"})
                return
            try:
                result = audio_commander(action)
            except Exception:
                self._json(503, {"ok": False, "status": "failed"})
                return
            statuses = {"ok": 200, "no_audio": 409}
            self._json(statuses.get(result.status, 503), result.public())

        def _artwork(self, artwork_id):
            variants = artwork_lookup(artwork_id)
            if variants is None or variants.artwork_id != artwork_id:
                self._json(404, {"error": "not_found"})
                return
            etag = f'"{artwork_id}"'
            cache_control = "private, max-age=31536000, immutable"
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", cache_control)
                self.end_headers()
                return
            self._json(
                200,
                variants.public(),
                headers={"ETag": etag, "Cache-Control": cache_control},
                max_bytes=MAX_BUNDLE_BYTES,
            )

        def do_DELETE(self):
            self._method_not_allowed()

        def do_HEAD(self):
            self._method_not_allowed()

        def do_OPTIONS(self):
            self._method_not_allowed()

        def do_PATCH(self):
            self._method_not_allowed()

        def do_PUT(self):
            self._method_not_allowed()
        do_CONNECT = do_PUT
        do_TRACE = do_PUT

        def _method_not_allowed(self):
            if self._protected() and not self._authorized():
                return
            self._json(405, {"error": "method_not_allowed"})

        def _protected(self):
            return self.path == "/state" or self.path.startswith(
                ("/artwork/", "/command/", "/lifecycle/")
            )

        def _authorized(self):
            values = self.headers.get_all("Authorization", [])
            if not values:
                self._json(401, {"error": "unauthorized"})
                return False
            if len(values) != 1:
                self._json(403, {"error": "unauthorized"})
                return False
            value = values[0]
            expected = f"Bearer {token}"
            if len(value) > 64 or not value.isascii() or not hmac.compare_digest(
                value.encode("ascii"), expected.encode("ascii")
            ):
                self._json(403, {"error": "unauthorized"})
                return False
            return True

        def _instance_matches(self):
            values = self.headers.get_all("X-Companion-Instance", [])
            if len(values) != 1 or len(values[0]) > 64 or not values[0].isascii() or not hmac.compare_digest(
                values[0].encode("ascii"), lifecycle.instance_id.encode("ascii")
            ):
                self._json(409, {"error": "companion_mismatch"})
                return False
            return True

        def _json(self, status, payload, headers=None, max_bytes=None):
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            if max_bytes is not None and len(body) > max_bytes:
                status = 500
                body = b'{"error":"response_too_large"}'
                headers = None
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            response_headers = headers or {"Cache-Control": "no-store"}
            for name, value in response_headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer((host, port), BridgeHandler)
    server.daemon_threads = True
    return server
