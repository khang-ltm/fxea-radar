# Repair the update watchdog, restart the agent, and report what actually happened.
#
#   powershell -ExecutionPolicy Bypass -File fix_watchdog.ps1
#
# Two bugs this fixes:
#   * New-ScheduledTaskTrigger -RepetitionInterval without -RepetitionDuration is
#     registered as a one-shot, so the watchdog ran once and stopped. Passing
#     [TimeSpan]::MaxValue instead is rejected outright by Task Scheduler
#     ("P99999999DT23H59M59S"). schtasks /SC MINUTE /MO 20 just works.
#   * the previous script reported success even when registration threw.

$ErrorActionPreference = 'Continue'
$InstallDir = 'C:\fxea-radar'
$agentTask  = 'fxea-mt5-agent'
$wdName     = 'fxea-mt5-updater'
$updater    = Join-Path $InstallDir 'update_agent.ps1'
$port       = 8788

function Say($m, $c = 'Gray') { Write-Host "  $m" -ForegroundColor $c }
Write-Host "`nWatchdog repair" -ForegroundColor Cyan

foreach ($t in $agentTask, $wdName) {
    $info = Get-ScheduledTaskInfo -TaskName $t -ErrorAction SilentlyContinue
    if ($info) { Say ("{0}: last run {1}, result {2}" -f $t, $info.LastRunTime, $info.LastTaskResult) }
    else       { Say ("{0}: NOT REGISTERED" -f $t) 'Yellow' }
}

if (-not (Test-Path $updater)) {
    Say "updater missing at $updater - re-run install_vps.ps1" 'Red'
    exit 1
}

# --- re-register the watchdog with schtasks ---------------------------------
schtasks /Delete /TN $wdName /F 2>$null | Out-Null
$cmd = "powershell.exe -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$updater`""
schtasks /Create /TN $wdName /TR $cmd /SC MINUTE /MO 20 /RL HIGHEST /F | Out-Null
if ($LASTEXITCODE -eq 0) {
    Say 'watchdog registered: every 20 minutes' 'Green'
} else {
    Say "schtasks failed with exit code $LASTEXITCODE" 'Red'
}

# --- pull the newest code right now -----------------------------------------
Say 'updating code...'
& powershell -ExecutionPolicy Bypass -File $updater

# --- make sure the agent is actually up -------------------------------------
Start-Sleep -Seconds 5
$running = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
             Where-Object { $_.CommandLine -like '*app.mt5_agent*' }).Count
if ($running -eq 0) {
    Say 'agent not running - starting it' 'Yellow'
    Start-ScheduledTask -TaskName $agentTask
    Start-Sleep -Seconds 12
}

# --- verify -----------------------------------------------------------------
$token = ((Get-Content (Join-Path $InstallDir '.env.mt5') | Select-String '^MT5_TOKEN=').Line -split '=', 2)[1]
try {
    $r = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/charts" `
        -Headers @{ Authorization = "Bearer $token" } -TimeoutSec 15
    if ($r.ok) {
        Say ("agent updated. charts reported: {0}" -f $r.charts.Count) 'Green'
        foreach ($c in $r.charts) {
            Say ("   chart {0}  {1}  expert: {2}" -f $c.chart, $c.symbol,
                 $(if ($c.expert) { $c.expert } else { '(none)' }))
        }
    } else {
        Say ("agent updated, manager says: {0}" -f $r.error) 'Yellow'
        Say 'expected until FxeaManager.mq5 is attached to a spare chart' 'Gray'
    }
} catch {
    Say "agent still not answering: $_" 'Red'
    foreach ($log in 'agent.err.log', 'agent.out.log') {
        $f = Join-Path $InstallDir $log
        if (Test-Path $f) {
            Say "--- $log (last 15 lines) ---" 'Yellow'
            Get-Content $f -Tail 15 | ForEach-Object { Write-Host "    $_" }
        }
    }
    Say '--- scheduled task state ---' 'Yellow'
    Get-ScheduledTaskInfo -TaskName $agentTask -ErrorAction SilentlyContinue |
        Format-List TaskName, LastRunTime, LastTaskResult, NumberOfMissedRuns | Out-String |
        ForEach-Object { Write-Host $_ }
}
