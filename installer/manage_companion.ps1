[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('Install','Repair','UninstallTask','Query')][string]$Action,
    [Parameter(Mandatory)][string]$VersionRoot,
    [Parameter(Mandatory)][string]$DataRoot,
    [string]$TaskName = 'GSMTCD200Controller-Companion',
    [string]$LocalAppDataRoot = [Environment]::GetFolderPath('LocalApplicationData'),
    [string]$CurrentUserSid,
    [string]$FailurePoints = '',
    [string]$PriorTaskXml,
    [ValidateSet('Running','Ready','Disabled')][string]$PriorTaskStatus = 'Ready',
    [string]$StatusPath,
    [switch]$DisposableDaclTest,
    [switch]$DryRun
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
function Assert-Canonical([string]$Value) {
    $parts = if ($Value.Length -gt 3) { $Value.Substring(3).Split('\') } else { @() }
    $invalid = @($parts | Where-Object { $_ -in @('.','..') -or $_.EndsWith('.') -or $_.EndsWith(' ') -or $_ -match '^(?i:con|prn|aux|nul|clock\$|com[1-9]|lpt[1-9])(?:\.|$)' })
    if ($Value -notmatch '^[A-Z]:\\[^\p{Cc}\\/:*?"<>|]+(?:\\[^\p{Cc}\\/:*?"<>|]+)*$' -or $invalid.Count) { throw 'Noncanonical path' }
    $path = [IO.Path]::GetFullPath($Value); $cursor = $path
    while ($cursor) { if (Test-Path -LiteralPath $cursor) { if ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Reparse path' } }; $parent = Split-Path -Parent $cursor; if (-not $parent -or $parent -eq $cursor) { break }; $cursor = $parent }
    return $path
}
function New-ExactAcl([Security.Principal.SecurityIdentifier]$UserSid, [bool]$Container) {
    $acl = if ($Container) { [Security.AccessControl.DirectorySecurity]::new() } else { [Security.AccessControl.FileSecurity]::new() }
    $acl.SetOwner($UserSid); $acl.SetAccessRuleProtection($true, $false)
    $inherit = if ($Container) { [Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit' } else { [Security.AccessControl.InheritanceFlags]::None }
    foreach ($sid in @($UserSid, [Security.Principal.SecurityIdentifier]::new('S-1-5-18'))) { $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', $inherit, 'None', 'Allow')) }
    return $acl
}
function Get-TypedAcl([string]$Path, [bool]$Container) { $sections=[Security.AccessControl.AccessControlSections]'Access,Owner'; if($Container){return [IO.Directory]::GetAccessControl($Path,$sections)}else{return [IO.File]::GetAccessControl($Path,$sections)} }
function Set-TypedAcl([string]$Path, [bool]$Container, $Acl) { if($Container){[IO.Directory]::SetAccessControl($Path,[Security.AccessControl.DirectorySecurity]$Acl)}else{[IO.File]::SetAccessControl($Path,[Security.AccessControl.FileSecurity]$Acl)} }
function Test-ExactAcl([string]$Path, $Desired, [Security.Principal.SecurityIdentifier]$UserSid, [bool]$Instrument=$true) {
    if($Instrument){Set-Phase 'dacl_descriptor' 41;if(Test-Failure 'DaclDescriptor'){throw 'DACL diagnostic'}}; $acl=Get-TypedAcl $Path ($Desired -is [Security.AccessControl.DirectorySecurity])
    if($Instrument){Set-Phase 'dacl_owner' 42;if(Test-Failure 'DaclOwner'){throw 'DACL diagnostic'}}; $owner=$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    if($Instrument){Set-Phase 'dacl_rules' 43;if(Test-Failure 'DaclRules'){throw 'DACL diagnostic'}}; $rules=@($acl.GetAccessRules($true,$false,[Security.Principal.SecurityIdentifier])); $wanted=@($Desired.GetAccessRules($true,$false,[Security.Principal.SecurityIdentifier])); $key={param($r) "$($r.IdentityReference.Value)|$([int]$r.FileSystemRights)|$([int]$r.InheritanceFlags)|$([int]$r.PropagationFlags)|$([int]$r.AccessControlType)"}; $actual=@($rules|ForEach-Object{&$key $_})|Sort-Object; $expected=@($wanted|ForEach-Object{&$key $_})|Sort-Object
    if($Instrument){Set-Phase 'dacl_compare' 44;if(Test-Failure 'DaclCompare'){throw 'DACL diagnostic'}}; return $acl.AreAccessRulesProtected -and $owner -eq $UserSid.Value -and $rules.Count -eq $wanted.Count -and -not (Compare-Object $expected $actual)
}
function Set-ExactTreeAcl {
    $stack=[Collections.Generic.Stack[string]]::new(); $stack.Push($DataRoot); $visited=[Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase); $stats=[ordered]@{visited=0;applied=0;skipped=0}
    while($stack.Count){Set-Phase 'dacl_enumerate' 47; if(Test-Failure 'DaclEnumerate'){throw 'DACL diagnostic'}; $target=[IO.Path]::GetFullPath($stack.Pop()); if(-not $visited.Add($target)){throw 'Duplicate DACL path'}; Set-Phase 'dacl_metadata' 40; if(Test-Failure 'DaclMetadata'){throw 'DACL diagnostic'}; $item=Get-Item -LiteralPath $target -Force; if($item.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Unexpected reparse descendant'}; $container=$item -is [IO.DirectoryInfo]; if(-not $container -and $item -isnot [IO.FileInfo]){throw 'Unexpected nonregular descendant'}; $desired=New-ExactAcl $userSid $container; $stats.visited++
        if(Test-ExactAcl $target $desired $userSid){$stats.skipped++}else{Set-Phase 'dacl_apply' 45; if(Test-Failure 'DaclApply'){throw 'DACL diagnostic'}; Set-TypedAcl $target $container $desired; $stats.applied++; Set-Phase 'dacl_verify' 46; if(Test-Failure 'DaclVerify'){throw 'DACL diagnostic'}; if(-not (Test-ExactAcl $target $desired $userSid $false)){throw 'ACL verification failed'}}
        if($container){Set-Phase 'dacl_enumerate' 47; if(Test-Failure 'DaclEnumerate'){throw 'DACL diagnostic'}; foreach($child in Get-ChildItem -LiteralPath $target -Force){$stack.Push($child.FullName)}}
    }; return [pscustomobject]$stats
}
function Test-ListenerOrMutex {
    $open = $false; $client = [Net.Sockets.TcpClient]::new()
    try { $async = $client.BeginConnect('127.0.0.1', 43821, $null, $null); $open = $async.AsyncWaitHandle.WaitOne(200) -and $client.Connected } catch {} finally { $client.Dispose() }
    try { $mutex = [Threading.Mutex]::OpenExisting('Global\GSMTCD200Controller.Companion'); $mutex.Dispose(); $open = $true } catch [Threading.WaitHandleCannotBeOpenedException] {}
    return $open
}
function Test-Mutex { try { $mutex = [Threading.Mutex]::OpenExisting('Global\GSMTCD200Controller.Companion'); $mutex.Dispose(); return $true } catch [Threading.WaitHandleCannotBeOpenedException] { return $false } }
function Test-ExactRuntime([int]$ProcessId, [string]$Path) {
    if ($DryRun) { $runtime = $script:runtime; return $runtime.process_alive -and $runtime.pid -eq $ProcessId -and $runtime.path -ceq $Path -and $runtime.listener -and $runtime.mutex -and $runtime.health }
    try { $process = Get-Process -Id $ProcessId -ErrorAction Stop; if ($process.HasExited -or [IO.Path]::GetFullPath($process.Path) -cne $Path) { return $false }; $listeners = @(Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort 43821 -State Listen -ErrorAction Stop); if ($listeners.Count -ne 1 -or $listeners[0].OwningProcess -ne $ProcessId -or -not (Test-Mutex)) { return $false }; $health = Invoke-RestMethod -Uri 'http://127.0.0.1:43821/health' -TimeoutSec 1; $instance = [guid]::Empty; return $health.service -eq 'd200-gsmtc-bridge' -and $health.companion_version -eq '1.2.0' -and $health.api_major -eq 1 -and $health.api_minor -ge 0 -and $health.status -in @('ready','degraded') -and [guid]::TryParse([string]$health.instance_id,[ref]$instance) } catch { return $false }
}
function Get-XmlTarget([string]$Xml) { try { $doc = [xml]$Xml; $node = $doc.SelectSingleNode("/*[local-name()='Task']/*[local-name()='Actions']/*[local-name()='Exec']/*[local-name()='Command']"); if (-not $node -or -not $node.InnerText) { throw 'missing' }; return [IO.Path]::GetFullPath($node.InnerText) } catch { throw 'Malformed task XML' } }
function Test-Failure([string]$Name) { return $script:failures.ContainsKey($Name) }
function Test-TaskNotFoundHResult([object]$Value) { try { $code=[uint64]([int64]$Value -band 0xffffffffL) } catch { return $false }; return $code -in @(0x80070002L,0x8004130FL) }
function Set-Phase([string]$Name, [int]$Code) { $script:phase=$Name; $script:exitCode=$Code; if($DryRun){$script:phaseTrace += $Name} }
function Save-Phase { if (-not $DryRun) { [IO.File]::WriteAllText($StatusPath, $script:phase, [Text.UTF8Encoding]::new($false)) } }
function Get-OwnedTask {
    if ($DryRun) { $probes=@{QueryMissingSigned=-2147024894;QueryMissingUnsigned=2147942402;QueryAccess=2147942405;QueryService=2147750677}; foreach($name in $probes.Keys){if(Test-Failure $name){if(Test-TaskNotFoundHResult $probes[$name]){return $null};throw 'Task query failed'}}; if (Test-Failure 'Query') { throw 'Task query failed' }; return $script:memoryTask }
    try { $raw = $script:taskFolder.GetTask($TaskName) } catch { if (Test-TaskNotFoundHResult $_.Exception.GetBaseException().HResult) { return $null }; throw }
    if (-not $raw) { return $null }; $actions = $raw.Definition.Actions
    if ($actions.Count -ne 1 -or $actions.Item(1).Type -ne 0) { throw 'Unexpected task definition' }
    $states = @('Unknown','Disabled','Queued','Ready','Running'); return [pscustomobject]@{Xml=$raw.Xml;Status=$states[[int]$raw.State];Target=[IO.Path]::GetFullPath($actions.Item(1).Path);Raw=$raw}
}
function Set-OwnedTask([string]$Xml, [string]$Target, [string]$Status, [string]$Phase) {
    if ($DryRun) { if (Test-Failure $Phase) { throw "$Phase failed" }; $script:memoryTask = [pscustomobject]@{Xml=$Xml;Status=if($Status -eq 'Disabled'){'Disabled'}else{'Ready'};Target=$Target}; return }
    [void]$script:taskFolder.RegisterTask($TaskName, $Xml, 6, $null, $null, 3, $null)
    $task = Get-OwnedTask
    if (-not $task -or $task.Target -cne $Target -or ($Status -eq 'Disabled' -and $task.Status -ne 'Disabled') -or ($Status -ne 'Disabled' -and $task.Status -notin @('Ready','Running'))) { throw 'Task registration verification failed' }
}
function Remove-OwnedTask {
    if ($DryRun) { if (Test-Failure 'Delete') { throw 'Candidate task removal failed' }; $script:memoryTask = $null }
    else { $script:taskFolder.DeleteTask($TaskName, 0); if (Get-OwnedTask) { throw 'Task removal verification failed' } }
}
function Stop-Owner([string]$Owner) {
    if ($DryRun) { if ($Owner -eq 'Candidate') { $script:runtime.stop_invoked=$true }; if (Test-Failure ($Owner + 'Stop')) { throw "$Owner stop failed" }; if ($Owner -eq 'Candidate' -and (Test-Failure 'CandidateAliveNoListener')) { $script:runtime.listener=$false; $script:runtime.mutex=$false; $script:runtime.health=$false; throw 'Candidate process did not exit' }; if ($script:owner -eq $Owner) { $script:owner = 'None'; $script:runtime.process_alive=$false; $script:runtime.listener=$false; $script:runtime.mutex=$false; $script:runtime.health=$false }; return }
    if ($Owner -eq 'Candidate') { & $exe --stop | Out-Null; if ($LASTEXITCODE -ne 0) { throw 'Candidate stop failed' }; if (-not $script:candidateProcess.WaitForExit(10000)) { throw 'Candidate process did not exit' }; $script:owner='None'; return }
    if (Test-ListenerOrMutex) { & $exe --stop | Out-Null; if ($LASTEXITCODE -ne 0) { throw "$Owner stop failed" }; $deadline = [DateTime]::UtcNow.AddSeconds(10); while (Test-ListenerOrMutex) { if ([DateTime]::UtcNow -ge $deadline) { throw "$Owner release failed" }; Start-Sleep -Milliseconds 200 } }
}
function Start-Prior {
    if ($DryRun) { if (Test-Failure 'Restart') { throw 'Prior restart failed' }; if (Test-Failure 'PriorImmediateExit') { $script:memoryTask.Status='Ready'; $script:runtime.process_alive=$false; throw 'Prior process exited' }; $script:memoryTask.Status='Running'; $script:owner='Prior'; $script:runtime=[ordered]@{process_alive=$true;listener=$true;mutex=$true;health=$true;pid=101;path=$script:memoryTask.Target;stable_polls=0;stop_invoked=$false}; foreach($poll in 1..6){if(Test-Failure "PriorPoll$poll"){$script:runtime.health=$false};if(-not (Test-ExactRuntime 101 $script:memoryTask.Target)){throw 'Prior runtime invalid'};$script:runtime.stable_polls=$poll}; return }
    $task = Get-OwnedTask; $running = $task.Raw.Run($null); $deadline = [DateTime]::UtcNow.AddSeconds(30); do { $processId = [int]$running.EnginePID; if ($processId -gt 0) { break }; Start-Sleep -Milliseconds 250 } while ([DateTime]::UtcNow -lt $deadline); if ($processId -le 0) { throw 'Prior PID unavailable' }
    do { $task = Get-OwnedTask; if ($task.Status -eq 'Running' -and (Test-ExactRuntime $processId $task.Target)) { break }; Start-Sleep -Milliseconds 250 } while ([DateTime]::UtcNow -lt $deadline)
    if ($task.Status -ne 'Running' -or -not (Test-ExactRuntime $processId $task.Target)) { throw 'Prior running state was not restored' }; foreach ($poll in 1..6) { Start-Sleep -Milliseconds 500; $task = Get-OwnedTask; if ($task.Status -ne 'Running' -or -not (Test-ExactRuntime $processId $task.Target)) { throw 'Prior running state was not stable' } }
}
function Start-Candidate {
    if ($DryRun) { $script:candidateStarted=$true; $script:candidatePid=202; $script:candidatePath=$exe; $script:owner='Candidate'; $script:runtime=[ordered]@{process_alive=$true;listener=$true;mutex=$true;health=(!(Test-Failure 'Health'));pid=202;path=$exe;stable_polls=1;stop_invoked=$false}; if (Test-Failure 'CandidateAliveNoListener') { $script:runtime.listener=$false; $script:runtime.mutex=$false; $script:runtime.health=$false }; return }
    $script:candidateProcess = Start-Process -FilePath $exe -WorkingDirectory $VersionRoot -WindowStyle Hidden -PassThru
    $script:candidateStarted=$true; $script:candidatePid=$script:candidateProcess.Id; $script:candidatePath=$exe; $script:owner='Candidate'
    if ([IO.Path]::GetFullPath($script:candidateProcess.MainModule.FileName) -cne $script:candidatePath) { throw 'Candidate executable mismatch' }
}
function Test-CandidateHealth {
    if ($DryRun) { return (Test-ExactRuntime 202 $exe) -and -not (@('CandidateStop','Delete','Restore','Restart','PriorImmediateExit') | Where-Object { Test-Failure $_ }) }
    $deadline = [DateTime]::UtcNow.AddSeconds(30); while ([DateTime]::UtcNow -lt $deadline) { if ($script:candidateProcess.HasExited) { return $false }; if (Test-ExactRuntime $script:candidatePid $script:candidatePath) { return $true }; Start-Sleep -Milliseconds 250 }; return $false
}
function Write-DryResult([string]$ErrorText) { if($DisposableDaclTest){[ordered]@{success=(!$ErrorText);phase=$script:phase;exit_code=$script:exitCode;phase_trace=$script:phaseTrace;dacl=$script:daclStats}|ConvertTo-Json -Compress;return}; [ordered]@{operation=$operation;success=(!$ErrorText);phase=$script:phase;exit_code=$script:exitCode;phase_trace=$script:phaseTrace;dacl=$script:daclStats;error=$ErrorText;rollback=$script:rollback;rollback_errors=$script:rollbackErrors;task=if($script:memoryTask){[ordered]@{status=$script:memoryTask.Status;target=$script:memoryTask.Target}}else{$null};owner=$script:owner;runtime=$script:runtime;task_name=$TaskName;sid=$CurrentUserSid;task_xml=$xml;directory_sddl=$dirAcl.GetSecurityDescriptorSddlForm('All');acl_targets=@($ownedDirs + $tokenPath)} | ConvertTo-Json -Compress }
$LocalAppDataRoot = Assert-Canonical $LocalAppDataRoot; $VersionRoot = Assert-Canonical $VersionRoot; $DataRoot = Assert-Canonical $DataRoot
if ($VersionRoot -cne (Join-Path $LocalAppDataRoot 'Programs\GSMTCD200Controller\versions\1.2.0\bridge') -or $DataRoot -cne (Join-Path $LocalAppDataRoot 'GSMTCD200Controller') -or $TaskName -cne 'GSMTCD200Controller-Companion') { throw 'Unexpected companion path or task' }
if (-not $DryRun -and ($FailurePoints -or $PriorTaskXml)) { throw 'Simulation options are dry-run only' }; if (-not $DryRun -and $CurrentUserSid) { throw 'SID injection is dry-run only' }
if($DisposableDaclTest -and (-not $DryRun -or -not $DataRoot.StartsWith([IO.Path]::GetFullPath([IO.Path]::GetTempPath()),[StringComparison]::OrdinalIgnoreCase))){throw 'Disposable DACL test requires user temp dry-run'}
$allowedFailures = @('Dacl','DaclMetadata','DaclDescriptor','DaclOwner','DaclRules','DaclCompare','DaclApply','DaclVerify','DaclEnumerate','Query','QueryMissingSigned','QueryMissingUnsigned','QueryAccess','QueryService','PriorStop','Create','Start','Health','CandidateStop','CandidateAliveNoListener','Delete','Restore','Restart','PriorImmediateExit') + @(1..6 | ForEach-Object { "PriorPoll$_" }); $script:failures = @{}; foreach ($point in @($FailurePoints.Split(',', [StringSplitOptions]::RemoveEmptyEntries))) { if ($point -notin $allowedFailures) { throw 'Unknown failure point' }; $script:failures[$point] = $true }
if (-not $CurrentUserSid) { $CurrentUserSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value }; try { $userSid = [Security.Principal.SecurityIdentifier]::new($CurrentUserSid) } catch { throw 'Invalid user SID' }
$CurrentUserSid = $userSid.Value; $exe = Join-Path $VersionRoot 'GSMTCD200Companion.exe'; $dirAcl = New-ExactAcl $userSid $true; $fileAcl = New-ExactAcl $userSid $false
$ownedDirs = @($DataRoot) + @('config','logs','cache','diagnostics' | ForEach-Object { Join-Path $DataRoot $_ }); $tokenPath = Join-Path $DataRoot 'config\bridge-token'; $escapedExe = [Security.SecurityElement]::Escape($exe); $escapedWork = [Security.SecurityElement]::Escape($VersionRoot)
if (-not $DryRun) { $appRoot=Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $VersionRoot)); if (-not $StatusPath -or [IO.Path]::GetFullPath($StatusPath) -cne (Join-Path $appRoot 'installer\activation-status.txt')) { throw 'Unexpected status path' }; $StatusPath=[IO.Path]::GetFullPath($StatusPath) }
$xml = "<?xml version=`"1.0`" encoding=`"UTF-16`"?><Task version=`"1.4`" xmlns=`"http://schemas.microsoft.com/windows/2004/02/mit/task`"><RegistrationInfo><URI>$TaskName</URI></RegistrationInfo><Triggers><LogonTrigger><Enabled>true</Enabled><UserId>$CurrentUserSid</UserId><Delay>PT10S</Delay></LogonTrigger></Triggers><Principals><Principal id=`"Author`"><UserId>$CurrentUserSid</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals><Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable><RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><RestartOnFailure><Interval>PT30S</Interval><Count>3</Count></RestartOnFailure></Settings><Actions Context=`"Author`"><Exec><Command>$escapedExe</Command><WorkingDirectory>$escapedWork</WorkingDirectory></Exec></Actions></Task>"
$operation = if ($Action -eq 'UninstallTask') { 'delete_exact_task' } elseif ($Action -eq 'Query') { 'query_exact_task' } else { 'migrate_exact_task' }; $script:rollback = 'none'; $script:rollbackErrors = @(); $script:owner = 'None'; $script:candidateStarted=$false; $script:candidatePid=0; $script:candidatePath=$exe; $script:phase='query'; $script:exitCode=21; $script:phaseTrace=@(); $script:daclStats=$null; $script:runtime=[ordered]@{process_alive=$false;listener=$false;mutex=$false;health=$false;pid=0;path='';stable_polls=0;stop_invoked=$false}
if ($DryRun) { $script:memoryTask = if($PriorTaskXml){[pscustomobject]@{Xml=$PriorTaskXml;Status=$PriorTaskStatus;Target=(Get-XmlTarget $PriorTaskXml)}}else{$null}; if ($PriorTaskXml -and $PriorTaskStatus -eq 'Running') { $script:owner='Prior'; $script:runtime=[ordered]@{process_alive=$true;listener=$true;mutex=$true;health=$true;pid=101;path=$script:memoryTask.Target;stable_polls=0;stop_invoked=$false} } }
else { if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { throw 'Companion executable missing' } }
try {
    Set-Phase 'query' 21; if (-not $DryRun) { $scheduler = New-Object -ComObject 'Schedule.Service'; $scheduler.Connect(); $script:taskFolder = $scheduler.GetFolder('\') }
    if ($Action -eq 'Query') { $task = Get-OwnedTask; Set-Phase 'success' 0; Save-Phase; if ($DryRun) { Write-DryResult '' } elseif ($task) { $task.Xml }; exit 0 }
    if ($Action -eq 'UninstallTask') { $task = Get-OwnedTask; Set-Phase 'stop' 22; Stop-Owner 'Prior'; if ($task) { Set-Phase 'task_remove' 26; Remove-OwnedTask }; Set-Phase 'success' 0; Save-Phase; if ($DryRun) { Write-DryResult '' }; exit 0 }
    $prior = Get-OwnedTask; if ($prior -and $prior.Status -notin @('Running','Ready','Disabled')) { throw 'Unsupported prior task status' }; if ($prior -and $prior.Status -eq 'Running') { Set-Phase 'stop' 22; Stop-Owner 'Prior' }
    $candidate = $false
    try {
        Set-Phase 'dacl_create' 20; if ($DryRun -and (Test-Failure 'Dacl')) { throw 'Dacl failed' }; if (-not $DryRun -or $DisposableDaclTest) { foreach ($name in ('config','logs','cache','diagnostics')) { New-Item -ItemType Directory -Path (Join-Path $DataRoot $name) -Force | Out-Null }; $script:daclStats=Set-ExactTreeAcl }; if($DisposableDaclTest){Set-Phase 'success' 0; Write-DryResult ''; exit 0}
        Set-Phase 'task_register' 23; Set-OwnedTask $xml $exe 'Ready' 'Create'; $candidate = $true; Set-Phase 'start' 24; if ($DryRun -and (Test-Failure 'Start')) { throw 'Start failed' }; Start-Candidate; Set-Phase 'health' 25; if (-not (Test-CandidateHealth)) { throw 'Candidate health gate failed' }
    } catch {
        $primary=$_.Exception.Message; $primaryPhase=$script:phase; $primaryCode=$script:exitCode; $rollbackPhase=''; $rollbackCode=0
        if ($candidate) { if ($script:candidateStarted) { Set-Phase 'rollback_stop' 30; try { Stop-Owner 'Candidate' } catch { $script:rollbackErrors += $_.Exception.Message; $rollbackPhase=$script:phase; $rollbackCode=$script:exitCode } }; Set-Phase 'rollback_remove' 31; try { Remove-OwnedTask } catch { $script:rollbackErrors += $_.Exception.Message; if(-not $rollbackCode){$rollbackPhase=$script:phase;$rollbackCode=$script:exitCode} } }
        if ($prior) { Set-Phase 'rollback_restore' 32; try { Set-OwnedTask $prior.Xml $prior.Target $prior.Status 'Restore'; if ($prior.Status -eq 'Running') { if ($script:owner -eq 'Candidate') { throw 'Prior restart blocked by candidate' }; Set-Phase 'rollback_restart' 33; Start-Prior } else { $restored = Get-OwnedTask; if ($restored.Status -ne $prior.Status) { throw 'Prior status restoration failed' } } } catch { $script:rollbackErrors += $_.Exception.Message; if(-not $rollbackCode){$rollbackPhase=$script:phase;$rollbackCode=$script:exitCode} } }
        $script:rollback = if($script:rollbackErrors.Count){'incomplete'}else{'complete'}; if($rollbackCode){Set-Phase $rollbackPhase $rollbackCode}else{Set-Phase $primaryPhase $primaryCode}; throw "$primary; rollback $script:rollback"
    }
    Set-Phase 'success' 0; Save-Phase; if ($DryRun) { Write-DryResult '' }
} catch { Save-Phase; if ($DryRun) { Write-DryResult $_.Exception.Message }; exit $script:exitCode }
