# Per-User Companion Installer Foundation

Builds an unsigned local Inno Setup 7.1 companion-only installer. It does not
manage the Ulanzi plugin, signing, upgrades, version/file rollback, or releases.

## Compile

Use the verified frozen bundle and fresh external output/metadata roots:

```powershell
.\installer\build_installer.ps1 `
  -ISCC "$env:LOCALAPPDATA\Programs\Inno Setup 7\ISCC.exe" `
  -BundleRoot 'C:\approved-temp\dist\GSMTCD200Companion' `
  -OutputRoot 'C:\approved-temp\installer-output' `
  -MetadataRoot 'C:\approved-temp\installer-metadata' `
  -VerifierPython 'C:\approved-temp\venv\Scripts\python.exe' `
  -CompanionSourceCommit '482f680'
```

The builder rejects system-managed roots before any helper, validates the pinned
compiler, independently hashes the exact 85-file input, and copies it into an
isolated metadata snapshot. Inno reads only that snapshot, which is rehashed after
compilation. Receipts separately identify source and compiled snapshots without paths.

## Installed Layout

- Immutable bridge: `%LOCALAPPDATA%\Programs\GSMTCD200Controller\versions\1.2.0\bridge`
- Helper: `%LOCALAPPDATA%\Programs\GSMTCD200Controller\installer`
- Mutable data: `%LOCALAPPDATA%\GSMTCD200Controller`
- Root task: `GSMTCD200Controller-Companion`, interactive current user, 10-second delay

The task uses `IgnoreNew`, least privilege, no password, three 30-second restart
attempts, no execution limit, and no battery/network restriction. Task Scheduler
does not expose a separate ten-minute retry window for this XML policy.

Uninstall always removes cache and preserves config/logs/diagnostics by default.
Run the uninstaller with `/REMOVELOCALDATA` to remove all mutable data. A dedicated
uninstall checkbox is deferred; it is not simulated here.

Activation uses Task Scheduler COM to snapshot exact task XML, target, and language-
independent state before cooperative stop, ACL hardening, registration, and health
gating. Rollback verifies exact candidate PID exit, restores exact XML/target, and
requires a previously running task's PID/port/mutex/health to stay stable for three
seconds; any stop/delete/restore/restart failure exits nonzero and
reports incomplete rollback. Files may remain because file rollback is Slice 6.
Helper failures expose only a bounded phase and code; Setup returns custom code 1603
while retaining the uninstall entry for honest cleanup of files that may remain.
