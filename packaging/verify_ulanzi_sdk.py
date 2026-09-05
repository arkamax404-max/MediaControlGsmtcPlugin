import inspect
import json
import sys
import threading
import time
import tempfile
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
    from now_playing_action import (ACTION_UUID as NOW_PLAYING_UUID, AUDIO_ACTIONS,
                                    MOSAIC_ACTIONS, MUTE_TOGGLE_UUID, PREVIOUS_UUID,
                                    TOGGLE_UUID, TRANSPORT_DISPLAY,
                                    NowPlayingActionModel, mute_toggle_data_uri)
    from progress_action import ACTION_UUID, ProgressActionModel
    from largeitem_action import ACTION_UUID as LARGEITEM_ACTION_UUID
    from setup_action import (ACTION_UUID as SETUP_ACTION_UUID, BUILTIN_UUID,
                              SetupActionController)
    from progress_scheduler import ProgressScheduler, register_progress_handlers
    from transport_actions import TransportRouter, register_transport_handlers

    parameters = tuple(inspect.signature(UlanziApi.connect).parameters)
    if parameters[:len(EXPECTED_CONNECT_PARAMETERS)] != EXPECTED_CONNECT_PARAMETERS:
        raise RuntimeError(f"Unexpected UlanziApi.connect signature: {parameters}")
    settings_parameters = tuple(inspect.signature(UlanziApi.setSettings).parameters)
    if settings_parameters != EXPECTED_SET_SETTINGS_PARAMETERS:
        raise RuntimeError(f"Unexpected UlanziApi.setSettings signature: {settings_parameters}")
    inspector_parameters = tuple(inspect.signature(
        UlanziApi.sendToPropertyInspector).parameters)
    if inspector_parameters != ("self", "settings", "context") \
            or not callable(getattr(UlanziApi, "onSendToPlugin", None)):
        raise RuntimeError("UlanziApi property inspector messaging contract is unavailable")
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

        def execute(self, command, cancelled=None, audio_target=None):
            self.commands.append(command)
            if len(self.commands) == 7:
                self.completed.set()
            return BridgeResult(command, "ok")

        def get_state(self, cancelled=None):
            now = datetime.now(timezone.utc).isoformat()
            return BridgeStateResult("ok", {
                "available": True, "is_playing": len(self.commands) < 7, "title": "Track",
                "artist": "Artist", "artwork_id": "a" * 64,
                "audio_available": True, "volume_percent": 55,
                "is_muted": len(self.commands) >= 6, "audio_mixed": False,
                "audio_sources": [
                    {"target": "system", "label": "System volume",
                     "volume_percent": 55, "is_muted": False,
                     "session_count": 1, "mixed": False},
                    {"target": "process:spotify.exe", "label": "spotify",
                     "volume_percent": 55, "is_muted": len(self.commands) >= 6,
                     "session_count": 1, "mixed": False},
                ],
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
    setup_temp = tempfile.TemporaryDirectory()
    setup_root = Path(setup_temp.name)
    setup_package = setup_root / "10000000-0000-4000-8000-000000000000.ulanziProfile"
    setup_page = setup_package / "Profiles" / "20000000-0000-4000-8000-000000000000"
    setup_page.mkdir(parents=True)
    (setup_package / "manifest.json").write_text(json.dumps({
        "Device": {"Model": "D200", "UUID": "device"}, "Name": "SDK Probe",
        "Pages": {"Current": setup_page.name, "Pages": [setup_page.name]},
    }), "utf-8")
    setup_action_id = "30000000-0000-4000-8000-000000000000"
    (setup_page / "manifest.json").write_text(json.dumps({
        "Controllers": [{"Actions": {}}, {"Actions": {
            "1_1": {"Action": SETUP_ACTION_UUID, "ActionID": setup_action_id},
            "3_2": {"Action": BUILTIN_UUID,
                    "ActionID": "40000000-0000-4000-8000-000000000000"},
        }}],
    }), "utf-8")
    setup_controller = SetupActionController(api, lambda: setup_root,
                                             assistant_launcher=None)
    scheduler = ProgressScheduler(api, probe_client, ProgressActionModel(),
                                  NowPlayingActionModel(), ArtworkBundleCache(),
                                  setup_controller=setup_controller)
    router.configure_runtime(scheduler.handle_run, scheduler.request_poll,
                             scheduler.now_playing_model.audio_target_from_event)
    router.start()
    scheduler.start()
    register_transport_handlers(api, router)
    register_progress_handlers(api, scheduler)
    handler_counts = {
        name: len(api._listeners.get(name, []))
        for name in ("add", "run", "keydown", "keyup", "clear", "setactive", "paramfromplugin",
                      "didReceiveSettings", "sendToPlugin")
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
        "sendToPlugin": 1,
    }
    if handler_counts != expected_handler_counts:
        raise RuntimeError(f"Unexpected real SDK handler counts: {handler_counts}")
    api.emit("add", {"uuid": ACTION_UUID, "context": context, "param": {}})
    now_context = "now-uuid___now-key___now-action"
    api.emit("add", {"uuid": NOW_PLAYING_UUID, "context": now_context})
    large_context = f"{LARGEITEM_ACTION_UUID}___3_2___large-action"
    api.emit("add", {"uuid": LARGEITEM_ACTION_UUID, "context": large_context})
    mosaic_contexts = []
    for index, action in enumerate(MOSAIC_ACTIONS):
        mosaic_context = f"tile-{index}___tile-key-{index}___tile-action-{index}"
        mosaic_contexts.append(mosaic_context)
        api.emit("add", {"uuid": action, "context": mosaic_context})
    audio_contexts = []
    for index, action in enumerate(AUDIO_ACTIONS):
        audio_context = f"audio-{index}___audio-key-{index}___audio-action-{index}"
        audio_contexts.append(audio_context)
        api.emit("add", {"uuid": action, "context": audio_context})
    api.emit("sendToPlugin", {"context": audio_contexts[2],
                              "payload": {"type": "requestAudioSources"}})
    transport_contexts = []
    for index, action in enumerate(TRANSPORT_DISPLAY):
        transport_context = f"transport-{index}___transport-key-{index}___transport-action-{index}"
        transport_contexts.append(transport_context)
        api.emit("add", {"uuid": action, "context": transport_context})
    transport_uuids = {value.split("___")[0] for value in transport_contexts}
    deadline = time.monotonic() + 2
    while len([item for _, message in socket.messages
               for item in message.get("param", {}).get("statelist", [])
               if item.get("uuid") in transport_uuids]) < 3 \
            and time.monotonic() < deadline:
        time.sleep(0.005)
    deadline = time.monotonic() + 1
    while len(socket.messages) < 15 and time.monotonic() < deadline:
        time.sleep(0.005)
    if not socket.messages:
        raise RuntimeError("Integrated progress scheduler did not emit a display")
    inspector_messages = [item for item in socket.messages
                          if item[1].get("cmd") == "sendToPropertyInspector"]
    if not inspector_messages or inspector_messages[-1][1].get("payload") != {
            "audioSources": [{"target": "system", "label": "System volume"},
                             {"target": "process:spotify.exe", "label": "spotify"}]}:
        raise RuntimeError(f"Integrated inspector response is missing: {socket.messages}")
    settings_messages = [item for item in socket.messages if item[1].get("cmd") == "setSettings"]
    display_messages = [item for item in socket.messages if item[1].get("cmd") == "state"]
    if len(settings_messages) != 2 or not display_messages:
        raise RuntimeError(f"Integrated settings/display sends are missing: {socket.messages}")
    progress_settings = [item for item in settings_messages
                         if item[1].get("uuid") == "context-uuid"]
    large_settings = [item for item in settings_messages
                      if item[1].get("uuid") == LARGEITEM_ACTION_UUID]
    if len(progress_settings) != 1 or len(large_settings) != 1:
        raise RuntimeError(f"Integrated per-action settings sends are missing: {settings_messages}")
    settings_thread_id, settings_payload = progress_settings[0]
    progress_displays = [item for item in display_messages
                         if item[1].get("param", {}).get("statelist", [{}])[0].get("uuid")
                         == "context-uuid"]
    if not progress_displays:
        raise RuntimeError("Integrated progress display is missing")
    display_thread_id, display_payload = progress_displays[0]
    state_item = display_payload.get("param", {}).get("statelist", [{}])[0]
    if (display_thread_id == threading.get_ident()
            or state_item.get("uuid") != "context-uuid"
            or state_item.get("key") != "context-key"
            or state_item.get("actionid") != "context-action"
            or state_item.get("type") != 1
            or not state_item.get("data", "").startswith("data:image/svg+xml;base64,")):
        raise RuntimeError(f"Unexpected integrated legacy SDK payload: {display_payload}")
    large_items = [item for _, message in display_messages
                   for item in message.get("param", {}).get("statelist", [])
                   if item.get("uuid") == LARGEITEM_ACTION_UUID]
    if not large_items:
        raise RuntimeError("Integrated LargeItem display is missing")
    large_item = large_items[-1]
    try:
        import base64
        large_svg = base64.b64decode(large_item["data"].split(",", 1)[1]).decode("utf-8")
    except Exception as exc:
        raise RuntimeError("Integrated LargeItem payload is not a UTF-8 SVG data URI") from exc
    if (large_item.get("key") != "3_2"
            or large_item.get("actionid") != "large-action"
            or large_item.get("type") != 1
            or 'width="458" height="196" viewBox="0 0 458 196"' not in large_svg):
        raise RuntimeError(f"Unexpected integrated LargeItem payload: {large_item}")
    setup_context = f"{SETUP_ACTION_UUID}___1_1___{setup_action_id}"
    api.emit("add", {"uuid": SETUP_ACTION_UUID, "context": setup_context})
    setup_started = time.monotonic()
    api.emit("run", {"uuid": SETUP_ACTION_UUID, "context": setup_context})
    setup_callback_seconds = time.monotonic() - setup_started
    if setup_callback_seconds >= 0.25:
        raise RuntimeError(f"Setup callback blocked: {setup_callback_seconds:.6f}s")
    deadline = time.monotonic() + 1
    while not any(message.get("cmd") == "sendToPropertyInspector"
                  and message.get("uuid") == SETUP_ACTION_UUID
                  and message.get("payload", {}).get("setupStatus", {}).get("reason")
                  == "Live page identity verified" for _, message in socket.messages) \
            and time.monotonic() < deadline:
        time.sleep(0.005)
    api.emit("sendToPlugin", {"context": setup_context,
                              "payload": {"type": "requestSetupStatus"}})
    setup_inspectors = [message for _, message in socket.messages
                        if message.get("cmd") == "sendToPropertyInspector"
                        and message.get("uuid") == SETUP_ACTION_UUID]
    if (not setup_inspectors
            or setup_inspectors[-1].get("payload", {}).get("setupStatus") != {
                "status": "Ready", "reason": "Live page identity verified",
                "profileName": "SDK Probe",
                "packageId": "10000000-0000-4000-8000-000000000000",
                "profileId": "20000000-0000-4000-8000-000000000000",
            }):
        raise RuntimeError(f"Unexpected integrated Setup inspector payload: {setup_inspectors}")
    setup_items = [item for _, message in socket.messages
                   for item in message.get("param", {}).get("statelist", [])
                   if item.get("uuid") == SETUP_ACTION_UUID]
    if (len(setup_items) < 3
            or any(item.get("key") != "1_1" or item.get("actionid") != setup_action_id
                   or item.get("type") != 2
                   or item.get("path") != "./assets/setup-large-display.svg"
                   or item.get("textData") != "Ready" for item in setup_items)):
        raise RuntimeError(f"Unexpected integrated Setup payloads: {setup_items}")
    expected_large_settings = {
        "showArtwork": True, "pausedArtwork": "grayscale", "showProgress": True,
        "showElapsed": False, "showRemaining": True, "backgroundColor": "#0B0D10",
        "primaryColor": "#FFFFFF", "secondaryColor": "#B8BEC8",
        "accentColor": "#1DB954", "fit": "contain", "SmallViewMode": 2,
    }
    if (large_settings[0][1].get("key") != "3_2"
            or large_settings[0][1].get("settings") != expected_large_settings):
        raise RuntimeError(f"Unexpected integrated LargeItem settings: {large_settings[0][1]}")
    now_items = [item for _, message in display_messages
                 for item in message.get("param", {}).get("statelist", [])
                 if item.get("uuid") == "now-uuid"]
    if ([item.get("type") for item in now_items] != [2, 1]
            or now_items[0].get("path") != "./assets/music.svg"
            or now_items[0].get("textData") != "Track\nArtist"
            or now_items[1].get("data") != "data:image/png;base64,color"
            or now_items[1].get("textData") != "Track\nArtist"):
        raise RuntimeError(f"Unexpected integrated Now Playing payloads: {now_items}")
    mosaic_payloads = []
    for index, context_value in enumerate(mosaic_contexts):
        uuid, key, actionid = context_value.split("___")
        items = [item for _, message in display_messages
                 for item in message.get("param", {}).get("statelist", [])
                 if item.get("uuid") == uuid]
        mosaic_payloads.append(items)
        _, fallback, title = tuple(MOSAIC_ACTIONS.values())[index]
        if ([item.get("type") for item in items] != [2, 1]
                or items[0].get("path") != fallback
                or items[0].get("textData") != title
                or items[1].get("data") != ("tl", "tr", "bl", "br")[index]
                or items[1].get("textData") != ""
                or [item.get("showtext") for item in items] != [True, False]
                or any(item.get("key") != key or item.get("actionid") != actionid
                       for item in items)):
            raise RuntimeError(f"Unexpected integrated mosaic payloads: {items}")
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
    if len([item for item in socket.messages if item[1].get("cmd") == "setSettings"]) != 2:
        raise RuntimeError("Canonical settings echo caused a persistence loop")
    started_at = time.monotonic()
    api.emit("run", {"uuid": "com.arkamax404.ulanzi.mediacontrol.previous"})
    for _ in range(3):
        api.emit("run", {"uuid": "com.arkamax404.ulanzi.mediacontrol.volume-up"})
    api.emit("run", {"uuid": NOW_PLAYING_UUID, "context": now_context})
    api.emit("run", {"uuid": MUTE_TOGGLE_UUID, "context": audio_contexts[2]})
    api.emit("run", {"uuid": TOGGLE_UUID, "context": transport_contexts[0]})
    callback_seconds = time.monotonic() - started_at
    if callback_seconds >= 0.25 or not probe_client.completed.wait(1):
        raise RuntimeError(f"Real SDK run callback blocked: {callback_seconds:.6f}s")
    expected_commands = ["previous", "volume-up", "volume-up", "volume-up", "toggle",
                         "mute-toggle", "toggle"]
    if probe_client.commands != expected_commands:
        raise RuntimeError(f"Unexpected real SDK routing: {probe_client.commands}")
    def state_items():
        return [item for _, message in socket.messages
                for item in message.get("param", {}).get("statelist", [])]
    mute_uuid = audio_contexts[2].split("___")[0]
    deadline = time.monotonic() + 1
    while len([item for item in state_items() if item.get("uuid") == mute_uuid
               and item.get("type") == 1]) < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    audio_payloads = []
    for index, (action, icon) in enumerate(AUDIO_ACTIONS.items()):
        uuid, key, actionid = audio_contexts[index].split("___")
        items = [item for item in state_items() if item.get("uuid") == uuid]
        audio_payloads.append(items)
        if action == MUTE_TOGGLE_UUID:
            if ([(item.get("type"), item.get("path"), item.get("data"), item.get("textData"),
                   item.get("showtext"), item.get("key"), item.get("actionid"))
                  for item in items] != [
                    (1, None, mute_toggle_data_uri("55%", False), "", False, key, actionid),
                    (1, None, mute_toggle_data_uri("Muted", True), "", False, key, actionid)]):
                raise RuntimeError(f"Unexpected integrated mute-toggle payloads: {items}")
        elif ([(item.get("type"), item.get("path"), item.get("textData"),
                item.get("showtext"), item.get("key"), item.get("actionid"))
               for item in items] != [
                (2, icon, "55%", True, key, actionid),
                (2, icon, "Muted", True, key, actionid)]):
            raise RuntimeError(f"Unexpected integrated audio payloads: {items}")
    toggle_display_uuid = transport_contexts[0].split("___")[0]
    deadline = time.monotonic() + 1
    while len([item for item in state_items() if item.get("uuid") == toggle_display_uuid
               and item.get("path") == "./assets/play.svg"]) < 1 \
            and time.monotonic() < deadline:
        time.sleep(0.005)
    transport_payloads = []
    for index, (action, icon) in enumerate(TRANSPORT_DISPLAY.items()):
        uuid, key, actionid = transport_contexts[index].split("___")
        items = [item for item in state_items() if item.get("uuid") == uuid]
        transport_payloads.append(items)
        if action == TOGGLE_UUID:
            if ([(item.get("type"), item.get("path"), item.get("textData"),
                   item.get("showtext"), item.get("key"), item.get("actionid"))
                  for item in items] != [
                    (2, "./assets/pause.svg", "Pause", True, key, actionid),
                    (2, "./assets/play.svg", "Play", True, key, actionid)]):
                raise RuntimeError(f"Unexpected integrated toggle payloads: {items}")
        elif ([(item.get("type"), item.get("path"), item.get("textData"),
                item.get("showtext"), item.get("key"), item.get("actionid"))
               for item in items] != [
                (2, icon, "Previous" if action == PREVIOUS_UUID else "Next",
                 True, key, actionid)]):
            raise RuntimeError(f"Unexpected integrated transport payloads: {items}")
    if any(thread.name == "ulanzi-volume-repeat" for thread in threading.enumerate()):
        raise RuntimeError("Unexpected volume repeat scheduler thread")
    if not router.stop(timeout=0.5):
        raise RuntimeError("Transport worker did not stop")
    if not scheduler.stop(timeout=0.5):
        raise RuntimeError("Progress scheduler did not stop")
    setup_temp.cleanup()
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
        "setup_callback_seconds": setup_callback_seconds,
        "mosaic_payloads": mosaic_payloads,
        "audio_payloads": audio_payloads,
        "transport_payloads": transport_payloads,
        "largeitem_payload_bytes": len(large_item["data"].encode("ascii")),
        "setup_payloads": setup_items,
        "worker_display_payload": display_payload,
        "worker_settings_payload": settings_payload,
    }
    if result["sdk"] != "0.1.0" or result["websocket_client"] != "1.8.0":
        raise RuntimeError(f"Unexpected locked versions: {result}")
    return result


if __name__ == "__main__":
    print(json.dumps(inspect_sdk(), sort_keys=True))
