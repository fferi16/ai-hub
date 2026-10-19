# AI HUB telepítő – rendszergazdai jog nélkül fut. Indítás: Telepites.cmd (dupla kattintás)
# Lépések: Python (3.11+) keresése/telepítése → .venv → csomagok → parancsikonok → automatikus indítás
#          → Claude Code bekötése (ha telepítve van) → a hub indítása.
param(
  [string]$Repo = "",          # frissítési forrás, pl. fferi16/ai-hub (üres = a config.json-ban lévő marad)
  [switch]$NoShortcuts,        # a Setup.exe maga készíti a parancsikonokat
  [switch]$NoAutostart,
  [switch]$NoStart,
  [switch]$NoClaude,
  [switch]$Quiet
)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root
function Say($msg) { Write-Host "  » $msg" -ForegroundColor Cyan }
function Ok($msg)  { Write-Host "  ✓ $msg" -ForegroundColor Green }
Write-Host "`n  AI HUB telepítése – $root`n" -ForegroundColor Magenta

# 1. Python 3.11+
function Find-Python {
  foreach ($c in @(@("py", "-3.13"), @("py", "-3.12"), @("py", "-3.11"), @("python"), @("python3"))) {
    try {
      $v = & $c[0] $c[1..9] -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
      if ($LASTEXITCODE -eq 0 -and [version]$v -ge [version]"3.11") { return ,$c }
    } catch {}
  }
  return $null
}
$py = Find-Python
if (-not $py) {
  Say "Nem találtam Python 3.11+-ot – telepítem (winget, felhasználói szintre)…"
  winget install --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
  $py = Find-Python
  if (-not $py) { throw "A Python telepítése nem sikerült. Telepítsd kézzel (python.org, 3.12), majd futtasd újra a Telepites.cmd-t." }
}
Ok ("Python: " + (& $py[0] $py[1..9] --version))

# 2. Virtuális környezet
if (-not (Test-Path "$root\.venv\Scripts\python.exe")) {
  Say "Virtuális környezet létrehozása (.venv)…"
  & $py[0] $py[1..9] -m venv "$root\.venv"
}
$vpy = "$root\.venv\Scripts\python.exe"
& $vpy -m pip install --upgrade pip --disable-pip-version-check -q
Ok "Virtuális környezet kész"

# 3. Csomagok (a hub + minden plugin; GPU nélkül a CUDA-csomagok kimaradnak)
Say "Csomagok telepítése – első alkalommal ez több percig tarthat…"
& $vpy "$root\deps.py"
if ($LASTEXITCODE -ne 0) { throw "A csomagok telepítése nem sikerült (lásd fent)." }
Ok "Csomagok rendben"

# 4. Frissítési forrás + telepített fájlok listája (a frissítő ebből tudja, mi törölhető később)
$cfgPath = "$root\config.json"
$cfg = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $Repo -and (Test-Path "$root\.git")) {
  $url = git -C $root remote get-url origin 2>$null
  if ($url -match "github\.com[:/](.+?)(\.git)?$") { $Repo = $Matches[1] }
}
if ($Repo) {
  & $vpy "$root\installer\set_repo.py" $cfgPath $Repo | Out-Null
  $cfg = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
}
if (-not (Test-Path "$root\.git")) {
  $skip = '^(\.venv|logs|out|\.git)\\|\\__pycache__\\|\.tested$|^config\.json$|^\.installed_files$|^unins\d{3}\.(exe|dat|msg)$'
  $files = Get-ChildItem $root -Recurse -File | ForEach-Object { $_.FullName.Substring($root.Length + 1) } |
           Where-Object { $_ -notmatch $skip } | ForEach-Object { $_ -replace '\\', '/' }
  [IO.File]::WriteAllLines("$root\.installed_files", $files)
}
Ok ("Frissítési forrás: " + $(if ($cfg.update.repo) { $cfg.update.repo } else { "(nincs)" }))

# 5. Parancsikonok: Asztal + Start menü
if (-not $NoShortcuts) {
  $ws = New-Object -ComObject WScript.Shell
  $pyw = "$root\.venv\Scripts\pythonw.exe"
  $menu = Join-Path ([Environment]::GetFolderPath("Programs")) "AI HUB"
  New-Item -ItemType Directory -Force $menu | Out-Null
  $links = @(
    @{ Path = (Join-Path ([Environment]::GetFolderPath("Desktop")) "AI HUB.lnk"); Args = "`"$root\launcher.pyw`""; Desc = "AI HUB indítása / folyamatjelző" },
    @{ Path = "$menu\AI HUB.lnk"; Args = "`"$root\launcher.pyw`""; Desc = "AI HUB indítása / folyamatjelző" },
    @{ Path = "$menu\AI HUB frissítése.lnk"; Args = "`"$root\updater.py`" ui"; Desc = "Új verzió keresése és telepítése" }
  )
  foreach ($l in $links) {
    $s = $ws.CreateShortcut($l.Path)
    $s.TargetPath = $pyw; $s.Arguments = $l.Args; $s.WorkingDirectory = $root
    $s.IconLocation = "$root\assets\ai-hub.ico,0"; $s.Description = $l.Desc
    $s.Save()
  }
  Ok "Parancsikonok: Asztal → AI HUB, Start menü → AI HUB, AI HUB frissítése"
}

# 6. Automatikus indítás bejelentkezéskor
if (-not $NoAutostart) {
  & "$root\install_autostart.ps1" | Out-Null
  Ok "Automatikus indítás bejelentkezéskor (Feladatütemező: Hub)"
}

# 7. Claude Code bekötése (ha telepítve van)
$claude = Get-Command claude -ErrorAction SilentlyContinue
if ($NoClaude) { Say "Claude Code bekötése kihagyva" }
elseif ($claude) {
  $list = & claude mcp list 2>$null
  if (-not ($list -match "^hub:")) {
    & claude mcp add --scope user --transport http hub http://127.0.0.1:8765/mcp 2>$null | Out-Null
    Ok "Claude Code: hub MCP-szerver hozzáadva"
  } else { Ok "Claude Code: a hub már be van kötve" }
} else { Say "Claude Code nincs telepítve – később: claude mcp add --scope user --transport http hub http://127.0.0.1:8765/mcp" }

# 8. Indítás
if (-not $NoStart) {
  Start-Process -FilePath "$root\.venv\Scripts\pythonw.exe" -ArgumentList "`"$root\launcher.pyw`"" -WorkingDirectory $root
  Ok "Az AI HUB elindult (tálcaikon)"
}
Write-Host "`n  Kész! Asztali ikon: AI HUB · frissítés: tálcaikon menü vagy Start menü → AI HUB frissítése`n" -ForegroundColor Magenta
if (-not $Quiet) { Read-Host "  Nyomj Entert a bezáráshoz" | Out-Null }
