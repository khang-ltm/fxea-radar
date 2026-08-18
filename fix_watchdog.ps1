# Repair the update watchdog and force an update now.
#
#   powershell -ExecutionPolicy Bypass -File fix_watchdog.ps1
#
# Why this exists: the watchdog trigger was registered with a repetition interval
# but no duration, which Windows treats as a one-shot - so it ran once and never
# again. This re-registers it correctly and runs it immediately.

$ErrorActionPreference = 'Continue'
$InstallDir = 'C:\fxea-radar'
$wdName     = 'fxea-mt5-updater'
$updater    = Join-Path $InstallDir 'update_agent.ps1'

function Say($m, $c = 'Gray') { Write-Host "  $m" -ForegroundColor $c }
Write-Host "`nWatchdog repair" -ForegroundColor Cyan

foreach ($t in 'fxea-mt5-agent', $wdName) {
    $info = Get-ScheduledTaskInfo -TaskName $t -ErrorAction SilentlyContinue
    if ($info) {
        Say ("{0}: last run {1}, result {2}" -f $t, $info.LastRunTime, $info.LastTaskResult)
    } else {
        Say ("{0}: NOT REGISTERED" -f $t) 'Yellow'
    }
}

if (-not (Test-Path $updater)) {
    Say "updater script missing at $updater - re-run install_vps.ps1" 'Red'
    exit 1
}

$action  = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$updater`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 20) `
    -RepetitionDuration ([TimeSpan]::MaxValue)
Unregister-ScheduledTask -TaskName $wdName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $wdName -Action $action -Trigger $trigger `
    -RunLevel Highest -Force | Out-Null
Say 'watchdog re-registered with a repeating trigger' 'Green'

Say 'running the updater now...'
& powershell -ExecutionPolicy Bypass -File $updater
Start-Sleep -Seconds 10

try {
    $token = ((Get-Content (Join-Path $InstallDir '.env.mt5') | Select-String '^MT5_TOKEN=').Line -split '=', 2)[1]
    $r = Invoke-RestMethod -Uri 'http://127.0.0.1:8788/api/charts' `
        -Headers @{ Authorization = "Bearer $token" } -TimeoutSec 15
    if ($r.ok) {
        Say ("agent updated. charts reported: {0}" -f $r.charts.Count) 'Green'
    } else {
        Say ("agent updated, manager says: {0}" -f $r.error) 'Yellow'
        Say 'that message is expected until FxeaManager.mq5 is attached to a chart' 'Gray'
    }
} catch {
    Say "agent still not serving /api/charts: $_" 'Red'
    Say 'check: Get-Content C:\fxea-radar\agent.err.log -Tail 20' 'Yellow'
}
