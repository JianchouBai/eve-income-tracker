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
RedirectionGuard=yes
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
  DataLocationKey = 'Software\JianchouBai\EVE Income Tracker';
  DataMarkerName = '.eve-income-tracker-data.json';
  DataAppId = '59D95F12-7D3B-4C8A-A57D-98F836769441';
  RegistryStringType = 1;
  RegistryDWordType = 4;
  ReparsePointAttribute = $400;

var
  DeleteUserData: Boolean;
  UserDataRoot: String;
  UserDataId: String;

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

function NativeGetFileAttributes(const FileName: String): Integer;
  external 'GetFileAttributesW@kernel32.dll stdcall';
function NativeRegOpenKey(const Key: HKEY; const SubKey: String;
  Options, Access: Cardinal; var OpenedKey: HKEY): Integer;
  external 'RegOpenKeyExW@advapi32.dll stdcall';
function NativeRegQueryValueType(const Key: HKEY; const ValueName: String;
  Reserved: Integer; var ValueType: Cardinal; Data: Integer;
  var DataBytes: Cardinal): Integer;
  external 'RegQueryValueExW@advapi32.dll stdcall';
function NativeRegCloseKey(const Key: HKEY): Integer;
  external 'RegCloseKey@advapi32.dll stdcall';

function HasRegistryType(const ValueName: String; ExpectedType: Cardinal): Boolean;
var
  Key: HKEY;
  ActualType, DataBytes: Cardinal;
begin
  Result := False;
  if NativeRegOpenKey(HKCU, DataLocationKey, 0, 1, Key) <> 0 then
    Exit;
  try
    DataBytes := 0;
    Result :=
      (NativeRegQueryValueType(Key, ValueName, 0, ActualType, 0, DataBytes) = 0) and
      (ActualType = ExpectedType);
  finally
    NativeRegCloseKey(Key);
  end;
end;

function IsUuidV4(const Value: String): Boolean;
var
  I: Integer;
begin
  Result := False;
  if Length(Value) = 32 then
  begin
    if (Value[13] <> '4') or (Pos(Value[17], '89ab') = 0) then
      Exit;
    for I := 1 to 32 do
      if Pos(Value[I], '0123456789abcdef') = 0 then
        Exit;
    Result := True;
    Exit;
  end;
  if Length(Value) <> 36 then
    Exit;
  if (Value[15] <> '4') or
    (Pos(Value[20], '89ab') = 0) then
    Exit;
  for I := 1 to 36 do
  begin
    if (I = 9) or (I = 14) or (I = 19) or (I = 24) then
    begin
      if Value[I] <> '-' then
        Exit;
    end
    else if Pos(Value[I], '0123456789abcdef') = 0 then
      Exit;
  end;
  Result := True;
end;

function IsReparsePoint(const CurrentPath: String): Boolean;
var
  Attributes: Integer;
begin
  Attributes := NativeGetFileAttributes(CurrentPath);
  Result := (Attributes = -1) or ((Attributes and ReparsePointAttribute) <> 0);
end;

function PathsOverlap(const LeftPath, RightPath: String): Boolean;
var
  LeftLower, RightLower: String;
begin
  LeftLower := LowerCase(RemoveBackslash(LeftPath));
  RightLower := LowerCase(RemoveBackslash(RightPath));
  Result :=
    (LeftLower = RightLower) or
    (Pos(LeftLower + '\', RightLower + '\') = 1) or
    (Pos(RightLower + '\', LeftLower + '\') = 1);
end;

function IsSafeDataRoot(const Root: String): Boolean;
var
  CurrentPath, ParentPath, UserProfile: String;
  I: Integer;
begin
  Result := False;
  if Length(Root) < 5 then
    Exit;
  if (Root[2] <> ':') or (Root[3] <> '\') or
    (Pos(Root[1], 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz') = 0) or
    (Pos('/', Root) <> 0) or (Pos('~', Root) <> 0) or
    (Pos(':', Copy(Root, 3, Length(Root))) <> 0) or
    (Root <> ExpandFileName(Root)) or
    (ExtractFileName(Root) <> 'EVE Income Tracker') then
    Exit;
  for I := 1 to Length(Root) do
    if (Ord(Root[I]) < 32) or (Root[I] = '"') then
      Exit;
  if (Pos('.\', Root) <> 0) or (Pos(' \', Root) <> 0) then
    Exit;
  UserProfile := GetEnv('USERPROFILE');
  if UserProfile = '' then
    Exit;
  if PathsOverlap(Root, ExpandConstant('{win}')) or
    PathsOverlap(Root, ExpandConstant('{commonpf32}')) or
    PathsOverlap(Root, ExpandConstant('{commonpf64}')) or
    PathsOverlap(Root, ExpandConstant('{app}')) or
    (LowerCase(RemoveBackslash(Root)) = LowerCase(RemoveBackslash(UserProfile))) or
    (Pos(LowerCase(Root) + '\', LowerCase(UserProfile) + '\') = 1) then
    Exit;
  CurrentPath := Root;
  while True do
  begin
    if IsReparsePoint(CurrentPath) then
      Exit;
    ParentPath := ExtractFileDir(CurrentPath);
    if (ParentPath = '') or PathSame(ParentPath, CurrentPath) then
      Break;
    CurrentPath := ParentPath;
  end;
  Result := DirExists(Root);
end;

function JsonEscape(const Value: String): String;
var
  I: Integer;
begin
  Result := '';
  for I := 1 to Length(Value) do
  begin
    if Value[I] = '\' then
      Result := Result + '\\'
    else if Value[I] = '"' then
      Result := Result + '\"'
    else
      Result := Result + Value[I];
  end;
end;

function IsUtcTimestamp(const Value: String): Boolean;
var
  I, Year, Month, Day, Hour, Minute, Second, DaysInMonth: Integer;
begin
  Result := False;
  if Length(Value) < 20 then
    Exit;
  if (Length(Value) > 27) or
    (Value[5] <> '-') or (Value[8] <> '-') or (Value[11] <> 'T') or
    (Value[14] <> ':') or (Value[17] <> ':') or
    (Value[Length(Value)] <> 'Z') then
    Exit;
  if (Length(Value) > 20) and
    ((Value[20] <> '.') or (Length(Value) < 22)) then
    Exit;
  for I := 1 to Length(Value) - 1 do
    if (I <> 5) and (I <> 8) and (I <> 11) and
      (I <> 14) and (I <> 17) and (I <> 20) and
      (Pos(Value[I], '0123456789') = 0) then
      Exit;
  Year := StrToIntDef(Copy(Value, 1, 4), 0);
  Month := StrToIntDef(Copy(Value, 6, 2), 0);
  Day := StrToIntDef(Copy(Value, 9, 2), 0);
  Hour := StrToIntDef(Copy(Value, 12, 2), -1);
  Minute := StrToIntDef(Copy(Value, 15, 2), -1);
  Second := StrToIntDef(Copy(Value, 18, 2), -1);
  if (Year = 0) or (Month < 1) or (Month > 12) or
    (Hour > 23) or (Minute > 59) or (Second > 59) then
    Exit;
  DaysInMonth := 31;
  if (Month = 4) or (Month = 6) or (Month = 9) or (Month = 11) then
    DaysInMonth := 30
  else if Month = 2 then
  begin
    DaysInMonth := 28;
    if ((Year mod 4 = 0) and (Year mod 100 <> 0)) or (Year mod 400 = 0) then
      DaysInMonth := 29;
  end;
  Result := (Day >= 1) and (Day <= DaysInMonth);
end;

function IsCanonicalMarker(const Root, RootId: String): Boolean;
var
  MarkerPath, MarkerText, Expected, CreatedAt, Prefix: String;
  Contents: AnsiString;
  I: Integer;
begin
  Result := False;
  MarkerPath := AddBackslash(Root) + DataMarkerName;
  if IsReparsePoint(MarkerPath) or
    (not LoadStringFromFile(MarkerPath, Contents)) then
    Exit;
  MarkerText := Utf8Decode(Contents);
  Prefix := '{"app_id":"' + DataAppId + '","created_at":"';
  if Copy(MarkerText, 1, Length(Prefix)) <> Prefix then
    Exit;
  I := Length(Prefix) + 1;
  while I <= Length(MarkerText) do
  begin
    if MarkerText[I] = '"' then
      Break;
    I := I + 1;
  end;
  CreatedAt := Copy(MarkerText, Length(Prefix) + 1,
    I - Length(Prefix) - 1);
  if not IsUtcTimestamp(CreatedAt) then
    Exit;
  Expected := Prefix + CreatedAt + '","root":"' + JsonEscape(Root) +
    '","root_id":"' + RootId + '","schema":1,"state":"ready"}' + #10;
  Result := Contents = Utf8Encode(Expected);
end;

function ReadDeleteTarget(var Root, RootId: String): Boolean;
var
  DataRootSchema: Cardinal;
begin
  Result := False;
  if RegValueExists(HKCU, DataLocationKey, 'PreviousDataRoot') or
    RegValueExists(HKCU, DataLocationKey, 'PreviousDataRootId') or
    RegValueExists(HKCU, DataLocationKey, 'PreviousDataRootSchema') or
    RegValueExists(HKCU, DataLocationKey, 'PreviousDataRootAbsent') or
    RegValueExists(HKCU, DataLocationKey, 'CurrentSwitchCommitted') or
    RegValueExists(HKCU, DataLocationKey, 'PendingMigrationTarget') or
    RegValueExists(HKCU, DataLocationKey, 'PendingMigrationCreateRecovery') or
    RegValueExists(HKCU, DataLocationKey, 'MigrationTransaction') then
    Exit;
  if not HasRegistryType('DataRoot', RegistryStringType) or
    not HasRegistryType('DataRootId', RegistryStringType) or
    not HasRegistryType('DataRootSchema', RegistryDWordType) then
    Exit;
  Result :=
    RegQueryStringValue(HKCU, DataLocationKey, 'DataRoot', Root) and
    RegQueryStringValue(HKCU, DataLocationKey, 'DataRootId', RootId) and
    RegQueryDWordValue(HKCU, DataLocationKey, 'DataRootSchema',
      DataRootSchema) and
    (DataRootSchema = 1) and IsUuidV4(RootId);
end;

function HasPendingCleanupState: Boolean;
begin
  { The helper alone validates complete, partial, or malformed receipts. }
  Result :=
    RegValueExists(HKCU, DataLocationKey, 'PendingCleanupRoot') or
    RegValueExists(HKCU, DataLocationKey, 'PendingCleanupRootId') or
    RegValueExists(HKCU, DataLocationKey, 'PendingCleanupQuarantine');
end;

function ValidateDeleteTarget(const ExpectedRoot, ExpectedId: String): Boolean;
var
  CurrentRoot, CurrentId: String;
begin
  Result :=
    ReadDeleteTarget(CurrentRoot, CurrentId) and
    (CurrentRoot = ExpectedRoot) and
    (CurrentId = ExpectedId) and
    (HasPendingCleanupState or
      (IsSafeDataRoot(CurrentRoot) and
        IsCanonicalMarker(CurrentRoot, CurrentId)));
end;

function InitializeUninstall: Boolean;
var
  FirstChoice, ConfirmChoice: Integer;
begin
  Result := True;
  DeleteUserData := False;
  UserDataRoot := '';
  UserDataId := '';
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
  if not ReadDeleteTarget(UserDataRoot, UserDataId) or
    not ValidateDeleteTarget(UserDataRoot, UserDataId) then
  begin
    MsgBox(
      '当前数据目录或安全标记无法验证。卸载程序将保留全部个人数据和位置记录。',
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
var
  ResultCode: Integer;
begin
  if (CurUninstallStep <> usUninstall) or (not DeleteUserData) then
    Exit;
  if not ValidateDeleteTarget(UserDataRoot, UserDataId) then
  begin
    Log('Refused to delete an unverified data root.');
    MsgBox('数据目录校验失败，个人数据和位置记录已保留：' + #13#10 +
      UserDataRoot, mbError, MB_OK);
    Exit;
  end;
  ResultCode := -1;
  if Exec(
    ExpandConstant('{app}\{#MyAppExeName}'),
    '--uninstall-delete-data ' + AddQuotes(UserDataRoot) + ' ' + UserDataId,
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  ) and (ResultCode = 0) and (not DirExists(UserDataRoot)) and
    (not FileExists(UserDataRoot)) then
  begin
    if RegDeleteValue(HKCU, DataLocationKey, 'DataRoot') and
      RegDeleteValue(HKCU, DataLocationKey, 'DataRootId') and
      RegDeleteValue(HKCU, DataLocationKey, 'DataRootSchema') then
    begin
      RegDeleteValue(HKCU, DataLocationKey, 'MigrationTransaction');
      RegDeleteValue(HKCU, DataLocationKey, 'PendingCleanupRoot');
      RegDeleteValue(HKCU, DataLocationKey, 'PendingCleanupRootId');
      RegDeleteValue(HKCU, DataLocationKey, 'PendingCleanupQuarantine');
      RegDeleteKeyIfEmpty(HKCU, DataLocationKey)
    end
    else
      Log('Data removed, but location registry values could not be cleared.');
    Exit;
  end;
  Log('Data directory was not fully removed: ' + UserDataRoot);
  MsgBox('个人数据目录未能完全删除，位置记录已保留，请稍后手动处理：' + #13#10 +
    UserDataRoot, mbError, MB_OK);
end;
