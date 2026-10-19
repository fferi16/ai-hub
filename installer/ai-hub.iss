; AI HUB – Inno Setup telepítő. Fordítás: installer\build.ps1 (a git-ben követett fájlokból)
; ISCC /DAppVersion=0.2.0 /DAppRepo=fferi16/ai-hub /DSrc=<tiszta forrásmappa> /O<kimenet> ai-hub.iss
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef AppRepo
  #define AppRepo "fferi16/ai-hub"
#endif
#ifndef Src
  #define Src ".."
#endif

[Setup]
AppId={{7B1E6F3A-6C1D-4E58-9A51-2F0C9D7A4E11}
AppName=AI HUB
AppVersion={#AppVersion}
AppVerName=AI HUB {#AppVersion}
AppPublisher=fferi16
AppPublisherURL=https://github.com/{#AppRepo}
AppSupportURL=https://github.com/{#AppRepo}/issues
AppUpdatesURL=https://github.com/{#AppRepo}/releases
DefaultDirName={localappdata}\AI-HUB
DisableDirPage=no
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputBaseFilename=AI-HUB-Setup-{#AppVersion}
SetupIconFile={#Src}\assets\ai-hub.ico
UninstallDisplayIcon={app}\assets\ai-hub.ico
UninstallDisplayName=AI HUB
LicenseFile={#Src}\LICENSE
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=no

[Languages]
Name: "hu"; MessagesFile: "compiler:Languages\Hungarian.isl"

[Tasks]
Name: "autostart"; Description: "Indítás bejelentkezéskor (Feladatütemező)"
Name: "claude"; Description: "Bekötés a Claude Code-ba (ha telepítve van)"

[Files]
Source: "{#Src}\*"; DestDir: "{app}"; Excludes: "config.json,\.git\*,\.venv\*,\logs\*,\out\*,__pycache__,\installer\out\*"; Flags: recursesubdirs ignoreversion
; a beállításokat újratelepítéskor sem írjuk felül
Source: "{#Src}\config.json"; DestDir: "{app}"; Flags: onlyifdoesntexist uninsneveruninstall

[Icons]
Name: "{userdesktop}\AI HUB"; Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: """{app}\launcher.pyw"""; WorkingDir: "{app}"; IconFilename: "{app}\assets\ai-hub.ico"; Comment: "AI HUB indítása / folyamatjelző"
Name: "{userprograms}\AI HUB\AI HUB"; Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: """{app}\launcher.pyw"""; WorkingDir: "{app}"; IconFilename: "{app}\assets\ai-hub.ico"
Name: "{userprograms}\AI HUB\AI HUB frissítése"; Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: """{app}\updater.py"" ui"; WorkingDir: "{app}"; IconFilename: "{app}\assets\ai-hub.ico"
Name: "{userprograms}\AI HUB\AI HUB eltávolítása"; Filename: "{uninstallexe}"

[Run]
; Python + csomagok + beállítások (látható ablakban, hogy követhető legyen a letöltés)
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\install.ps1"" -Repo {#AppRepo} -NoShortcuts -Quiet -NoStart {code:InstallFlags}"; WorkingDir: "{app}"; StatusMsg: "Python és a csomagok telepítése (első alkalommal több perc)…"; Flags: waituntilterminated
Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: """{app}\launcher.pyw"""; WorkingDir: "{app}"; Description: "AI HUB indítása most"; Flags: postinstall nowait skipifsilent

[UninstallRun]
Filename: "powershell.exe"; Parameters: "-NoProfile -Command ""Unregister-ScheduledTask -TaskName Hub -Confirm:$false -ErrorAction SilentlyContinue; Get-CimInstance Win32_Process -Filter \""Name like 'python%'\"" | Where-Object {{ $_.CommandLine -like '*{app}*' }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}"""; Flags: runhidden; RunOnceId: "StopHub"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: files; Name: "{app}\.installed_files"

[Code]
function InstallFlags(Param: String): String;
begin
  Result := '';
  if not WizardIsTaskSelected('autostart') then Result := Result + ' -NoAutostart';
  if not WizardIsTaskSelected('claude') then Result := Result + ' -NoClaude';
end;
