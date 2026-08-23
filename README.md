# SpotifyPip

SpotifyPip is a local Windows integration that connects Spotify Desktop to an
Ulanzi D200 through Windows GSMTC, Core Audio, a loopback Python bridge, and an
Ulanzi Studio plugin. It requires no cloud service or account configuration.

## D200 Quick Path

Requirements: Windows 10/11, Spotify Desktop, Ulanzi Studio 2.1.4 or newer,
Python 3.11 or newer, and Node.js 20.12.2 or newer with npm. Python 3.11 is
the current minimum because the bridge uses the 3.11 `asyncio.wait_for`
`TimeoutError` behavior in addition to Python 3.10 syntax. Core Audio support
is pinned to `pycaw==20251023`, `comtypes==1.4.16`, and `psutil==7.2.2` on
Windows. Pillow processes GSMTC artwork locally in memory.

1. Confirm the supported tool versions from the project root:

   ```powershell
   python -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11 or newer is required'"
   node -e "const [major, minor, patch] = process.versions.node.split('.').map(Number); if (major < 20 || (major === 20 && (minor < 12 || (minor === 12 && patch < 2)))) throw new Error('Node.js 20.12.2 or newer is required')"
   ```

   The Node.js requirement is also declared in the plugin's `package.json` and
   `package-lock.json`.

2. Install Python dependencies from the project root:

   ```powershell
   python -m pip install -r requirements.txt
   ```

   `requirements.txt` is the canonical Python dependency input. The Windows
   bridge dependencies are pinned. There is no Python lock file because
   producing a trustworthy lock requires dependency resolution for the intended
   Python and Windows target; this project does not treat a lock generated from
   one developer machine as reproducible.

3. Start the loopback GSMTC bridge and leave it running:

   ```powershell
   python -m d200_bridge
   ```

   It listens only on `http://127.0.0.1:43821`. Verify it with
   `Invoke-RestMethod http://127.0.0.1:43821/health`.

4. Install the plugin's locked Node.js dependencies once:

   ```powershell
   cd plugin\com.ulanzi.spotifygsm.ulanziPlugin
   npm ci
   ```

   The pinned Ulanzi SDK files are already vendored under `vendor\ulanzi-sdk`.
   `setup-sdk.ps1` is a maintainer recovery/refresh command, not a normal clean
   install step. Run it only when those vendored files are missing or when
   intentionally restoring them to their recorded pins:

   ```powershell
   .\setup-sdk.ps1
   ```

   The script downloads four Node runtime files from the official Ulanzi
   `plugin-common-node` repository at commit
   `112bd13a7ff9d45bd68656f7e069fd61851d1812` and five Property Inspector
   scripts from the official SDK's `plugin-common-html` pin
   `79de0b0b087546e684afd23f97223f7a7bc392da`. It verifies every SHA-256
   checksum, preserves the Node runtime's Apache-2.0 license, and records
   provenance for both upstream sources. `node_modules` is not part of the
   project-owned plugin files. See `THIRD_PARTY_NOTICES.md` for the locally
   verifiable license boundary.

5. In Ulanzi Studio, use its plugin import/install interface and select the complete
   `plugin\com.ulanzi.spotifygsm.ulanziPlugin` folder. Restart Studio if the plugin
   list does not refresh. Official SDK documentation describes installation into a
   designated plugin folder but does not publish a stable Windows filesystem path,
   so this project intentionally does not invent one.

6. Assign the desired actions from the `Spotify GSMTC` category to D200 keys.
   The existing `Now Playing`, `Track Progress`, transport, and volume actions
   remain available alongside the four artwork mosaic actions.

7. Select the `Track Progress` key in Studio to configure its colors and stroke
   width. Each key instance keeps its own settings. Native color inputs and
   visible HEX fields are both available.

The now-playing key displays the GSMTC thumbnail with title and artist. It uses
the bridge's color PNG while playing and its matching grayscale PNG while paused;
if the grayscale variant is unavailable, it keeps the color PNG and title text
without changing framing. If artwork is missing it uses a bundled music icon.
Artwork processing stays in memory and makes no remote API request. Controls
operate the Spotify Desktop GSMTC session when present, otherwise the current
Windows media session. The play/pause key tracks local playback state.

### Artwork Mosaic

Place the four artwork actions as one adjacent 2x2 block in this exact order:

```text
Artwork Top Left     | Artwork Top Right
Artwork Bottom Left  | Artwork Bottom Right
```

Together they display one centered 392x392 color artwork image. The complete
source image is preserved: non-square media uses transparent letterboxing or
pillarboxing so the D200's black key background shows through instead of cropping
the artwork. Each key receives one exact 196x196 PNG quadrant. The mosaic remains
in color while playback is paused. These four buttons have no press functionality
yet; pressing them sends no command and changes no playback or local mode state.
The polled state contains only an artwork content ID. The plugin fetches one
immutable color, grayscale, and four-tile bundle when that ID changes, then
shares the validated bundle across every artwork key instead of retransmitting
images on each state poll.

The volume actions operate only on Core Audio sessions owned by `Spotify.exe`.
They never change the Windows master volume, endpoint volume, other applications,
or media playback. Volume changes by five percentage points per press, clamps to
0-100%, and preserves mute. Mute Toggle applies one aggregate rule to all current
Spotify sessions: mute all if any is unmuted, otherwise unmute all. The keys show
the current percentage, `Muted`, `Mixed`, `No audio`, or `Offline`.

`pycaw`'s stable `GetAllSessions()` API enumerates sessions on the default render
endpoint. Spotify sessions playing through another render endpoint are therefore
outside this implementation; the bridge intentionally does not use lower-level
unsafe COM enumeration or `IAudioEndpointVolume` as a fallback.

### Track Progress

The circular arc starts at 12 o'clock and always shows the played fraction. The
centered label defaults to remaining time. Press the progress key to cycle that
key through remaining, elapsed, and total time, then back to remaining. This
display-only interaction sends no playback command and does not change the ring's
progress or animation. Each key keeps its mode for the current session and resets
to remaining when its context is recreated.

Labels use `m:ss`, or `h:mm:ss` for durations of at least one hour, and shrink to
fit longer values or thicker configured strokes while staying centered. Remaining
time uses ceiling rounding so a playing track does not show `0:00` before it ends.
Pause freezes the display; the final remaining-time state renders `0:00` once.

| Setting | Default | Accepted value |
|---|---:|---|
| Progress color | `#1DB954` | `#RRGGBB` |
| Track color | `#333333` | `#RRGGBB` |
| Text color | `#FFFFFF` | `#RRGGBB` |
| Background color | `#000000` | `#RRGGBB` |
| Stroke width | `14` | Integer `6`-`30` |

The plugin animates active progress keys with one shared timer, at most once per
second, while playback is running. The existing `/state` polling remains one
shared loop. Studio settings and inspector input are treated as untrusted and are
normalized again in the Node runtime before rendering SVG.

GSMTC owns the timeline data. Some applications, streams, advertisements, or
session transitions may temporarily expose no duration; the key then shows
`No timeline`. The bridge corrects a fresh GSMTC position with
`last_updated_time`, publishes a new UTC anchor, and refreshes that anchor from
events and periodic polling rather than extrapolating indefinitely from wall
clock alone.

## Troubleshooting

| Symptom | Check |
|---|---|
| Keys show `Offline` | Start `python -m d200_bridge`; verify `/health`; keep both apps on the same machine. |
| Music icon instead of cover | The active GSMTC session did not provide a thumbnail; controls and text still work. |
| Wrong media app is shown | Start playback in Spotify Desktop. Spotify sessions take precedence over the Windows current session. |
| Plugin is absent in Studio | Import the whole `.ulanziPlugin` folder after `npm ci`; confirm Studio is at least 2.1.4. |
| State stops changing | Restart the bridge. State older than 15 seconds is shown as offline rather than as current. |
| Volume key shows `No audio` | Start Spotify Desktop and ensure it has a Core Audio session on the default render endpoint. |
| Volume key shows `Mixed` | Multiple Spotify sessions disagree on volume or mute; the next action still applies once to each accessible session. |
| Progress key shows `No timeline` | GSMTC did not publish a positive finite duration yet. Change tracks or wait for the Spotify session timeline event. |
| Progress colors do not save | Enter a complete `#RRGGBB` value in the visible HEX field; invalid values revert to defaults. |
| Progress freezes while playing | Confirm the key is active in the current Studio profile and `/state` reports `timeline_available: true` and `is_playing: true`. |

## Architecture

```text
Spotify Desktop -> Windows GSMTC -> Python cache/API (127.0.0.1:43821)
                                      ^
                                      | local HTTP every 1.5 s
Ulanzi D200 <- Ulanzi Studio <- Node plugin
```

The bridge subscribes to GSMTC media, playback, and timeline changes and performs
a five-second local refresh for freshness and recovery. Timeline-only events do
not reread media properties or thumbnails. Media, timeline, and audio are cached independently;
`available` remains GSMTC media state, while `audio_available`, `volume_percent`,
`is_muted`, `audio_session_count`, and `audio_mixed` describe Spotify Core Audio.
`timeline_available`, `position_seconds`, `duration_seconds`, `playback_rate`, and
`position_updated_at` describe the normalized timeline anchor.
Its API is limited to `GET /health`, `GET /state`, `GET /artwork/{artwork_id}`, and
`POST /command/{previous,toggle,next,volume-up,volume-down,mute-toggle}`. It has no CORS support, remote bind, shell
execution, cloud component, or device-discovery loop. The plugin connects to Studio
through its launch-provided local WebSocket arguments and polls only the bridge.

**Local-only guarantee:** the bridge and plugin use Windows GSMTC, Core Audio,
and loopback traffic only. They do not communicate with cloud services.

## Other Possible Actions

Core Audio could also support explicit volume presets or per-session diagnostics,
but those actions are not implemented. GSMTC media controls remain deliberately
separate from Core Audio volume control.

## Local Verification

Run the Python suite from the project root:

```powershell
python -m unittest discover -s tests -v
```

Run the Node.js suite from the plugin directory after `npm ci`:

```powershell
cd plugin\com.ulanzi.spotifygsm.ulanziPlugin
npm test
```

These suites use mocks and local ephemeral test servers; they do not start the
bridge, operate Ulanzi Studio, connect to a D200, or change real media or audio state.

The [Windows CI workflow](.github/workflows/ci.yml) runs the same complete suites at
the minimum supported Python and Node.js versions.

## Contributing and Security

See [CONTRIBUTING.md](CONTRIBUTING.md) for the focused Windows contribution workflow,
architecture boundaries, and safe verification expectations.

Potential vulnerabilities require private handling. Read [SECURITY.md](SECURITY.md)
before preparing a report; a verified private contact remains a publication blocker.

## License

Project-owned material is distributed under the MIT License; see `LICENSE`.
The project-specific SVG files under the plugin's `assets/` directory have no
recorded external source or attribution and are distributed as project material.
Third-party components retain their own license terms and are documented in
`THIRD_PARTY_NOTICES.md`.
