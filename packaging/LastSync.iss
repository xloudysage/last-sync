#define MyAppName "LastSync"
#define MyAppVersion "2.0.0"
#ifndef AppBinary
  #define AppBinary "..\dist\LastSync.exe"
#endif

[Setup]
AppId={{2AE51E28-7F79-4A2F-B0C8-3C401C26715C}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=shessaanand
DefaultDirName={localappdata}\Programs\LastSync
DefaultGroupName=LastSync
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=LastSync-2.0.0-Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
WizardStyle=modern
LicenseFile=..\LICENSE
UninstallDisplayIcon={app}\LastSync.exe

[Files]
Source: "{#AppBinary}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

Source: "..\docs\third-party-licenses\*"; DestDir: "{app}\third-party-licenses"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\LastSync"; Filename: "{app}\LastSync.exe"
Name: "{group}\Uninstall LastSync"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\LastSync.exe"; Description: "Launch LastSync"; Flags: nowait postinstall skipifsilent
