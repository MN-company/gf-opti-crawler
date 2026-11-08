# PowerShell wrapper: one command for both Docker modes.
param([Parameter(ValueFromRemainingArguments=$true)][string[]]$CrawlerArgs)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not $CrawlerArgs -or $CrawlerArgs.Count -eq 0) {
    Write-Host 'Uso: .\crawler.ps1 run --limit 10 [--user-agent valore] | stats | audit --deep | issues | export'
    exit 2
}
$withBrowser = $false
for ($i=0; $i -lt ($CrawlerArgs.Count - 1); $i++) {
    if ($CrawlerArgs[$i] -eq '--browser' -and $CrawlerArgs[$i+1] -eq 'auto') { $withBrowser = $true }
}
if ($withBrowser) {
    & docker compose --profile browser run --rm crawler-browser @CrawlerArgs
} else {
    & docker compose run --rm crawler @CrawlerArgs
}
exit $LASTEXITCODE
