# Media Control for D200

Media Control for D200 is a local Windows integration that connects Spotify Desktop to an
Ulanzi D200 through Windows GSMTC, Core Audio, a loopback Python bridge, and an Ulanzi
Studio plugin. It requires no cloud service or account configuration: every component runs
on the same machine and communicates over loopback only.

> **Important — the plugin does not work without the companion bridge.** The bridge is a
> separate component that must be installed and running on the same Windows machine
> before the plugin can show media state. Without it, every key renders its `Offline`
> or `Companion setup required` fallback. See [Companion setup](#companion-setup) below.

## Requirements

- Windows 10/11
- Spotify Desktop
- Ulanzi Studio 2.1.4 or newer with a D200 device
- **The companion bridge, installed and running** — either through the companion
  installer (recommended, see `installer\README.md`) or manually with Python 3.11 or
  newer. This is mandatory: the plugin has no media data source without it.

The plugin package itself is self-contained: it runs on Ulanzi Studio's embedded Node.js
and a frozen Python runtime, so plugin users do not install Python, Node.js, or npm.
The managed Large Display workflow is physically validated on Ulanzi Studio 3.2.11;
revalidate profile compatibility after upgrading Studio.

## Components

Two pieces cooperate:

1. **Companion bridge** — a Python service that subscribes to Windows GSMTC and Core
   Audio, caches media state and artwork, and exposes a token-authenticated API on
   `http://127.0.0.1:43821` only.
2. **Plugin** — an Ulanzi Studio plugin with fourteen actions. A small Node.js launcher
   starts the bundled frozen Python runtime, which polls the bridge and renders every key.

### Companion setup

The companion bridge **must be installed and running before the plugin can display
anything**. Every key shows `Offline` or `Companion setup required` until the bridge
answers on `http://127.0.0.1:43821/health`.

Option A — prebuilt installer (recommended): download
`GSMTCD200Companion-<version>-local-unsigned.exe` from the latest
[GitHub release](https://github.com/arkamax404-max/MediaControlGsmtcPlugin/releases)
and run it. It installs the bridge under
`%LOCALAPPDATA%\Programs\GSMTCD200Controller`, registers an interactive
per-user scheduled task that starts the bridge silently at every logon, applies token
ACL hardening, and keeps the token, logs, and diagnostics under
`%LOCALAPPDATA%\GSMTCD200Controller`. The bridge runs in the background without any
window on the desktop. Advanced users can build the same installer from source with
`installer\build_installer.ps1` as described in `installer\README.md`.

Option B — manual: from the project root,

```powershell
python -m pip install -r requirements.txt
python -m d200_bridge
```

This option runs the bridge in a foreground console window; it must keep running while
you use the plugin, so start it before opening Ulanzi Studio and leave it open. The
installer of Option A is the unattended alternative: it starts the bridge automatically
and silently on every logon.

The bridge listens only on `http://127.0.0.1:43821`. Verify it with
`Invoke-RestMethod http://127.0.0.1:43821/health`. It is single-instance; stop a
foreground instance with `python -m d200_bridge --stop`. A privacy-filtered diagnostics
bundle can be produced without starting the bridge using
`python -m d200_bridge --diagnose`.

### Plugin installation

Prerequisite: the companion bridge must already be installed and running (see
[Companion setup](#companion-setup)); otherwise every key will show its offline fallback
right after installation.

- **Ulanzi Community Store**: once published, search for *Media Control for D200*.
- **Manual**: download `com.arkamax404.mediacontrold200.ulanziPlugin.zip` from the
  latest [GitHub release](https://github.com/arkamax404-max/MediaControlGsmtcPlugin/releases),
  then use Ulanzi Studio's plugin import interface and select the extracted
  `com.arkamax404.mediacontrold200.ulanziPlugin` folder. Restart Studio if the plugin
  list does not refresh.

Assign the desired actions from the `Media Control for D200` category to D200 keys.

For `Volume Up`, `Volume Down`, or `Mute Toggle`, select the key in Studio and choose
**Audio source** in its settings. The list contains **System volume** plus application
sessions currently active on the default Windows output. Each key remembers its own
choice: select the same source on all three keys for a unified volume control, or assign
different sources when desired. If an application is missing, start audio in it and wait
for the list to refresh.

### Large display quick setup

1. Open the target page and drag **Setup Large Display** to an unused normal key.
2. Select that key, choose **Install** in its Property Inspector, and press it once.
3. Wait for `Waiting for Studio to close`, then close Studio normally. Do not end its
   process or edit profile files.
4. Profile Assistant creates and verifies a complete backup, installs Large Now Playing,
   validates the result, and attempts to open Studio again. If restart fails, the profile
   result remains committed but Setup reports `Failed`; open Studio manually after reading
   the status.
5. Return to the target page. The center display now shows current media and the Setup key
   can be removed or reused.

Choose **Repair** if Studio later restores the built-in center widget. Choose **Restore
original** to recover the exact widget entry captured during Install. Both operations use
the same close and validation flow, followed by an attempted automatic relaunch.

## The Fourteen Actions

| Action | Press behavior | Key display |
|---|---|---|
| Now Playing | Toggle play/pause | Full artwork: color while playing, grayscale while paused, with title and artist |
| Large Now Playing | None (display only) | Center display with artwork, title, artist, playback state, progress, and time |
| Previous | Previous track | Transport icon with `Previous` label |
| Play/Pause | Toggle play/pause | `Pause` icon while playing, `Play` icon while paused |
| Next | Next track | Transport icon with `Next` label |
| Volume Up | Selected source volume +5 points | Volume icon with the selected percentage |
| Volume Down | Selected source volume −5 points | Volume icon with the selected percentage |
| Mute Toggle | Mute or unmute the selected source | Generated key: selected volume percentage at the top, speaker icon below |
| Track Progress | Cycle time mode | Circular progress arc with the selected time |
| Artwork Top Left | None (display only) | Top-left 196×196 quadrant of the artwork |
| Artwork Top Right | None (display only) | Top-right quadrant of the artwork |
| Artwork Bottom Left | None (display only) | Bottom-left quadrant of the artwork |
| Artwork Bottom Right | None (display only) | Bottom-right quadrant of the artwork |
| Setup Large Display | Install, repair, or restore the managed center action | Live transaction, recovery, and failure status |

Every action renders its own fallback when the companion is unreachable
(`Offline`), not yet configured (`Companion setup required`), or running an
incompatible API version (`Incompatible companion`).

### Large Now Playing

Ulanzi Studio reserves the D200 center slot `3_2` for its built-in small-window action,
so this action cannot be assigned by dragging it in the page editor. The managed Setup
action described below can update the exact live page while Studio is closed. The offline
clone workflow remains available when a separately imported profile is preferred.

With Studio closed, inspect an exported Version 2 profile:

```powershell
python .\tools\profile_tool.py inspect .\MyProfile.ulanziDeckProfile
```

Select the intended page ID from the output, then create a new clone. Use the manifest
from the exact plugin build that will be installed:

```powershell
python .\tools\profile_tool.py patch `
  .\MyProfile.ulanziDeckProfile `
  .\MyProfile.media-control.ulanziDeckProfile `
  --profile-id <PROFILE_ID> `
  --plugin-manifest .\com.arkamax404.mediacontrold200.ulanziPlugin\manifest.json
```

The command refuses to run while Studio is active, never overwrites an output, clones
the package, every page, and every action identity, and writes a sibling receipt with
hashes and validation results. Preserve the untouched export: it is the only rollback
authority. Start Studio only after both files have been written, import the clone, and
select it as a separate profile.

The runtime renders a fixed 458x196 SVG through the legacy `setBaseDataIcon` contract.
Its Property Inspector controls artwork visibility and fit, paused artwork, progress and
time labels, and presentation colors. Profile cloning is an offline installation step;
it is never imported or executed by the running plugin.
Large Now Playing persistently sets the private `SmallViewMode` to `2`, preventing the
built-in clock layer from returning when the page is reloaded. A user may temporarily
cycle the presentation from the device, but leaving and reopening the page restores the
background-only media view.

### Setup Large Display

Setup Large Display is a managed local profile operation. Place it on a normal key in the
page whose center display will be managed, choose **Install**, **Repair**, or **Restore** in
the Property Inspector, and press it. Close Ulanzi Studio when requested. The detached
helper derives the production paths itself, waits for Studio to stop, creates and verifies
a complete backup, and only then atomically updates the identified center entry. Install
and Repair require the built-in center action; Restore requires the exact managed Large
Now Playing entry and restores the original entry from its authenticated backup lineage.

This operation changes Studio's live `ProfilesV2` data and is therefore not risk-free.
Do not move, edit, or synchronize profile files while it runs. Keep the backup and state
directories under `%LOCALAPPDATA%\GSMTCD200Controller`. If the status reports that manual
recovery is required, the helper detected bytes that belong to neither side of its
transaction and deliberately preserved them instead of overwriting them. Back up that
profile before resolving the conflict. Process checks are repeated immediately before
every write and rollback; Windows cannot make process observation and process start one
indivisible operation, so a Studio start in the final instruction-level gap remains an
unavoidable residual race. The helper fails closed whenever it observes Studio running or
cannot determine its state.

| Status | Meaning |
|---|---|
| `Ready` | The Setup key uniquely identifies a compatible page with the built-in center widget. |
| `Launching` | The detached Profile Assistant process is starting. |
| `Waiting for Studio to close` | Close Studio normally so the offline transaction can continue. |
| `Installed` | Install or Repair completed; no restart failure was reported. |
| `Restored` | The exact original center widget was restored; no restart failure was reported. |
| `Failed` | The operation failed or Studio could not be restarted. A restart failure can occur after a validated profile update; read the reason before retrying or opening Studio manually. |

### Now Playing

The key displays the GSMTC thumbnail with the track title and artist. While playing it
uses the bridge's color PNG; while paused it uses the matching grayscale PNG of the same
artwork, and on resume it restores the identical color image. If no artwork is available
it falls back to a bundled music icon. Pressing the key toggles play/pause exactly once
per press.

### Transport keys

Previous, Play/Pause, and Next send exactly one command per press through the shared
command queue. The dedicated Play/Pause key reflects local playback state: it shows the
pause icon while playing and the play icon while paused, so the state is visible without
the artwork. Previous and Next keep static icons with their labels.

### Volume and mute

Volume Up, Volume Down, and Mute Toggle each have an Audio source selector in Studio.
It lists the Windows master volume and the application audio sessions currently present
on the default render endpoint. Each button stores its own selection, so the three can
use the same source or different sources. Spotify remains the default for existing keys.
Volume changes by five percentage points per press, clamps to 0-100%, and preserves
mute. When a process owns multiple sessions, Mute Toggle mutes all if any is unmuted,
otherwise it unmutes all.

Volume Up and Volume Down show the current percentage, `Muted`, `Mixed`, `No audio`, or
`Offline`. Mute Toggle renders a generated key image with the selected source percentage
at the top and a speaker icon below — the muted speaker while audio is active and the
unmuted speaker while muted — switching to `Muted`, `Mixed`, or `No audio` states as
appropriate. Its speaker is vertically aligned with Volume Up and Volume Down. Studio's
normal key title remains optional and user-controlled, so it can label the selected audio
source without the plugin forcing that text.

`pycaw` enumerates application sessions and accesses the master volume on the default
render endpoint. Sources playing through another render endpoint are outside this
implementation.

### Artwork Mosaic

Place the four artwork actions as one adjacent 2x2 block in this exact order:

```text
Artwork Top Left     | Artwork Top Right
Artwork Bottom Left  | Artwork Bottom Right
```

Together they display one centered 392x392 color artwork image. The complete source
image is preserved: non-square media uses transparent letterboxing or pillarboxing so
the D200's black key background shows through instead of cropping the artwork. Each key
receives one exact 196x196 PNG quadrant. The mosaic remains in color while playback is
paused. These four buttons are display-only: pressing them sends no command and changes
no playback or local mode state. The polled state contains only an artwork content ID;
the plugin fetches one immutable color, grayscale, and four-tile bundle when that ID
changes, then shares the validated bundle across every artwork key instead of
retransmitting images on each state poll.

### Track Progress

The circular arc starts at 12 o'clock and always shows the played fraction. The centered
label defaults to remaining time. Press the progress key to cycle that key through
remaining, elapsed, and total time, then back to remaining. This display-only
interaction sends no playback command. Each key keeps its mode for the current session
and resets to remaining when its context is recreated.

Labels use `m:ss`, or `h:mm:ss` for durations of at least one hour, and shrink to fit
longer values or thicker configured strokes while staying centered. Remaining time uses
ceiling rounding so a playing track does not show `0:00` before it ends. Pause freezes
the display; the final remaining-time state renders `0:00` once.

| Setting | Default | Accepted value |
|---|---:|---|
| Progress color | `#1DB954` | `#RRGGBB` |
| Track color | `#333333` | `#RRGGBB` |
| Text color | `#FFFFFF` | `#RRGGBB` |
| Background color | `#000000` | `#RRGGBB` |
| Stroke width | `14` | Integer `6`-`30` |

Select the Track Progress key in Studio to configure its colors and stroke width. Each
key instance keeps its own settings. Native color inputs and visible HEX fields are both
available. Settings and inspector input are treated as untrusted and normalized again in
the Python runtime before rendering SVG.

GSMTC owns the timeline data. Some applications, streams, advertisements, or session
transitions may temporarily expose no duration; the key then shows `No timeline`.

## Troubleshooting

| Symptom | Check |
|---|---|
| Keys show `Offline` | The companion bridge is not running. If it is not installed yet, run the prebuilt installer from the [latest release](https://github.com/arkamax404-max/MediaControlGsmtcPlugin/releases). With the installer, run the `GSMTCD200Controller-Companion` scheduled task (it also starts automatically at logon); manually, run `python -m d200_bridge`. Verify `http://127.0.0.1:43821/health`. Keep both apps on the same machine. |
| Keys show `Offline` right after a reboot | Wait about ten seconds after logon — the scheduled task starts the bridge with a short delay. If it still does not come up, start the task manually and check the logs under `%LOCALAPPDATA%\GSMTCD200Controller\logs`. |
| Installer reports a legacy task ACL repair failure | Open Task Scheduler as administrator, delete only `\GSMTCD200Controller-Companion`, then rerun the installer. |
| Keys show `Companion setup required` | The plugin could not read the bridge token; confirm the companion was set up for the same Windows user. |
| Music icon instead of cover | The active GSMTC session did not provide a thumbnail; controls and text still work. |
| Wrong media app is shown | Start playback in Spotify Desktop. Spotify sessions take precedence over the Windows current session. |
| Plugin is absent in Studio | Import the whole `.ulanziPlugin` folder; confirm Studio is at least 2.1.4. |
| State stops changing | Restart the bridge. State older than 15 seconds is shown as offline rather than as current. |
| Volume key shows `No audio` | Start the selected application and ensure it has a Core Audio session on the default render endpoint, or select System volume. |
| An audio key shows `Mixed` | Multiple sessions for the selected application disagree on volume or mute; the next action still applies once to each accessible session. |
| A source is absent from an audio selector | Start audio in that application, then reopen or wait for the selector to refresh. Only current sessions on the default render endpoint are listed. |
| Progress key shows `No timeline` | GSMTC did not publish a positive finite duration yet. Change tracks or wait for the Spotify session timeline event. |
| Progress colors do not save | Enter a complete `#RRGGBB` value in the visible HEX field; invalid values revert to defaults. |
| Progress freezes while playing | Confirm the key is active in the current Studio profile and `/state` reports `timeline_available: true` and `is_playing: true`. |
| Setup remains on `Launching` | Keep Studio open until the status changes to `Waiting for Studio to close`. The helper waits up to ten minutes after that request. |
| Setup reports `Failed` | Do not repeat the operation blindly. Read the Property Inspector reason. The helper either stopped before writing, rolled back a proven partial update, or completed the profile update but could not restart Studio; the message distinguishes these cases. |
| Setup reports manual recovery | The live manifest differs from both the original and intended result. The helper preserves it. Keep Studio closed and back up `%APPDATA%\Ulanzi\UlanziDeck\ProfilesV2` before resolving the conflict. |
| The center clock returns | Confirm the Large Now Playing settings contain `SmallViewMode: 2`; reopening the page should restore background-only mode. |

## Architecture

```text
Spotify Desktop -> Windows GSMTC -> Python companion (127.0.0.1:43821)
                                        ^
                                        | local HTTP polling every 1.5 s
Ulanzi D200 <- Ulanzi Studio <- plugin (Node launcher + frozen Python runtime)
```

The bridge subscribes to GSMTC media, playback, and timeline changes and performs a
five-second local refresh for freshness and recovery. Timeline-only events do not reread
media properties or thumbnails. Media, timeline, and audio are cached independently:
`available` remains GSMTC media state, while `audio_available`, `volume_percent`,
`is_muted`, `audio_session_count`, and `audio_mixed` describe Spotify Core Audio, while
`audio_sources` contains the current selectable process groups and system volume state.
`timeline_available`, `position_seconds`, `duration_seconds`, `playback_rate`, and
`position_updated_at` describe the normalized timeline anchor.

Its API is limited to `GET /health`, `GET /state`, `GET /artwork/{artwork_id}`, and
`POST /command/{previous,toggle,next,volume-up,volume-down,mute-toggle}`, plus
`POST /lifecycle/stop`. Every route except health requires the per-user Bearer token.
The plugin loads that token locally, authenticates every bridge request, and rechecks
API compatibility before every poll and command. Commands pin the validated companion
instance. The API has no CORS support, remote bind, shell execution, cloud component,
or device-discovery loop.

The plugin runtime connects to Studio through its launch-provided local WebSocket
arguments, renders keys through one scheduler worker, and sends transport commands
through one bounded serial queue. A successful command triggers one immediate state
poll so key displays update right away.

Mutable data is independent of the working directory under
`%LOCALAPPDATA%\GSMTCD200Controller`: `config\bridge-token`, rotating UTF-8 logs in
`logs`, and reserved `cache` and `diagnostics` roots. The token persists across
launches and is never returned or logged. Python creates the file atomically with the
restrictive semantics available to the standard library; the companion installer
additionally applies user-only ACL hardening.

Token authentication blocks browser-origin and accidental loopback callers; it does not
defend against a malicious process running as the same Windows user. Path metadata
checks are best-effort misconfiguration defense, not race-free containment.

A machine-wide named mutex coordinates ownership of the fixed loopback port across
Windows sessions. If that namespace is inaccessible, startup fails closed; it never
kills or replaces another process. The fixed port therefore permits only one companion
instance per machine.

**Local-only guarantee:** the bridge and plugin use Windows GSMTC, Core Audio, and
loopback traffic only. They do not communicate with cloud services.

## Development

The repository contains the plugin source, the companion bridge, the packaging that
produces the release plugin folder, and the per-user companion installer.

- Plugin source: `com.arkamax404.mediacontrold200.ulanziPlugin`
- Companion bridge: `d200_bridge`
- Release packaging: `packaging\README.md` (builds the frozen runtime and the
  fourteen-action plugin folder whose manifest points at `src/launcher.js`)
- Companion installer: `installer\README.md`

The source manifest keeps `CodePath: src/app.js` for in-repo Node.js development and
testing; the packaged release replaces it with the Node launcher plus frozen Python
runtime, and that packaged form is what ships to users. The Node implementation remains
the behavioral reference for parity, and its suite keeps running in CI alongside the
Python suites.

Install the plugin's locked Node.js development dependencies once:

```powershell
cd com.arkamax404.mediacontrold200.ulanziPlugin
npm ci
```

The pinned Ulanzi SDK files are already vendored under `vendor\ulanzi-sdk`.
`setup-sdk.ps1` is a maintainer recovery/refresh command, not a normal install step; it
re-downloads the four Node runtime files and five Property Inspector scripts from the
official Ulanzi SDK pins, verifying every SHA-256 checksum and preserving provenance.

## Other Possible Actions

Core Audio could also support explicit volume presets or per-session diagnostics, but
those actions are not implemented. GSMTC media controls remain deliberately separate
from Core Audio volume control.

## Local Verification

Run the Python suite from the project root:

```powershell
python -m unittest discover -s tests -v
```

Run the Node.js suite from the plugin directory after `npm ci`:

```powershell
cd com.arkamax404.mediacontrold200.ulanziPlugin
npm test
```

These suites use mocks and local ephemeral test servers; they do not start the bridge,
operate Ulanzi Studio, connect to a D200, or change real media or audio state.

The [Windows CI workflow](.github/workflows/ci.yml) runs the same complete suites at
the minimum supported Python and Node.js versions.

## Contributing and Security

See [CONTRIBUTING.md](CONTRIBUTING.md) for the focused Windows contribution workflow,
architecture boundaries, and safe verification expectations.

Potential vulnerabilities require private handling. Read [SECURITY.md](SECURITY.md)
before preparing a report; a verified private contact remains a publication blocker.

## Acknowledgements

Special thanks to [chilleno/claude-deck](https://github.com/chilleno/claude-deck) for
publicly documenting and demonstrating the community technique for assigning a
third-party action to the Ulanzi D200 reserved center slot (`3_2`). That evidence was
an important reference for the Large Now Playing implementation in this project.

Media Control for D200 independently implements its runtime rendering, profile safety,
backup, validation, repair, and rollback behavior and is not affiliated with or endorsed
by the Claude Deck project.

## License

Project-owned material is distributed under the MIT License; see `LICENSE`. The
project-specific SVG files under the plugin's `assets/` directory have no recorded
external source or attribution and are distributed as project material. Third-party
components retain their own license terms and are documented in
`THIRD_PARTY_NOTICES.md`.
