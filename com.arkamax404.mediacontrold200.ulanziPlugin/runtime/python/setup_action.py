from __future__ import annotations

import json
import os
import stat
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


ACTION_UUID = "com.arkamax404.ulanzi.mediacontrol.setup-large-display"
LARGEITEM_UUID = "com.arkamax404.ulanzi.mediacontrol.largeitem-nowplaying"
BUILTIN_UUID = "com.ulanzi.ulanzideck.smallwindow.window"
ICON = "./assets/setup-large-display.svg"
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_MANIFESTS = 10_000
MAX_PACKAGES = 256
PROBE_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class SetupProbe:
    status: str
    reason: str
    package_id: str | None = None
    profile_id: str | None = None
    profile_name: str = ""
    controller_index: int | None = None
    setup_key: str | None = None


@dataclass
class _Context:
    action_id: str
    generation: int
    active: bool = True
    status: str = "Ready"
    reason: str = "Press Setup to verify this page"
    package_id: str | None = None
    profile_id: str | None = None
    profile_name: str = ""
    operation: str = "install"
    launch_reserved: bool = False
    launch_generation: int | None = None
    launch_operation: str | None = None
    launch_id: str | None = None


def default_profiles_root() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise ValueError("APPDATA is unavailable")
    return Path(appdata) / "Ulanzi" / "UlanziDeck" / "ProfilesV2"


def default_assistant_launcher(action_id: str, operation: str, launch_id: str) -> bool:
    from profile_assistant import launch_profile_assistant
    return launch_profile_assistant(action_id, operation, launch_id=launch_id)


def default_assistant_status() -> Mapping[str, object] | None:
    local_appdata = os.environ.get("LOCALAPPDATA")
    if not local_appdata:
        return None
    path = (Path(local_appdata) / "GSMTCD200Controller" /
            "profile-assistant-state" / "status.json")
    value = _read_manifest(path)
    return value if isinstance(value, Mapping) else None


def probe_setup_action(profiles_root: Path, action_id: str,
                       cancelled: Callable[[], bool] = lambda: False,
                       monotonic: Callable[[], float] = time.monotonic,
                       timeout: float = PROBE_TIMEOUT_SECONDS) -> SetupProbe:
    action_id = _canonical_uuid(action_id, "setup action")
    root = Path(profiles_root)
    if not _safe_directory_chain(root):
        return SetupProbe("Failed", "ProfilesV2 is unavailable or unsafe")
    matches = []
    scanned = 0
    package_count = 0
    deadline = monotonic() + max(0.01, timeout)
    try:
        packages = root.iterdir()
    except OSError:
        return SetupProbe("Failed", "ProfilesV2 could not be enumerated")
    for package in packages:
        if cancelled():
            return SetupProbe("Failed", "Profile scan was cancelled")
        if monotonic() > deadline:
            return SetupProbe("Failed", "Profile scan timed out")
        if not package.name.endswith(".ulanziProfile") or not _safe_directory(package):
            continue
        package_count += 1
        if package_count > MAX_PACKAGES:
            return SetupProbe("Failed", "ProfilesV2 contains too many packages")
        package_id = package.name.removesuffix(".ulanziProfile")
        try:
            _canonical_uuid(package_id, "package")
        except ValueError:
            continue
        package_manifest = _read_manifest(package / "manifest.json")
        device = package_manifest.get("Device") if isinstance(package_manifest, Mapping) else None
        if (package_manifest is None or not isinstance(device, Mapping)
                or device.get("Model") != "D200"):
            continue
        profiles = package / "Profiles"
        if not _safe_directory(profiles):
            continue
        pages = package_manifest.get("Pages")
        if not isinstance(pages, Mapping) or not isinstance(pages.get("Pages"), list):
            continue
        page_ids = list(pages["Pages"])
        if pages.get("Current") not in page_ids:
            page_ids.append(pages.get("Current"))
        visited = set()
        page_index = 0
        while page_index < len(page_ids):
            candidate = page_ids[page_index]
            page_index += 1
            if cancelled():
                return SetupProbe("Failed", "Profile scan was cancelled")
            if monotonic() > deadline:
                return SetupProbe("Failed", "Profile scan timed out")
            scanned += 1
            if scanned > MAX_MANIFESTS:
                return SetupProbe("Failed", "ProfilesV2 contains too many pages")
            try:
                page_id = _canonical_uuid(candidate, "page")
            except ValueError:
                continue
            if page_id in visited:
                continue
            visited.add(page_id)
            page = profiles / page_id
            if not _safe_directory(page):
                continue
            document = _read_manifest(page / "manifest.json")
            if document is None:
                continue
            controllers = document.get("Controllers")
            if not isinstance(controllers, list):
                continue
            for reference in _profile_references(document):
                if reference not in visited:
                    page_ids.append(reference)
            for controller_index, controller in enumerate(controllers):
                actions = controller.get("Actions") if isinstance(controller, Mapping) else None
                if not isinstance(actions, Mapping):
                    continue
                for key, entry in actions.items():
                    if (isinstance(entry, Mapping) and entry.get("ActionID") == action_id
                            and entry.get("Action") == ACTION_UUID):
                        matches.append((package_id, page_id, package_manifest, document,
                                        controller_index, str(key)))
    if len(matches) != 1:
        return SetupProbe(
            "Failed",
            "Setup action was not found" if not matches else "Setup action is not unique",
        )
    package_id, page_id, package_manifest, document, controller_index, setup_key = matches[0]
    controller = document["Controllers"][controller_index]
    actions = controller.get("Actions") if isinstance(controller, Mapping) else None
    center = actions.get("3_2") if isinstance(actions, Mapping) else None
    if not isinstance(center, Mapping):
        return SetupProbe("Failed", "The page does not contain one center display",
                          package_id, page_id, str(package_manifest.get("Name") or ""),
                          controller_index, setup_key)
    center_action = center.get("Action")
    profile_name = str(package_manifest.get("Name") or "")
    if center_action == LARGEITEM_UUID:
        return SetupProbe("Installed", "Large Now Playing is assigned", package_id,
                          page_id, profile_name, controller_index, setup_key)
    if center_action != BUILTIN_UUID:
        return SetupProbe("Failed", "The center display contains another action",
                          package_id, page_id, profile_name, controller_index, setup_key)
    return SetupProbe("Ready", "Live page identity verified", package_id, page_id,
                      profile_name, controller_index, setup_key)


class SetupActionController:
    def __init__(self, api, profiles_root_factory: Callable[[], Path] = default_profiles_root,
                 assistant_launcher: Callable[[str, str, str], bool] | None = default_assistant_launcher,
                 assistant_status: Callable[[], Mapping[str, object] | None] = default_assistant_status,
                 launch_id_factory: Callable[[], uuid.UUID] = uuid.uuid4) -> None:
        self.api = api
        self._profiles_root_factory = profiles_root_factory
        self._assistant_launcher = assistant_launcher
        self._assistant_status = assistant_status
        self._launch_id_factory = launch_id_factory
        self._lock = threading.Lock()
        self._contexts: dict[str, _Context] = {}
        self._shutdown = False
        self._next_generation = 0
        self._worker: threading.Thread | None = None
        self._cancel = threading.Event()

    def add(self, event: object) -> bool:
        identity = _event_identity(event)
        if identity is None:
            return False
        context, action_id = identity
        with self._lock:
            if self._shutdown:
                return False
            self._next_generation += 1
            raw = event.get("param") if isinstance(event, Mapping) else None
            operation = raw.get("operation") if isinstance(raw, Mapping) else None
            self._contexts[context] = _Context(
                action_id, self._next_generation,
                operation=operation if operation in ("install", "repair", "restore") else "install",
            )
            self._apply_durable_status(self._contexts[context])
        self._publish(context)
        return True

    def run(self, event: object) -> bool:
        context = event.get("context") if isinstance(event, Mapping) else None
        if not isinstance(context, str):
            return False
        with self._lock:
            entry = self._contexts.get(context)
            if self._shutdown or entry is None or not entry.active:
                return False
            if entry.launch_reserved or entry.status in {"Launching", "Waiting for Studio to close"}:
                entry.reason = "A Profile Assistant operation is already active"
                busy = True
            else:
                busy = False
            if self._worker is not None and self._worker.is_alive():
                entry.status = "Failed"
                entry.reason = "Another Setup check is running"
                busy = True
            if busy:
                worker = None
            else:
                action_id, generation = entry.action_id, entry.generation
                entry.status = "Ready"
                entry.reason = "Checking live page identity"
                self._worker = threading.Thread(
                    target=self._probe,
                    args=(context, action_id, generation),
                    name="ulanzi-setup-probe",
                    daemon=True,
                )
                worker = self._worker
        self._publish(context)
        if worker is None:
            return False
        worker.start()
        return True

    def clear(self, event: object) -> bool:
        items = event.get("param", ()) if isinstance(event, Mapping) else ()
        try:
            contexts = tuple(
                item.get("context") for item in tuple(items)
                if isinstance(item, Mapping) and isinstance(item.get("context"), str)
            )
        except Exception:
            contexts = ()
        with self._lock:
            changed = False
            for context in contexts:
                changed |= self._contexts.pop(context, None) is not None
            return changed

    def set_active(self, event: object) -> bool:
        context = event.get("context") if isinstance(event, Mapping) else None
        if not isinstance(context, str):
            return False
        with self._lock:
            entry = self._contexts.get(context)
            if self._shutdown or entry is None:
                return False
            if entry.launch_reserved:
                return False
            entry.active = not (isinstance(event, Mapping) and event.get("active") is False)
            active = entry.active
        if active:
            self._publish(context)
        return True

    def inspector_message(self, event: object) -> bool:
        payload = event.get("payload") if isinstance(event, Mapping) else None
        if not isinstance(payload, Mapping) or payload.get("type") != "requestSetupStatus":
            return False
        context = event.get("context")
        with self._lock:
            if not isinstance(context, str) or context not in self._contexts:
                return False
            self._apply_durable_status(self._contexts[context])
        self._publish(context)
        return True

    def has_context(self, context: object) -> bool:
        with self._lock:
            return isinstance(context, str) and context in self._contexts

    def receive_settings(self, event: object, persist: bool = False) -> bool:
        if not isinstance(event, Mapping):
            return False
        context, raw = event.get("context"), event.get("settings")
        operation = raw.get("operation") if isinstance(raw, Mapping) else None
        if not isinstance(context, str) or operation not in ("install", "repair", "restore"):
            return False
        with self._lock:
            entry = self._contexts.get(context)
            if self._shutdown or entry is None:
                return False
            if entry.launch_reserved:
                return False
            if entry.operation != operation:
                self._next_generation += 1
                entry.generation = self._next_generation
                entry.status = "Ready"
                entry.reason = "Operation changed; press Setup to validate again"
            entry.operation = operation
        if persist:
            try:
                self.api.setSettings({"operation": operation}, context)
            except Exception:
                return False
        self._publish(context)
        return True

    def shutdown(self) -> None:
        with self._lock:
            self._shutdown = True
            self._cancel.set()
            self._contexts.clear()
            worker = self._worker
        if worker is not None and worker.is_alive() and threading.current_thread() is not worker:
            worker.join(0.5)

    def _probe(self, context: str, action_id: str, generation: int) -> None:
        try:
            probe = probe_setup_action(
                self._profiles_root_factory(), action_id, cancelled=self._cancel.is_set)
        except Exception:
            probe = SetupProbe("Failed", "Setup validation failed")
        with self._lock:
            entry = self._contexts.get(context)
            if (self._shutdown or entry is None or not entry.active
                    or (entry.action_id, entry.generation) != (action_id, generation)):
                return
            entry.status, entry.reason = probe.status, probe.reason
            entry.package_id, entry.profile_id = probe.package_id, probe.profile_id
            entry.profile_name = probe.profile_name
            operation = entry.operation
            should_launch = ((operation in ("install", "repair") and probe.status == "Ready")
                             or (operation == "restore" and probe.status == "Installed"))
            should_launch &= self._assistant_launcher is not None
            if should_launch:
                launch_id = _canonical_uuid(str(self._launch_id_factory()), "launch")
                entry.launch_reserved = True
                entry.launch_generation = generation
                entry.launch_operation = operation
                entry.launch_id = launch_id
            else:
                launch_id = None
        if should_launch:
            launched = False
            try:
                launched = bool(self._assistant_launcher(action_id, operation, str(launch_id)))
            except Exception:
                launched = False
            with self._lock:
                entry = self._contexts.get(context)
                if (self._shutdown or entry is None
                        or (entry.action_id, entry.generation) != (action_id, generation)
                        or entry.operation != operation
                        or not entry.launch_reserved
                        or entry.launch_generation != generation
                        or entry.launch_operation != operation
                        or entry.launch_id != launch_id):
                    return
                if not launched:
                    entry.launch_reserved = False
                    entry.launch_generation = None
                    entry.launch_operation = None
                    entry.launch_id = None
                entry.status = "Waiting for Studio to close" if launched else "Failed"
                entry.reason = ("Close Ulanzi Studio to continue" if launched
                                else "Profile Assistant could not be started")
        elif operation == "restore" and probe.status != "Installed":
            with self._lock:
                entry = self._contexts.get(context)
                if entry is not None:
                    entry.status, entry.reason = "Failed", "Restore requires an installed LargeItem"
        self._publish(context)

    def _apply_durable_status(self, entry: _Context) -> None:
        try:
            status = self._assistant_status()
        except Exception:
            return
        if not isinstance(status, Mapping):
            return
        if status.get("action_id") != entry.action_id:
            return
        state = status.get("state")
        if entry.launch_reserved:
            direct_match = (status.get("operation") == entry.launch_operation
                            and status.get("launch_id") == entry.launch_id)
            recovery_match = (status.get("request_disposition") == "recovery_only"
                              and status.get("request_action_id") == entry.action_id
                              and status.get("request_operation") == entry.launch_operation
                              and status.get("request_launch_id") == entry.launch_id)
            if not direct_match and not recovery_match:
                return
        if state in {"failed", "rolled_back", "manual_recovery_required", "succeeded"}:
            entry.launch_reserved = False
            entry.launch_generation = None
            entry.launch_operation = None
            entry.launch_id = None
        if state == "launching":
            entry.status, entry.reason = "Launching", "Profile Assistant is starting"
        elif state == "waiting":
            entry.status, entry.reason = ("Waiting for Studio to close",
                                          "Close Ulanzi Studio to continue")
        elif state in {"prepared", "manual_recovery_required"}:
            entry.status, entry.reason = "Failed", "Profile Assistant recovery is required"
        elif state in {"failed", "rolled_back"}:
            failure = status.get("relaunch_failure") or status.get("failure")
            entry.status = "Failed"
            if status.get("profile_result") == "succeeded" and status.get("relaunch_failure"):
                entry.reason = f"Profile updated, but Studio restart failed: {failure}"
            else:
                entry.reason = str(failure or "Profile Assistant failed")
        elif state == "succeeded":
            if status.get("request_disposition") == "recovery_only":
                actual = str(status.get("operation") or "operation").title()
                requested = str(status.get("request_operation") or "operation").title()
                entry.status = "Restored" if status.get("operation") == "restore" else "Installed"
                entry.reason = f"Recovered prior {actual}; press Setup again to run {requested}"
            elif status.get("operation") == "restore":
                entry.status, entry.reason = "Restored", "Original center action restored successfully"
            else:
                entry.status, entry.reason = "Installed", "Profile Assistant completed successfully"

    def _publish(self, context: str) -> None:
        with self._lock:
            entry = self._contexts.get(context)
            if self._shutdown or entry is None or not entry.active:
                return
            payload = {
                "status": entry.status,
                "reason": entry.reason,
                "profileName": entry.profile_name,
                "packageId": entry.package_id or "",
                "profileId": entry.profile_id or "",
            }
        try:
            self.api.setPathIcon(context, ICON, payload["status"])
        except Exception:
            pass
        try:
            self.api.sendToPropertyInspector({"setupStatus": payload}, context)
        except Exception:
            pass


def _event_identity(event: object) -> tuple[str, str] | None:
    if not isinstance(event, Mapping) or event.get("uuid", event.get("action")) != ACTION_UUID:
        return None
    context = event.get("context")
    if not isinstance(context, str) or not context:
        return None
    action_id = event.get("actionid")
    parts = context.split("___", 2)
    context_action_id = parts[2] if len(parts) == 3 else None
    if isinstance(action_id, str) and action_id != context_action_id:
        return None
    action_id = action_id if isinstance(action_id, str) else context_action_id
    try:
        return context, _canonical_uuid(action_id, "setup action")
    except ValueError:
        return None


def _canonical_uuid(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Invalid {label} UUID")
    canonical = str(uuid.UUID(value))
    if canonical != value:
        raise ValueError(f"Non-canonical {label} UUID")
    return canonical


def _safe_directory(path: Path) -> bool:
    try:
        info = path.lstat()
        attributes = getattr(info, "st_file_attributes", 0)
        return stat.S_ISDIR(info.st_mode) and not path.is_symlink() and not (
            attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )
    except OSError:
        return False


def _safe_directory_chain(path: Path) -> bool:
    current = path
    while True:
        if not _safe_directory(current):
            return False
        if current.parent == current:
            return True
        current = current.parent


def _read_manifest(path: Path) -> dict | None:
    try:
        info = path.lstat()
        attributes = getattr(info, "st_file_attributes", 0)
        if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
                or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                or not 1 <= info.st_size <= MAX_MANIFEST_BYTES):
            return None
        value = json.loads(path.read_text("utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _profile_references(value: object) -> tuple[str, ...]:
    found = []

    def visit(item: object) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if key == "ProfileUUID":
                    try:
                        found.append(_canonical_uuid(child, "profile reference"))
                    except ValueError:
                        continue
                else:
                    visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return tuple(found)
