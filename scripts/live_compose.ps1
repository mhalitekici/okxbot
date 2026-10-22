# Always select the isolated Live project and explicit NON-secret interpolation file.
# This wrapper never reads a credential file or displays resolved Compose configuration.
# Keep Docker flags verbatim: an advanced param() block would consume -d as -Debug.
$ComposeArgs = @($args)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $ComposeArgs -or $ComposeArgs.Count -eq 0) { throw 'A Live Compose command is required.' }
$checkedArgs = $ComposeArgs
# exec/run stop parsing Compose flags at the service; later -f/-p belong to pg_dump etc.
if ($ComposeArgs[0] -in @('exec', 'run')) {
    for ($index = 1; $index -lt $ComposeArgs.Count; $index++) {
        if ($ComposeArgs[$index] -in @('postgres-live', 'live-observer', 'live-migrate', 'live-identity', 'live-session')) {
            $checkedArgs = $ComposeArgs[0..$index]
            break
        }
    }
}
foreach ($argument in $checkedArgs) {
    if ($argument -in @('-f', '--file', '-p', '--project-name', '--env-file', '--project-directory', '-v', '--volumes') -or
        $argument -match '^(--file|--project-name|--env-file|--project-directory)=' -or
        $argument -match '^-[fp].+') {
        throw 'Project overrides and volume deletion are disabled in the Live wrapper.'
    }
}
if ($ComposeArgs[0] -notin @('config', 'build', 'up', 'stop', 'ps', 'logs', 'run', 'exec', 'cp', 'images')) {
    throw 'Unsupported Live maintenance command.'
}
if ($ComposeArgs[0] -eq 'config') { $ComposeArgs = @('config', '--quiet') }
& docker compose --project-directory $projectRoot --env-file "$projectRoot/config/live.compose.env" `
    --file "$projectRoot/docker-compose.live.yml" --project-name telegram-okx-live @ComposeArgs
exit $LASTEXITCODE
