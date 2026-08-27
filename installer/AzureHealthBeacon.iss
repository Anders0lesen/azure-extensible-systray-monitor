#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#define AppName "Azure Health Beacon"
#define AppExeName "AzureHealthBeacon.exe"
#define AppPublisher "Anders Olesen"
#define AppURL "https://github.com/Anders0lesen/azure-extensible-systray-monitor"

[Setup]
AppId={{5D9060E9-4ECA-4A74-9F20-8C7343F4DB89}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
DefaultDirName={userpf}\Azure Health Beacon
DefaultGroupName=Azure Health Beacon
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
MinVersion=10.0.22000
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\assets\AzureHealthBeacon.ico
WizardImageFile=..\assets\AzureHealthBeacon-Installer-Wizard.png
WizardSmallImageFile=..\assets\AzureHealthBeacon-Brand-Square.png
CloseApplications=yes
RestartApplications=yes
OutputDir=..\dist
OutputBaseFilename=AzureHealthBeacon-Setup-v{#AppVersion}
UninstallDisplayIcon={app}\{#AppExeName}
VersionInfoVersion={#AppVersion}.0
VersionInfoCompany={#AppPublisher}
VersionInfoDescription=Azure multi-subscription health monitor installer
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "..\dist\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Azure Health Beacon"; Filename: "{app}\{#AppExeName}"
Name: "{userdesktop}\Azure Health Beacon"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Start Azure Health Beacon"; Flags: nowait

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "AzureHealthBeacon"; Flags: uninsdeletevalue
