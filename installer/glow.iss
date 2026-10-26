; Inno Setup 6 script. Build with: python installer/build.py
; Per-user install (no admin): GLOW writes glow.json and its profiles next to GLOW.exe.

#ifndef AppVer
  #define AppVer "0.0.0"
#endif

[Setup]
AppId=GLOW
AppName=GLOW
AppVersion={#AppVer}
AppPublisher=Pablo Romero
AppPublisherURL=https://github.com/pablo29855/Glow
DefaultDirName={localappdata}\Programs\GLOW
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=GLOW-Setup
SetupIconFile=..\glow.ico
UninstallDisplayIcon={app}\GLOW.exe
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Tasks]
Name: desktopicon; Description: "Create a desktop shortcut"

[Files]
Source: "..\dist\GLOW\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion createallsubdirs

[Icons]
Name: "{userprograms}\GLOW"; Filename: "{app}\GLOW.exe"
Name: "{userdesktop}\GLOW"; Filename: "{app}\GLOW.exe"; Tasks: desktopicon

[Registry]
; Written by the settings window ("start with Windows"); only removed here, never created.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "OpenClawGlow"; ValueType: none; Flags: uninsdeletevalue

[Run]
Filename: "{app}\GLOW.exe"; Description: "Launch GLOW"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\GLOW.exe"; Parameters: "quit"; RunOnceId: "QuitGlow"; Flags: runhidden waituntilterminated

[UninstallDelete]
; settings and profiles the app saved; not part of the installed files
Type: files; Name: "{app}\glow.json"
Type: files; Name: "{app}\glow_profiles.json"
