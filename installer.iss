; 证件照批量换底色工具_Czq Inno Setup 安装脚本
#define MyAppName "证件照批量换底色工具_Czq"
#define MyAppVersion "2.1.0.4"
#define MyAppPublisher "Czq"
#define MyAppExeName "证件照批量换底色工具_Czq.exe"

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
Compression=lzma2/fast
SolidCompression=no
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
SelectDirDesc=您希望将 {#MyAppName} 安装到何处？
SelectDirBrowseLabel=选择 {#MyAppName} 的安装文件夹，然后单击“下一步”。
DiskSpaceMBLabel=请确保目标磁盘有充足的可用空间。
ReadyLabel1=安装程序现在准备开始将 {#MyAppName} 安装到您的电脑中。
ReadyLabel2a=单击“安装”继续安装，或单击“上一步”查看或更改设置。
InstallingLabel=正在提取并安装 {#MyAppName} 文件，请稍候...
FinishedHeadingLabel=正在完成 {#MyAppName} 安装向导
FinishedLabelNoIcons=安装程序已完成在您的电脑上安装 {#MyAppName}。
FinishedLabel=安装程序已完成在您的电脑上安装 {#MyAppName}。您可以选择运行已安装的应用程序。

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式(&D)"; GroupDescription: "附加图标:"

[Files]
Source: "dist\{#MyAppName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "app.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\app.ico"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\app.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "运行 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
  begin
    WizardForm.StatusLabel.Caption := '正在提取程序与 AI 模型文件，请稍候...';
    WizardForm.Update;
  end;
end;