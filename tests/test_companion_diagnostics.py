import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import d200_bridge.__main__ as bridge_main
from d200_bridge.diagnostics import build_zip, bounded_http_get, create_diagnostics, read_log_tail
from d200_bridge.paths import CompanionPaths


TOKEN = "T" * 43
NOW = datetime(2026, 8, 24, 12, 30, tzinfo=timezone.utc)


def response(payload, status=200):
    return status, json.dumps(payload).encode("utf-8")


class DiagnosticsTests(unittest.TestCase):
    def test_bundle_schema_order_bounds_and_privacy(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = CompanionPaths(Path(directory))
            paths.logs.mkdir()
            paths.logs.joinpath("companion.log").write_text(
                "INFO companion_listening\n"
                f"Authorization: Bearer {TOKEN} C:\\Users\\Synthetic Name\\cover.png "
                "\\\\server\\share person@example.test data:image/png;base64,AAAA\x01\n"
                + "x" * 5000,
                encoding="utf-8",
            )
            requests = []
            def http_get(url, headers, timeout):
                requests.append((url, headers, timeout))
                if url.endswith("/health"):
                    return response({"status": "degraded"})
                return response({"available": True, "timeline_available": True,
                    "audio_available": False, "artwork_id": "a" * 64,
                    "title": "SECRET TITLE", "volume_percent": 80})
            result = create_diagnostics(paths=paths, clock=lambda: NOW, http_get=http_get,
                token_loader=lambda _paths: TOKEN,
                dependency_provider=lambda name: None if name == "pycaw" else
                    "C:\\Synthetic\\metadata" if name == "comtypes" else "1.2.3")
            self.assertLessEqual(result.stat().st_size, 1024 * 1024)
            with zipfile.ZipFile(result) as archive:
                self.assertEqual(archive.namelist(), ["summary.json", "runtime.json",
                    "dependencies.json", "logs.txt"])
                self.assertTrue(all(not Path(name).is_absolute() and ".." not in Path(name).parts
                                    for name in archive.namelist()))
                summary = json.loads(archive.read("summary.json"))
                runtime = json.loads(archive.read("runtime.json"))
                dependencies = json.loads(archive.read("dependencies.json"))
                content = b"".join(archive.read(name) for name in archive.namelist())
                self.assertLessEqual(len(archive.read("logs.txt")), 512 * 1024)
                self.assertTrue(all(len(archive.read(name)) <= 64 * 1024
                                    for name in archive.namelist()[:3]))
            self.assertEqual(summary["generated_at"], "2026-08-24T12:30:00Z")
            self.assertEqual(summary["health_status"], "degraded")
            self.assertEqual(runtime, {"artwork_id_present": True, "audio_available": False,
                "available": True, "online": True, "payload_size": requests[1][2] * 0
                + len(json.dumps({"available": True, "timeline_available": True,
                    "audio_available": False, "artwork_id": "a" * 64,
                    "title": "SECRET TITLE", "volume_percent": 80}).encode()),
                "timeline_available": True})
            self.assertEqual(dependencies["pycaw"], "unavailable")
            self.assertEqual(dependencies["comtypes"], "unavailable")
            self.assertTrue(all(value == "1.2.3" for name, value in dependencies.items()
                                if name not in {"pycaw", "comtypes"}))
            for secret in (TOKEN.encode(), b"SECRET TITLE", b"person@example.test",
                           b"data:image", b"Synthetic Name", b"server", b"\x01", b"a" * 64):
                self.assertNotIn(secret, content)
            self.assertIn(b"INFO companion_listening", content)
            self.assertEqual(requests[1][1]["Authorization"], f"Bearer {TOKEN}")
            self.assertLessEqual(requests[0][2], 2)

    def test_zip_metadata_and_bytes_are_deterministic(self):
        entries = (("summary.json", b"{}"), ("runtime.json", b"{}"),
                   ("dependencies.json", b"{}"), ("logs.txt", b"INFO redacted_event\n"))
        first = build_zip(entries); self.assertEqual(first, build_zip(entries))
        with zipfile.ZipFile(io.BytesIO(first)) as archive:
            for info in archive.infolist():
                self.assertEqual(info.date_time, (1980, 1, 1, 0, 0, 0))
                self.assertEqual(info.create_system, 3)
                self.assertEqual(info.external_attr, 0o100600 << 16)
                self.assertEqual(info.compress_type, zipfile.ZIP_DEFLATED)
                self.assertTrue(info.filename.isascii())

    def test_unreachable_or_token_unavailable_still_creates_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = CompanionPaths(Path(directory))
            cases = (("health", "health_unreachable"), ("health_json", "health_unavailable"),
                     ("token", "token_unavailable"), ("invalid_token", "token_unavailable"),
                     ("state_size", "state_unavailable"), ("state_json", "state_unavailable"),
                     ("state_http", "state_unavailable"))
            for failure, reason in cases:
                calls = []
                def http_get(url, headers, timeout):
                    calls.append(url)
                    if failure == "health": raise TimeoutError()
                    if failure == "health_json": return 200, b"{"
                    if url.endswith("/state") and failure == "state_size":
                        return 200, b"x" * (64 * 1024 + 1)
                    if url.endswith("/state") and failure == "state_json": return 200, b"{"
                    if url.endswith("/state") and failure == "state_http": return 503, b"{}"
                    return response({"status": "ready"})
                def token_loader(_paths):
                    if failure == "token": raise OSError()
                    return "invalid" if failure == "invalid_token" else TOKEN
                result = create_diagnostics(paths=paths, clock=lambda: NOW,
                    http_get=http_get, token_loader=token_loader,
                    dependency_provider=lambda _name: None)
                with zipfile.ZipFile(result) as archive:
                    runtime = json.loads(archive.read("runtime.json"))
                self.assertEqual(runtime["reason"], reason)
                self.assertTrue(result.exists())
                if failure in {"token", "invalid_token"}: self.assertEqual(len(calls), 1)

    def test_bounded_http_ignores_lying_length(self):
        response = MagicMock(status=200, headers={"Content-Length": "1"})
        response.read.return_value = b"x" * (64 * 1024 + 1)
        response.__enter__.return_value = response
        with patch("d200_bridge.diagnostics.urlopen", return_value=response):
            status, body = bounded_http_get("http://127.0.0.1/health", {}, 1)
        self.assertEqual(status, 200); self.assertEqual(len(body), 64 * 1024 + 1)
        response.read.assert_called_once_with(64 * 1024 + 1)

    def test_unsafe_log_metadata_is_skipped(self):
        regular = dict(st_mode=stat.S_IFREG, st_nlink=1, st_size=10,
                       st_file_attributes=0, st_dev=1, st_ino=2)
        for changes in ({"st_mode": stat.S_IFDIR}, {"st_mode": stat.S_IFLNK}, {"st_nlink": 2},
                        {"st_size": 3_000_000}, {"st_file_attributes": 0x400}):
            metadata = SimpleNamespace(**{**regular, **changes})
            fs = SimpleNamespace(lstat=lambda _path: metadata)
            self.assertEqual(read_log_tail(Path("C:/synthetic.log"), 1024, fs=fs), b"")
        before = SimpleNamespace(**regular); after = SimpleNamespace(**{**regular, "st_ino": 9})
        fs = SimpleNamespace(lstat=lambda _path: before, fstat=Mock(side_effect=[before, after]),
            open=lambda *_args: 3, lseek=lambda *_args: 0, read=lambda *_args: b"safe",
            close=lambda *_args: None, path=SimpleNamespace(isjunction=lambda _path: False),
            O_RDONLY=0, O_BINARY=0, SEEK_SET=0)
        self.assertEqual(read_log_tail(Path("C:/synthetic.log"), 1024, fs=fs), b"")

    def test_atomic_concurrent_outputs_and_failure_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = CompanionPaths(Path(directory))
            kwargs = dict(paths=paths, clock=lambda: NOW,
                http_get=lambda *_args: response({"status": "ready"}),
                token_loader=lambda _paths: (_ for _ in ()).throw(OSError()),
                dependency_provider=lambda _name: None)
            with ThreadPoolExecutor(max_workers=2) as executor:
                outputs = list(executor.map(lambda _index: create_diagnostics(**kwargs), range(2)))
            self.assertEqual(len(set(outputs)), 2)
            self.assertTrue(all(zipfile.is_zipfile(path) for path in outputs))
            collision = paths.diagnostics / f"diagnostics-20260824T123000Z-{'a' * 12}.zip"
            collision.write_bytes(b"existing")
            names = iter(("a" * 12, "b" * 12))
            created = create_diagnostics(**kwargs, name_source=lambda _size: next(names))
            self.assertEqual(collision.read_bytes(), b"existing"); self.assertNotEqual(created, collision)
            with self.assertRaises(FileExistsError):
                create_diagnostics(**kwargs, name_source=lambda _size: "a" * 12)
            self.assertEqual(collision.read_bytes(), b"existing")
            with patch("d200_bridge.diagnostics.os.link", side_effect=OSError("failed")):
                with self.assertRaises(OSError): create_diagnostics(**kwargs)
            self.assertEqual(list(paths.diagnostics.glob("*.tmp")), [])

    def test_cli_diagnose_bypasses_runtime_and_bounds_output(self):
        destination = Path("C:/Synthetic/diagnostics/report.zip")
        with patch.object(bridge_main, "create_diagnostics", return_value=destination), \
             patch.object(bridge_main, "NamedMutex") as mutex, \
             patch.object(bridge_main.asyncio, "run") as run, \
             patch("builtins.print") as output:
            self.assertEqual(bridge_main.main(["--diagnose"]), 0)
        mutex.assert_not_called(); run.assert_not_called()
        self.assertIn(str(destination), output.call_args.args[0])
        with patch.object(bridge_main, "create_diagnostics", side_effect=OSError()), \
             patch("builtins.print") as output:
            self.assertNotEqual(bridge_main.main(["--diagnose"]), 0)
        self.assertNotIn(TOKEN, str(output.call_args))

    def test_cold_diagnose_imports_no_runtime_modules(self):
        with tempfile.TemporaryDirectory() as directory:
            script = f'''import importlib.abc,os,pathlib,runpy,sys,types
os.environ["LOCALAPPDATA"] = r"C:\\Synthetic\\Local"
blocked={{"d200_bridge.gsmtc","d200_bridge.core_audio","d200_bridge.lifecycle","d200_bridge.server"}}
class Blocker(importlib.abc.MetaPathFinder):
 def find_spec(self,name,*args):
  if name in blocked: raise ImportError(name)
sys.meta_path.insert(0,Blocker())
fake=types.ModuleType("d200_bridge.diagnostics"); fake.create_diagnostics=lambda:pathlib.Path(r"{directory}")/"report.zip"
sys.modules["d200_bridge.diagnostics"]=fake; sys.argv=["d200_bridge","--diagnose"]
runpy.run_module("d200_bridge",run_name="__main__")'''
            result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).parents[1],
                                    capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("d200_bridge.server", result.stderr)


if __name__ == "__main__":
    unittest.main()
