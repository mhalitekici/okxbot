# Restore into a NEW verification database only. Never overwrite okx_live or Demo.
param([Parameter(Mandatory = $true)][string]$BackupFile)
$ErrorActionPreference = 'Stop'
$resolvedBackup = (Resolve-Path -LiteralPath $BackupFile).Path
if (-not (Test-Path -LiteralPath $resolvedBackup -PathType Leaf)) { throw 'Backup file is required.' }
$compose = Join-Path $PSScriptRoot 'live_compose.ps1'
$restoreDatabase = 'okx_live_restore_' + [guid]::NewGuid().ToString('N')
if ($restoreDatabase -notmatch '^okx_live_restore_[a-f0-9]{32}$') { throw 'Invalid restore target.' }
$insideFile = '/tmp/' + $restoreDatabase + '.dump'
& powershell -NoProfile -File $compose cp $resolvedBackup "postgres-live:${insideFile}"
if ($LASTEXITCODE -ne 0) { throw 'Live restore-check copy failed.' }
& powershell -NoProfile -File $compose exec -T postgres-live pg_restore --list $insideFile > $null
if ($LASTEXITCODE -ne 0) { throw 'Invalid PostgreSQL dump.' }
& powershell -NoProfile -File $compose exec -T postgres-live createdb -U okx_live $restoreDatabase
if ($LASTEXITCODE -ne 0) { throw 'Restore database creation failed; existing databases are never reused.' }
& powershell -NoProfile -File $compose exec -T postgres-live pg_restore -U okx_live -d $restoreDatabase --exit-on-error --no-owner $insideFile
if ($LASTEXITCODE -ne 0) { throw 'Live restore verification failed.' }
$provenanceQuery = "SELECT CASE WHEN (SELECT count(*) FROM persistent_settings WHERE id=1 AND environment='live')=1 AND NOT EXISTS (SELECT 1 FROM capital_cycles WHERE environment<>'live') AND NOT EXISTS (SELECT 1 FROM trade_orders) THEN 1 ELSE 0 END;"
$provenance = & powershell -NoProfile -File $compose exec -T postgres-live psql -U okx_live -d $restoreDatabase -Atc $provenanceQuery
if ($LASTEXITCODE -ne 0 -or ($provenance -join '').Trim() -ne '1') {
    throw 'Restore rejected: backup is not a bound Live observer database. Active databases unchanged.'
}
& powershell -NoProfile -File $compose exec -T postgres-live psql -U okx_live -d $restoreDatabase -c 'SELECT version_num FROM alembic_version; SELECT paused, emergency FROM runtime_state WHERE id=1;'
if ($LASTEXITCODE -ne 0) { throw 'Live restored state check failed.' }
& powershell -NoProfile -File $compose exec -T postgres-live rm -- $insideFile
if ($LASTEXITCODE -ne 0) { throw 'Live temporary restore dump cleanup failed.' }
Write-Output "Restored into isolated database $restoreDatabase. No bot started; active databases unchanged."
