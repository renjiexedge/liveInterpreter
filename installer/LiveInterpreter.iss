; Inno Setup 6 installer (guide/distribution_plan.md Phase 5, §4.4).
; Build with `build.ps1 -Installer`, which packs dist\LiveInterpreter\ and passes the
; version defines from version.py. By hand (after build.ps1):
;   ISCC.exe /DAppVersion=1.0.0-alpha.1 /DAppVerName="1.0.0 Alpha (build 1)" /DFileVersion=1.0.0.1 installer\LiveInterpreter.iss
; Output: dist\XedgeLiveInterpreter-<version>-setup.exe
;
; Per-user install (no UAC) into %LOCALAPPDATA%\Programs. The app writes only under
; %LOCALAPPDATA%\Xedge\LiveInterpreter and the key lives in Credential Manager, so an
; upgrade keeps both; uninstall deletes them only when the tester answers Yes.
; VB-Cable / Voicemeeter are never bundled or removed (decision 10).

#ifndef AppVersion
  #error Pass the version defines: build with build.ps1 -Installer
#endif

#define AppName "Xedge Live Interpreter"
#define AppExe "LiveInterpreter.exe"
#define DistDir "..\dist\LiveInterpreter"

[Setup]
; Fixed forever: Windows identifies the installed app (upgrade, uninstall) by it.
AppId={{179C984F-15F3-4039-808F-783F511304D0}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVerName}
VersionInfoVersion={#FileVersion}
VersionInfoProductVersion={#FileVersion}
VersionInfoProductTextVersion={#AppVerName}
AppPublisher=Xedge Resource
AppPublisherURL=https://xedgeresource.com
; {autopf} under lowest privileges = %LOCALAPPDATA%\Programs
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
UsePreviousAppDir=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Windows 11 only (decision 4)
MinVersion=10.0.22000
LicenseFile=tester_agreement.txt
SetupIconFile=..\ui\assets\app.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
OutputDir=..\dist
OutputBaseFilename=XedgeLiveInterpreter-{#AppVersion}-setup
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
; Setup and uninstall refuse to go on while the app holds this mutex (main.py MUTEX_NAME).
AppMutex=XedgeLiveInterpreter
CloseApplications=yes
RestartApplications=no

[InstallDelete]
; An upgrade replaces the whole bundle, so no stale module from the old build stays behind.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Tasks]
Name: desktopicon; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"
#if FileExists(AddBackslash(SourcePath) + DistDir + "\_internal\docs\guide.html")
; Phase 7 bundles the guide; the shortcut appears once it's in the build.
Name: "{group}\Setup guide"; Filename: "{app}\_internal\docs\guide.html"
#endif
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start {#AppName}"; Flags: nowait postinstall skipifsilent

[Code]
var
  RemoveData: Boolean;

function InitializeUninstall(): Boolean;
begin
  // A silent uninstall keeps the data: deleting it needs an explicit Yes.
  RemoveData := False;
  if not UninstallSilent() then
    RemoveData := MsgBox('Also delete your settings, logs, saved bug reports and the saved Gemini key?' + #13#10 + #13#10 +
                         'Choose No to keep them for a reinstall.' + #13#10 +
                         'VB-Cable and Voicemeeter are not removed either way.',
                         mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  // Not [UninstallRun]: its Check is evaluated at install time, before the answer exists.
  // usUninstall runs before the files go, so the exe is still there to forget the key.
  if (CurUninstallStep = usUninstall) and RemoveData then
    Exec(ExpandConstant('{app}\{#AppExe}'), '--uninstall-cleanup', '', SW_HIDE,
         ewWaitUntilTerminated, ResultCode);
  if (CurUninstallStep = usPostUninstall) and RemoveData then
  begin
    DelTree(ExpandConstant('{localappdata}\Xedge\LiveInterpreter'), True, True, True);
    RemoveDir(ExpandConstant('{localappdata}\Xedge'));  // only if nothing else of Xedge's is there
  end;
end;
