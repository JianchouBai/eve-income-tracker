#ifndef MyAppVersion
  #error MyAppVersion must be supplied by scripts/build_windows.ps1
#endif
#ifndef MySourceDir
  #error MySourceDir must be supplied by scripts/build_windows.ps1
#endif
#ifndef MyOutputDir
  #error MyOutputDir must be supplied by scripts/build_windows.ps1
#endif
#ifndef MyWebView2Bootstrapper
  #error MyWebView2Bootstrapper must be supplied by scripts/build_windows.ps1
#endif
#ifndef MyIcon
  #error MyIcon must be supplied by scripts/build_windows.ps1
#endif

#define MyAppName "EVE Income Tracker"
#define MyAppExeName "EVE Income Tracker.exe"
#define WebView2BootstrapperName "MicrosoftEdgeWebview2Setup.exe"

[Setup]
AppId={{59D95F12-7D3B-4C8A-A57D-98F836769441}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
VersionInfoVersion={#MyAppVersion}
VersionInfoDescription={#MyAppName} 安装程序
VersionInfoProductName={#MyAppName}
DefaultDirName={localappdata}\Programs\EVE Income Tracker
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UsePreviousAppDir=yes
CreateUninstallRegKey=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir={#MyOutputDir}
OutputBaseFilename=EVE-Income-Tracker-{#MyAppVersion}-x64-Setup
SetupIconFile={#MyIcon}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
CloseApplicationsFilter={#MyAppExeName}
RestartApplications=no
AppMutex=Local\EVEIncomeTracker.SingleInstance
SetupLogging=yes
DisableProgramGroupPage=yes
DisableDirPage=auto
ChangesAssociations=no
ChangesEnvironment=no

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Files]
Source: "{#MyWebView2Bootstrapper}"; DestName: "{#WebView2BootstrapperName}"; Flags: dontcopy noencryption
Source: "{#MySourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\EVE Income Tracker"; Filename: "{app}\EVE Income Tracker.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\EVE Income Tracker"; Filename: "{app}\EVE Income Tracker.exe"; WorkingDir: "{app}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
const
  WebView2ClientKey =
    'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

var
  DeleteUserData: Boolean;
  UserDataRoot: String;
  ExpectedUserDataRoot: String;

function HasPositiveWebView2Version(const RootKey: HKEY): Boolean;
var
  VersionText: String;
  PackedVersion: Int64;
begin
  Result :=
    RegQueryStringValue(RootKey, WebView2ClientKey, 'pv', VersionText) and
    StrToVersion(VersionText, PackedVersion) and
    (ComparePackedVersion(PackedVersion, 0) > 0);
end;

function IsWebView2Installed: Boolean;
begin
  { HKLM32 resolves the official WOW6432Node location on 64-bit Windows. }
  Result :=
    HasPositiveWebView2Version(HKLM32) or
    HasPositiveWebView2Version(HKCU);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  Attempt: Integer;
begin
  Result := '';
  if IsWebView2Installed then
    Exit;

  ExtractTemporaryFile('MicrosoftEdgeWebview2Setup.exe');
  ResultCode := -1;
  if (not Exec(
    ExpandConstant('{tmp}\{#WebView2BootstrapperName}'),
    '/silent /install',
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  )) or (ResultCode <> 0) then
  begin
    Result :=
      'Microsoft Edge WebView2 Runtime 安装失败。' + #13#10 +
      '请检查网络连接后重新运行安装程序。';
    Exit;
  end;

  for Attempt := 1 to 20 do
  begin
    if IsWebView2Installed then
      Exit;
    Sleep(500);
  end;

  Result :=
    'Microsoft Edge WebView2 Runtime 安装失败：安装后仍未检测到有效运行时。' + #13#10 + '应用程序文件尚未安装，请重新运行安装程序。';
end;

function InitializeUninstall: Boolean;
var
  FirstChoice: Integer;
  ConfirmChoice: Integer;
begin
  Result := True;
  DeleteUserData := False;
  UserDataRoot := ExpandConstant('{localappdata}\EVE Income Tracker');
  ExpectedUserDataRoot :=
    AddBackslash(ExpandConstant('{localappdata}')) + 'EVE Income Tracker';

  if UninstallSilent then
    Exit;

  FirstChoice := MsgBox(
    '是否同时删除这台电脑上的个人数据？' + #13#10 + #13#10 +
    '选择“否”将默认保留钱包数据库、授权密钥、日志和备份，' +
    '以后重新安装仍可继续使用。',
    mbConfirmation,
    MB_YESNO or MB_DEFBUTTON2
  );
  if FirstChoice <> IDYES then
    Exit;

  if not PathSame(UserDataRoot, ExpectedUserDataRoot) then
  begin
    MsgBox(
      '数据目录校验失败。为避免误删，卸载程序将保留全部个人数据。',
      mbError,
      MB_OK
    );
    Exit;
  end;

  ConfirmChoice := MsgBox(
    '确认永久删除以下完整目录及其中全部内容？' + #13#10 + #13#10 +
    UserDataRoot + #13#10 + #13#10 +
    '此操作不可撤销。选择“否”将保留数据。',
    mbError,
    MB_YESNO or MB_DEFBUTTON2
  );
  DeleteUserData := ConfirmChoice = IDYES;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and DeleteUserData then
  begin
    if PathSame(UserDataRoot, ExpectedUserDataRoot) then
    begin
      if not DelTree(UserDataRoot, True, True, True) then
        MsgBox(
          '个人数据目录未能完全删除，请稍后手动处理：' + #13#10 +
          UserDataRoot,
          mbError,
          MB_OK
        );
    end
    else
      Log('Refused to delete an unexpected user data path.');
  end;
end;
