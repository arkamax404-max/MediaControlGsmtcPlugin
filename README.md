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

## Components

Two pieces cooperate:

1. **Companion bridge** — a Python service that subscribes to Windows GSMTC and Core
   Audio, caches media state and artwork, and exposes a token-authenticated API on
   `http://127.0.0.1:43821` only.
2. **Plugin** — an Ulanzi Studio plugin with twelve actions. A small Node.js launcher
   starts the bundled frozen Python runtime, which polls the bridge and renders every key.

### Companion setup

The companion bridge **must be installed and running before the plugin can display
anything**. Every key shows `Offline` or `Companion setup required` until the bridge
answers on `http://127.0.0.1:43821/health`.

Option A — companion installer (recommended): build the per-user installer with
`installer\build_installer.ps1` as described in `installer\README.md`. It installs the
bridge under `%LOCALAPPDATA%\Programs\GSMTCD200Controller`, registers an interactive
per-user scheduled task that starts the bridge silently at every logon, applies token
ACL hardening, and keeps the token, logs, and diagnostics under
`%LOCALAPPDATA%\GSMTCD200Controller`. The bridge runs in the background without any
window on the desktop.

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

## The Twelve Actions

| Action | Press behavior | Key display |
|---|---|---|
| Now Playing | Toggle play/pause | Full artwork: color while playing, grayscale while paused, with title and artist |
| Previous | Previous track | Transport icon with `Previous` label |
| Play/Pause | Toggle play/pause | `Pause` icon while playing, `Play` icon while paused |
| Next | Next track | Transport icon with `Next` label |
| Volume Up | Spotify volume +5 points | Volume icon with the current percentage |
| Volume Down | Spotify volume −5 points | Volume icon with the current percentage |
| Mute Toggle | Mute or unmute Spotify | Generated key: volume percentage at the top, speaker icon below |
| Track Progress | Cycle time mode | Circular progress arc with the selected time |
| Artwork Top Left | None (display only) | Top-left 196×196 quadrant of the artwork |
| Artwork Top Right | None (display only) | Top-right quadrant of the artwork |
| Artwork Bottom Left | None (display only) | Bottom-left quadrant of the artwork |
| Artwork Bottom Right | None (display only) | Bottom-right quadrant of the artwork |

Every action renders its own fallback when the companion is unreachable
(`Offline`), not yet configured (`Companion setup required`), or running an
incompatible API version (`Incompatible companion`).

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

The volume actions operate only on Core Audio sessions owned by `Spotify.exe`. They
never change the Windows master volume, endpoint volume, other applications, or media
playback. Volume changes by five percentage points per press, clamps to 0-100%, and
preserves mute. Mute Toggle applies one aggregate rule to all current Spotify sessions:
mute all if any is unmuted, otherwise unmute all.

Volume Up and Volume Down show the current percentage, `Muted`, `Mixed`, `No audio`, or
`Offline`. Mute Toggle renders a generated key image with the Spotify volume percentage
at the top and a speaker icon below — the muted speaker while audio is active and the
unmuted speaker while muted — switching to `Muted`, `Mixed`, or `No audio` states as
appropriate.

`pycaw`'s stable `GetAllSessions()` API enumerates sessions on the default render
endpoint. Spotify sessions playing through another render endpoint are therefore outside
this implementation; the bridge intentionally does not use lower-level unsafe COM
enumeration or `IAudioEndpointVolume` as a fallback.

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
| Keys show `Offline` | The companion bridge is not running. Start it: with the installer, run the `GSMTCD200Controller-Companion` scheduled task (it also starts automatically at logon); manually, run `python -m d200_bridge`. Verify `http://127.0.0.1:43821/health`. Keep both apps on the same machine. |
| Keys show `Offline` right after a reboot | Wait about ten seconds after logon — the scheduled task starts the bridge with a short delay. If it still does not come up, start the task manually and check the logs under `%LOCALAPPDATA%\GSMTCD200Controller\logs`. |
| Keys show `Companion setup required` | The plugin could not read the bridge token; confirm the companion was set up for the same Windows user. |
| Music icon instead of cover | The active GSMTC session did not provide a thumbnail; controls and text still work. |
| Wrong media app is shown | Start playback in Spotify Desktop. Spotify sessions take precedence over the Windows current session. |
| Plugin is absent in Studio | Import the whole `.ulanziPlugin` folder; confirm Studio is at least 2.1.4. |
| State stops changing | Restart the bridge. State older than 15 seconds is shown as offline rather than as current. |
| Volume key shows `No audio` | Start Spotify Desktop and ensure it has a Core Audio session on the default render endpoint. |
| Volume key shows `Mixed` | Multiple Spotify sessions disagree on volume or mute; the next action still applies once to each accessible session. |
| Progress key shows `No timeline` | GSMTC did not publish a positive finite duration yet. Change tracks or wait for the Spotify session timeline event. |
| Progress colors do not save | Enter a complete `#RRGGBB` value in the visible HEX field; invalid values revert to defaults. |
| Progress freezes while playing | Confirm the key is active in the current Studio profile and `/state` reports `timeline_available: true` and `is_playing: true`. |

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
`is_muted`, `audio_session_count`, and `audio_mixed` describe Spotify Core Audio.
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
  twelve-action plugin folder whose manifest points at `src/launcher.js`)
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

## License

Project-owned material is distributed under the MIT License; see `LICENSE`. The
project-specific SVG files under the plugin's `assets/` directory have no recorded
external source or attribution and are distributed as project material. Third-party
components retain their own license terms and are documented in
`THIRD_PARTY_NOTICES.md`.
