$ErrorActionPreference = "Stop"

$commit = "112bd13a7ff9d45bd68656f7e069fd61851d1812"
$baseUrl = "https://raw.githubusercontent.com/UlanziTechnology/plugin-common-node/$commit"
$htmlCommit = "79de0b0b087546e684afd23f97223f7a7bc392da"
$htmlBaseUrl = "https://raw.githubusercontent.com/UlanziTechnology/plugin-common-html/$htmlCommit"
$root = Join-Path $PSScriptRoot "vendor\ulanzi-sdk"
$files = @{
    "LICENSE" = "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4"
    "libs/constants.js" = "35c2cd088ebdcecd256412f5436555ec15a2510d9f09fedaef03f9014d227c62"
    "libs/utils.js" = "37a9ba4fc1f1346c733dfc55ced35d29325cf1a7252a4d96fcfecd9958f8b8d1"
    "libs/ulanziApi.js" = "4f4f307ae556a658669ab0bdf36016b1e151b25e4a5ec40451ab2c0724935168"
}
$htmlFiles = @{
    "html/js/constants.js" = "4d6581c19e34379cf28f4c56835545a81f8c036e4395f40f1e449debdb57302e"
    "html/js/eventEmitter.js" = "1167eaf1c4dce87942186bd35042851a3925ba8f19932dbd42e8050f991027c5"
    "html/js/timers.js" = "3b6948510c2136d8e58c9337682fbcd7d02aad8d67a5befa7bd1691e897aaae2"
    "html/js/utils.js" = "6f13551f6e2d771401e2ddf8763d22d692b5ecfc0dd2ddcaa4ddde5e7274691e"
    "html/js/ulanziApi.js" = "ebd369d1616ef77d93701ecd0ddf95f0c1dc1e4e248aa6c3e3db5f68d250112b"
}

foreach ($relativePath in $files.Keys) {
    $destination = Join-Path $root $relativePath
    $parent = Split-Path $destination -Parent
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    Invoke-WebRequest -Uri "$baseUrl/$relativePath" -OutFile $destination
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $destination).Hash.ToLowerInvariant()
    if ($actual -ne $files[$relativePath]) {
        Remove-Item -LiteralPath $destination -Force
        throw "Checksum mismatch for $relativePath"
    }
}

foreach ($relativePath in $htmlFiles.Keys) {
    $destination = Join-Path $root $relativePath
    $parent = Split-Path $destination -Parent
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $sourcePath = $relativePath.Substring(5)
    Invoke-WebRequest -Uri "$htmlBaseUrl/$sourcePath" -OutFile $destination
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $destination).Hash.ToLowerInvariant()
    if ($actual -ne $htmlFiles[$relativePath]) {
        Remove-Item -LiteralPath $destination -Force
        throw "Checksum mismatch for $relativePath"
    }
}

$provenance = @"
Official Ulanzi plugin-common-node runtime files.
Repository: https://github.com/UlanziTechnology/plugin-common-node
Commit: $commit
License: Apache-2.0 (see LICENSE)
Vendored files: libs/constants.js, libs/utils.js, libs/ulanziApi.js

Official Ulanzi Property Inspector runtime files.
Repository: https://github.com/UlanziTechnology/plugin-common-html
SDK submodule pin: https://github.com/UlanziTechnology/UlanziDeckPlugin-SDK/commit/550ab80c69285ecf259bd494a7fff767c14f0c0f
Commit: $htmlCommit
Protocol: Ulanzi JS Plugin Development Protocol V2.1.2 (compatible with Studio 3.1.9)
Vendored files and SHA-256:
html/js/constants.js 4d6581c19e34379cf28f4c56835545a81f8c036e4395f40f1e449debdb57302e
html/js/eventEmitter.js 1167eaf1c4dce87942186bd35042851a3925ba8f19932dbd42e8050f991027c5
html/js/timers.js 3b6948510c2136d8e58c9337682fbcd7d02aad8d67a5befa7bd1691e897aaae2
html/js/utils.js 6f13551f6e2d771401e2ddf8763d22d692b5ecfc0dd2ddcaa4ddde5e7274691e
html/js/ulanziApi.js ebd369d1616ef77d93701ecd0ddf95f0c1dc1e4e248aa6c3e3db5f68d250112b
"@

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText(
    (Join-Path $root "PROVENANCE.txt"),
    $provenance,
    $utf8NoBom
)
