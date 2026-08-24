[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$PythonExe,
    [Parameter(Mandatory)][string]$VenvRoot,
    [Parameter(Mandatory)][string]$Wheelhouse,
    [Parameter(Mandatory)][string]$BuildRoot,
    [Parameter(Mandatory)][string]$DistRoot,
    [Parameter(Mandatory)][string]$MetadataRoot,
    [Parameter(Mandatory)][string]$CacheRoot,
    [Parameter(Mandatory)][string]$LicenseEvidenceRoot,
    [long]$SourceDateEpoch = 1787529600,
    [switch]$PopulateWheelhouse
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$lock = Join-Path $PSScriptRoot 'requirements-companion-win64.lock'
$spec = Join-Path $PSScriptRoot 'companion.spec'
$metadataTool = Join-Path $PSScriptRoot 'build_metadata.py'

function Assert-ExternalPath([string]$Value) {
    $full = [IO.Path]::GetFullPath($Value)
    if ($full.StartsWith($repo + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Build paths must be outside the repository"
    }
    return $full
}
function Ensure-Directory([string]$Value, [switch]$Empty) {
    $full = Assert-ExternalPath $Value
    $parent = Split-Path -Parent $full
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) { throw "Parent directory does not exist" }
    if (-not (Test-Path -LiteralPath $full)) { New-Item -ItemType Directory -Path $full | Out-Null }
    if ($Empty -and (Get-ChildItem -LiteralPath $full -Force)) { throw "Directory must be empty" }
    return $full
}

& $PythonExe -I -s $metadataTool roots $repo $VenvRoot $Wheelhouse $BuildRoot $DistRoot $MetadataRoot $CacheRoot $LicenseEvidenceRoot
$VenvRoot = Assert-ExternalPath $VenvRoot
$Wheelhouse = Ensure-Directory $Wheelhouse
$BuildRoot = Ensure-Directory $BuildRoot -Empty
$DistRoot = Ensure-Directory $DistRoot -Empty
$MetadataRoot = Ensure-Directory $MetadataRoot -Empty
$CacheRoot = Ensure-Directory $CacheRoot -Empty
$LicenseEvidenceRoot = Assert-ExternalPath $LicenseEvidenceRoot
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) { throw "Python executable missing" }
if (-not (Test-Path -LiteralPath (Split-Path -Parent $VenvRoot))) { throw "Venv parent missing" }
if (-not (Test-Path -LiteralPath (Join-Path $VenvRoot 'Scripts\python.exe'))) {
    & $PythonExe -I -s -m venv $VenvRoot
}
$venvPython = Join-Path $VenvRoot 'Scripts\python.exe'
$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
$env:PYTHONNOUSERSITE = '1'
Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue
Remove-Item Env:\PYTHONHOME -ErrorAction SilentlyContinue
if ($PopulateWheelhouse) {
    & $venvPython -I -s -m pip download --no-cache-dir --require-hashes --only-binary=:all: --dest $Wheelhouse -r $lock
}
& $venvPython -I -s -m pip install --no-cache-dir --no-index --find-links $Wheelhouse --require-hashes --only-binary=:all: -r $lock

$env:PYTHONHASHSEED = '0'
$env:SOURCE_DATE_EPOCH = [string]$SourceDateEpoch
$env:GSMTC_BUILD_METADATA = $MetadataRoot
$env:PYINSTALLER_CONFIG_DIR = $CacheRoot
& $venvPython -I -s $metadataTool prepare --lock $lock --wheelhouse $Wheelhouse --evidence-lock (Join-Path $PSScriptRoot 'license-evidence.lock') --evidence-root $LicenseEvidenceRoot --output $MetadataRoot --epoch $SourceDateEpoch
& $venvPython -I -s -m PyInstaller --noconfirm --clean --workpath $BuildRoot --distpath $DistRoot $spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
& $venvPython -I -s $metadataTool normalize-zip (Join-Path $DistRoot 'GSMTCD200Companion\_internal\base_library.zip')
& $venvPython -I -s (Join-Path $PSScriptRoot 'verify_companion_bundle.py') --finalize (Join-Path $DistRoot 'GSMTCD200Companion')
