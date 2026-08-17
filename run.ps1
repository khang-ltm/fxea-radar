# fxea-radar helper.
#   .\run.ps1 serve    local server with real file downloads (http://127.0.0.1:8787)
#   .\run.ps1 sync     pull new messages now
#   .\run.ps1 latest   download the newest site built by GitHub Actions and open it
#   .\run.ps1 static   build the static site locally from the current store
#   .\run.ps1 login    one-time Telegram login
param([Parameter(Position = 0)][ValidateSet('install', 'login', 'sync', 'serve', 'latest', 'static')][string]$Task = 'serve')

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$py = Join-Path $root '.venv\Scripts\python.exe'

if (-not (Test-Path $py)) {
    Write-Host 'Creating venv...'
    py -3.12 -m venv (Join-Path $root '.venv')
    & $py -m pip install --upgrade pip
    & $py -m pip install -r (Join-Path $root 'requirements.txt')
}

if (-not (Test-Path (Join-Path $root '.env'))) {
    Copy-Item (Join-Path $root '.env.example') (Join-Path $root '.env')
    Write-Host "Created .env - fill TG_API_ID and TG_API_HASH from https://my.telegram.org, then re-run." -ForegroundColor Yellow
    exit 1
}

Set-Location $root
switch ($Task) {
    'install' { Write-Host 'Dependencies ready.' }
    'login' { & $py -m app.login }
    'sync' { & $py -m app.sync }
    'serve' { & $py -m app.server }
    'static' {
        & $py -m app.export_static
        Start-Process (Join-Path $root 'site\index.html')
    }
    'latest' {
        # Grab whatever GitHub Actions built last - no local sync needed.
        $out = Join-Path $root 'site-latest'
        if (Test-Path $out) { Remove-Item $out -Recurse -Force }
        $runId = (gh run list --workflow sync.yml --status success --limit 1 --json databaseId --jq '.[0].databaseId').Trim()
        if (-not $runId) { Write-Host 'No successful run yet.' -ForegroundColor Yellow; exit 1 }
        Write-Host "Downloading site from run $runId ..."
        gh run download $runId -n fxea-site -D $out
        Start-Process (Join-Path $out 'index.html')
    }
}
