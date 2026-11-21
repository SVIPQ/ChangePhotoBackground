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

  AppDirEdit := TEdit.Create(PathsPage);
  AppDirEdit.Parent := PathsPage.Surface;
  AppDirEdit.Left := 0;
  AppDirEdit.Top := 28;
  AppDirEdit.Width := PathsPage.SurfaceWidth - 92;
  AppDirEdit.Text := WizardDirValue(False);

  AppBrowseBtn := TNewButton.Create(PathsPage);
  AppBrowseBtn.Parent := PathsPage.Surface;
  AppBrowseBtn.Caption := '浏览...';
  AppBrowseBtn.Left := PathsPage.SurfaceWidth - 82;
  AppBrowseBtn.Top := 27;
  AppBrowseBtn.Width := 82;
  AppBrowseBtn.Height := 25;
  AppBrowseBtn.OnClick := @BrowseAppDirClick;

  ModelLabel := TNewStaticText.Create(PathsPage);
  ModelLabel.Parent := PathsPage.Surface;
  ModelLabel.Caption := '2. AI 模型存放位置（建议选非 C 盘，节省空间）：';
  ModelLabel.Left := 0;
  ModelLabel.Top := 74;
  ModelLabel.Width := PathsPage.SurfaceWidth;

  ModelDirEdit := TEdit.Create(PathsPage);
  ModelDirEdit.Parent := PathsPage.Surface;
  ModelDirEdit.Left := 0;
  ModelDirEdit.Top := 94;
  ModelDirEdit.Width := PathsPage.SurfaceWidth - 92;
  ModelDirEdit.Text := 'D:\证件照模型\.u2net';

  ModelBrowseBtn := TNewButton.Create(PathsPage);
  ModelBrowseBtn.Parent := PathsPage.Surface;
  ModelBrowseBtn.Caption := '浏览...';
  ModelBrowseBtn.Left := PathsPage.SurfaceWidth - 82;
  ModelBrowseBtn.Top := 93;
  ModelBrowseBtn.Width := 82;
  ModelBrowseBtn.Height := 25;
  ModelBrowseBtn.OnClick := @BrowseModelDirClick;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := (PageID = wpSelectDir);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  SelectedAppDir, SelectedModelDir: string;
begin
  Result := True;
  if CurPageID = PathsPage.ID then
  begin
    SelectedAppDir := Trim(AppDirEdit.Text);
    SelectedModelDir := Trim(ModelDirEdit.Text);
    if SelectedAppDir = '' then
    begin
      MsgBox('软件安装位置不能为空。', mbError, MB_OK);
      Result := False;
      Exit;
    end;
    if SelectedModelDir = '' then
    begin
      MsgBox('AI 模型存放位置不能为空。', mbError, MB_OK);
      Result := False;
      Exit;
    end;
    WizardForm.DirEdit.Text := SelectedAppDir;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ChosenModelDir: string;
begin
  if CurStep = ssPostInstall then
  begin
    ChosenModelDir := Trim(ModelDirEdit.Text);
    if ChosenModelDir <> '' then
    begin
      ForceDirectories(ChosenModelDir);
      RegWriteStringValue(
        HKEY_CURRENT_USER,
        'Software\证件照批量换底色工具_Czq',
        'ModelDir',
        ChosenModelDir
      );
    end;
  end;
end;
