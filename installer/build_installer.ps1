[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ISCC,
    [Parameter(Mandatory)][string]$BundleRoot,
    [Parameter(Mandatory)][string]$OutputRoot,
    [Parameter(Mandatory)][string]$MetadataRoot,
    [Parameter(Mandatory)][string]$VerifierPython,
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-f]{7,40}$')][string]$CompanionSourceCommit
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$support = Join-Path $PSScriptRoot 'build_support.py'
$verifier = Join-Path $repo 'packaging\verify_companion_bundle.py'
$script = Join-Path $PSScriptRoot 'companion.iss'
$versionSource = Join-Path $repo 'd200_bridge\version.py'
$tooling = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'tooling.lock.json') -Raw | ConvertFrom-Json
function Assert-NoReparse([string]$Value) { $cursor = [IO.Path]::GetFullPath($Value); while ($cursor) { $item = Get-Item -LiteralPath $cursor -Force; if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Compiler reparse path' }; $parent = Split-Path -Parent $cursor; if (-not $parent -or $parent -eq $cursor) { break }; $cursor = $parent } }
$managed = @([IO.Directory]::GetParent([Environment]::SystemDirectory).FullName, [Environment]::GetFolderPath('ProgramFiles'), [Environment]::GetFolderPath('ProgramFilesX86'), [Environment]::GetFolderPath('CommonApplicationData')) | Where-Object { $_ }
function Assert-SafeBuildPath([string]$Value) { $path = [IO.Path]::GetFullPath($Value); if ($path -eq [IO.Path]::GetPathRoot($path) -or $path -match '(?i)^[A-Z]:\\(?:\$Recycle\.Bin|Recovery|System Volume Information)(?:\\|$)') { throw 'System-managed build path' }; foreach ($root in $managed) { if ($path -eq $root -or $path.StartsWith($root + '\',[StringComparison]::OrdinalIgnoreCase)) { throw 'System-managed build path' } } }
foreach ($path in ($repo,$ISCC,$VerifierPython,$BundleRoot,$OutputRoot,$MetadataRoot)) { Assert-SafeBuildPath $path }
foreach ($path in ($ISCC,$VerifierPython,$support,$verifier,$script,$versionSource)) { if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Required file missing" } }
$versionMatches = [regex]::Matches((Get-Content -LiteralPath $versionSource -Raw), '(?m)^COMPANION_VERSION\s*=\s*["'']((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))["'']\s*$')
if ($versionMatches.Count -ne 1) { throw 'Invalid companion version source' }
$appVersion = $versionMatches[0].Groups[1].Value
$expectedISCC = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Programs\Inno Setup 7\ISCC.exe'
if ([IO.Path]::GetFullPath($ISCC) -cne $expectedISCC) { throw 'Unexpected ISCC location' }
Assert-NoReparse $ISCC
$signature = Get-AuthenticodeSignature -LiteralPath $ISCC
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Pyrsys B\.V\.') { throw 'Untrusted ISCC signature' }
if ((Get-FileHash -LiteralPath $ISCC -Algorithm SHA256).Hash.ToLowerInvariant() -ne $tooling.iscc_sha256) { throw 'Unexpected ISCC hash' }
$innoVersion = (& $ISCC --version).Trim()
if ($innoVersion -ne '7.1.0') { throw 'Unexpected Inno version' }
$managedArgs = @($managed | ForEach-Object { '--managed-root'; $_ })
& $VerifierPython -I -s $support prepare --repo $repo --bundle $BundleRoot --output $OutputRoot --metadata $MetadataRoot --python $VerifierPython --verifier $verifier --source-root $PSScriptRoot @managedArgs
if ($LASTEXITCODE -ne 0) { throw 'Bundle preparation failed' }
if (Get-ChildItem -LiteralPath $OutputRoot -Force) { throw 'Output root must be empty' }
if (Get-ChildItem -LiteralPath $MetadataRoot -Force | Where-Object { $_.Name -notin @('bundle-manifest.json','bundle-snapshot-manifest.json','bundle-snapshot','bundle-files.iss','installer-source-snapshot.json','synthetic') }) { throw 'Unexpected metadata output' }
$include = Join-Path $MetadataRoot 'bundle-files.iss'
$snapshotRoot = Join-Path $MetadataRoot 'bundle-snapshot'
& $ISCC --no-ide-signtools --no-signing --output-dir=$OutputRoot --define=AppVersion=$appVersion --define=BundleRoot=$snapshotRoot --define=BundleFilesInclude=$include $script
if ($LASTEXITCODE -ne 0) { throw 'Inno compilation failed' }
$installer = Join-Path $OutputRoot ("GSMTCD200Companion-{0}-local-unsigned.exe" -f $appVersion)
if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) { throw 'Installer output missing' }
if ((Get-AuthenticodeSignature -LiteralPath $installer).Status -ne 'NotSigned') { throw 'Unexpected installer signature' }
& $VerifierPython -I -s $support receipt --repo $repo --companion-commit $CompanionSourceCommit --source-snapshot (Join-Path $MetadataRoot 'installer-source-snapshot.json') --source-root $PSScriptRoot --include $include --inno-version $innoVersion --installer $installer --bundle-manifest (Join-Path $MetadataRoot 'bundle-manifest.json') --snapshot-manifest (Join-Path $MetadataRoot 'bundle-snapshot-manifest.json') --snapshot-root $snapshotRoot --output (Join-Path $OutputRoot 'installer-build-receipt.json')
if ($LASTEXITCODE -ne 0) { throw 'Receipt generation failed' }
