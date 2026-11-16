# Start ATLAS X now (dashboard + worker). IB Gateway must be running and logged in to Paper Trading.
Start-ScheduledTask -TaskName "ATLAS-X-Dashboard"
Start-ScheduledTask -TaskName "ATLAS-X-Worker"
Start-Sleep -Seconds 5
Get-ScheduledTask -TaskName "ATLAS-X-*" | Select-Object TaskName, State | Format-Table -AutoSize
Write-Host "Dashboard: http://127.0.0.1:8600"
