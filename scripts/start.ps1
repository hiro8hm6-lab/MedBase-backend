param([switch]$Mock)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    $dockerBins = @(
        (Join-Path $env:LOCALAPPDATA 'Programs/DockerDesktop/resources/bin'),
        'C:\Program Files\Docker\Docker\resources\bin'
    )
    foreach ($dockerBin in $dockerBins) {
        if (Test-Path -LiteralPath (Join-Path $dockerBin 'docker.exe')) {
            $env:PATH = $dockerBin + [IO.Path]::PathSeparator + $env:PATH
            break
        }
    }
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Docker Desktop is required: https://docs.docker.com/desktop/setup/install/windows-install/'
}
docker info --format '{{.ServerVersion}}'
if ($LASTEXITCODE -ne 0) { throw 'Start Docker Desktop with the WSL 2 backend, then retry. Windows may require a restart after WSL installation.' }
if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
$composeArgs = @('compose', '-f', 'compose.yaml')
if ($Mock) { $composeArgs += @('-f', 'compose.mock.yaml') }
& docker @composeArgs up -d --build --wait
if ($LASTEXITCODE -ne 0) { throw 'Docker Compose startup failed. Check docker compose logs.' }
Write-Output 'MedBase API: http://127.0.0.1:8000/docs'
if ($Mock) {
    & docker @composeArgs exec -T api python scripts/smoke.py --directory /app/storage/smoke
    if ($LASTEXITCODE -ne 0) { throw 'Synthetic smoke test failed.' }
}
