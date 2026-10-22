# Promote a proven Live backup ONLY into a fresh, empty target. No drop/clean/overwrite.
param(
    [Parameter(Mandatory = $true)][string]$BackupFile,
    [switch]$ConfirmEmptyTarget
)
$ErrorActionPreference = 'Stop'
if (-not $ConfirmEmptyTarget) { throw 'Explicit -ConfirmEmptyTarget is required.' }
$compose = Join-Path $PSScriptRoot 'live_compose.ps1'
$resolvedBackup = (Resolve-Path -LiteralPath $BackupFile).Path
$backupHash = (Get-FileHash -LiteralPath $resolvedBackup -Algorithm SHA256).Hash
$running = & powershell -NoProfile -File $compose ps --status running -q live-observer
if ($LASTEXITCODE -ne 0 -or ($running -join '').Trim()) { throw 'Stop the Live observer before restore.' }
$countQuery = "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE';"
$tables = & powershell -NoProfile -File $compose exec -T postgres-live psql -U okx_live -d okx_live -Atc $countQuery
if ($LASTEXITCODE -ne 0 -or ($tables -join '').Trim() -ne '0') {
    throw 'Target okx_live must be empty. Existing schemas are never overwritten.'
}
& powershell -NoProfile -File (Join-Path $PSScriptRoot 'restore_live_check.ps1') -BackupFile $resolvedBackup
if ($LASTEXITCODE -ne 0) { throw 'Isolated restore/provenance verification failed.' }
if ((Get-FileHash -LiteralPath $resolvedBackup -Algorithm SHA256).Hash -ne $backupHash) {
    throw 'Backup changed during validation.'
}
$insideFile = '/tmp/okx-live-restore-' + [guid]::NewGuid().ToString('N') + '.dump'
& powershell -NoProfile -File $compose cp $resolvedBackup "postgres-live:${insideFile}"
if ($LASTEXITCODE -ne 0) { throw 'Verified Live backup copy failed.' }
& powershell -NoProfile -File $compose exec -T postgres-live pg_restore -U okx_live -d okx_live --exit-on-error --no-owner $insideFile
if ($LASTEXITCODE -ne 0) { throw 'Restore failed. Target may be partially populated; do not start observer.' }
& powershell -NoProfile -File $compose exec -T postgres-live rm -- $insideFile
if ($LASTEXITCODE -ne 0) { throw 'Temporary restore dump cleanup failed.' }
Write-Output 'Verified Live backup restored into fresh target. Run explicit migration; start paused only.'
