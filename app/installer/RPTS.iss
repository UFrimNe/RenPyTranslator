#ifndef AppVersion
  #define AppVersion "11.28"
#endif
#ifndef PayloadDir
  #error PayloadDir is required: run Build.bat from the repository root.
#endif
#ifndef ArtifactDir
  #define ArtifactDir "..\..\dist"
#endif

[Setup]
AppId={{D10BDB8D-D891-49A9-A593-FC9D934C6858}
AppName=RenPyTranslator
AppVersion={#AppVersion}
AppPublisher=UFrimNe
AppPublisherURL=https://github.com/UFrimNe/RenPyTranslator
AppSupportURL=https://github.com/UFrimNe/RenPyTranslator/issues
AppUpdatesURL=https://github.com/UFrimNe/RenPyTranslator/releases/latest
DefaultDirName={localappdata}\Programs\RenPyTranslator
DefaultGroupName=RenPyTranslator
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
SetupIconFile=..\desktop\src\app.ico
UninstallDisplayIcon={app}\RPTS.exe
OutputDir={#ArtifactDir}
OutputBaseFilename=RenPyTranslator_{#AppVersion}_Setup_win64
Compression=lzma2/fast
SolidCompression=yes
AppMutex=Local\RPTS-Running
SetupMutex=Local\RPTS-Setup
CloseApplications=no
RestartApplications=no

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Ярлыки:"; Flags: unchecked

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\RenPyTranslator"; Filename: "{app}\RPTS.exe"; WorkingDir: "{app}"
Name: "{group}\Диагностика запуска"; Filename: "{app}\RPTS-Debug.bat"; WorkingDir: "{app}"
Name: "{autodesktop}\RenPyTranslator"; Filename: "{app}\RPTS.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\RPTS.exe"; Description: "Запустить RenPyTranslator"; Flags: nowait postinstall skipifsilent

; User data, downloaded models and translations are never in [Files] or [UninstallDelete].
