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

## Ulanzi Plugin Release Package

The repository plugin remains functional through `manifest.CodePath: src/app.js` for
in-repo Node.js development and testing. The launcher runtime is intentionally separate
from that Node implementation and from the production companion. Build it and prepare
the release plugin package entirely outside the repository:

```powershell
$runtimeRoot = 'C:\approved-temp\ulanzi-runtime'
New-Item -ItemType Directory -Path $runtimeRoot
py -3.14 -m venv "$runtimeRoot\venv"
& "$runtimeRoot\venv\Scripts\python.exe" -I -s -m pip install `
  --require-hashes -r .\packaging\requirements-ulanzi-bootstrap.lock
& "$runtimeRoot\venv\Scripts\python.exe" -I -s -m pip install `
  --require-hashes --no-build-isolation `
  -r .\packaging\requirements-ulanzi-runtime.lock
& "$runtimeRoot\venv\Scripts\python.exe" -I -s -m PyInstaller `
  --noconfirm --clean --workpath "$runtimeRoot\build" `
  --distpath "$runtimeRoot\dist" .\packaging\ulanzi_runtime.spec
New-Item -ItemType Directory -Path "$runtimeRoot\package"
python .\packaging\prepare_ulanzi_spike.py `
  --runtime-bundle "$runtimeRoot\dist\runtime" `
  --output-root "$runtimeRoot\package"
```

The lock fixes `plugin-common-python` to commit
`9158324b777dd1f1643a0a7107528ffc506984f7` by archive hash and fixes
`websocket-client` to 1.8.0. The generated runtime includes project, CPython,
PyInstaller, plugin-common-python, and websocket-client license texts under
`_internal\licenses`. The
preparer validates those files, copies the launcher-facing assets and minimal ESM
package metadata without npm dependencies, and changes only the external manifest
to `src/launcher.js`.

The resulting external
`package\com.arkamax404.mediacontrold200.ulanziPlugin` is the release artifact: it is
what ships to users through the Ulanzi Community Store and the GitHub release ZIP.
Its manifest exposes exactly fourteen Python actions: Now Playing, Large Now Playing, Previous,
Play/Pause, Next, Volume Up, Volume Down, Mute Toggle, Track Progress, and the
four artwork mosaic actions, and Setup Large Display. The package includes the explicit music and offline
fallbacks, every transport/audio asset, the four artwork tile icons, and the
progress and LargeItem icons, progress, LargeItem, and audio-source inspectors, and five
shared browser SDK scripts.
The thirteen media actions use the existing authenticated loopback bridge. Setup Large Display
can install, repair, or restore the center action in Studio's live `ProfilesV2` store. Its
detached helper derives production roots, authenticates and consumes one-shot requests,
serializes work with a Windows named mutex, waits for Studio to stop, publishes a complete
fsynced backup, and uses authenticated journals for rollback or crash recovery. Divergent
live bytes are never overwritten automatically and are reported as requiring manual
recovery. Packaging this helper therefore enables managed profile mutation and must not be
described or reviewed as a diagnostic-only artifact.
The helper polls Studio process state without opening console windows, publishes
`Waiting for Studio to close`, and allows ten minutes for the user to close Studio. Large
Now Playing persists `SmallViewMode: 2` so page activation returns to the background-only
presentation instead of the built-in clock layer.
The runtime retains the validated color and grayscale artwork bundle foundation so
Now Playing can render color while playing, grayscale while paused, and the
identical color data URI when playback resumes, while the four mosaic actions
render their exact artwork quadrants from the same bundle. Mute Toggle renders a
generated composite showing the selected audio source volume percent at the top plus the mute
or unmute speaker icon; all three audio actions retain independent source selections and
volume keys keep their host-rendered label; the dedicated
Play/Pause key switches between play and pause icons with playback state, and
Previous/Next render their transport labels. Generated
executables and transformed manifests do not belong in the repository, and this
packaged runtime does not replace the companion installer.

Run events enter a bounded 16-item queue and one serial HTTP worker. Progress polls
share that client, so each health request and its following state or command request
occupy one serialized turn; the two HTTP calls each retain their one-second timeout.
A full queue
or stopped worker rejects new work immediately with a sanitized status. Shutdown
does not drain old commands: queued work is discarded, the in-flight request is
cancelled before entering its serialized turn when possible, and worker shutdown is
bounded to 2.5 seconds. Cancellation that races after a turn is claimed cannot abort
that health-plus-command/state sequence; the one-second HTTP timeouts bound each call.
`run()` is the only cleanup and result authority. WebSocket close, EOF, and signal
callbacks only request stop and return immediately; they do not consume the final
result. A request before `run()` creates no API or router. External `stop()`
callers wait up to 10.5 seconds: 2.5 seconds each for progress stop, router stop, API
close, and API wait, plus 0.5 seconds of publication grace. If progress stop times
out, cleanup reports `progress_worker_alive` and does not stop the router, close the
API, or finish the API-wait stage while that worker can still call them. The original
`run()` cleanup owner waits for that worker and then finalizes the same transaction;
an indefinitely blocked SDK display call can therefore outlive the 10.5-second
external wait rather than triggering unsafe dependent cleanup. A live worker, close
timeout, or wait timeout produces exit code 1 rather than claiming clean shutdown.

Progress settings are normalized once in callback handling, rendered after a
scheduler wakeup, and persisted canonically by the same serialized progress worker.
Host echoes of canonical settings are not persisted again. Failed persistence is
contained and retried at most three times on normal scheduler cycles; stale,
inactive, cleared, or recreated contexts are revalidated before every SDK send.
