#ifndef SourceDir
  #error SourceDir must point to the staged FormForge application directory
#endif

[Setup]
AppId={{9D80DE8C-6289-44E8-A339-F87C81092496}
AppName=FormForge Studio
AppVersion=0.10.0
AppPublisher=FormForge Studio
DefaultDirName={localappdata}\Programs\FormForge Studio
DefaultGroupName=FormForge Studio
UninstallDisplayIcon={app}\FormForge.exe
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
OutputDir=output
OutputBaseFilename=FormForgeStudio-Setup
SetupLogging=yes
LicenseFile={#SourceDir}\GPL-license.txt

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\FormForge Studio"; Filename: "{app}\FormForge.exe"; WorkingDir: "{app}"
Name: "{userdesktop}\FormForge Studio"; Filename: "{app}\FormForge.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Run]
Filename: "{app}\FormForge.exe"; Description: "Launch FormForge Studio"; Flags: nowait postinstall skipifsilent
