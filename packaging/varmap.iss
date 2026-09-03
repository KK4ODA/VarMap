; Inno Setup script for the VarMap Windows installer.
; Built by the release workflow:  iscc /DAppVersion=0.1.0 packaging\varmap.iss
; Installs per-user (no admin prompt) into %LOCALAPPDATA%\Programs\VarMap.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{7B0C6C58-4D0E-4C7C-9C55-VARMAP000001}
AppName=VarMap
AppVersion={#AppVersion}
AppVerName=VarMap {#AppVersion}
AppPublisher=KK4ODA
AppPublisherURL=https://github.com/KK4ODA/VarMap
AppSupportURL=https://github.com/KK4ODA/VarMap/issues
AppUpdatesURL=https://github.com/KK4ODA/VarMap/releases
DefaultDirName={autopf}\VarMap
DefaultGroupName=VarMap
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=VarMap-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=VarMap
LicenseFile=..\LICENSE
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startup"; Description: "Start VarMap automatically when I log in"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "..\dist\VarMap\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\VarMap"; Filename: "{app}\VarMap.exe"; WorkingDir: "{app}"
Name: "{group}\Uninstall VarMap"; Filename: "{uninstallexe}"
Name: "{autodesktop}\VarMap"; Filename: "{app}\VarMap.exe"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{userstartup}\VarMap"; Filename: "{app}\VarMap.exe"; WorkingDir: "{app}"; Tasks: startup

[Run]
Filename: "{app}\VarMap.exe"; Description: "Start VarMap now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Settings and the station database live in %LOCALAPPDATA%\VarMap and are deliberately kept.
