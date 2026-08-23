# Third-Party Notices

SpotifyPip includes or depends on third-party components. Those components
retain their own copyright and license terms; the root MIT License applies only
to project-owned material.

## Vendored Components

| Component | Repository evidence | Local license evidence |
|---|---|---|
| Ulanzi `plugin-common-node` runtime files | `plugin/com.ulanzi.spotifygsm.ulanziPlugin/vendor/ulanzi-sdk/PROVENANCE.txt` identifies commit `112bd13a7ff9d45bd68656f7e069fd61851d1812` and the three vendored files. | Apache License 2.0, preserved at `plugin/com.ulanzi.spotifygsm.ulanziPlugin/vendor/ulanzi-sdk/LICENSE`. |
| Ulanzi `plugin-common-html` Property Inspector files | The five files listed in `PROVENANCE.txt` are unmodified, byte-identical copies from https://github.com/UlanziTechnology/plugin-common-html at commit [`79de0b0b087546e684afd23f97223f7a7bc392da`](https://github.com/UlanziTechnology/plugin-common-html/commit/79de0b0b087546e684afd23f97223f7a7bc392da). | Apache License 2.0. The upstream root license at that revision is byte-identical to `plugin/com.ulanzi.spotifygsm.ulanziPlugin/vendor/ulanzi-sdk/LICENSE`. The upstream tree has no `NOTICE` file at that revision, and the five files contain no additional embedded notices. |

See the vendored `PROVENANCE.txt` and `LICENSE` files for the complete local
record. The `plugin-common-html` conclusion applies only to the five exact
vendored files at the recorded revision.

## Installed Runtime Dependencies

The plugin declares `ws` version `8.21.3`. Its entry in
`plugin/com.ulanzi.spotifygsm.ulanziPlugin/package-lock.json` identifies its
license as MIT. The package is installed through npm and is not vendored as
project material.

The direct Python requirements are recorded in `requirements.txt`:

- `winrt-Windows.Foundation==3.2.1` on Windows
- `winrt-Windows.Foundation.Collections==3.2.1` on Windows
- `winrt-Windows.Media.Control==3.2.1` on Windows
- `winrt-Windows.Storage.Streams==3.2.1` on Windows
- `pycaw==20251023` on Windows
- `comtypes==1.4.16` on Windows
- `psutil==7.2.2` on Windows

`requirements.txt` contains no license fields and the repository has no Python
lockfile or copied license metadata for these distributions. Their license
status, and the complete set of their transitive dependencies, therefore
cannot be established from repository files alone. Installed distributions
retain their respective license terms.

## Project Assets

The repository records no external source or attribution for the SVG files
under `plugin/com.ulanzi.spotifygsm.ulanziPlugin/assets/`. They are distributed
as project material under the root MIT License.
