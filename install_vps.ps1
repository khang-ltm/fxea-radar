# One-time installer for the read-only MT5 monitor. Run ON the VPS, as Administrator.
#
#   powershell -ExecutionPolicy Bypass -File install_vps.ps1
#
# Needs nothing preinstalled: no git, no winget, no Python. It fetches the code as
# a ZIP over HTTPS and installs Python 3.12 itself if the VPS has none.
#
# What it does:
#   * downloads this repo to C:\fxea-radar (keeping any existing .env.mt5)
#   * ensures Python 3.12, creates a venv, installs the MetaTrader5 package
#   * generates an access token
#   * registers a scheduled task: starts at boot, self-updates on every start
#   * starts the agent now, bound to 127.0.0.1
#
# What it deliberately does NOT do:
#   * never starts, restarts, closes or logs into MetaTrader 5
#   * never places, modifies or closes a trade - the agent has no such code path
#   * never opens a firewall port; remote access is an outbound tunnel only

param(
    [string]$InstallDir = 'C:\fxea-radar',
    [string]$ZipUrl = 'https://codeload.github.com/khang-ltm/fxea-radar/zip/refs/heads/main',
    [string]$PythonVersion = '3.12.10',
    [int]$Port = 8788,
    # Set by run_agent.ps1 on each boot: refresh code and deps only. Without this
    # the self-update would re-register the task and start a second agent.
    [switch]$SkipTask
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
function Say($msg, $color = 'Gray') { Write-Host "  $msg" -ForegroundColor $color }

Write-Host "`nMT5 monitor installer (read-only)" -ForegroundColor Cyan

if (Get-Process terminal64 -ErrorAction SilentlyContinue) {
    Say 'MetaTrader 5 is running - it will not be touched.' 'Green'
} else {
    Say 'No terminal64.exe running yet. Install continues; the agent attaches when MT5 is up.' 'Yellow'
}

# --- code: ZIP, so git is not required --------------------------------------
function Get-Code($dir, $url) {
    $tmpZip = Join-Path $env:TEMP 'fxea.zip'
    $tmpDir = Join-Path $env:TEMP 'fxea_unzip'
    Say "downloading code"
    Invoke-WebRequest -Uri $url -OutFile $tmpZip -UseBasicParsing
    if (Test-Path $tmpDir) { Remove-Item $tmpDir -Recurse -Force }
    Expand-Archive -Path $tmpZip -DestinationPath $tmpDir -Force
    $root = (Get-ChildItem $tmpDir -Directory | Select-Object -First 1).FullName
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    # copy code only - never clobber .env.mt5, .venv or data/
    foreach ($item in 'app', 'public', 'requirements.txt', 'install_vps.ps1', 'README.md') {
        $src = Join-Path $root $item
        if (Test-Path $src) { Copy-Item $src -Destination $dir -Recurse -Force }
    }
    Remove-Item $tmpZip, $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
}
Get-Code $InstallDir $ZipUrl
Set-Location $InstallDir

# --- python: install if absent ----------------------------------------------
function Resolve-Python {
    foreach ($c in @('py -3.12 -V', 'python -V')) {
        $exe, $rest = $c -split ' ', 2
        if (Get-Command $exe -ErrorAction SilentlyContinue) {
            $out = & cmd /c "$c 2>&1"
            if ($out -match 'Python 3\.(1[0-9]|[89])') { return $c -replace ' -V', '' }
        }
    }
    return $null
}

$pyCmd = Resolve-Python
if (-not $pyCmd) {
    Say "Python not found - installing $PythonVersion (silent)" 'Yellow'
    $exe = Join-Path $env:TEMP "python-$PythonVersion-amd64.exe"
    Invoke-WebRequest -Uri "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe" `
        -OutFile $exe -UseBasicParsing
    Start-Process -FilePath $exe -ArgumentList '/quiet', 'InstallAllUsers=1', 'PrependPath=1', 'Include_launcher=1' -Wait
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'User')
    $pyCmd = Resolve-Python
    if (-not $pyCmd) { Say 'Python install did not register. Reboot and re-run this script.' 'Red'; exit 1 }
    Say 'Python installed' 'Green'
}
Say "using: $pyCmd"

$py = Join-Path $InstallDir '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
    Say 'creating venv'
    & cmd /c "$pyCmd -m venv `"$(Join-Path $InstallDir '.venv')`""
}
Say 'installing MetaTrader5 package'
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet MetaTrader5

# --- token -------------------------------------------------------------------
$envFile = Join-Path $InstallDir '.env.mt5'
if (Test-Path $envFile) {
    Say 'reusing existing token from .env.mt5'
    $token = ((Get-Content $envFile | Select-String '^MT5_TOKEN=').Line -split '=', 2)[1]
} else {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $token = ([Convert]::ToBase64String($bytes) -replace '[^A-Za-z0-9]', '')
    "MT5_TOKEN=$token" | Set-Content -Path $envFile -Encoding utf8
    Say 'generated access token -> .env.mt5' 'Green'
}

if ($SkipTask) {
    Say 'self-update done (task untouched)' 'Green'
    exit 0
}

# --- boot task, self-updating ------------------------------------------------
$runner = Join-Path $InstallDir 'run_agent.ps1'
@"
# Started by Scheduled Task at boot: refresh code, then run the read-only agent.
`$ErrorActionPreference = 'Continue'
Set-Location '$InstallDir'
try {
    powershell -ExecutionPolicy Bypass -File '$InstallDir\install_vps.ps1' -SkipTask 2>&1 | Out-Null
} catch { }
`$env:MT5_TOKEN = ((Get-Content '$envFile' | Select-String '^MT5_TOKEN=').Line -split '=', 2)[1]
& '$py' -m app.mt5_agent --host 127.0.0.1 --port $Port
"@ | Set-Content -Path $runner -Encoding utf8

$taskName = 'fxea-mt5-agent'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`""
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 2) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -RunLevel Highest -Force | Out-Null
Say "scheduled task '$taskName' registered (boot start, auto-restart)" 'Green'

Get-Process python -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -like "$InstallDir*" } | Stop-Process -Force -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 8
try {
    $h = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 10
    Say "agent up. terminal detected: $($h.terminal_running)" 'Green'
} catch {
    Say "agent not answering on $Port yet. Check: Get-ScheduledTaskInfo $taskName" 'Yellow'
}

Write-Host "`nLocal URL on the VPS:  http://127.0.0.1:$Port" -ForegroundColor Cyan
Write-Host "Access token:          $token"
Write-Host @"

To reach it from your phone (no inbound port opened):

  cd `$env:TEMP
  Invoke-WebRequest https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe -OutFile cloudflared.exe
  .\cloudflared.exe tunnel login          # one browser login, once
  .\cloudflared.exe tunnel --url http://127.0.0.1:$Port

Then put Cloudflare Access (free) on that hostname so only your email can open it.
The token is a second lock, not a replacement for Access.
"@ -ForegroundColor Gray
