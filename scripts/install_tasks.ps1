# Registers ATLAS X as Windows Scheduled Tasks for the CURRENT user (no admin rights needed):
#   ATLAS-X-IBGateway  - opens IB Gateway at logon. You log in yourself; no credentials are stored anywhere.
#   ATLAS-X-Worker     - autonomous worker; at logon (+2 min) and re-checked every 15 min. If it has died
#                        it is relaunched; if it is running the new instance exits (single-instance lock).
#   ATLAS-X-Dashboard  - dashboard on http://127.0.0.1:8600, same relaunch behaviour.
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
# Resolve the real interpreter (not the Microsoft Store "python" alias shim)
$py = (& python -c "import sys; print(sys.executable)").Trim()
$pyw = Join-Path (Split-Path $py) "pythonw.exe"
if (-not (Test-Path $pyw)) { $pyw = $py }
$gateway = "C:\Jts\ibgateway\ibgateway.exe"
$user = "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

function New-AtlasSettings {
  New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
}
function New-LogonRepeatTrigger([int]$delayMin) {
  $t = New-ScheduledTaskTrigger -AtLogOn -User $user
  $t.Delay = "PT$($delayMin)M"
  $t.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 15)).Repetition
  return $t
}

if (Test-Path $gateway) {
  $a = New-ScheduledTaskAction -Execute $gateway -WorkingDirectory (Split-Path $gateway)
  $t = New-ScheduledTaskTrigger -AtLogOn -User $user
  Register-ScheduledTask -TaskName "ATLAS-X-IBGateway" -Action $a -Trigger $t -Principal $principal `
    -Settings (New-AtlasSettings) -Description "Opens IB Gateway at logon (manual paper login)." -Force | Out-Null
  Write-Host "registered ATLAS-X-IBGateway"
} else { Write-Warning "IB Gateway not found at $gateway; skipped" }

$a = New-ScheduledTaskAction -Execute $pyw -Argument "-m atlas.worker" -WorkingDirectory $root
Register-ScheduledTask -TaskName "ATLAS-X-Worker" -Action $a -Trigger (New-LogonRepeatTrigger 2) -Principal $principal `
  -Settings (New-AtlasSettings) -Description "ATLAS X autonomous paper-trading worker." -Force | Out-Null
Write-Host "registered ATLAS-X-Worker"

$a = New-ScheduledTaskAction -Execute $pyw -Argument "-m atlas.serve" -WorkingDirectory $root
Register-ScheduledTask -TaskName "ATLAS-X-Dashboard" -Action $a -Trigger (New-LogonRepeatTrigger 1) -Principal $principal `
  -Settings (New-AtlasSettings) -Description "ATLAS X dashboard on http://127.0.0.1:8600." -Force | Out-Null
Write-Host "registered ATLAS-X-Dashboard"
