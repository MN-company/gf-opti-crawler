param([switch]$Browser)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Docker Desktop non trovato nel PATH. Installa e avvia Docker Desktop (backend WSL2).'
}
docker info *> $null
if ($LASTEXITCODE -ne 0) { throw 'Docker non è avviato: avvia Docker Desktop e riprova.' }
New-Item -ItemType Directory -Path 'data' -Force | Out-Null
Write-Host 'Compilazione del crawler HTTP leggero...'
docker compose build crawler
if ($LASTEXITCODE -ne 0) { throw 'Build HTTP fallita.' }
if ($Browser) {
    Write-Host 'Compilazione del browser opzionale con Playwright/Chromium...'
    docker compose --profile browser build crawler-browser
    if ($LASTEXITCODE -ne 0) { throw 'Build Playwright fallita.' }
}
Write-Host 'Installazione completata. Consulta README.md per eseguire una prova da 10 URL.'
