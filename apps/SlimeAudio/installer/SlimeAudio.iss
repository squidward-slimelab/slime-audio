#define MyAppName "Slime Audio"
#define MyAppVersion GetEnv("SLIME_AUDIO_VERSION")
#if MyAppVersion == ""
#define MyAppVersion "0.1.0"
#endif
#define MyAppPublisher "Slime Lab"
#define MyAppExeName "SlimeAudio.Tray.exe"

[Setup]
AppId={{8A90CF6E-FF1B-4F58-B401-C67F8750BBAE}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\Slime Audio
DefaultGroupName=Slime Audio
DisableProgramGroupPage=yes
OutputDir=..\..\..\artifacts\installer
OutputBaseFilename=SlimeAudioSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupIconFile=..\assets\slime-audio.ico

[Files]
Source: "..\..\..\artifacts\slime-audio-tray-win-x64\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Slime Audio"; Filename: "{app}\{#MyAppExeName}"
Name: "{userstartup}\Slime Audio"; Filename: "{app}\{#MyAppExeName}"; Tasks: startup

[Tasks]
Name: "startup"; Description: "Start Slime Audio when I sign in"; GroupDescription: "Startup:"; Flags: unchecked

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Slime Audio"; Flags: nowait postinstall skipifsilent
