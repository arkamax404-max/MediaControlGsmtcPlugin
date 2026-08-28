[CmdletBinding()]
param([switch]$ProbeLegacyFr)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-Dacl([string]$SecurityDescriptor) {
    return ([Security.AccessControl.RawSecurityDescriptor]::new($SecurityDescriptor)).GetSddlForm([Security.AccessControl.AccessControlSections]::Access)
}
function Normalize-Dacl([string]$Dacl) {
    $normalized = [regex]::Replace($Dacl.ToUpperInvariant(), '0X0+([0-9A-F]+)', '0X$1')
    return $normalized.Replace('D:PAI(', 'D:P(')
}
function Get-PrincipalSid([string]$Identity) {
    try { return ([Security.Principal.SecurityIdentifier]::new($Identity)).Value } catch { return ([Security.Principal.NTAccount]::new($Identity)).Translate([Security.Principal.SecurityIdentifier]).Value }
}
function Assert-Task([object]$Task, [string]$ExpectedDacl, [string]$Sid) {
    if ((Get-PrincipalSid $Task.Definition.Principal.UserId) -ne $Sid -or [int]$Task.Definition.Principal.LogonType -ne 3) { throw "Interactive current-user principal was not preserved: user=$($Task.Definition.Principal.UserId), logon=$([int]$Task.Definition.Principal.LogonType)" }
    $actualDacl = Get-Dacl ($Task.GetSecurityDescriptor(7))
    if ((Normalize-Dacl $actualDacl) -ne (Normalize-Dacl $ExpectedDacl)) { throw "Task DACL was not preserved: $actualDacl" }
}

$CurrentUserSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$name = "GSMTCD200Controller-DaclProbe-$PID-$([guid]::NewGuid().ToString('N'))"
$targetDacl = "D:P(A;;0x001301BF;;;$CurrentUserSid)(A;;FA;;;SY)(A;;FA;;;BA)"
$legacyDacl = "D:P(A;;FR;;;$CurrentUserSid)(A;;FA;;;SY)(A;;FA;;;BA)"
# The disabled task is never run. Notepad is only a harmless, existing action target.
$xml = "<?xml version=`"1.0`" encoding=`"UTF-16`"?><Task version=`"1.4`" xmlns=`"http://schemas.microsoft.com/windows/2004/02/mit/task`"><RegistrationInfo><Description>DACL integration probe</Description></RegistrationInfo><Principals><Principal id=`"Author`"><UserId>$CurrentUserSid</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals><Settings><Enabled>false</Enabled></Settings><Actions Context=`"Author`"><Exec><Command>$env:WINDIR\System32\notepad.exe</Command></Exec></Actions></Task>"
$updatedXml = $xml.Replace('DACL integration probe', 'DACL integration probe updated').Replace('</Exec>', '<Arguments>--dacl-probe-update</Arguments></Exec>')
$scheduler = New-Object -ComObject 'Schedule.Service'
$scheduler.Connect()
$folder = $scheduler.GetFolder('\')

try {
    $stage = 'create explicit DACL'
    [void]$folder.RegisterTask($name, $xml, 0x16, $CurrentUserSid, $null, 3, $targetDacl)
    Assert-Task ($folder.GetTask($name)) $targetDacl $CurrentUserSid
    $stage = 'update explicit DACL task'
    [void]$folder.RegisterTask($name, $updatedXml, 0x16, $CurrentUserSid, $null, 3, $null)
    Assert-Task ($folder.GetTask($name)) $targetDacl $CurrentUserSid
    $stage = 'delete explicit DACL task'
    $folder.DeleteTask($name, 0)
    if ($folder.GetTasks(0) | Where-Object Name -eq $name) { throw 'Updated task was not deleted' }

    if ($ProbeLegacyFr) {
        # Strict FR proves the production failure. Cleanup uses the same exact-task
        # elevated continuation printed by the installer because FR denies deletion.
        $stage = 'create legacy FR task'
        [void]$folder.RegisterTask($name, $xml, 0x16, $CurrentUserSid, $null, 3, $legacyDacl)
        $denied = $false
        try { [void]$folder.RegisterTask($name, $updatedXml, 0x16, $CurrentUserSid, $null, 3, $null) } catch { $denied = $true }
        if (-not $denied) { throw 'Legacy FR DACL unexpectedly allowed task update' }
        $repairDenied = $false
        try { $folder.GetTask($name).SetSecurityDescriptor($targetDacl, 0) } catch { $repairDenied = $true }
        if (-not $repairDenied) { throw 'Legacy FR DACL unexpectedly allowed owner repair' }
    }
    'Task Scheduler DACL integration passed.'
} catch {
    throw "$stage failed: $($_.Exception.Message)"
} finally {
    try { $folder.DeleteTask($name, 0) } catch {
        if ($ProbeLegacyFr) { [void](Start-Process -FilePath schtasks.exe -Verb RunAs -Wait -PassThru -ArgumentList @('/Delete', '/TN', $name, '/F')) }
    }
}
