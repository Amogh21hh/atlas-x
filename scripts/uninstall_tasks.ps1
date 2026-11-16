# Removes the ATLAS X scheduled tasks (does not touch IB Gateway or any data).
& (Join-Path $PSScriptRoot "stop.ps1")
foreach ($t in "ATLAS-X-Worker", "ATLAS-X-Dashboard", "ATLAS-X-IBGateway", "ATLAS-X-RoundtripCheck", "ATLAS-X-VerifyFills", "ATLAS-X-GiltCheck") {
  Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction SilentlyContinue
}
Write-Host "ATLAS X scheduled tasks removed."
