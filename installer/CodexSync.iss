; Codex Sync Windows 安装程序（Inno Setup 6）。
; 由 scripts/build_windows_release.ps1 调用：先用 PyInstaller 打出 dist\CodexSync\（目录模式），
; 再把整个目录打成 CodexSyncSetup-vX.Y.Z-windows-x64.exe。
;
; 命令行参数（ISCC /D...）：
;   AppVersion  版本号，例如 0.4.0
;   SourceDir   PyInstaller 输出目录，默认 ..\dist\CodexSync
;   OutputDir   安装包输出目录，默认 ..\dist
;   TestMode    只用于自测：换一套名称 / 注册表键 / 互斥锁，不会影响本机正式安装

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\CodexSync"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif

#ifdef TestMode
  #define AppName "Codex Sync Test"
  #define AppIdGuid "FE2C6F9D-2FA6-4B8B-B103-887AFFBA24D3"
  #define Suffix "Test"
#else
  #define AppName "Codex Sync"
  ; 必须和 codex_sync/app_install.py 的 APP_ID 一致（测试会校验）
  #define AppIdGuid "680F4FE3-3511-457B-981C-BE2D98CEDDB4"
  #define Suffix ""
#endif

#define AppExeName "CodexSync.exe"
#define AppPublisher "Codex Sync contributors"
#define AppURL "https://github.com/tang12306/codex-sync"
; 桌面端的单实例互斥锁（codex_sync/web_desktop.py 的 SingleInstance 默认名）
#define AppMutexName "Local\CodexSyncDesktop" + Suffix
; 开机自启写在 HKCU Run 下，值名和 codex_sync/app_install.py 的 STARTUP_VALUE 一致
#define RunKey "Software\Microsoft\Windows\CurrentVersion\Run"
#define RunValueName "CodexSync" + Suffix
; 0.4.0 及更早版本是“exe 自己复制自己”的安装方式，留下这两个注册表键，需要接管并清理
#define LegacySoftwareKey "Software\CodexSync" + Suffix
#define LegacyUninstallKey "Software\Microsoft\Windows\CurrentVersion\Uninstall\CodexSync" + Suffix

[Setup]
AppId={{{#AppIdGuid}}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
AppCopyright=Copyright (c) 2026 {#AppPublisher}
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoDescription={#AppName} Setup
VersionInfoCompany={#AppPublisher}
; 默认装在当前用户目录（%LOCALAPPDATA%\Programs），不需要管理员权限；旧版安装过的话沿用旧目录
PrivilegesRequired=lowest
DefaultDirName={code:GetDefaultDirName}
DisableProgramGroupPage=yes
DisableDirPage=auto
DisableWelcomePage=no
LicenseFile=..\LICENSE
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=CodexSyncSetup-v{#AppVersion}-windows-x64
SetupIconFile=..\assets\CodexSync.ico
WizardStyle=modern dynamic
; 图片由 scripts/make_installer_images.py 生成；按 DPI 提供多份，安装程序自动挑选
WizardSmallImageFile=wizard-small-100.png,wizard-small-150.png,wizard-small-200.png
WizardImageFile=wizard-large-100.png,wizard-large-150.png,wizard-large-200.png
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2/max
SolidCompression=yes
SetupMutex=CodexSyncSetup{#Suffix}
; 覆盖安装时由下面的 [Code] 先关闭正在运行的 Codex Sync；Restart Manager 只兜底处理其它占用
CloseApplications=yes
RestartApplications=no
ShowLanguageDialog=no
LanguageDetectionMethod=uilanguage

[Languages]
Name: "chs"; MessagesFile: "ChineseSimplified.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
chs.TaskStartup=开机时自动启动 %1
en.TaskStartup=Start %1 when I sign in to Windows
chs.OtherTasks=其他：
en.OtherTasks=Other:
chs.AppRunningSetup=%1 正在运行。%n%n安装前需要先关闭它。点击“确定”自动关闭并继续，点击“取消”返回。
en.AppRunningSetup=%1 is running.%n%nIt needs to be closed before installing. Click OK to close it and continue, or Cancel to go back.
chs.AppRunningUninstall=%1 正在运行。%n%n卸载前需要先关闭它。点击“确定”自动关闭并继续卸载，点击“取消”放弃卸载。
en.AppRunningUninstall=%1 is running.%n%nIt needs to be closed before uninstalling. Click OK to close it and continue, or Cancel to stop.
chs.AppStillRunning=%1 仍在运行。请在系统托盘图标上右键选择“退出”，然后重新运行安装程序。
en.AppStillRunning=%1 is still running. Right-click its tray icon, choose Exit, and run Setup again.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "startup"; Description: "{cm:TaskStartup,{#AppName}}"; GroupDescription: "{cm:OtherTasks}"; Flags: unchecked

[InstallDelete]
; 覆盖安装前清掉旧的运行库目录，避免残留上个版本的 DLL
Type: filesandordirs; Name: "{app}\_internal"
; 旧版安装方式留下的文件和开始菜单里的“卸载”快捷方式
Type: files; Name: "{app}\{#AppExeName}.new"
Type: files; Name: "{userprograms}\卸载 {#AppName}.lnk"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "{#RunKey}"; ValueType: string; ValueName: "{#RunValueName}"; ValueData: """{app}\{#AppExeName}"""; Tasks: startup

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; 撤掉指向本程序的 Codex hooks 和 Windows 定时任务（源码模式装的不动）
Filename: "{app}\{#AppExeName}"; Parameters: "uninstall-cleanup"; Flags: runhidden waituntilterminated; RunOnceId: "UninstallCleanup"

[UninstallDelete]
; 旧版安装方式创建、不在安装记录里的桌面快捷方式
Type: files; Name: "{userdesktop}\{#AppName}.lnk"
Type: dirifempty; Name: "{app}"

[Code]
var
  LegacyDir: String;

// 0.4.0 及更早版本记录的安装目录（目录还在才算）
function ReadLegacyDir(): String;
begin
  Result := '';
  if RegQueryStringValue(HKCU, '{#LegacySoftwareKey}', 'InstallDir', Result) then
  begin
    Result := RemoveBackslashUnlessRoot(Trim(Result));
    if (Result <> '') and not DirExists(Result) then
      Result := '';
  end
  else
    Result := '';
end;

function InitializeSetup(): Boolean;
begin
  LegacyDir := ReadLegacyDir();
  Result := True;
end;

// 默认安装目录：旧版装在哪就原地升级（hooks、定时任务里记的程序路径保持有效），否则用标准的用户程序目录
function GetDefaultDirName(Param: String): String;
begin
  if LegacyDir <> '' then
    Result := LegacyDir
  else
    Result := ExpandConstant('{autopf}\CodexSync{#Suffix}');
end;

// 桌面端在运行时持有单实例互斥锁；征得同意后结束它（托盘常驻时用户不一定意识到它还开着）
function CloseRunningApp(const Prompt: String): Boolean;
var
  ResultCode, I: Integer;
begin
  Result := True;
  if not CheckForMutexes('{#AppMutexName}') then
    Exit;
  if SuppressibleMsgBox(Prompt, mbConfirmation, MB_OKCANCEL, IDOK) <> IDOK then
  begin
    Result := False;
    Exit;
  end;
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM {#AppExeName}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  for I := 1 to 40 do
  begin
    if not CheckForMutexes('{#AppMutexName}') then
      Exit;
    Sleep(250);
  end;
  Result := not CheckForMutexes('{#AppMutexName}');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if not CloseRunningApp(FmtMessage(CustomMessage('AppRunningSetup'), ['{#AppName}'])) then
    Result := FmtMessage(CustomMessage('AppStillRunning'), ['{#AppName}']);
end;

// 接管旧版安装：删掉旧的卸载项和安装记录；如果换了目录，再清掉旧程序并把开机自启指向新位置
procedure MigrateLegacyInstall();
var
  AppDir, LegacyExe, RunValue: String;
begin
  RegDeleteKeyIncludingSubkeys(HKCU, '{#LegacyUninstallKey}');
  RegDeleteKeyIncludingSubkeys(HKCU, '{#LegacySoftwareKey}');
  AppDir := RemoveBackslashUnlessRoot(ExpandConstant('{app}'));
  if (LegacyDir = '') or (CompareText(LegacyDir, AppDir) = 0) then
    Exit;
  LegacyExe := AddBackslash(LegacyDir) + '{#AppExeName}';
  if FileExists(LegacyExe) then
    DeleteFile(LegacyExe);
  RemoveDir(LegacyDir);
  if RegQueryStringValue(HKCU, '{#RunKey}', '{#RunValueName}', RunValue) and
     (Pos(Lowercase(LegacyExe), Lowercase(RunValue)) > 0) then
    RegWriteStringValue(HKCU, '{#RunKey}', '{#RunValueName}', '"' + AddBackslash(AppDir) + '{#AppExeName}"');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    MigrateLegacyInstall();
end;

function InitializeUninstall(): Boolean;
begin
  Result := CloseRunningApp(FmtMessage(CustomMessage('AppRunningUninstall'), ['{#AppName}']));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  // 开机自启也可能是在应用里打开的（不在安装记录里），卸载时一并删除
  if CurUninstallStep = usPostUninstall then
    RegDeleteValue(HKCU, '{#RunKey}', '{#RunValueName}');
end;
