# Register a daily Windows scheduled task for the current user.
param(
    [ValidatePattern('^([01]\d|2[0-3]):[0-5]\d$')][string]$At = '03:00',
    [switch]$Browser
)
$ErrorActionPreference = 'Stop'
$script = Join-Path $PSScriptRoot 'scheduled-run.ps1'
$arguments = '-NoProfile -NonInteractive -File "' + $script + '"'
if ($Browser) { $arguments += ' -Browser' }
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments -WorkingDirectory $PSScriptRoot
$when = [datetime]::Today.Add([TimeSpan]::Parse($At))
$trigger = New-ScheduledTaskTrigger -Daily -At $when
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 24)
Register-ScheduledTask -TaskName 'RecipeScraperDaily' -Description 'Raccolta incrementale ricette, audit e log' -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host ('Attivita giornaliera RecipeScraperDaily creata alle ' + $At + '. Per rimuoverla: Unregister-ScheduledTask -TaskName RecipeScraperDaily -Confirm:$false')
