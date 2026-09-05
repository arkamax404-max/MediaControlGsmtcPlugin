#!/usr/bin/env python3
"""Inspect and safely clone exported Ulanzi Studio Version 2 profiles."""
from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path


HEADER = b"#Version: 2\n"
BUILTIN_ACTION = "com.ulanzi.ulanzideck.smallwindow.window"
LARGEITEM_SUFFIX = "largeitem-nowplaying"
PROFILE_RE = re.compile(r"(?:^|/)Profiles/([^/]+)/manifest\.json$")
PACKAGE_RE = re.compile(r"^([^/]+)\.ulanziProfile/")
MAX_MEMBERS = 10_000
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_COMPRESSION_RATIO = 500


class ProfileError(Exception):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def studio_is_running() -> bool:
    if os.name != "nt":
        return False
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq UlanziDeck.exe", "/FO", "CSV", "/NH"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProfileError("Could not verify whether Ulanzi Studio is running") from exc
    return "ulanzideck.exe" in result.stdout.lower()


def read_archive(path: Path) -> tuple[bytes, zipfile.ZipFile]:
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ProfileError("Profile archive exceeds the input size limit")
    data = path.read_bytes()
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ProfileError("Profile archive exceeds the input size limit")
    if not data.startswith(HEADER):
        raise ProfileError("Input must start with the exact '#Version: 2\\n' header")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data[len(HEADER):]), "r")
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ProfileError("Duplicate ZIP member names are not supported")
        if len(names) != len({name.casefold() for name in names}):
            raise ProfileError("Case-colliding ZIP member names are not supported")
        if len(names) > MAX_MEMBERS:
            raise ProfileError("Profile contains too many ZIP members")
        total_size = 0
        for info in archive.infolist():
            _validate_member_name(info.filename)
            if info.flag_bits & 1:
                raise ProfileError("Encrypted ZIP members are not supported")
            if info.file_size > MAX_MEMBER_BYTES:
                raise ProfileError(f"ZIP member exceeds the size limit: {info.filename}")
            total_size += info.file_size
            if total_size > MAX_TOTAL_BYTES:
                raise ProfileError("Expanded profile exceeds the total size limit")
            if info.file_size and info.compress_size == 0:
                raise ProfileError(f"ZIP member has an invalid compression size: {info.filename}")
            if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                raise ProfileError(f"ZIP member exceeds the compression ratio limit: {info.filename}")
        bad = archive.testzip()
        if bad:
            raise ProfileError(f"Corrupt ZIP member: {bad}")
        return data, archive
    except zipfile.BadZipFile as exc:
        raise ProfileError("Profile payload is not a valid ZIP archive") from exc


def package_identity(archive: zipfile.ZipFile):
    roots = {
        match.group(1) for name in archive.namelist()
        if (match := PACKAGE_RE.match(name))
    }
    if len(roots) != 1:
        raise ProfileError(f"Expected exactly one .ulanziProfile package root; found {len(roots)}")
    package_id = next(iter(roots))
    _canonical_uuid(package_id, "package")
    root = f"{package_id}.ulanziProfile"
    if any(name != root and not name.startswith(root + "/") for name in archive.namelist()):
        raise ProfileError("Every ZIP member must be inside the single package root")
    root_name = f"{root}/manifest.json"
    if root_name not in archive.namelist():
        raise ProfileError("Package root manifest.json is missing")
    root_manifest = _json_member(archive, root_name)
    profile_ids: set[str] = set()
    manifests = {}
    for info in archive.infolist():
        match = PROFILE_RE.search(info.filename)
        if match:
            profile_id = _canonical_uuid(match.group(1), "profile/page")
            profile_ids.add(profile_id)
            manifests[info.filename] = _json_member(archive, info.filename)
    if not profile_ids:
        raise ProfileError("No profile/page manifests were found")
    return package_id, root, root_name, root_manifest, profile_ids, manifests


def candidates(archive: zipfile.ZipFile) -> list[dict[str, object]]:
    package_id, root, _, root_manifest, _, manifests = package_identity(archive)
    result = []
    for manifest_name, document in manifests.items():
        profile_id = PROFILE_RE.search(manifest_name).group(1)
        controllers = document.get("Controllers", [])
        if not isinstance(controllers, list):
            continue
        for controller_index, controller in enumerate(controllers):
            entry = (controller.get("Actions") or {}).get("3_2") if isinstance(controller, dict) else None
            if isinstance(entry, dict):
                result.append({
                    "package_id": package_id,
                    "package_root": root,
                    "profile_id": profile_id,
                    "name": document.get("Name") or root_manifest.get("Name", ""),
                    "action": entry.get("Action", ""),
                    "action_id": entry.get("ActionID", ""),
                    "manifest": manifest_name,
                    "controller_index": controller_index,
                    "entry": entry,
                })
    return result


def inspect_profile(path: Path) -> None:
    _, archive = read_archive(path)
    found = candidates(archive)
    if not found:
        raise ProfileError("No Controllers[*].Actions['3_2'] candidates found")
    fields = ("package_id", "profile_id", "name", "action", "action_id", "manifest",
              "controller_index")
    for item in found:
        print(json.dumps({key: item[key] for key in fields}, ensure_ascii=False, sort_keys=True))


def load_plugin_contract(path: Path) -> dict[str, str]:
    try:
        manifest = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProfileError("Plugin manifest is unavailable or invalid") from exc
    plugin_uuid = manifest.get("UUID")
    version = manifest.get("Version")
    name = manifest.get("Name")
    action_uuid = f"{plugin_uuid}.{LARGEITEM_SUFFIX}"
    actions = manifest.get("Actions")
    matches = [item for item in actions if isinstance(item, dict) and item.get("UUID") == action_uuid] \
        if isinstance(actions, list) else []
    if (not all(isinstance(value, str) and value for value in (plugin_uuid, version, name))
            or len(matches) != 1):
        raise ProfileError("Plugin manifest does not expose exactly one Large Now Playing action")
    action_name = matches[0].get("Name")
    if not isinstance(action_name, str) or not action_name:
        raise ProfileError("Large Now Playing action name is invalid")
    return {"uuid": plugin_uuid, "version": version, "name": name,
            "action_uuid": action_uuid, "action_name": action_name}


def patch(input_path: Path, output_path: Path, profile_id: str, plugin_manifest: Path,
          clone_name: str | None = None, uuid_factory=None,
          running_check=studio_is_running) -> dict[str, object]:
    if running_check():
        raise ProfileError("Close Ulanzi Studio before cloning a profile")
    if input_path.resolve() == output_path.resolve():
        raise ProfileError("In-place patching is forbidden; choose a different output path")
    receipt_path = output_path.with_name(output_path.name + ".receipt.json")
    if output_path.exists() or receipt_path.exists():
        raise ProfileError("Output or receipt already exists; choose a new filename")
    if not output_path.parent.is_dir():
        raise ProfileError("Output parent directory must already exist")
    contract = load_plugin_contract(plugin_manifest)
    source_data, archive = read_archive(input_path)
    (old_package_id, old_root, root_manifest_name, root_manifest,
     old_profile_ids, manifests) = package_identity(archive)
    device = root_manifest.get("Device")
    if not isinstance(device, dict) or str(device.get("Model", "")).upper() != "D200":
        raise ProfileError("Source profile must target a D200 device")
    matches = [item for item in candidates(archive) if item["profile_id"] == profile_id]
    if len(matches) != 1:
        raise ProfileError(f"--profile-id must select exactly one candidate; matched {len(matches)}")
    selected = matches[0]
    if selected["action"] != BUILTIN_ACTION:
        raise ProfileError(f"Selected action is '{selected['action']}', not the built-in small-window action")

    factory = uuid_factory or uuid.uuid4
    old_action_ids = collect_key_values(manifests.values(), "ActionID")
    for action_id in old_action_ids:
        _canonical_uuid(action_id, "action")
    forbidden = {old_package_id, *old_profile_ids, *old_action_ids}
    new_package_id = _fresh_uuid(factory, forbidden)
    profile_map = {
        old: _fresh_uuid(factory, forbidden) for old in sorted(old_profile_ids)
    }
    transformed = {
        name: _walk_transform(copy.deepcopy(document), profile_map, factory, forbidden)
        for name, document in manifests.items()
    }
    cloned_root = copy.deepcopy(root_manifest)
    old_name = str(cloned_root.get("Name") or "Profile")
    new_name = clone_name or f"{old_name} Media Control"
    if not new_name.strip() or new_name == old_name:
        raise ProfileError("Clone name must be non-empty and different from the source name")
    cloned_root["Name"] = new_name
    pages = cloned_root.get("Pages")
    if not isinstance(pages, dict) or pages.get("Current") not in profile_map:
        raise ProfileError("Root Pages.Current is missing or unresolved")
    listed_pages = pages.get("Pages")
    if not isinstance(listed_pages, list) or any(item not in profile_map for item in listed_pages):
        raise ProfileError("Root Pages.Pages contains an unresolved profile/page UUID")
    pages["Current"] = profile_map[pages["Current"]]
    pages["Pages"] = [profile_map[item] for item in listed_pages]

    before = copy.deepcopy(selected["entry"])
    action_id = _fresh_uuid(factory, forbidden)
    after = _new_entry(action_id, contract)
    transformed[selected["manifest"]]["Controllers"][selected["controller_index"]]["Actions"]["3_2"] = after
    new_root = f"{new_package_id}.ulanziProfile"
    replacements = {root_manifest_name: cloned_root, **transformed}
    output_data = _write_clone(archive, old_root, new_root, profile_map, replacements)
    validation = _validate_clone(
        source_data, output_data, old_package_id, new_package_id, old_profile_ids,
        profile_map, old_action_ids, profile_id, selected, after,
    )
    receipt = {
        "schema": "com.arkamax404.ulanzi.mediacontrol.profile-clone-patch/v1",
        "input_sha256": sha256(source_data),
        "output_sha256": sha256(output_data),
        "source_package_id": old_package_id,
        "clone_package_id": new_package_id,
        "source_name": old_name,
        "clone_name": new_name,
        "source_profile_id": profile_id,
        "clone_profile_id": profile_map[profile_id],
        "profile_id_map": profile_map,
        "action_ids_regenerated": validation["action_count"],
        "manifest_member": _rename_member(
            selected["manifest"], old_root, new_root, profile_map),
        "controller_index": selected["controller_index"],
        "key": "3_2",
        "new_action_id": action_id,
        "semantic_diff": {"before": before, "after": after},
        "plugin_manifest_sha256": sha256(plugin_manifest.read_bytes()),
        "validation": validation["receipt"],
        "rollback": "Import the untouched original exported profile; it remains the rollback authority.",
    }
    temp_output = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
    temp_receipt = receipt_path.with_name(f".{receipt_path.name}.{uuid.uuid4().hex}.tmp")
    published_output = False
    published_receipt = False
    try:
        temp_output.write_bytes(output_data)
        read_back, _ = read_archive(temp_output)
        if read_back != output_data:
            raise ProfileError("Output read-back differs from the validated clone")
        temp_receipt.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.link(temp_receipt, receipt_path)
        published_receipt = True
        os.link(temp_output, output_path)
        published_output = True
    except Exception:
        if published_output:
            _unlink_if_same(output_path, temp_output)
        if published_receipt:
            _unlink_if_same(receipt_path, temp_receipt)
        raise
    finally:
        temp_output.unlink(missing_ok=True)
        temp_receipt.unlink(missing_ok=True)
    print(json.dumps({
        "output": str(output_path), "receipt": str(receipt_path),
        "output_sha256": receipt["output_sha256"],
        "clone_profile_id": profile_map[profile_id],
        "clone_package_id": new_package_id, "action_id": action_id,
    }, sort_keys=True))
    return receipt


def deterministic_uuid_factory(seed: str):
    counter = 0

    def factory():
        nonlocal counter
        counter += 1
        return uuid.uuid5(uuid.NAMESPACE_URL, f"media-control-largeitem:{seed}:{counter}")

    return factory


def collect_key_values(documents, wanted: str) -> list[str]:
    values = []

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == wanted:
                    values.append(str(item))
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for document in documents:
        visit(document)
    return values


def _json_member(archive: zipfile.ZipFile, name: str):
    if archive.getinfo(name).file_size > MAX_MANIFEST_BYTES:
        raise ProfileError(f"Manifest exceeds the size limit: {name}")
    try:
        value = json.loads(archive.read(name).decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProfileError(f"Invalid JSON manifest: {name}") from exc
    if not isinstance(value, dict):
        raise ProfileError(f"Manifest must contain a JSON object: {name}")
    return value


def _validate_member_name(name: str) -> None:
    if (not name or "\\" in name or name.startswith("/") or "\x00" in name
            or re.match(r"^[A-Za-z]:", name)):
        raise ProfileError(f"Unsafe ZIP member path: {name!r}")
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts[:-1]) or parts[-1] in (".", ".."):
        raise ProfileError(f"Unsafe ZIP member path: {name!r}")


def _canonical_uuid(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ProfileError(f"Invalid {label} UUID")
    try:
        canonical = str(uuid.UUID(value))
    except ValueError as exc:
        raise ProfileError(f"Invalid {label} UUID: {value}") from exc
    if canonical != value:
        raise ProfileError(f"Non-canonical {label} UUID: {value}")
    return canonical


def _unlink_if_same(published: Path, temporary: Path) -> None:
    try:
        if os.path.samefile(published, temporary):
            published.unlink()
    except (FileNotFoundError, OSError):
        return


def _fresh_uuid(factory, forbidden: set[str]) -> str:
    for _ in range(10_000):
        try:
            value = str(uuid.UUID(str(factory())))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ProfileError("UUID factory returned an invalid UUID") from exc
        if value not in forbidden:
            forbidden.add(value)
            return value
    raise ProfileError("UUID factory repeatedly returned colliding UUIDs")


def _walk_transform(value, profile_map, factory, forbidden):
    if isinstance(value, dict):
        transformed = {}
        for key, item in value.items():
            if key == "ActionID":
                transformed[key] = _fresh_uuid(factory, forbidden)
            elif key == "ProfileUUID":
                if item not in profile_map:
                    raise ProfileError(f"Unresolved ActionParam.ProfileUUID reference: {item}")
                transformed[key] = profile_map[item]
            else:
                transformed[key] = _walk_transform(item, profile_map, factory, forbidden)
        return transformed
    if isinstance(value, list):
        return [_walk_transform(item, profile_map, factory, forbidden) for item in value]
    return value


def _new_entry(action_id: str, contract: dict[str, str]) -> dict[str, object]:
    name = contract["action_name"]
    return {
        "Action": contract["action_uuid"], "ActionID": action_id, "ActionParam": {},
        "LinkedTitle": True, "Name": name,
        "Plugin": {"Name": contract["name"], "UUID": contract["uuid"],
                   "Version": contract["version"]},
        "State": 0, "ViewParam": [{"Icon": "", "IconRel": "", "Name": name}],
    }


def _clone_info(info: zipfile.ZipInfo, filename: str) -> zipfile.ZipInfo:
    output = zipfile.ZipInfo(filename, info.date_time)
    for attribute in ("compress_type", "comment", "extra", "internal_attr", "external_attr",
                      "create_system", "create_version", "extract_version", "flag_bits", "volume"):
        setattr(output, attribute, getattr(info, attribute))
    return output


def _rename_member(name: str, old_root: str, new_root: str,
                   profile_map: dict[str, str]) -> str:
    if name == old_root:
        return new_root
    if name.startswith(old_root + "/"):
        name = new_root + name[len(old_root):]
    match = re.match(rf"^{re.escape(new_root)}/Profiles/([^/]+)(/.*)?$", name)
    if match and match.group(1) in profile_map:
        return f"{new_root}/Profiles/{profile_map[match.group(1)]}{match.group(2) or ''}"
    return name


def _write_clone(archive, old_root, new_root, profile_map, replacements) -> bytes:
    target = io.BytesIO()
    renamed = set()
    with zipfile.ZipFile(target, "w", allowZip64=True) as output:
        output.comment = archive.comment
        for info in archive.infolist():
            new_member = _rename_member(info.filename, old_root, new_root, profile_map)
            if new_member in renamed:
                raise ProfileError(f"Clone mapping produced duplicate member: {new_member}")
            renamed.add(new_member)
            body = archive.read(info)
            if info.filename in replacements:
                body = json.dumps(
                    replacements[info.filename], ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            output.writestr(_clone_info(info, new_member), body)
    return HEADER + target.getvalue()


def _validate_clone(source, output, old_package_id, new_package_id, old_profile_ids,
                    profile_map, old_action_ids, selected_profile_id, selected, after):
    archive = zipfile.ZipFile(io.BytesIO(output[len(HEADER):]), "r")
    package_id, old_root, _, root_manifest, new_profile_ids, manifests = package_identity(archive)
    expected_profiles = set(profile_map.values())
    if package_id != new_package_id or package_id == old_package_id:
        raise ProfileError("Output package identity was not independently cloned")
    if new_profile_ids != expected_profiles or new_profile_ids & old_profile_ids:
        raise ProfileError("Output profile/page identities collide with or do not match the clone map")
    pages = root_manifest.get("Pages") or {}
    if (pages.get("Current") not in new_profile_ids
            or not set(pages.get("Pages") or []).issubset(new_profile_ids)):
        raise ProfileError("Output root Pages references do not resolve")
    refs = collect_key_values(manifests.values(), "ProfileUUID")
    if any(item not in new_profile_ids for item in refs):
        raise ProfileError("Output contains an unresolved ProfileUUID reference")
    action_ids = collect_key_values(manifests.values(), "ActionID")
    if len(action_ids) != len(set(action_ids)) or set(action_ids) & set(old_action_ids):
        raise ProfileError("Output ActionIDs are not unique or collide with the source")
    new_selected_id = profile_map[selected_profile_id]
    verified = [item for item in candidates(archive) if item["profile_id"] == new_selected_id]
    if len(verified) != 1 or verified[0]["entry"] != after:
        raise ProfileError("Output read-back did not match the requested LargeItem patch")
    source_archive = zipfile.ZipFile(io.BytesIO(source[len(HEADER):]), "r")
    (_, source_root, _, source_manifest, _,
     source_manifests) = package_identity(source_archive)
    if root_manifest.get("Device") != source_manifest.get("Device"):
        raise ProfileError("Device binding changed during cloning")
    reverse_profiles = {new: old for old, new in profile_map.items()}
    source_assets = {
        _member_semantics(name, source_root, {}): source_archive.read(name)
        for name in source_archive.namelist() if not name.endswith("manifest.json")
    }
    clone_assets = {
        _member_semantics(name, old_root, reverse_profiles): archive.read(name)
        for name in archive.namelist() if not name.endswith("manifest.json")
    }
    if source_assets != clone_assets:
        raise ProfileError("Non-manifest profile members changed during cloning")
    normalized_root = copy.deepcopy(root_manifest)
    normalized_root["Name"] = source_manifest.get("Name")
    normalized_pages = normalized_root.get("Pages") or {}
    normalized_pages["Current"] = reverse_profiles.get(
        normalized_pages.get("Current"), normalized_pages.get("Current"))
    normalized_pages["Pages"] = [
        reverse_profiles.get(item, item) for item in normalized_pages.get("Pages", [])
    ]
    if normalized_root != source_manifest:
        raise ProfileError("Package manifest changed outside cloned identity and name fields")
    for source_name, source_document in source_manifests.items():
        clone_name = _rename_member(source_name, source_root, old_root, profile_map)
        clone_document = copy.deepcopy(manifests.get(clone_name))
        if not isinstance(clone_document, dict):
            raise ProfileError(f"Cloned page manifest is missing: {clone_name}")
        normalized_source = _normalize_manifest(copy.deepcopy(source_document), {})
        normalized_clone = _normalize_manifest(clone_document, reverse_profiles)
        source_page_id = PROFILE_RE.search(source_name).group(1)
        if source_page_id == selected_profile_id:
            normalized_clone["Controllers"][selected["controller_index"]]["Actions"]["3_2"] = \
                normalized_source["Controllers"][selected["controller_index"]]["Actions"]["3_2"]
        if normalized_clone != normalized_source:
            raise ProfileError(f"Page semantics changed outside selected 3_2: {source_name}")
    return {
        "action_count": len(action_ids),
        "receipt": {
            "header": "#Version: 2\\n", "zip": "valid", "read_back": "valid",
            "package_id_collision": False, "profile_id_collisions": 0,
            "action_id_collisions": 0, "profile_references": "resolved",
            "device": "preserved", "assets": "byte-identical",
            "unrelated_semantics": "preserved",
            "selected_manifest": _rename_member(
                selected["manifest"], source_root, old_root, profile_map),
        },
    }


def _normalize_manifest(value, reverse_profiles):
    if isinstance(value, dict):
        normalized = {}
        for key, item in value.items():
            if key == "ActionID":
                normalized[key] = "<action-id>"
            elif key == "ProfileUUID":
                normalized[key] = reverse_profiles.get(item, item)
            else:
                normalized[key] = _normalize_manifest(item, reverse_profiles)
        return normalized
    if isinstance(value, list):
        return [_normalize_manifest(item, reverse_profiles) for item in value]
    return value


def _member_semantics(name: str, root: str, reverse_profiles: dict[str, str]) -> str:
    suffix = name[len(root):]
    match = re.match(r"^/Profiles/([^/]+)(/.*)?$", suffix)
    if match and match.group(1) in reverse_profiles:
        return f"/Profiles/{reverse_profiles[match.group(1)]}{match.group(2) or ''}"
    return suffix


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect_command = commands.add_parser("inspect", help="List every 3_2 candidate")
    inspect_command.add_argument("input", type=Path)
    patch_command = commands.add_parser("patch", help="Create an independent LargeItem profile clone")
    patch_command.add_argument("input", type=Path)
    patch_command.add_argument("output", type=Path)
    patch_command.add_argument("--profile-id", required=True)
    patch_command.add_argument("--plugin-manifest", required=True, type=Path)
    patch_command.add_argument("--clone-name")
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "inspect":
            inspect_profile(arguments.input)
        else:
            patch(arguments.input, arguments.output, arguments.profile_id,
                  arguments.plugin_manifest, arguments.clone_name)
        return 0
    except (OSError, ProfileError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
