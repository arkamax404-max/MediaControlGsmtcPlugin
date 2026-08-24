# Local Windows Companion Build Spike

Builds and verifies an unsigned CPython 3.14 Windows x64 PyInstaller one-folder
companion. Installer, task, signing, updater, release, and physical device work
remain out of scope.

## Build

Use fresh caller-owned roots outside the repository:

```powershell
$root = 'C:\approved-temp\unique-build'
.\packaging\build_companion.ps1 `
  -PythonExe 'C:\Python314\python.exe' -VenvRoot "$root\venv" `
  -Wheelhouse "$root\wheelhouse" -BuildRoot "$root\build" `
  -DistRoot "$root\dist" -MetadataRoot "$root\metadata" `
  -CacheRoot "$root\cache" -LicenseEvidenceRoot "$root\license-evidence" `
  -PopulateWheelhouse
```

Installation is binary-only and hash-locked. The packaging lock adds
`winrt-Windows.System==3.2.1`, required by Storage Streams. License evidence must
contain `pywinrt-LICENSE.txt` matching `license-evidence.lock`.

## Verify

```powershell
& "$root\venv\Scripts\python.exe" .\packaging\verify_companion_bundle.py `
  "$root\dist\GSMTCD200Companion" "$root\synthetic" "$root\manifest.json"
```

Use `--compare <first-manifest>` for reproducibility. Verification covers notices,
provenance, forbidden modules/native files/PE imports, frozen native/codec fallbacks,
offline diagnostics privacy, and per-file hashes. Venv, wheelhouse, caches, outputs,
manifests, and synthetic data remain outside the repository.
