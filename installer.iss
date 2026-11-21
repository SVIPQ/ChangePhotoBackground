; 证件照批量换底色工具_Czq Inno Setup 安装脚本（SHA-256 增量覆盖 + 彻底卸载模型版）
#define MyAppName "证件照批量换底色工具_Czq"
#define MyAppVersion "3.1.0.0"
#define MyAppPublisher "Czq"
#define MyAppExeName "证件照批量换底色工具_Czq.exe"
#define MyDesktopShortcutName "证件照批量换底色工具"

[Setup]
AppId={{8D6E7B42-4F5B-4C7A-9B2E-1F4A8C3D9E01}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
SetupIconFile=app.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
; 内置三个 ONNX 离线模型体积较大，使用 lzma2/normal 兼顾压缩率与安装速度
Compression=lzma2/normal
SolidCompression=yes
WizardStyle=modern
OutputDir=output
OutputBaseFilename=证件照批量换底色工具_安装程序_Czq
; 允许覆盖正在运行或只读属性文件时更顺畅
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
SetupAppTitle=安装 {#MyAppName}
SetupWindowTitle=安装 - {#MyAppName}
NextButtonLabel=下一步(&N) >
BackButtonLabel=< 上一步(&B)
InstallButtonLabel=安装(&I)
CancelButtonLabel=取消
CloseButtonLabel=关闭(&C)
FinishButtonLabel=完成(&F)
BrowseLabel=浏览(&B)...
ClickNext=继续请单击“下一步”，或单击“取消”退出安装程序。
SelectDirLabel=选择目标位置
SelectDirDesc=您希望将 {#MyAppName}（含内置三套离线 AI 模型）安装到何处？
SelectDirBrowseLabel=选择 {#MyAppName} 的安装文件夹，然后单击“下一步”。
DiskSpaceMBLabel=请确保目标磁盘有充足的可用空间（建议预留 1.5 GB 以上）。
ReadyLabel1=安装程序现在准备开始将 {#MyAppName} 及三套离线 AI 模型安装到您的电脑中。
ReadyLabel2a=单击“安装”继续安装，或单击“上一步”查看或更改设置。
InstallingLabel=正在提取并安装 {#MyAppName} 与三套离线 AI 模型，请稍候...
FinishedHeadingLabel=正在完成 {#MyAppName} 安装向导
FinishedLabelNoIcons=安装程序已完成在您的电脑上安装 {#MyAppName}。
FinishedLabel=安装程序已完成在您的电脑上安装 {#MyAppName}。您可以选择运行已安装的应用程序。

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式(&D)"; GroupDescription: "附加图标:"

[InstallDelete]
Type: files; Name: "{autodesktop}\{#MyAppName}.lnk"
Type: files; Name: "{autodesktop}\{#MyDesktopShortcutName}.lnk"

[UninstallDelete]
; 卸载时彻底清理三个模型及 _internal\.u2net 目录下所有文件与子目录
Type: files; Name: "{app}\_internal\.u2net\*"
Type: files; Name: "{app}\_internal\.u2net\models\*"
Type: files; Name: "{app}\_internal\.u2net\models\birefnet-portrait\*"
Type: files; Name: "{app}\_internal\.u2net\models\isnet-general-use\*"
Type: files; Name: "{app}\_internal\.u2net\models\u2net\*"
Type: filesandordirs; Name: "{app}\_internal\.u2net\models\birefnet-portrait"
Type: filesandordirs; Name: "{app}\_internal\.u2net\models\isnet-general-use"
Type: filesandordirs; Name: "{app}\_internal\.u2net\models\u2net"
Type: filesandordirs; Name: "{app}\_internal\.u2net\models"
Type: filesandordirs; Name: "{app}\_internal\.u2net"
Type: filesandordirs; Name: "{app}\_internal"

[Dirs]
; 预先确保三模型固定存放目录存在
Name: "{app}\_internal\.u2net\models\birefnet-portrait"
Name: "{app}\_internal\.u2net\models\isnet-general-use"
Name: "{app}\_internal\.u2net\models\u2net"

[Files]
; overwritereadonly 确保即便目标文件有只读属性也能覆盖；核心主程序与模型走 SHA-256 校验逻辑
Source: "dist\{#MyAppName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs overwritereadonly; Check: ShouldInstallFile(ExpandConstant('{app}\_internal'), '')
Source: "dist\{#MyAppName}\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion overwritereadonly; Check: ShouldInstallFile('', ExpandConstant('{app}\{#MyAppExeName}'))
Source: "dist\{#MyAppName}\_internal\.u2net\models\*"; DestDir: "{app}\_internal\.u2net\models"; Flags: ignoreversion recursesubdirs createallsubdirs overwritereadonly skipifsourcedoesntexist; Check: ShouldInstallFile('', ExpandConstant('{app}\_internal\.u2net\models'))
Source: "app.ico"; DestDir: "{app}"; Flags: ignoreversion overwritereadonly

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\app.ico"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyDesktopShortcutName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\app.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "运行 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
function GetFileSha256Hex(const FilePath: string): string;
var
  PsCmd, OutFile, ResultText: string;
  ResultCode: Integer;
begin
  Result := '';
  if not FileExists(FilePath) then
    exit;

  OutFile := ExpandConstant('{tmp}\sha256_check.txt');
  if FileExists(OutFile) then
    DeleteFile(OutFile);

  PsCmd := Format(
    '-NoProfile -ExecutionPolicy Bypass -Command "(Get-FileHash -Algorithm SHA256 -LiteralPath ''%s'').Hash.ToLower()" > "%s"',
    [FilePath, OutFile]
  );

  if Exec('powershell.exe', PsCmd, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    if (ResultCode = 0) and FileExists(OutFile) and LoadStringFromFile(OutFile, ResultText) then
    begin
      Result := Trim(ResultText);
    end;
  end;

  if FileExists(OutFile) then
    DeleteFile(OutFile);
end;

function ShouldInstallFile(const BaseDir: string; const TargetFile: string): Boolean;
var
  DstPath, TmpNewFile, ExistingHash, NewHash: string;
begin
  { 目录级批量规则直接放行，具体单文件由 Inno Setup 逐项调用 }
  if (TargetFile = '') or DirExists(TargetFile) then
  begin
    Result := True;
    exit;
  end;

  DstPath := TargetFile;
  { 目标文件尚不存在，直接安装 }
  if not FileExists(DstPath) then
  begin
    Result := True;
    exit;
  end;

  { 若当前提取阶段无法直接拿到新文件句柄，交由 Inno Setup 默认覆盖 }
  TmpNewFile := ExpandConstant(CurrentFileName);
  if (TmpNewFile = '') or (not FileExists(TmpNewFile)) or (CompareText(TmpNewFile, DstPath) = 0) then
  begin
    Result := True;
    exit;
  end;

  ExistingHash := GetFileSha256Hex(DstPath);
  NewHash := GetFileSha256Hex(TmpNewFile);

  { 哈希计算失败时保守返回 True（确保覆盖，不漏更）；哈希完全一致则跳过 }
  if (ExistingHash <> '') and (NewHash <> '') and (CompareText(ExistingHash, NewHash) = 0) then
  begin
    Result := False;
  end
  else
  begin
    Result := True;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
  begin
    WizardForm.StatusLabel.Caption := '正在校验并更新主程序与三套离线 AI 抠图模型（SHA-256），请稍候...';
    WizardForm.Update;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  U2NetDir: string;
begin
  if CurUninstallStep = usUninstall then
  begin
    U2NetDir := ExpandConstant('{app}\_internal\.u2net');
    if DirExists(U2NetDir) then
    begin
      DelTree(U2NetDir, True, True, True);
    end;
  end;
end;