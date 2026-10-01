# Force the agent process to be the code that is on disk.
#
#   powershell -ExecutionPolicy Bypass -File restart_agent.ps1
#
# The agent updates itself by downloading the new file and then replacing its
# own process. When that second half fails, the download has already happened:
# the new code sits on disk, the old code keeps serving, and every later update
# reports success and changes nothing. The System check calls this out as
# "running <a> but <b> is on disk - a restart did not take".
#
# fix_watchdog.ps1 could not get out of it. It starts the agent only when no
# python.exe is running app.mt5_agent - and in this state one IS running, just
# the wrong one. A process existing is not the same as the right process
# existing, so it skipped the restart every time and the mismatch survived.
#
# This script does not ask whether the agent is running. It stops the task,
# kills whatever is still holding the port, starts the task again, and then
# reports the two shas so the outcome is checkable rather than assumed.

$ErrorActionPreference = 'Continue'
$InstallDir = 'C:\fxea-radar'
$agentTask  = 'fxea-mt5-agent'
$port       = 8788

function Say($m, $c = 'Gray') { Write-Host "  $m" -ForegroundColor $c }

function Get-AgentProcs {
    @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
      Where-Object { $_.CommandLine -like '*app.mt5_agent*' })
}

Write-Host "`nAgent restart" -ForegroundColor Cyan

$before = Get-AgentProcs
if ($before.Count) {
    Say ("running: {0} process(es) - pid {1}" -f $before.Count, ($before.ProcessId -join ', '))
} else {
    Say 'no agent process is running' 'Yellow'
}

# Stopping the task first is not enough on its own: the task launches a
# PowerShell wrapper, so /End kills the wrapper and leaves python.exe behind.
# That is the whole reason a stale process can outlive an update.
Say 'stopping the scheduled task'
schtasks /End /TN $agentTask 2>$null | Out-Null

foreach ($p in $before) {
    try {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
        Say ("killed pid {0}" -f $p.ProcessId) 'Yellow'
    } catch {
        Say ("could not kill pid {0}: {1}" -f $p.ProcessId, $_) 'Red'
    }
}

# The replacement cannot bind while the old one still holds the port.
for ($i = 0; $i -lt 15; $i++) {
    if (-not (Get-AgentProcs).Count) { break }
    Start-Sleep -Seconds 1
}
if ((Get-AgentProcs).Count) {
    Say 'a process survived the kill - stop it by hand before continuing' 'Red'
    exit 1
}

Say 'starting the agent task'
Start-ScheduledTask -TaskName $agentTask

# --- verify, rather than assume ---------------------------------------------
$envFile = Join-Path $InstallDir '.env.mt5'
if (-not (Test-Path $envFile)) {
    Say "no $envFile - cannot check the agent answers" 'Yellow'
    exit 0
}
$token = ((Get-Content $envFile | Select-String '^MT5_TOKEN=').Line -split '=', 2)[1]

$health = $null
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 2
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/health" `
            -Headers @{ Authorization = "Bearer $token" } -TimeoutSec 10
        if ($health) { break }
    } catch { }
}

if (-not $health) {
    Say 'the agent did not come back up' 'Red'
    foreach ($log in 'agent.err.log', 'agent.out.log') {
        $f = Join-Path $InstallDir $log
        if (Test-Path $f) {
            Say "--- $log (last 20 lines) ---" 'Yellow'
            Get-Content $f -Tail 20 | ForEach-Object { Write-Host "    $_" }
        }
    }
    exit 1
}

$now = Get-AgentProcs
Say ("agent answering on pid {0}" -f ($now.ProcessId -join ', ')) 'Green'
if ($health.code -eq $health.on_disk) {
    Say ("running {0}, which is what is on disk" -f $health.code) 'Green'
} else {
    Say ("still mismatched: running {0}, disk has {1}" -f $health.code, $health.on_disk) 'Red'
    Say 'the agent is starting from somewhere other than this folder' 'Red'
    exit 1
}
