param([switch]$Browser)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
New-Item -ItemType Directory -Path 'data' -Force | Out-Null
$logPath = Join-Path $PSScriptRoot ('data\daily-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
Start-Transcript -Path $logPath | Out-Null
try {
    # Backup weekly, before the new crawl. Avoid replacing an earlier backup.
    if ((Get-Date).DayOfWeek -eq 'Sunday') {
        $backup = 'data/backup-week-' + (Get-Date -Format 'yyyyMMdd') + '.sqlite3'
        if (-not (Test-Path -LiteralPath $backup)) {
            & (Join-Path $PSScriptRoot 'crawler.ps1') 'backup' '--output' $backup
            if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        }
    }
    $runArgs = @('pipeline', '--all', '--batch-size', '25', '--categories', '--category-limit', '50')
    if ($Browser) { $runArgs += @('--browser','auto') }
    & (Join-Path $PSScriptRoot 'crawler.ps1') @runArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & (Join-Path $PSScriptRoot 'crawler.ps1') 'audit' '--output' 'data/audit.json'
    exit $LASTEXITCODE
} finally {
    Stop-Transcript | Out-Null
}
