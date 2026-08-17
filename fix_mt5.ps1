# Diagnose + fix the MT5 agent's Python environment on the VPS.
#
# Known issue this handles: NumPy 2.x is built for the x86-64-v2 CPU baseline.
# On older VPS CPUs it raises
#   RuntimeError: NumPy was built with baseline optimizations: (X86_V2) but your
#   machine doesn't support: (X86_V2)
# and because MetaTrader5 imports numpy, the whole package fails. NumPy 1.26.4 is
# the last release built for pre-v2 CPUs, so we pin to that.
$ErrorActionPreference = 'Continue'
$agentPort = 8788
$install = 'C:\fxea-radar'

Write-Host "`n=== agent process ===" -ForegroundColor Cyan
$proc = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like '*app.mt5_agent*' } | Select-Object -First 1
$exe = "$install\.venv\Scripts\python.exe"
if ($proc) {
    Write-Host "  pid $($proc.ProcessId)"
    $found = ($proc.CommandLine -split '"')[1]
    if ($found -and (Test-Path $found)) { $exe = $found }
} else {
    Write-Host '  no running agent'
}
Write-Host "  interpreter: $exe"
cmd /c "`"$exe`" -c `"import sys,struct;print(sys.version.split()[0],struct.calcsize('P')*8,'bit')`" 2>&1"

Write-Host "`n=== pinning numpy for this CPU ===" -ForegroundColor Cyan
cmd /c "`"$exe`" -m pip install --quiet --force-reinstall `"numpy==1.26.4`" 2>&1" | Select-Object -Last 6
cmd /c "`"$exe`" -c `"import numpy;print('numpy', numpy.__version__)`" 2>&1"

Write-Host "`n=== MetaTrader5 import ===" -ForegroundColor Cyan
$mt5 = cmd /c "`"$exe`" -c `"import MetaTrader5;print(MetaTrader5.__version__)`" 2>&1"
$mt5 | Select-Object -Last 4
if (($mt5 -join ' ') -notmatch '\d+\.\d+') {
    Write-Host '  MetaTrader5 still not importable - stopping here.' -ForegroundColor Red
    exit 1
}

Write-Host "`n=== restarting agent ===" -ForegroundColor Cyan
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*app.mt5_agent*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Stop-ScheduledTask -TaskName 'fxea-mt5-agent' -ErrorAction SilentlyContinue

# Start it directly, so any startup error is visible instead of hidden in a task.
$token = ((Get-Content "$install\.env.mt5" | Select-String '^MT5_TOKEN=').Line -split '=', 2)[1]
$env:MT5_TOKEN = $token
Start-Process -FilePath $exe -ArgumentList '-m', 'app.mt5_agent', '--host', '127.0.0.1', '--port', $agentPort `
    -WorkingDirectory $install -WindowStyle Hidden `
    -RedirectStandardOutput "$install\agent.out.log" -RedirectStandardError "$install\agent.err.log"
Start-Sleep -Seconds 10

try {
    $r = Invoke-RestMethod -Uri "http://127.0.0.1:$agentPort/api/mt5" `
        -Headers @{ Authorization = "Bearer $token" } -TimeoutSec 20
    if ($r.ok) {
        Write-Host "  OK - $($r.account.login) @ $($r.account.server)" -ForegroundColor Green
        Write-Host ("  equity {0} {1} | floating {2} | {3} positions, {4} orders" -f `
            $r.account.equity, $r.account.currency, $r.account.profit, $r.totals.positions, $r.totals.orders) -ForegroundColor Green
    } else {
        Write-Host "  agent says: $($r.error)" -ForegroundColor Yellow
    }
} catch {
    Write-Host "  agent not answering: $_" -ForegroundColor Yellow
    Write-Host '  --- agent.err.log ---' -ForegroundColor Yellow
    Get-Content "$install\agent.err.log" -Tail 15 -ErrorAction SilentlyContinue
}

Write-Host "`n=== re-arming the boot task ===" -ForegroundColor Cyan
Start-ScheduledTask -TaskName 'fxea-mt5-agent' -ErrorAction SilentlyContinue
Write-Host '  done. The tunnel window can stay as it is.' -ForegroundColor Gray
