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
  InstallDirEdit: TEdit;
  ModelDirEdit: TEdit;

procedure BrowseInstallDirClick(Sender: TObject);
var
  Dir: string;
begin
  Dir := Trim(InstallDirEdit.Text);
  if Dir = '' then
    Dir := ExpandConstant('{autopf}\证件照批量换底色工具_Czq');
  if BrowseForFolder('请选择软件安装目录', Dir, True) then
    InstallDirEdit.Text := Dir;
end;

procedure BrowseModelDirClick(Sender: TObject);
var
  Dir: string;
begin
  Dir := Trim(ModelDirEdit.Text);
  if Dir = '' then
    Dir := 'D:\证件照模型\.u2net';
  if BrowseForFolder('请选择 AI 模型存放目录（建议选择非 C 盘）', Dir, True) then
    ModelDirEdit.Text := Dir;
end;

procedure InitializeWizard;
var
  InstallLabel, ModelLabel: TNewStaticText;
  InstallBrowseBtn, ModelBrowseBtn: TNewButton;
begin
  PathsPage := CreateCustomPage(
    wpWelcome,
    '选择安装位置与 AI 模型目录',
    '请分别设置软件安装文件夹和 AI 模型存放文件夹，然后点击“下一步”。'
  );

  InstallLabel := TNewStaticText.Create(PathsPage);
  InstallLabel.Parent := PathsPage.Surface;
  InstallLabel.Caption := '1. 软件安装位置：';
  InstallLabel.Left := 0;
  InstallLabel.Top := 8;
  InstallLabel.Width := PathsPage.SurfaceWidth;

  InstallDirEdit := TEdit.Create(PathsPage);
  InstallDirEdit.Parent := PathsPage.Surface;
  InstallDirEdit.Left := 0;
  InstallDirEdit.Top := 28;
  InstallDirEdit.Width := PathsPage.SurfaceWidth - 92;
  InstallDirEdit.Text := ExpandConstant('{autopf}\证件照批量换底色工具_Czq');

  InstallBrowseBtn := TNewButton.Create(PathsPage);
  InstallBrowseBtn.Parent := PathsPage.Surface;
  InstallBrowseBtn.Caption := '浏览...';
  InstallBrowseBtn.Left := PathsPage.SurfaceWidth - 82;
  InstallBrowseBtn.Top := 27;
  InstallBrowseBtn.Width := 82;
  InstallBrowseBtn.Height := 25;
  InstallBrowseBtn.OnClick := @BrowseInstallDirClick;

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
  SelectedInstallDir, SelectedModelDir: string;
begin
  Result := True;
  if CurPageID = PathsPage.ID then
  begin
    SelectedInstallDir := Trim(InstallDirEdit.Text);
    SelectedModelDir := Trim(ModelDirEdit.Text);
    if SelectedInstallDir = '' then
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
    if WizardForm.DirEdit <> nil then
      WizardForm.DirEdit.Text := SelectedInstallDir;
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
