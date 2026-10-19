# Setup.exe fordítása a git-ben követett (commitolt) fájlokból – így biztosan nem kerül bele helyi adat.
# Használat: powershell -ExecutionPolicy Bypass -File installer\build.ps1 [-Repo fferi16/ai-hub]
param([string]$Repo = "")
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$version = (Get-Content "$root\VERSION" -Raw).Trim()
if (-not $Repo) {
  $url = git -C $root remote get-url origin
  if ($url -match "github\.com[:/](.+?)(\.git)?$") { $Repo = $Matches[1] } else { throw "Add meg a -Repo paramétert." }
}
$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe") |
        Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Nem találom az Inno Setup 6-ot (ISCC.exe)." }
$stage = Join-Path $env:TEMP "ai-hub-build-$version"
Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
New-Item -ItemType Directory $stage | Out-Null
git -C $root archive --format=zip -o "$stage.zip" HEAD
Expand-Archive "$stage.zip" $stage -Force
Remove-Item "$stage.zip"
# a kiadott példány frissítési forrása legyen a cél-repó
& "$root\.venv\Scripts\python.exe" "$PSScriptRoot\set_repo.py" "$stage\config.json" $Repo | Out-Null
$out = "$root\installer\out"
New-Item -ItemType Directory -Force $out | Out-Null
& $iscc "/DAppVersion=$version" "/DAppRepo=$Repo" "/DSrc=$stage" "/O$out" "$PSScriptRoot\ai-hub.iss"
if ($LASTEXITCODE -ne 0) { throw "Az Inno Setup fordítás nem sikerült." }
Remove-Item -Recurse -Force $stage
Write-Host "Kész: $out\AI-HUB-Setup-$version.exe ($Repo)"
