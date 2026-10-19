# A nyilvános repó frissítése: a privát repó aktuális (commitolt) állapotáról tiszta pillanatkép,
# kiadásonként egyetlen commit – a privát előzmény nem kerül át.
# Használat: powershell -ExecutionPolicy Bypass -File installer\publish_public.ps1 [-Repo fferi16/ai-hub]
param([string]$Repo = "fferi16/ai-hub")
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$version = (Get-Content "$root\VERSION" -Raw).Trim()
if (git -C $root status --porcelain) { throw "Előbb commitold a változásokat (a pillanatkép a HEAD-ből készül)." }
$work = Join-Path $env:TEMP "ai-hub-public"
Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue

$ErrorActionPreference = "Continue"  # a gh hibakimenete (nincs még ilyen repó) ne állítsa le a szkriptet
gh repo view $Repo 2>&1 | Out-Null
$exists = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = "Stop"
if (-not $exists) {
  gh repo create $Repo --public --description "AI HUB – helyi eszközközpont Claude-hoz Windowson (MCP, pluginok, helyi AI)" | Out-Null
  New-Item -ItemType Directory $work | Out-Null
  git -C $work init -q -b main
  git -C $work remote add origin "https://github.com/$Repo.git"
} else {
  git clone -q "https://github.com/$Repo.git" $work
  if ($LASTEXITCODE -ne 0) { throw "A nyilvános repó klónozása nem sikerült." }
  Get-ChildItem $work -Force | Where-Object { $_.Name -ne ".git" } | Remove-Item -Recurse -Force
}

git -C $root archive --format=zip -o "$work.zip" HEAD
Expand-Archive "$work.zip" $work -Force
Remove-Item "$work.zip"
$py = if (Test-Path "$root\.venv\Scripts\python.exe") { "$root\.venv\Scripts\python.exe" } else { (Get-Command python -ErrorAction Stop).Source }
& $py "$PSScriptRoot\set_repo.py" "$work\config.json" $Repo | Out-Null

git -C $work add -A
git -C $work -c user.name="fferi16" -c user.email="fferi16@users.noreply.github.com" commit -q -m "AI HUB v$version" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
if ($LASTEXITCODE -ne 0) { Write-Host "Nincs változás a nyilvános repóhoz képest."; exit 0 }
git -C $work push -q -u origin HEAD:main
if ($LASTEXITCODE -ne 0) { throw "A push nem sikerült." }
Write-Host "Kész: https://github.com/$Repo (v$version)"
Write-Output $work
