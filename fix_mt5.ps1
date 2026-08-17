# Diagnose + fix: install MetaTrader5 into the interpreter the agent actually runs.
$ErrorActionPreference = 'Continue'

Write-Host "`n=== agent process ===" -ForegroundColor Cyan
$proc = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like '*app.mt5_agent*' } | Select-Object -First 1
if ($proc) {
    Write-Host "  pid $($proc.ProcessId)"
    Write-Host "  cmd $($proc.CommandLine)"
    $exe = ($proc.CommandLine -split '"')[1]
    if (-not $exe -or -not (Test-Path $exe)) { $exe = ($proc.CommandLine -split ' ')[0] }
} else {
    Write-Host "  no running agent found"
    $exe = 'C:\fxea-radar\.venv\Scripts\python.exe'
}
Write-Host "  interpreter: $exe"

Write-Host "`n=== interpreter details ===" -ForegroundColor Cyan
cmd /c "`"$exe`" -c `"import sys,struct;print(sys.version);print('bits',struct.calcsize('P')*8);print(sys.executable)`" 2>&1"

Write-Host "`n=== installing MetaTrader5 into it ===" -ForegroundColor Cyan
cmd /c "`"$exe`" -m pip install MetaTrader5 2>&1" | Select-Object -Last 12

Write-Host "`n=== import check ===" -ForegroundColor Cyan
cmd /c "`"$exe`" -c `"import MetaTrader5;print('MetaTrader5', MetaTrader5.__version__)`" 2>&1"

Write-Host "`n=== restarting agent task ===" -ForegroundColor Cyan
Stop-ScheduledTask -TaskName 'fxea-mt5-agent' -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*app.mt5_agent*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-ScheduledTask -TaskName 'fxea-mt5-agent' -ErrorAction SilentlyContinue
Start-Sleep -Seconds 8
try {
    $r = Invoke-RestMethod -Uri 'http://127.0.0.1:8788/api/mt5' -Headers @{ Authorization = "Bearer $((Get-Content 'C:\fxea-radar\.env.mt5' | Select-String '^MT5_TOKEN=').Line.Split('=',2)[1])" } -TimeoutSec 15
    if ($r.ok) {
        Write-Host "  OK - account $($r.account.login) equity $($r.account.equity) $($r.account.currency), $($r.totals.positions) positions" -ForegroundColor Green
    } else {
        Write-Host "  agent still reports: $($r.error)" -ForegroundColor Yellow
    }
} catch {
    Write-Host "  agent not answering: $_" -ForegroundColor Yellow
}
