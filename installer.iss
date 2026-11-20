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

[Files]
Source: "dist\证件照批量换底色工具_Czq\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autodesktop}\证件照批量换底色工具_Czq"; Filename: "{app}\证件照批量换底色工具_Czq.exe"; IconFilename: "{app}\证件照批量换底色工具_Czq.exe"
