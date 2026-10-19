# Hub automatikus indítása bejelentkezéskor (Feladatütemező, rendszergazda nem kell)
$root = $PSScriptRoot
$action = New-ScheduledTaskAction -Execute "$root\.venv\Scripts\pythonw.exe" -Argument "`"$root\hub.py`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName "Hub" -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "Claude Hub MCP-szerver (127.0.0.1:8765)" -Force | Out-Null
Write-Host "Kész: a Hub bejelentkezéskor indul. Azonnali indítás: Start-ScheduledTask -TaskName Hub"
