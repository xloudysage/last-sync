#define MyAppName "LastSync"
#define MyAppVersion "1.1.0"

[Setup]
AppId={{2AE51E28-7F79-4A2F-B0C8-3C401C26715C}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=shessaanand
DefaultDirName={localappdata}\Programs\LastSync
DefaultGroupName=LastSync
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=LastSync-1.1-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
LicenseFile=..\LICENSE
UninstallDisplayIcon={app}\LastSync.exe

[Files]
Source: "..\dist\LastSync.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\LastSync"; Filename: "{app}\LastSync.exe"
Name: "{group}\Uninstall LastSync"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\LastSync.exe"; Description: "Launch LastSync"; Flags: nowait postinstall skipifsilent
