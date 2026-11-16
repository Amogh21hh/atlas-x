# Stop ATLAS X (worker + dashboard). Scheduled tasks stay registered and will relaunch them at the next
# 15-minute check unless you also disable them:  Disable-ScheduledTask -TaskName ATLAS-X-Worker
param([switch]$Disable)
foreach ($t in "ATLAS-X-Worker", "ATLAS-X-Dashboard") {
  Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
  if ($Disable) { Disable-ScheduledTask -TaskName $t | Out-Null }
}
Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
  Where-Object { $_.CommandLine -match "atlas\.(worker|serve)" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host "stopped pid $($_.ProcessId): $($_.CommandLine)" }
Get-ScheduledTask -TaskName "ATLAS-X-*" | Select-Object TaskName, State | Format-Table -AutoSize
