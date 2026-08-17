# Give the MT5 agent a PERMANENT https URL, so the dashboard never needs the
# token or address re-entered. Run ON the VPS, as Administrator.
#
#   powershell -ExecutionPolicy Bypass -File install_public.ps1 -Hostname mt5.fxea-radar.linkpc.net
#
# Prerequisite you do once, in your DNS panel (DNSExit for linkpc.net):
#   A record   mt5.fxea-radar.linkpc.net  ->  <this VPS public IP>   TTL 5 min
#
# What this does:
#   * downloads Caddy, which gets a real Let's Encrypt certificate automatically
#   * reverse-proxies https://<hostname>/ to the agent on 127.0.0.1:8788
#   * opens inbound 80 and 443 (needed for the certificate and for access)
#   * registers Caddy as a scheduled task at boot
#
# Why this replaces cloudflared: quick tunnels hand out a new random hostname on
# every restart, which is what forced re-entering the URL. This hostname is yours
# and never changes.
#
# Exposure note: after this, the agent's read-only API is reachable from the
# internet and the bearer token is the only lock. It cannot place or close trades
# - there is no such endpoint - but anyone with the token can READ your account.

param(
    [Parameter(Mandatory = $true)][string]$Hostname,
    [int]$AgentPort = 8788,
    [string]$InstallDir = 'C:\fxea-radar',
    [string]$CaddyVersion = '2.11.4'
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
function Say($m, $c = 'Gray') { Write-Host "  $m" -ForegroundColor $c }

Write-Host "`nPublic HTTPS for the MT5 agent -> $Hostname" -ForegroundColor Cyan

# --- DNS sanity check --------------------------------------------------------
$myIp = (Invoke-RestMethod -Uri 'https://api.ipify.org?format=json' -TimeoutSec 20).ip
Say "this VPS public IP: $myIp"
try {
    $resolved = (Resolve-DnsName -Name $Hostname -Type A -Server 1.1.1.1 -ErrorAction Stop |
                 Where-Object Type -eq 'A').IPAddress
} catch { $resolved = $null }
Say "$Hostname resolves to: $(if ($resolved) { $resolved } else { '(nothing yet)' })"
if ($resolved -notcontains $myIp) {
    Say 'DNS does not point here yet. Add this record, wait for it, then re-run:' 'Yellow'
    Say "  A   $Hostname   ->   $myIp" 'Yellow'
    Say 'Certificate issuance will fail until it resolves.' 'Yellow'
    exit 1
}
Say 'DNS points here.' 'Green'

# --- caddy -------------------------------------------------------------------
$caddyDir = Join-Path $InstallDir 'caddy'
$caddy = Join-Path $caddyDir 'caddy.exe'
New-Item -ItemType Directory -Force -Path $caddyDir | Out-Null
if (-not (Test-Path $caddy)) {
    $zip = Join-Path $env:TEMP "caddy_$CaddyVersion.zip"
    Say "downloading Caddy $CaddyVersion"
    Invoke-WebRequest -UseBasicParsing -OutFile $zip `
        -Uri "https://github.com/caddyserver/caddy/releases/download/v$CaddyVersion/caddy_${CaddyVersion}_windows_amd64.zip"
    Expand-Archive -Path $zip -DestinationPath $caddyDir -Force
    Remove-Item $zip -Force
}
Say "caddy: $((& $caddy version) -join ' ')"

# --- config ------------------------------------------------------------------
$caddyfile = Join-Path $caddyDir 'Caddyfile'
@"
$Hostname {
	encode zstd gzip
	reverse_proxy 127.0.0.1:$AgentPort
}
"@ | Set-Content -Path $caddyfile -Encoding ascii
Say "wrote $caddyfile"

# --- firewall: 80 for the ACME challenge, 443 for traffic --------------------
foreach ($p in 80, 443) {
    $name = "fxea-caddy-$p"
    Remove-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue
    New-NetFirewallRule -DisplayName $name -Direction Inbound -Action Allow `
        -Protocol TCP -LocalPort $p -Profile Any | Out-Null
}
Say 'firewall: inbound 80 and 443 allowed' 'Green'

# --- boot task ---------------------------------------------------------------
$taskName = 'fxea-caddy'
$action = New-ScheduledTaskAction -Execute $caddy -Argument "run --config `"$caddyfile`"" -WorkingDirectory $caddyDir
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 2) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -RunLevel Highest -Force | Out-Null
Get-Process caddy -ErrorAction SilentlyContinue | Stop-Process -Force
Start-ScheduledTask -TaskName $taskName
Say "scheduled task '$taskName' registered and started" 'Green'

Say 'waiting for the certificate (usually 10-40s)...'
$ok = $false
foreach ($i in 1..12) {
    Start-Sleep -Seconds 6
    try {
        $r = Invoke-WebRequest -Uri "https://$Hostname/api/health" -UseBasicParsing -TimeoutSec 15
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch { }
}

$token = ((Get-Content (Join-Path $InstallDir '.env.mt5') | Select-String '^MT5_TOKEN=').Line -split '=', 2)[1]
if ($ok) {
    Write-Host "`nLive: https://$Hostname/" -ForegroundColor Green
    Write-Host "One-time setup link for the dashboard (open it once on each device):" -ForegroundColor Cyan
    Write-Host "  https://fxea-radar.linkpc.net/?tab=mt5&mt5=https://$Hostname&mt5token=$token"
    Write-Host "`nThat address never changes, so you will not be asked for it again." -ForegroundColor Gray
} else {
    Write-Host "`nNot answering on https yet." -ForegroundColor Yellow
    Say 'check: Get-Content $env:USERPROFILE\AppData\Roaming\Caddy\*.log -Tail 30' 'Yellow'
    Say 'common causes: port 80 blocked by the provider firewall, or DNS not propagated' 'Yellow'
}
