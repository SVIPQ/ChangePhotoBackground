; 证件照批量换底色工具_Czq Inno Setup 安装脚本
#define MyAppName "证件照批量换底色工具_Czq"
#define MyAppVersion "2.1"
#define MyAppPublisher "Czq"
#define MyAppExeName "证件照批量换底色工具_Czq.exe"
#define MyRegKey "Software\证件照批量换底色工具_Czq"

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
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
OutputDir=output
OutputBaseFilename=证件照批量换底色工具_安装程序_CZQ

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[CustomMessages]
ModelPageCaption=AI 模型存放位置设置
ModelPageDescription=请选择要使用的 AI 模型及存放文件夹。
ModelSelectLabel=选择 AI 模型：
ModelDirLabel=模型存放目录（随所选模型自动生成对应名称文件夹）：
ModelBrowseBtn=浏览(&B)...

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "dist\{#MyAppName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "app.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\app.ico"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\app.ico"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "{#MyRegKey}"; ValueType: string; ValueName: "ModelDir"; ValueData: "{code:GetModelDir}"; Flags: uninsdeletekey

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Code]
var
  ModelPage: TWizardPage;
  ModelCombo: TComboBox;
  ModelDirEdit: TEdit;
  ModelBrowseButton: TButton;
  SelectedModelDir: string;

function GetBaseDriveRoot(): string;
begin
  if DirExists('D:\') then
    Result := 'D:\'
  else if DirExists('E:\') then
    Result := 'E:\'
  else
    Result := AddBackslash(ExtractFileDrive(ExpandConstant('{userdocs}')));
end;

function GetModelFolderNameByIndex(Idx: Integer): string;
begin
  case Idx of
    0: Result := 'birefnet-portrait';
    1: Result := 'nr.net';
    2: Result := 'isnet-general-use';
    3: Result := 'u2net';
  else
    Result := 'birefnet-portrait';
  end;
end;

function BuildDefaultModelDirForIndex(Idx: Integer): string;
var
  BaseRoot, FolderName: string;
begin
  BaseRoot := GetBaseDriveRoot();
  FolderName := GetModelFolderNameByIndex(Idx);
  Result := BaseRoot + FolderName;
end;

procedure ModelComboChange(Sender: TObject);
begin
  if (ModelCombo <> nil) and (ModelDirEdit <> nil) then
  begin
    SelectedModelDir := BuildDefaultModelDirForIndex(ModelCombo.ItemIndex);
    ModelDirEdit.Text := SelectedModelDir;
  end;
end;

procedure ModelBrowseClick(Sender: TObject);
var
  Dir: string;
begin
  Dir := ModelDirEdit.Text;
  if BrowseForFolder(CustomMessage('ModelBrowseBtn'), Dir, False) then
    ModelDirEdit.Text := Dir;
end;

procedure InitializeWizard;
begin
  ModelPage := CreateCustomPage(
    wpSelectDir,
    CustomMessage('ModelPageCaption'),
    CustomMessage('ModelPageDescription')
  );

  TLabel.Create(ModelPage).Parent := ModelPage.Surface;
  with TLabel(ModelPage.Surface.Controls[ModelPage.Surface.ControlCount - 1]) do
  begin
    Caption := CustomMessage('ModelSelectLabel');
    Left := 0;
    Top := 4;
    Width := ModelPage.SurfaceWidth;
  end;

  ModelCombo := TComboBox.Create(ModelPage);
  with ModelCombo do
  begin
    Parent := ModelPage.Surface;
    Style := csDropDownList;
    Left := 0;
    Top := 24;
    Width := ModelPage.SurfaceWidth;
    Items.Add('birefnet-portrait（人像发丝默认）');
    Items.Add('nrnet（对应文件夹 nr.net）');
    Items.Add('isnet-general-use（高精度通用）');
    Items.Add('u2net（经典通用）');
    ItemIndex := 0;
    OnChange := @ModelComboChange;
  end;

  SelectedModelDir := BuildDefaultModelDirForIndex(0);

  TLabel.Create(ModelPage).Parent := ModelPage.Surface;
  with TLabel(ModelPage.Surface.Controls[ModelPage.Surface.ControlCount - 1]) do
  begin
    Caption := CustomMessage('ModelDirLabel');
    Left := 0;
    Top := 60;
    Width := ModelPage.SurfaceWidth;
  end;

  ModelDirEdit := TEdit.Create(ModelPage);
  with ModelDirEdit do
  begin
    Parent := ModelPage.Surface;
    Left := 0;
    Top := 82;
    Width := ModelPage.SurfaceWidth - 92;
    Height := 24;
    Text := SelectedModelDir;
  end;

  ModelBrowseButton := TButton.Create(ModelPage);
  with ModelBrowseButton do
  begin
    Parent := ModelPage.Surface;
    Left := ModelPage.SurfaceWidth - 82;
    Top := 81;
    Width := 82;
    Height := 26;
    Caption := CustomMessage('ModelBrowseBtn');
    OnClick := @ModelBrowseClick;
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (ModelPage <> nil) and (CurPageID = ModelPage.ID) then
  begin
    SelectedModelDir := Trim(ModelDirEdit.Text);
    if SelectedModelDir = '' then
    begin
      MsgBox('请填写或选择一个有效的 AI 模型存放目录。', mbError, MB_OK);
      Result := False;
      Exit;
    end;
    if not DirExists(SelectedModelDir) then
    begin
      if not ForceDirectories(SelectedModelDir) then
      begin
        MsgBox('无法创建模型文件夹，请检查路径或权限：' + SelectedModelDir, mbError, MB_OK);
        Result := False;
        Exit;
      end;
    end;
  end;
end;

function GetModelDir(Param: string): string;
begin
  if Trim(ModelDirEdit.Text) <> '' then
    Result := Trim(ModelDirEdit.Text)
  else
    Result := SelectedModelDir;
end;