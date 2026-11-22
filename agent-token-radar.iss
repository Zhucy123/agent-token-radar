; ---------------------------------------------------------------------------
;  Agent Token Radar - Inno Setup script
;
;  Build:  ISCC.exe agent-token-radar.iss
;  Output: dist-installer\AgentTokenRadar-Setup.exe
;
;  Two details here are load-bearing:
;
;  * DefaultDirName is an ALL-ASCII path (Program Files). The .NET CLR that
;    pywebview needs resolves native DLL paths as ANSI, so an install directory
;    containing non-ASCII characters makes the app die at startup with
;    "Failed to resolve Python.Runtime.Loader.Initialize". Measured: the same
;    DLLs load from C:\atr_ascii and fail from a Chinese path. The
;    [Code] section below refuses a non-ASCII destination with a readable
;    message instead of letting the user install a broken app.
;
;  * PrivilegesRequired=lowest would put the app in a user-writable folder, but
;    the conventional Program Files location needs admin. We ask for admin, and
;    the [Code] section still validates the path.
; ---------------------------------------------------------------------------

#define AppName        "Agent Token Radar"
#define AppVersion     "0.0.1"
#define AppPublisher   "agent-token-radar"
#define AppURL         "https://github.com/Zhucy123/agent-token-radar"
#define AppExeName     "AgentTokenRadar.exe"
#define AppId          "{{8F3A1C42-7B5D-4E96-9A21-3C6D8E4F0B17}"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
VersionInfoVersion=0.0.1.0
VersionInfoDescription=Agent Token Radar Setup
VersionInfoProductName=Agent Token Radar
VersionInfoProductVersion={#AppVersion}
VersionInfoCompany={#AppPublisher}
AppCopyright=MIT licensed. Third-party brand icons belong to their vendors.
DefaultDirName={autopf}\AgentTokenRadar
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; The bundled exe is 64-bit only.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Program Files needs elevation.
PrivilegesRequired=admin
OutputDir=dist-installer
OutputBaseFilename=AgentTokenRadar-Setup
SetupIconFile=build\app.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Windows 10 1809+ / Server 2019+ (WebView2 baseline).
MinVersion=10.0.17763

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
    GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The whole PyInstaller onedir tree: AgentTokenRadar.exe + _internal\.
Source: "dist\AgentTokenRadar\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; \
    Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; \
    Description: "{cm:LaunchProgram,{#AppName}}"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
; The app keeps its database in %LOCALAPPDATA%\agent-token-radar and
; deliberately outlives an uninstall: a user reinstalling should not lose
; months of usage history. Only the WebView2 runtime cache under the install
; directory is ours to remove.
Type: filesandordirs; Name: "{app}\_internal\atr-clr"

[Code]
{ Refuse a destination the .NET CLR cannot load from.

  This is not a theoretical guard: the whole reason this project ships a
  dedicated installer is that a build placed under a Chinese directory died at
  startup. Checking before the copy happens costs the user one dialog box
  instead of a silently broken install. }
function IsAsciiPath(const S: String): Boolean;
var
  I: Integer;
begin
  Result := True;
  for I := 1 to Length(S) do
    if Ord(S[I]) > 126 then
    begin
      Result := False;
      Exit;
    end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    if not IsAsciiPath(WizardDirValue) then
    begin
      MsgBox('安装目录只能包含英文字母、数字和常见符号。' + #13#10 +
             '路径里的中文会让程序的 .NET 运行时无法加载，装好后打不开。' + #13#10#13#10 +
             '请改到例如 C:\Program Files\AgentTokenRadar 这样只有英文的目录。' + #13#10#13#10 +
             'The install folder must be ASCII-only: the bundled .NET runtime ' +
             'cannot load native DLLs from a path containing non-ASCII ' +
             'characters, and the app would fail to start.',
             mbError, MB_OK);
      Result := False;
    end;
  end;
end;

{ A running copy would block the file copy and leave a half-updated install. }
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  Result := True;
  if Exec('taskkill.exe', '/F /IM {#AppExeName}', '', SW_HIDE,
          ewWaitUntilTerminated, ResultCode) then
    ; { A missing process makes taskkill return non-zero; that is the normal
        first-install case, so the result is intentionally ignored. }
end;
