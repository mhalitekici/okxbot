# Create only empty Live directories; never read, generate or overwrite credentials.
$ErrorActionPreference = 'Stop'
if (-not $env:LOCALAPPDATA) { throw 'LOCALAPPDATA is required.' }
$liveRoot = Join-Path $env:LOCALAPPDATA 'OKXTrading/live'
foreach ($directory in @('data', 'logs', 'backups', 'identity')) {
    New-Item -ItemType Directory -Force -Path (Join-Path $liveRoot $directory) | Out-Null
}
New-Item -ItemType Directory -Force -Path (Join-Path $env:LOCALAPPDATA 'OKXTrading/secrets') | Out-Null
Write-Output 'Live directories prepared. Credentials unchanged; no services started.'
