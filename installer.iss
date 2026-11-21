[Setup]
AppName=证件照批量换底色工具_Czq
AppVersion=1.0
DefaultDirName={autopf}\证件照批量换底色工具_Czq
DefaultGroupName=证件照批量换底色工具_Czq
OutputBaseFilename=证件照批量换底色工具_安装程序_Czq
SetupIconFile=app.ico
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
ShowLanguageDialog=no

[Files]
Source: "dist\证件照批量换底色工具_Czq\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autodesktop}\证件照批量换底色工具_Czq"; Filename: "{app}\证件照批量换底色工具_Czq.exe"; IconFilename: "{app}\证件照批量换底色工具_Czq.exe"

[Code]
var
  PathsPage: TWizardPage;
  AppDirEdit: TEdit;
  ModelDirEdit: TEdit;

procedure BrowseAppDirClick(Sender: TObject);
var
  Dir: string;
begin
  Dir := AppDirEdit.Text;
  if BrowseForFolder('请选择软件安装目录', Dir, False) then
    AppDirEdit.Text := Dir;
end;

procedure BrowseModelDirClick(Sender: TObject);
var
  Dir: string;
begin
  Dir := ModelDirEdit.Text;
  if BrowseForFolder('请选择 AI 模型存放目录（建议选择非 C 盘）', Dir, False) then
    ModelDirEdit.Text := Dir;
end;

procedure InitializeWizard;
var
  AppLabel, ModelLabel: TNewStaticText;
  AppBrowseBtn, ModelBrowseBtn: TNewButton;
begin
  PathsPage := CreateCustomPage(
    wpWelcome,
    '选择安装位置与 AI 模型目录',
    '请分别设置软件安装文件夹和 AI 模型存放文件夹，然后点击“下一步”。'
  );

  AppLabel := TNewStaticText.Create(PathsPage);
  AppLabel.Parent := PathsPage.Surface;
  AppLabel.Caption := '1. 软件安装位置：';
  AppLabel.Left := 0;
  AppLabel.Top := 8;
  AppLabel.Width := PathsPage.SurfaceWidth;

  App
