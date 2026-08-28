#ifndef AppVersion
  #error AppVersion is required
#endif
#define AppName "GSMTC D200 Companion"
#ifndef BundleRoot
  #error BundleRoot is required
#endif
#ifndef BundleFilesInclude
  #error BundleFilesInclude is required
#endif

[Setup]
AppId={{E86C186A-E474-44DA-AC7E-450929F747EA}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=GSMTC D200 Controller contributors
VersionInfoVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\GSMTCD200Controller
DefaultGroupName=GSMTC D200 Controller
UninstallDisplayName={#AppName}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupArchitecture=x64
OutputBaseFilename=GSMTCD200Companion-{#AppVersion}-local-unsigned
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
ChangesAssociations=no
CloseApplications=no
RestartApplications=no
SignedUninstaller=no

[Files]
#include BundleFilesInclude
Source: "manage_companion.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion

[Icons]
Name: "{group}\Start Companion"; Filename: "{app}\versions\{#AppVersion}\bridge\GSMTCD200Companion.exe"; WorkingDir: "{app}\versions\{#AppVersion}\bridge"
Name: "{group}\Create Diagnostics Bundle"; Filename: "{app}\versions\{#AppVersion}\bridge\GSMTCD200Companion.exe"; Parameters: "--diagnose"; WorkingDir: "{app}\versions\{#AppVersion}\bridge"
Name: "{group}\Stop Companion"; Filename: "{app}\versions\{#AppVersion}\bridge\GSMTCD200Companion.exe"; Parameters: "--stop"; WorkingDir: "{app}\versions\{#AppVersion}\bridge"
Name: "{group}\Uninstall Companion"; Filename: "{uninstallexe}"

[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\GSMTCD200Controller\cache"
Type: filesandordirs; Name: "{localappdata}\GSMTCD200Controller"; Check: RemoveLocalData

[Code]
var SetupExitCode: Integer;

function RemoveLocalData: Boolean;
var I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), '/REMOVELOCALDATA') = 0 then Result := True;
end;

function ReadHelperPhase(StatusPath: String): String;
var Raw: AnsiString;
begin
  Result := 'unavailable';
  if LoadStringFromFile(StatusPath, Raw) then Result := Trim(Raw);
  DeleteFile(StatusPath);
  if (Result <> 'success') and (Result <> 'validation') and (Result <> 'dacl_create') and (Result <> 'dacl_metadata') and
     (Result <> 'dacl_descriptor') and (Result <> 'dacl_owner') and (Result <> 'dacl_rules') and
     (Result <> 'dacl_compare') and (Result <> 'dacl_apply') and (Result <> 'dacl_verify') and
     (Result <> 'dacl_enumerate') and (Result <> 'query') and
     (Result <> 'stop') and (Result <> 'task_register') and (Result <> 'task_remove') and
      (Result <> 'task_acl_repair') and
     (Result <> 'start') and (Result <> 'health') and (Result <> 'rollback_stop') and
     (Result <> 'rollback_remove') and (Result <> 'rollback_restore') and
     (Result <> 'rollback_restart') then Result := 'unavailable';
end;

function GetCustomSetupExitCode: Integer;
begin
  Result := SetupExitCode;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var ResultCode: Integer; Params, Phase, StatusPath: String;
begin
  if CurStep = ssPostInstall then begin
    StatusPath := ExpandConstant('{app}\installer\activation-status.txt'); DeleteFile(StatusPath);
    Params := '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\installer\manage_companion.ps1') + '" -Action Install -VersionRoot "' + ExpandConstant('{app}\versions\{#AppVersion}\bridge') + '" -DataRoot "' + ExpandConstant('{localappdata}\GSMTCD200Controller') + '" -StatusPath "' + StatusPath + '"';
    ResultCode := -1;
    if (not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode)) or (ResultCode <> 0) then begin
      Phase := ReadHelperPhase(StatusPath); Log(Format('Companion helper failed: phase=%s exit=%d', [Phase, ResultCode]));
      SetupExitCode := 1603;
      if Phase = 'task_acl_repair' then
        RaiseException('Companion activation could not repair the legacy task ACL. Open Task Scheduler as administrator, delete only \GSMTCD200Controller-Companion, then rerun this installer.')
      else
        RaiseException(Format('Companion activation failed: %s (%d). Files and the uninstall entry may remain for cleanup.', [Phase, ResultCode]));
    end;
    DeleteFile(StatusPath);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var ResultCode: Integer; Params, Phase, StatusPath: String;
begin
  if CurUninstallStep = usUninstall then begin
    StatusPath := ExpandConstant('{app}\installer\activation-status.txt'); DeleteFile(StatusPath);
    Params := '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\installer\manage_companion.ps1') + '" -Action UninstallTask -VersionRoot "' + ExpandConstant('{app}\versions\{#AppVersion}\bridge') + '" -DataRoot "' + ExpandConstant('{localappdata}\GSMTCD200Controller') + '" -StatusPath "' + StatusPath + '"'; ResultCode := -1;
    if (not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode)) or (ResultCode <> 0) then begin
      Phase := ReadHelperPhase(StatusPath); Log(Format('Companion uninstall helper failed: phase=%s exit=%d', [Phase, ResultCode]));
      RaiseException('Companion uninstall preparation failed');
    end;
    DeleteFile(StatusPath);
  end;
end;
