# Contributing to SpotifyPip

Read the [README](README.md) first for the project overview, supported tools, and runtime setup. Routine contribution checks are Windows-oriented and use mocks and local ephemeral servers instead of Ulanzi Studio, a D200, or real media playback.

## Setup

Use Windows 10/11 with Python 3.11 or newer and Node.js 20.12.2 or newer. From the project root:

```powershell
python -m pip install -r requirements.txt
Push-Location plugin\com.ulanzi.spotifygsm.ulanziPlugin
npm ci
Pop-Location
```

`requirements.txt` is the canonical Python dependency input, not a resolved lock. The plugin dependencies are locked by `package-lock.json`; use `npm ci` rather than updating the lock during normal setup.

## Safe Verification

Run the complete Python suite from the project root:

```powershell
python -m unittest discover -s tests -v
```

Run the complete Node.js suite from the plugin directory:

```powershell
Push-Location plugin\com.ulanzi.spotifygsm.ulanziPlugin
npm test
Pop-Location
```

These tests use mocks and local ephemeral servers. Do not start the bridge, operate Ulanzi Studio, connect to a D200, or change real playback or audio state for routine verification.

## Architecture Boundaries

- `d200_bridge/` is the loopback-only GSMTC and Spotify Core Audio bridge.
- `plugin/com.ulanzi.spotifygsm.ulanziPlugin/` communicates with Ulanzi Studio and the fixed local bridge only.
- Media control, timeline state, and Spotify application audio are separate concerns. Do not add master-volume or unrelated application control as a fallback.

## Sensitive Data

The project requires no private configuration. Never include private machine data or sensitive logs and screenshots in a test, issue, or review. Follow [SECURITY.md](SECURITY.md) for vulnerability reports.

## Contribution Expectations

- Keep changes focused and preserve the local-only boundaries above.
- Add or update tests with behavioral changes and run both complete suites when the change can affect both runtimes.
- Update documentation when setup, behavior, or architecture changes.
- Explain dependency or lockfile changes explicitly; do not include incidental upgrades.
- Keep generated files, local caches, sensitive data, and machine-specific state out of contributions.
