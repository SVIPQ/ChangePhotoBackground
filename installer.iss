; 证件照批量换底色工具_Czq Inno Setup 安装脚本（内置三模型离线版）
#define MyAppName "证件照批量换底色工具_Czq"
#define MyAppVersion "3.0.0.0"
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

[Dirs]
; 预先确保三模型固定存放目录存在，避免无网络环境下首次启动权限或路径异常
Name: "{app}\_internal\.u2net\models\birefnet-portrait"
Name: "{app}\_internal\.u2net\models\isnet-general-use"
Name: "{app}\_internal\.u2net\models\u2net"

[Files]
; 打包主程序及 _internal 下的全部运行库与已预下载的三个离线模型
Source: "dist\{#MyAppName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; 显式保证三模型目录（birefnet-portrait / isnet-general-use / u2net）完整写入安装目录
Source: "dist\{#MyAppName}\_internal\.u2net\models\*"; DestDir: "{app}\_internal\.u2net\models"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist
Source: "app.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\app.ico"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyDesktopShortcutName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\app.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "运行 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
  begin
    WizardForm.StatusLabel.Caption := '正在提取主程序与三套离线 AI 抠图模型（birefnet / isnet / u2net），请稍候...';
    WizardForm.Update;
  end;
end;