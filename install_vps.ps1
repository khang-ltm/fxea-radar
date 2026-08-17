# One-time installer for the read-only MT5 monitor, run ON the VPS.
#
#   powershell -ExecutionPolicy Bypass -File install_vps.ps1
#
# What it does:
#   * clones/updates this repo into C:\fxea-radar
#   * creates a venv and installs the MetaTrader5 package
#   * generates an access token and writes .env.mt5
#   * registers a scheduled task so the agent starts at boot and self-updates
#   * starts the agent now, bound to 127.0.0.1
#
# What it deliberately does NOT do:
#   * never starts, restarts, closes or logs into MetaTrader 5
#   * never places, modifies or closes a trade - the agent has no such code
#   * never opens a firewall port; remote access is via an outbound tunnel only

param(
    [string]$InstallDir = 'C:\fxea-radar',
    [string]$Repo = 'https://github.com/khang-ltm/fxea-radar.git',
    [int]$Port = 8788
)

$ErrorActionPreference = 'Stop'

function Say($msg, $color = 'Gray') { Write-Host "  $msg" -ForegroundColor $color }
Write-Host "`nMT5 monitor installer (read-only)" -ForegroundColor Cyan

# --- prerequisites -----------------------------------------------------------
foreach ($tool in 'git', 'py') {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Say "$tool is missing. Install it first:" 'Yellow'
        if ($tool -eq 'git') { Say '  winget install --id Git.Git' 'Yellow' }
        else { Say '  winget install --id Python.Python.3.12' 'Yellow' }
        exit 1
    }
}

if (-not (Get-Process terminal64 -ErrorAction SilentlyContinue)) {
    Say 'Warning: no terminal64.exe running. Install continues; the agent will attach once MT5 is up.' 'Yellow'
} else {
    Say 'MetaTrader 5 detected and running (it will not be touched).' 'Green'
}

# --- code --------------------------------------------------------------------
if (Test-Path (Join-Path $InstallDir '.git')) {
    Say "updating $InstallDir"
    git -C $InstallDir pull --ff-only
} else {
    Say "cloning into $InstallDir"
    git clone --depth 1 $Repo $InstallDir
}
Set-Location $InstallDir

# --- python env --------------------------------------------------------------
$py = Join-Path $InstallDir '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
    Say 'creating venv'
    py -3.12 -m venv (Join-Path $InstallDir '.venv')
}
Say 'installing MetaTrader5 package'
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet MetaTrader5

# --- token -------------------------------------------------------------------
$envFile = Join-Path $InstallDir '.env.mt5'
if (Test-Path $envFile) {
    Say 'reusing existing token in .env.mt5'
    $token = ((Get-Content $envFile | Select-String '^MT5_TOKEN=').Line -split '=', 2)[1]
} else {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $token = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '').Replace('/', '')
    "MT5_TOKEN=$token" | Set-Content -Path $envFile -Encoding utf8
    Say 'generated a new access token -> .env.mt5' 'Green'
}

# --- boot task (self-updating) ----------------------------------------------
$runner = Join-Path $InstallDir 'run_agent.ps1'
@"
# Started by Scheduled Task at boot. Pulls the latest code, then runs the agent.
Set-Location '$InstallDir'
try { git pull --ff-only } catch { Write-Host "git pull skipped: `$_" }
`$env:MT5_TOKEN = ((Get-Content '.env.mt5' | Select-String '^MT5_TOKEN=').Line -split '=', 2)[1]
& '$py' -m app.mt5_agent --host 127.0.0.1 --port $Port
"@ | Set-Content -Path $runner -Encoding utf8

$taskName = 'fxea-mt5-agent'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`""
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 2) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -RunLevel Highest -Force | Out-Null
Say "scheduled task '$taskName' registered (starts at boot, restarts on failure)" 'Green'

# --- start now ---------------------------------------------------------------
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 6
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 10
    Say "agent is up: terminal_running=$($health.terminal_running)" 'Green'
} catch {
    Say "agent not answering yet on port $Port - check: Get-ScheduledTaskInfo $taskName" 'Yellow'
}

Write-Host "`nLocal URL on this VPS:  http://127.0.0.1:$Port" -ForegroundColor Cyan
Write-Host "Access token:           $token"
Write-Host @"

To reach it from your phone, run a tunnel (no inbound port needed):

  winget install --id Cloudflare.cloudflared
  cloudflared tunnel login              # one browser login, once
  cloudflared tunnel --url http://127.0.0.1:$Port

Then protect that hostname with Cloudflare Access (free) so only your email can
open it. The token above is a second lock, not a substitute for Access.
"@ -ForegroundColor Gray
