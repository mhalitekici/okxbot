# Operator-invoked only. The isolated Compose wrapper cannot select the Demo project.
$ErrorActionPreference = 'Stop'
$compose = Join-Path $PSScriptRoot 'live_compose.ps1'
$backupRoot = Join-Path $env:LOCALAPPDATA 'OKXTrading/live/backups'
if (-not (Test-Path -LiteralPath $backupRoot -PathType Container)) { throw 'Run prepare_live.ps1 first.' }
$backupName = 'okx-live-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N') + '.dump'
$insideFile = '/tmp/' + $backupName
$backupFile = Join-Path $backupRoot $backupName
& powershell -NoProfile -File $compose exec -T postgres-live pg_dump -U okx_live -d okx_live -Fc -f $insideFile
if ($LASTEXITCODE -ne 0) { throw 'Live pg_dump failed.' }
& powershell -NoProfile -File $compose cp "postgres-live:${insideFile}" $backupFile
if ($LASTEXITCODE -ne 0) { throw 'Live dump copy failed.' }
& powershell -NoProfile -File $compose exec -T postgres-live rm -- $insideFile
if ($LASTEXITCODE -ne 0) { throw 'Live temporary dump cleanup failed.' }
Write-Output "Live backup created: $backupFile (protect this file with encrypted storage)."
