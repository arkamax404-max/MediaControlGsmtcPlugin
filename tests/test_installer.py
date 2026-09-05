import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).parents[1]
INSTALLER = ROOT / "installer"


def load_support():
    path = INSTALLER / "build_support.py"
    spec = importlib.util.spec_from_file_location("installer_build_support", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_sections(source):
    sections, current = {}, None
    for raw in source.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]; sections[current] = []
        elif current and line and not line.startswith(";"):
            sections[current].append(line)
    return sections


class InstallerContractTests(unittest.TestCase):
    def test_roots_reject_repo_system_equal_and_overlap(self):
        support = load_support(); repo = "D:\\Development\\Project"
        good = ["C:\\Build\\bundle", "C:\\Build\\output", "C:\\Build\\metadata"]
        managed = ["C:\\Windows", "C:\\Program Files", "C:\\Program Files (x86)", "C:\\ProgramData"]
        support.validate_roots(repo, good, managed)
        support.validate_roots(repo, ["C:\\Users\\test\\Temp\\bundle", "C:\\Users\\test\\Temp\\output", "C:\\Users\\test\\Temp\\metadata"], managed)
        for roots in ([repo, *good[1:]], ["C:\\", *good[1:]],
                      ["C:\\Build\\same"] * 3,
                      ["C:\\Build\\a", "C:\\Build\\a\\b", "C:\\Build\\c"],
                      ["relative", *good[1:]], ["\\\\server\\share", *good[1:]],
                      ["C:\\Build\\bad.", *good[1:]], ["C:\\Build\\bad ", *good[1:]],
                      ["C:/Build/mixed", *good[1:]], ["C:\\Build\\a\\\\b", *good[1:]]):
            with self.assertRaises(ValueError): support.validate_roots(repo, roots)
        for path in ("C:\\Windows\\Temp", "C:\\Program Files\\Build", "C:\\ProgramData\\Build",
                     "C:\\Recovery\\Build", "D:\\System Volume Information\\Build", "E:\\$Recycle.Bin\\Build"):
            with self.assertRaises(ValueError): support.validate_roots(repo, [path, *good[1:]], managed)
        fake = SimpleNamespace(st_mode=0, st_file_attributes=0x400)
        with patch.object(Path, "exists", return_value=True), patch.object(Path, "lstat", return_value=fake):
            with self.assertRaises(ValueError): support.validate_roots(repo, good)

    def test_include_is_sorted_safe_and_companion_only(self):
        support = load_support()
        files = ["_internal/licenses/z.txt", "GSMTCD200Companion.exe",
                 "_internal/build-dependencies.json", "_internal/a.dll"]
        include = support.generate_include(files, "1.2.3")
        self.assertEqual(include, support.generate_include(reversed(files), "1.2.3"))
        self.assertLess(include.index("a.dll"), include.index("licenses\\z.txt"))
        for forbidden in ("plugin", "node_modules", "tests", "openspec", ".."):
            self.assertNotIn(forbidden, include.lower())
        for required in ("build-dependencies.json", "licenses\\z.txt",
                          "GSMTCD200Companion.exe"):
            self.assertIn(required, include)
        self.assertIn(r"{app}\versions\1.2.3\bridge", include)

    def test_receipt_contains_only_hashes_versions_and_counts(self):
        support = load_support()
        snapshot = {"sha256": "d" * 64, "files": [{"path": "companion.iss", "sha256": "e" * 64}]}
        bundle = {"tree_sha256": "b" * 64, "file_count": 85, "total_size": 22500387,
                  "files": [{"path": "GSMTCD200Companion.exe", "sha256": "c" * 64}]}
        receipt = support.build_receipt("482f680", snapshot, "1.2.3", "7.1.0", "a" * 64, 123, bundle, bundle)
        encoded = json.dumps(receipt, sort_keys=True)
        self.assertNotRegex(encoded, r"[A-Za-z]:\\|/Users/|\\\\")
        self.assertEqual(receipt["source_bundle_tree_sha256"], "b" * 64)
        self.assertEqual(receipt["source_bundle_exe_sha256"], "c" * 64)
        self.assertEqual(receipt["snapshot_bundle_tree_sha256"], "b" * 64)
        self.assertEqual(receipt["snapshot_bundle_exe_sha256"], "c" * 64)
        self.assertEqual(receipt["installer_size"], 123)
        self.assertEqual(receipt["companion_source_commit"], "482f680")
        self.assertEqual(receipt["app_version"], "1.2.3")
        self.assertNotIn("source_commit", receipt)

    def test_version_source_drives_installer_contract(self):
        support = load_support()
        self.assertEqual(support.companion_version(ROOT), "1.3.0")
        with self.assertRaises(ValueError): support.generate_include(["GSMTCD200Companion.exe"], "1.2")
        inno = INSTALLER.joinpath("companion.iss").read_text("utf-8")
        build = INSTALLER.joinpath("build_installer.ps1").read_text("utf-8")
        helper = INSTALLER.joinpath("manage_companion.ps1").read_text("utf-8")
        self.assertIn("#error AppVersion is required", inno)
        self.assertIn("OutputBaseFilename=GSMTCD200Companion-{#AppVersion}-local-unsigned", inno)
        self.assertNotRegex(inno, r"1\.2\.\d")
        self.assertIn("--define=AppVersion=$appVersion", build)
        self.assertIn('"GSMTCD200Companion-{0}-local-unsigned.exe" -f $appVersion', build)
        self.assertIn("companion_version(args.repo)", INSTALLER.joinpath("build_support.py").read_text("utf-8"))
        self.assertIn("$script:companionVersion = $Matches[1]", helper)
        self.assertIn("$health.companion_version -eq $script:companionVersion", helper)

    def test_bundle_and_installer_source_are_independently_hashed(self):
        support = load_support()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); bundle = root / "bundle"; bundle.mkdir()
            (bundle / "GSMTCD200Companion.exe").write_bytes(b"exe")
            actual = support.enumerate_bundle(bundle)
            forged = json.loads(json.dumps(actual)); forged["files"][0]["sha256"] = "0" * 64
            self.assertNotEqual(actual, forged)
            source = root / "source"; source.mkdir()
            for name in ("build_installer.ps1", "build_support.py", "companion.iss",
                         "manage_companion.ps1", "tooling.lock.json"):
                (source / name).write_text(name, "utf-8")
            include = root / "bundle-files.iss"; include.write_text("include", "utf-8")
            first = support.source_snapshot(source, include)
            (source / "companion.iss").write_text("changed", "utf-8")
            self.assertNotEqual(first, support.source_snapshot(source, include))

    def test_compilation_snapshot_isolated_and_reverified(self):
        support = load_support()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "source"; source.mkdir()
            (source / "GSMTCD200Companion.exe").write_bytes(b"verified")
            expected = support.enumerate_bundle(source); snapshot = root / "metadata" / "bundle-snapshot"
            snapshot.parent.mkdir(); compiled = support.create_bundle_snapshot(source, snapshot, expected)
            (source / "GSMTCD200Companion.exe").write_bytes(b"hostile mutation")
            self.assertEqual(support.verify_bundle(snapshot, compiled), expected)
            (snapshot / "GSMTCD200Companion.exe").write_bytes(b"snapshot mutation")
            with self.assertRaises(ValueError): support.verify_bundle(snapshot, compiled)
        build = INSTALLER.joinpath("build_installer.ps1").read_text("utf-8")
        self.assertIn("--define=BundleRoot=$snapshotRoot", build)
        self.assertIn("--snapshot-root $snapshotRoot", build)

    def test_inno_script_semantics_are_per_user_companion_only(self):
        source = INSTALLER.joinpath("companion.iss").read_text("utf-8")
        sections = parse_sections(source); setup = "\n".join(sections["Setup"])
        self.assertIn("PrivilegesRequired=lowest", setup)
        self.assertIn("PrivilegesRequiredOverridesAllowed=", setup)
        self.assertIn("SetupArchitecture=x64", setup)
        self.assertIn("DefaultDirName={localappdata}\\Programs\\GSMTCD200Controller", setup)
        self.assertIn("#ifndef AppVersion", source)
        self.assertIn("AppVersion={#AppVersion}", setup)
        self.assertNotIn("SignedUninstaller=yes", setup)
        self.assertNotIn("{userdesktop}", source)
        self.assertNotIn("ulanzi", source.lower())
        self.assertIn("versions\\{#AppVersion}\\bridge", source)
        icons = "\n".join(sections["Icons"])
        self.assertIn("--diagnose", icons); self.assertIn("--stop", icons)
        self.assertTrue(all("WorkingDir:" in line for line in sections["Icons"] if "Companion.exe" in line))
        uninstall_delete = "\n".join(sections["UninstallDelete"])
        self.assertIn("\\cache", uninstall_delete)
        self.assertIn("RemoveLocalData", uninstall_delete)
        self.assertNotIn("taskkill", source.lower())
        self.assertIn("CurUninstallStepChanged", source)
        self.assertIn("RaiseException('Companion uninstall preparation failed')", source)
        self.assertIn("GetCustomSetupExitCode", source); self.assertIn("SetupExitCode := 1603", source)
        self.assertIn("Result := SetupExitCode", source); self.assertIn("-StatusPath", source)
        self.assertNotIn("MsgBox(", source)

    def test_manage_helper_dry_run_task_acl_and_exact_removal(self):
        script = INSTALLER / "manage_companion.ps1"
        local = "C:\\Synthetic User\\Local"
        version = local + "\\Programs\\GSMTCD200Controller\\versions\\1.2.3\\bridge"
        data = local + "\\GSMTCD200Controller"
        common = ["pwsh", "-NoProfile", "-File", str(script), "-DryRun",
                  "-LocalAppDataRoot", local, "-VersionRoot", version,
                  "-DataRoot", data, "-CurrentUserSid", "S-1-5-21-1-2-1000"]
        install = subprocess.run(common + ["-Action", "Install"], capture_output=True,
                                 text=True, timeout=10, check=True)
        plan = json.loads(install.stdout)
        xml = plan["task_xml"]
        for value in ("<Delay>PT10S</Delay>", "<LogonType>InteractiveToken</LogonType>",
                      "<RunLevel>LeastPrivilege</RunLevel>", "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>",
            "<Interval>PT1M</Interval>", "<Count>3</Count>",
                      "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>", "<StartWhenAvailable>true</StartWhenAvailable>"):
            self.assertIn(value, xml)
        self.assertIn("GSMTCD200Companion.exe", xml)
        self.assertEqual(plan["sid"], "S-1-5-21-1-2-1000")
        self.assertIn("S-1-5-21-1-2-1000", plan["directory_sddl"])
        self.assertNotIn("BA", plan["directory_sddl"]); self.assertNotIn("WD", plan["directory_sddl"])
        self.assertTrue(plan["acl_targets"][-1].endswith("config\\bridge-token"))
        self.assertEqual(len(plan["acl_targets"]), 6)
        self.assertEqual(plan["task_name"], "GSMTCD200Controller-Companion")
        self.assertIn("<URI>GSMTCD200Controller-Companion</URI>", xml)
        self.assertEqual((plan["phase"], plan["exit_code"]), ("success", 0))
        self.assertEqual((plan["runtime"]["process_alive"], plan["runtime"]["listener"], plan["runtime"]["mutex"], plan["runtime"]["health"]), (True, True, True, True))
        self.assertEqual(plan["runtime"]["companion_version"], "1.2.3")
        self.assertEqual((plan["runtime"]["pid"], plan["runtime"]["path"]), (202, version + "\\GSMTCD200Companion.exe"))
        expected_task_dacl = "D:P(A;;0x001301BF;;;S-1-5-21-1-2-1000)(A;;FA;;;SY)(A;;FA;;;BA)"
        self.assertEqual(plan["task_registrations"], [{"flags": 0x16, "sddl": expected_task_dacl, "initial": True}])
        self.assertEqual(plan["task"]["owner"], "S-1-5-21-1-2-1000")
        self.assertEqual(plan["task"]["dacl"], expected_task_dacl.replace("0x001301BF", "0x1301bf"))
        removal = subprocess.run(common + ["-Action", "UninstallTask"], capture_output=True,
                                 text=True, timeout=10, check=True)
        self.assertEqual(json.loads(removal.stdout)["operation"], "delete_exact_task")
        source = script.read_text("utf-8")
        self.assertIn("Schedule.Service", source); self.assertNotIn("schtasks", source.lower())
        self.assertIn("RegisterTask($TaskName, $Xml, 0x16, $CurrentUserSid, $null, 3, $sddl)", source)
        self.assertIn("SetSecurityDescriptor($script:taskDacl, 0)", source)
        self.assertIn("OwningProcess", source); self.assertIn("Get-Process -Id", source)
        self.assertIn("candidateProcess.WaitForExit", source); self.assertIn("candidatePid", source)
        self.assertNotIn("Stop-Process", source); self.assertNotIn("taskkill", source.lower())
        for point in ("QueryMissingSigned", "QueryMissingUnsigned"):
            result = subprocess.run(common + ["-Action", "Query", "-FailurePoints", point], capture_output=True, text=True, timeout=10, check=True)
            state = json.loads(result.stdout); self.assertIsNone(state["task"]); self.assertEqual((state["phase"], state["exit_code"]), ("success", 0))
        for point in ("QueryAccess", "QueryService"):
            result = subprocess.run(common + ["-Action", "Query", "-FailurePoints", point], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 21); self.assertEqual(json.loads(result.stdout)["phase"], "query")

        legacy_xml = '<Task><Actions><Exec><Command>C:\\Prior\\Companion.exe</Command></Exec></Actions></Task>'
        repaired = subprocess.run(common + ["-Action", "Install", "-PriorTaskXml", legacy_xml],
                                 capture_output=True, text=True, timeout=10, check=True)
        repaired_plan = json.loads(repaired.stdout)
        self.assertTrue(repaired_plan["task_acl_repaired"])
        self.assertEqual(repaired_plan["task"]["dacl"], expected_task_dacl.replace("0x001301BF", "0x1301bf"))
        self.assertEqual([call["sddl"] for call in repaired_plan["task_registrations"]], [None])
        self.assertTrue(all(call["flags"] == 0x16 for call in repaired_plan["task_registrations"]))

        denied = subprocess.run(common + ["-Action", "Install", "-PriorTaskXml", legacy_xml,
                                           "-FailurePoints", "TaskAclRepair"],
                                capture_output=True, text=True, timeout=10)
        denied_plan = json.loads(denied.stdout)
        self.assertEqual((denied.returncode, denied_plan["phase"], denied_plan["exit_code"]), (27, "task_acl_repair", 27))
        self.assertEqual(denied_plan["task_registrations"], [])
        self.assertIn("delete only the named task as administrator, then rerun the installer", denied_plan["error"])

        rolled_back = subprocess.run(common + ["-Action", "Install", "-PriorTaskXml", legacy_xml,
                                                "-FailurePoints", "Start"],
                                      capture_output=True, text=True, timeout=10)
        rollback_plan = json.loads(rolled_back.stdout)
        self.assertEqual((rolled_back.returncode, rollback_plan["rollback"]), (24, "complete"))
        self.assertTrue(rollback_plan["task_acl_repaired"])
        self.assertEqual((rollback_plan["task"]["target"], rollback_plan["task"]["dacl"]),
                         ("C:\\Prior\\Companion.exe", expected_task_dacl.replace("0x001301BF", "0x1301bf")))
        integration = (ROOT / "tests/task_scheduler_dacl_integration.ps1").read_text("utf-8")
        self.assertIn("GSMTCD200Controller-DaclProbe-$PID", integration)
        self.assertIn("<Enabled>false</Enabled>", integration)
        self.assertIn("RegisterTask($name, $xml, 0x16, $CurrentUserSid, $null, 3, $targetDacl)", integration)
        self.assertIn("finally", integration)

    def test_disposable_dacl_walk_is_unique_semantic_and_idempotent(self):
        script = INSTALLER / "manage_companion.ps1"
        def command_for(local, data, version, failure=""):
            quote = lambda value: "'" + str(value).replace("'", "''") + "'"
            command = f"function global:Get-Acl {{ throw 'disabled' }}; function global:Set-Acl {{ throw 'disabled' }}; & {quote(script)} -DryRun -DisposableDaclTest -Action Install -LocalAppDataRoot {quote(local)} -VersionRoot {quote(version)} -DataRoot {quote(data)}"
            if failure: command += f" -FailurePoints {quote(failure)}"
            command += "; exit $LASTEXITCODE"
            return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command]
        with tempfile.TemporaryDirectory(prefix="GSMTC DACL ") as directory:
            local = Path(directory); data = local / "GSMTCD200Controller"
            for name in ("config", "logs/nested", "cache", "diagnostics"): (data / name).mkdir(parents=True, exist_ok=True)
            for name in ("config/bridge-token", "logs/nested/item.txt", "logs/held.log", "diagnostics/held.zip"): (data / name).write_bytes(b"held")
            version = local / "Programs/GSMTCD200Controller/versions/1.2.3/bridge"
            command = command_for(local, data, version)
            with (data / "logs/held.log").open("ab"), (data / "diagnostics/held.zip").open("ab"):
                first_run = subprocess.run(command, capture_output=True, text=True, timeout=15); self.assertEqual(first_run.returncode, 0, first_run.stderr + first_run.stdout)
                first = json.loads(first_run.stdout)
                count = 1 + sum(1 for _ in data.rglob("*"))
                second_run = subprocess.run(command, capture_output=True, text=True, timeout=15); self.assertEqual(second_run.returncode, 0, second_run.stderr + second_run.stdout)
                second = json.loads(second_run.stdout)
            self.assertEqual((first["dacl"]["visited"], first["dacl"]["applied"], first["dacl"]["skipped"]), (count, count, 0))
            self.assertEqual((second["dacl"]["visited"], second["dacl"]["applied"], second["dacl"]["skipped"]), (count, 0, count))
            self.assertTrue({"dacl_create", "dacl_metadata", "dacl_descriptor", "dacl_owner", "dacl_rules", "dacl_compare", "dacl_apply", "dacl_verify", "dacl_enumerate"}.issubset(first["phase_trace"]))
        source = script.read_text("utf-8"); self.assertIn("GetOwner([Security.Principal.SecurityIdentifier]).Value", source)
        self.assertNotIn("Get-Acl", source); self.assertNotIn("Set-Acl", source)

        diagnostics = {"DaclMetadata": ("dacl_metadata", 40), "DaclDescriptor": ("dacl_descriptor", 41),
                       "DaclOwner": ("dacl_owner", 42), "DaclRules": ("dacl_rules", 43),
                       "DaclCompare": ("dacl_compare", 44), "DaclApply": ("dacl_apply", 45),
                       "DaclVerify": ("dacl_verify", 46), "DaclEnumerate": ("dacl_enumerate", 47)}
        for failure, expected in diagnostics.items():
            with tempfile.TemporaryDirectory(prefix="GSMTC DACL diagnostic ") as directory:
                local = Path(directory); data = local / "GSMTCD200Controller"; data.mkdir()
                version = local / "Programs/GSMTCD200Controller/versions/1.2.3/bridge"
                command = command_for(local, data, version, failure)
                result = subprocess.run(command, capture_output=True, text=True, timeout=15); state = json.loads(result.stdout)
                self.assertEqual((result.returncode, state["phase"], state["exit_code"]), (expected[1], *expected), failure)
                self.assertEqual(set(state), {"success", "phase", "exit_code", "phase_trace", "dacl"}, failure)

    def test_shared_migration_state_machine_failure_table(self):
        script = INSTALLER / "manage_companion.ps1"; local = "C:\\Synthetic User\\Local"
        version = local + "\\Programs\\GSMTCD200Controller\\versions\\1.2.3\\bridge"; data = local + "\\GSMTCD200Controller"
        prior_target = "C:\\Prior\\Companion.exe"
        prior_xml = f'<Task><Actions><Exec><Command>{prior_target}</Command></Exec></Actions></Task>'
        base = ["pwsh", "-NoProfile", "-File", str(script), "-DryRun", "-Action", "Install",
                "-LocalAppDataRoot", local, "-VersionRoot", version, "-DataRoot", data,
                "-CurrentUserSid", "S-1-5-21-1-2-1000"]
        cases = [
            ("Dacl", None, "None", None, "complete"),
            ("Query", "Running", "Prior", "Running", "none"),
            ("PriorStop", "Running", "Prior", "Running", "none"),
            ("Create", "Running", "Prior", "Running", "complete"),
            ("Start", None, "None", None, "complete"),
            ("Health", "Running", "Prior", "Running", "complete"),
            ("CandidateStop", None, "Candidate", None, "incomplete"),
            ("CandidateAliveNoListener", None, "Candidate", None, "incomplete"),
            ("Delete", None, "None", "Ready", "incomplete"),
            ("Restore", "Ready", "None", None, "incomplete"),
            ("Restart", "Running", "None", "Ready", "incomplete"),
            ("PriorImmediateExit", "Running", "None", "Ready", "incomplete"),
        ]
        phase_codes = {"Dacl": ("dacl_create", 20), "Query": ("query", 21), "PriorStop": ("stop", 22),
                       "Create": ("task_register", 23), "Start": ("start", 24), "Health": ("health", 25),
                       "CandidateStop": ("rollback_stop", 30), "CandidateAliveNoListener": ("rollback_stop", 30),
                       "Delete": ("rollback_remove", 31), "Restore": ("rollback_restore", 32),
                       "Restart": ("rollback_restart", 33), "PriorImmediateExit": ("rollback_restart", 33)}
        for failure, status, owner, final_status, rollback in cases:
            command = base + ["-FailurePoints", failure]
            if status: command += ["-PriorTaskXml", prior_xml, "-PriorTaskStatus", status]
            result = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0, failure); state = json.loads(result.stdout)
            self.assertEqual((state["owner"], state["rollback"]), (owner, rollback), failure)
            self.assertEqual((state["phase"], state["exit_code"], result.returncode), (*phase_codes[failure], phase_codes[failure][1]), failure)
            self.assertEqual(state["task"]["status"] if state["task"] else None, final_status, failure)
            self.assertEqual(bool(state["rollback_errors"]), rollback == "incomplete", failure)
            if failure == "Health": self.assertEqual((state["runtime"]["process_alive"], state["runtime"]["stable_polls"]), (True, 6))
            if failure == "CandidateAliveNoListener": self.assertEqual((state["runtime"]["process_alive"], state["runtime"]["listener"], state["runtime"]["mutex"], state["runtime"]["stop_invoked"]), (True, False, False, True))
        for poll in range(1, 7):
            result = subprocess.run(base + ["-FailurePoints", f"Health,PriorPoll{poll}", "-PriorTaskXml", prior_xml, "-PriorTaskStatus", "Running"], capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0, poll); state = json.loads(result.stdout)
            self.assertEqual((state["rollback"], state["runtime"]["stable_polls"]), ("incomplete", poll - 1), poll)

    def test_manage_helper_rejects_path_and_task_injection(self):
        script = INSTALLER / "manage_companion.ps1"
        base = ["pwsh", "-NoProfile", "-File", str(script), "-DryRun", "-Action", "Query",
                "-LocalAppDataRoot", "C:\\Local", "-VersionRoot",
            "C:\\Local\\Programs\\GSMTCD200Controller\\versions\\1.2.3\\bridge",
                "-DataRoot", "C:\\Local\\GSMTCD200Controller", "-CurrentUserSid", "S-1-5-21-1-2-1000"]
        for extra in (["-TaskName", "*"], ["-DataRoot", "C:\\Other"],
                      ["-VersionRoot", "C:\\Windows"],
                      ["-VersionRoot", "C:\\Local\\Programs\\GSMTCD200Controller\\versions\\1.2\\bridge"],
        ["-VersionRoot", "C:\\Local\\Programs\\GSMTCD200Controller\\versions\\1.2.3\\bridge\\..\\bridge"]):
            result = subprocess.run(base + extra, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
        invalid_sid = subprocess.run(base[:-1] + ["not-a-sid"], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(invalid_sid.returncode, 0)

    def test_early_version_root_failure_writes_validation_status(self):
        script = INSTALLER / "manage_companion.ps1"
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory)
            status = local / "Programs/GSMTCD200Controller/installer/activation-status.txt"
            status.parent.mkdir(parents=True)
            status.write_text("pending", "utf-8")
            version = local / "Programs/GSMTCD200Controller/versions/not-a-version/bridge"
            data = local / "GSMTCD200Controller"
            result = subprocess.run([
                "pwsh", "-NoProfile", "-File", str(script), "-Action", "Query",
                "-LocalAppDataRoot", str(local), "-VersionRoot", str(version),
                "-DataRoot", str(data), "-StatusPath", str(status),
            ], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 10, result.stderr)
            self.assertEqual(status.read_text("utf-8"), "validation")


if __name__ == "__main__":
    unittest.main()
