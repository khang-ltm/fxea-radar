# One-time installer for the read-only MT5 monitor. Run ON the VPS, as Administrator.
#
#   powershell -ExecutionPolicy Bypass -File install_vps.ps1
#
# Needs nothing preinstalled: no git, no winget, no Python. It fetches the code as
# a ZIP over HTTPS and installs Python 3.12 itself if the VPS has none.
#
# What it does:
#   * downloads this repo to C:\fxea-radar (keeping any existing .env.mt5)
#   * ensures Python 3.12, creates a venv, installs MetaTrader5 and telethon
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
    # MetaTrader5 ships wheels for 3.8-3.12 only; a 3.13+ interpreter installs the
    # venv fine and then fails on the package, so prefer an explicit 3.12/3.11/3.10.
    foreach ($c in @('py -3.12 -V', 'py -3.11 -V', 'py -3.10 -V', 'python -V')) {
        $exe = ($c -split ' ')[0]
        if (Get-Command $exe -ErrorAction SilentlyContinue) {
            $out = & cmd /c "$c 2>&1"
            if ($out -match 'Python 3\.(8|9|10|11|12)\b') { return ($c -replace ' -V', '') }
            Say "skipping '$exe' -> $out (needs Python 3.8-3.12 for MetaTrader5)" 'Yellow'
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
# cmd /c throughout: PowerShell turns a native exe's stderr into NativeCommandError
# noise, which hid the real pip failure last time.
$venvVer = (& cmd /c "`"$py`" -V 2>&1") -join ' '
Say "venv interpreter: $venvVer"

$pipLog = Join-Path $InstallDir 'pip-mt5.log'
Say 'installing MetaTrader5 package'
& cmd /c "`"$py`" -m pip install --upgrade pip >nul 2>&1"
# numpy pinned to 1.26.4: numpy 2.x is built for the x86-64-v2 CPU baseline and
# raises "your machine doesn't support (X86_V2)" on older VPS CPUs, which breaks
# MetaTrader5 too since it imports numpy.
# telethon as well: the agent installs EA files straight from the channel, and
# app/tg_login_vps.py needs it for the one-time login
& cmd /c "`"$py`" -m pip install `"numpy==1.26.4`" MetaTrader5 `"telethon==1.36.0`" > `"$pipLog`" 2>&1"
Get-Content $pipLog -ErrorAction SilentlyContinue |
    Where-Object { $_ -match 'ERROR|error:|Successfully|already satisfied|no matching distribution' } |
    ForEach-Object { Say $_ }

# Verify rather than assume: a failed install previously left the agent running
# with no package, reporting "MetaTrader5 package not installed" over the tunnel.
$check = (& cmd /c "`"$py`" -c `"import MetaTrader5;print(MetaTrader5.__version__)`" 2>&1") -join ' '
if ($check -notmatch '^\s*\d+\.\d+') {
    Say 'MetaTrader5 is NOT importable in the venv.' 'Red'
    Say "  interpreter: $venvVer" 'Red'
    Say "  import said: $check" 'Red'
    Say "  full pip log: $pipLog" 'Yellow'
    Say '  MetaTrader5 needs 64-bit Python 3.8-3.12.' 'Yellow'
    Say "  fix: rmdir /s /q `"$InstallDir\.venv`"  then re-run this installer" 'Yellow'
    exit 1
}
Say "MetaTrader5 $check ready" 'Green'

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

# --- 7-Zip: the channel posts .rar, and Windows' own tar only reads rar4 -----
# Installed silently, and skipped when it is already there. Without it an
# install from the catalog fails on any rar5 archive, which is most of them.
$sevenZip = 'C:\Program Files-Zipz.exe'
if (-not (Test-Path $sevenZip)) {
    Say 'installing 7-Zip (needed to unpack .rar posts)'
    $exe = Join-Path $env:TEMP '7z-setup.exe'
    try {
        Invoke-WebRequest 'https://www.7-zip.org/a/7z2408-x64.exe' -OutFile $exe -UseBasicParsing
        Start-Process -FilePath $exe -ArgumentList '/S' -Wait
        Remove-Item $exe -Force -ErrorAction SilentlyContinue
        if (Test-Path $sevenZip) { Say '7-Zip installed' 'Green' }
        else { Say '7-Zip did not install - .rar archives will need extracting by hand' 'Yellow' }
    } catch {
        Say "7-Zip download failed: $_" 'Yellow'
    }
} else {
    Say '7-Zip already installed'
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

# --- watchdog: update + restart the agent without anyone logging in ----------
# The agent updates itself too, but that only works while it is healthy. This
# task runs independently every 20 minutes, so a wedged or crashed agent still
# gets new code and a restart.
$updater = Join-Path $InstallDir 'update_agent.ps1'
@"
`$ErrorActionPreference = 'Continue'
`$dir  = '$InstallDir'
`$mark = Join-Path `$dir 'data\.agent_version'
try {
    `$remote = (Invoke-RestMethod -Uri 'https://api.github.com/repos/khang-ltm/fxea-radar/commits/main' ``
        -Headers @{ 'User-Agent' = 'fxea-updater' } -TimeoutSec 30).sha
} catch { return }
`$local = if (Test-Path `$mark) { (Get-Content `$mark -Raw).Trim() } else { '' }

`$agentUp = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { `$_.CommandLine -like '*app.mt5_agent*' }).Count -gt 0

if (`$remote -eq `$local -and `$agentUp) { return }   # current and running: nothing to do

if (`$remote -ne `$local) {
    `$zip = Join-Path `$env:TEMP 'fxea_upd.zip'
    `$tmp = Join-Path `$env:TEMP 'fxea_upd'
    Invoke-WebRequest -Uri '$ZipUrl' -OutFile `$zip -UseBasicParsing
    if (Test-Path `$tmp) { Remove-Item `$tmp -Recurse -Force }
    Expand-Archive -Path `$zip -DestinationPath `$tmp -Force
    `$src = (Get-ChildItem `$tmp -Directory | Select-Object -First 1).FullName
    foreach (`$f in 'app', 'public', 'mql5') {
        if (Test-Path (Join-Path `$src `$f)) {
            Copy-Item (Join-Path `$src `$f) -Destination `$dir -Recurse -Force
        }
    }
    Remove-Item `$zip, `$tmp -Recurse -Force -ErrorAction SilentlyContinue
    # the agent promotes this itself once it is actually running, so a
    # restart that fails leaves the old value and this task tries again
    Set-Content -Path (Join-Path `$dir 'data\.agent_pending') -Value `$remote -Encoding utf8
}

Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { `$_.CommandLine -like '*app.mt5_agent*' } |
    ForEach-Object { Stop-Process -Id `$_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2
Start-ScheduledTask -TaskName '$taskName'
"@ | Set-Content -Path $updater -Encoding utf8

$wdName = 'fxea-mt5-updater'
$wdAction = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$updater`""
# schtasks, not New-ScheduledTaskTrigger: a repetition interval without a
# duration registers as a one-shot, and [TimeSpan]::MaxValue is rejected by Task
# Scheduler as out of range. /SC MINUTE /MO 20 is unambiguous.
schtasks /Delete /TN $wdName /F 2>$null | Out-Null
$wdCmd = "powershell.exe -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$updater`""
schtasks /Create /TN $wdName /TR $wdCmd /SC MINUTE /MO 20 /RL HIGHEST /F | Out-Null
Say "watchdog task '$wdName' registered (checks every 20 min, restarts if needed)" 'Green'

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
