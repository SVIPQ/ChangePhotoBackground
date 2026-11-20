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

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Files]
Source: "dist\证件照批量换底色工具_Czq\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autodesktop}\证件照批量换底色工具_Czq"; Filename: "{app}\证件照批量换底色工具_Czq.exe"; IconFilename: "{app}\证件照批量换底色工具_Czq.exe"

[Code]
var
  ModelDirPage: TInputDirWizardPage;

procedure InitializeWizard;
begin
  ModelDirPage := CreateInputDirPage(
    wpSelectDir,
    '选择 AI 模型存放位置',
    '请选择 AI 抠图模型的存放文件夹（建议选择非 C 盘目录）',
    '安装完成后，程序将使用此处指定的目录存放或读取 AI 模型。点击“浏览”可逐级选择目录，也可直接输入路径。',
    False,
    ''
  );
  ModelDirPage.Add('');
  ModelDirPage.Values[0] := 'D:\证件照模型\.u2net';
end;

procedure RegisterPreviousData(PreviousDataKey: Integer);
begin
  SetPreviousData(PreviousDataKey, 'ModelDir', ModelDirPage.Values[0]);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ChosenModelDir: string;
begin
  if CurStep = ssPostInstall then
  begin
    ChosenModelDir := Trim(ModelDirPage.Values[0]);
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
