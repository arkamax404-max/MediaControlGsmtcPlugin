import inspect
import json
import sys
import threading
import time
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path


EXPECTED_CONNECT_PARAMETERS = (
    "self", "uuid", "port", "address", "language", "argv", "threaded", "daemon"
)
EXPECTED_SET_SETTINGS_PARAMETERS = ("self", "settings", "context")
EXPECTED_DISPLAY_PARAMETERS = ("self", "context", "data", "text")
EXPECTED_PATH_PARAMETERS = ("self", "context", "path", "text")


def inspect_sdk():
    from ulanzi_api import UlanziApi

    root = Path(__file__).resolve().parents[1]
    runtime = root / "com.arkamax404.mediacontrold200.ulanziPlugin" / "runtime" / "python"
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(runtime))
    from artwork_bundle import ArtworkBundle, ArtworkBundleCache
    from bridge_client import BridgeArtworkResult, BridgeResult, BridgeStateResult
    from now_playing_action import ACTION_UUID as NOW_PLAYING_UUID, NowPlayingActionModel
    from progress_action import ACTION_UUID, ProgressActionModel
    from progress_scheduler import ProgressScheduler, register_progress_handlers
    from transport_actions import TransportRouter, register_transport_handlers

    parameters = tuple(inspect.signature(UlanziApi.connect).parameters)
    if parameters[:len(EXPECTED_CONNECT_PARAMETERS)] != EXPECTED_CONNECT_PARAMETERS:
        raise RuntimeError(f"Unexpected UlanziApi.connect signature: {parameters}")
    settings_parameters = tuple(inspect.signature(UlanziApi.setSettings).parameters)
    if settings_parameters != EXPECTED_SET_SETTINGS_PARAMETERS:
        raise RuntimeError(f"Unexpected UlanziApi.setSettings signature: {settings_parameters}")
    display_parameters = tuple(inspect.signature(UlanziApi.setBaseDataIcon).parameters)
    path_parameters = tuple(inspect.signature(UlanziApi.setPathIcon).parameters)
    if display_parameters != EXPECTED_DISPLAY_PARAMETERS or path_parameters != EXPECTED_PATH_PARAMETERS:
        raise RuntimeError(f"Unexpected UlanziApi display signatures: {display_parameters}, {path_parameters}")
    api = UlanziApi()
    if api.onClose(lambda _event: None) is not api or not callable(api.close):
        raise RuntimeError("UlanziApi lifecycle contract is unavailable")

    class CaptureSocket:
        connected = True

        def __init__(self):
            self.messages = []

        def send(self, message):
            self.messages.append((threading.get_ident(), json.loads(message)))

    socket = CaptureSocket()
    api.websocket = socket
    api.uuid, api.key, api.actionid = "plugin", "main-key", "main-action"
    context = "context-uuid___context-key___context-action"
    class ProbeClient:
        def __init__(self):
            self.commands = []
            self.completed = threading.Event()

        def execute(self, command, cancelled=None):
            self.commands.append(command)
            if len(self.commands) == 5:
                self.completed.set()
            return BridgeResult(command, "ok")

        def get_state(self, cancelled=None):
            now = datetime.now(timezone.utc).isoformat()
            return BridgeStateResult("ok", {
                "available": True, "is_playing": True, "title": "Track",
                "artist": "Artist", "artwork_id": "a" * 64,
                "timeline_available": False, "position_seconds": 0,
                "duration_seconds": 0, "playback_rate": 1,
                "position_updated_at": "", "updated_at": now,
            }, 200)

        def get_artwork(self, artwork_id, cancelled=None):
            return BridgeArtworkResult("ok", ArtworkBundle(
                artwork_id, "data:image/png;base64,color", "data:image/png;base64,gray",
                ("tl", "tr", "bl", "br")), 200)

    probe_client = ProbeClient()
    router = TransportRouter(client=probe_client)
    scheduler = ProgressScheduler(api, probe_client, ProgressActionModel(),
                                  NowPlayingActionModel(), ArtworkBundleCache())
    router.configure_runtime(scheduler.handle_run, scheduler.request_poll)
    router.start()
    scheduler.start()
    register_transport_handlers(api, router)
    register_progress_handlers(api, scheduler)
    handler_counts = {
        name: len(api._listeners.get(name, []))
        for name in ("add", "run", "keydown", "keyup", "clear", "setactive", "paramfromplugin",
                      "didReceiveSettings")
    }
    expected_handler_counts = {
        "add": 1,
        "run": 1,
        "keydown": 0,
        "keyup": 0,
        "clear": 1,
        "setactive": 1,
        "paramfromplugin": 1,
        "didReceiveSettings": 1,
    }
    if handler_counts != expected_handler_counts:
        raise RuntimeError(f"Unexpected real SDK handler counts: {handler_counts}")
    api.emit("add", {"uuid": ACTION_UUID, "context": context, "param": {}})
    now_context = "now-uuid___now-key___now-action"
    api.emit("add", {"uuid": NOW_PLAYING_UUID, "context": now_context})
    deadline = time.monotonic() + 1
    while len(socket.messages) < 4 and time.monotonic() < deadline:
        time.sleep(0.005)
    if not socket.messages:
        raise RuntimeError("Integrated progress scheduler did not emit a display")
    settings_messages = [item for item in socket.messages if item[1].get("cmd") == "setSettings"]
    display_messages = [item for item in socket.messages if item[1].get("cmd") == "state"]
    if len(settings_messages) != 1 or not display_messages:
        raise RuntimeError(f"Integrated settings/display sends are missing: {socket.messages}")
    settings_thread_id, settings_payload = settings_messages[0]
    display_thread_id, display_payload = display_messages[0]
    state_item = display_payload.get("param", {}).get("statelist", [{}])[0]
    if (display_thread_id == threading.get_ident()
            or state_item.get("uuid") != "context-uuid"
            or state_item.get("key") != "context-key"
            or state_item.get("actionid") != "context-action"
            or state_item.get("type") != 1
            or not state_item.get("data", "").startswith("data:image/svg+xml;base64,")):
        raise RuntimeError(f"Unexpected integrated legacy SDK payload: {display_payload}")
    now_items = [item for _, message in display_messages
                 for item in message.get("param", {}).get("statelist", [])
                 if item.get("uuid") == "now-uuid"]
    if ([item.get("type") for item in now_items] != [2, 1]
            or now_items[0].get("path") != "./assets/music.svg"
            or now_items[0].get("textData") != "Track\nArtist"
            or now_items[1].get("data") != "data:image/png;base64,color"
            or now_items[1].get("textData") != "Track\nArtist"):
        raise RuntimeError(f"Unexpected integrated Now Playing payloads: {now_items}")
    expected_settings = {"progressColor": "#1DB954", "trackColor": "#333333",
                         "textColor": "#FFFFFF", "backgroundColor": "#000000",
                         "strokeWidth": 14}
    if (settings_thread_id != display_thread_id
            or settings_payload.get("settings") != expected_settings
            or settings_payload.get("uuid") != "context-uuid"
            or settings_payload.get("key") != "context-key"
            or settings_payload.get("actionid") != "context-action"):
        raise RuntimeError(f"Unexpected integrated settings payload: {settings_payload}")
    api.emit("didReceiveSettings", {"context": context, "settings": expected_settings})
    time.sleep(0.05)
    if len([item for item in socket.messages if item[1].get("cmd") == "setSettings"]) != 1:
        raise RuntimeError("Canonical settings echo caused a persistence loop")
    started_at = time.monotonic()
    api.emit("run", {"uuid": "com.arkamax404.ulanzi.mediacontrol.previous"})
    for _ in range(3):
        api.emit("run", {"uuid": "com.arkamax404.ulanzi.mediacontrol.volume-up"})
    api.emit("run", {"uuid": NOW_PLAYING_UUID, "context": now_context})
    callback_seconds = time.monotonic() - started_at
    if callback_seconds >= 0.25 or not probe_client.completed.wait(1):
        raise RuntimeError(f"Real SDK run callback blocked: {callback_seconds:.6f}s")
    expected_commands = ["previous", "volume-up", "volume-up", "volume-up", "toggle"]
    if probe_client.commands != expected_commands:
        raise RuntimeError(f"Unexpected real SDK routing: {probe_client.commands}")
    if any(thread.name == "ulanzi-volume-repeat" for thread in threading.enumerate()):
        raise RuntimeError("Unexpected volume repeat scheduler thread")
    if not router.stop(timeout=0.5):
        raise RuntimeError("Transport worker did not stop")
    if not scheduler.stop(timeout=0.5):
        raise RuntimeError("Progress scheduler did not stop")
    result = {
        "sdk": version("ulanzistudio-plugin-sdk-python"),
        "websocket_client": version("websocket-client"),
        "connect_parameters": list(parameters),
        "set_settings_parameters": list(settings_parameters),
        "display_parameters": list(display_parameters),
        "path_parameters": list(path_parameters),
        "handler_counts": handler_counts,
        "synthetic_commands": probe_client.commands,
        "callbacks_nonblocking": True,
        "worker_display_payload": display_payload,
        "worker_settings_payload": settings_payload,
    }
    if result["sdk"] != "0.1.0" or result["websocket_client"] != "1.8.0":
        raise RuntimeError(f"Unexpected locked versions: {result}")
    return result


if __name__ == "__main__":
    print(json.dumps(inspect_sdk(), sort_keys=True))
