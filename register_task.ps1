<#
    Register a Windows Scheduled Task that runs the GLaDOS check-in daily.

    Usage:
        powershell -ExecutionPolicy Bypass -File .\register_task.ps1 -Time 10:00

        -Time      Daily trigger time, 24h format, default 10:00
        -TaskName  Scheduled task name, default GLaDOS-Daily-Checkin
        -RunNow    Immediately start the task once after registering

    Notes:
        - Registered for the current user, runs without admin rights.
        - StartWhenAvailable means a missed run (PC off / asleep) fires on next wake.
        - Verify:  schtasks /Query /TN GLaDOS-Daily-Checkin /V /FO LIST
        - Remove:  schtasks /Delete /TN GLaDOS-Daily-Checkin /F
#>
param(
    [string]$Time = "10:00",
    [string]$TaskName = "GLaDOS-Daily-Checkin",
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$bat = Join-Path $root "run_checkin.bat"

if (-not (Test-Path $bat)) {
    throw "run_checkin.bat not found at: $bat"
}

if ($Time -notmatch '^\d{1,2}:\d{2}$') {
    throw "Invalid -Time '$Time'. Expected HH:mm, e.g. 10:00"
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "Removed existing task: $TaskName"
}

$action = New-ScheduledTaskAction -Execute $bat -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Daily GLaDOS check-in via glados_checkin.py (glados.cloud)" | Out-Null

Write-Output "Registered task '$TaskName' -> daily at $Time"
Write-Output "Script: $bat"

if ($RunNow) {
    Write-Output "Starting task now..."
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 5
    Get-ScheduledTaskInfo -TaskName $TaskName |
        Select-Object TaskName, LastRunTime, LastTaskResult, NextRunTime |
        Format-List
}

Write-Output "Verify : schtasks /Query /TN $TaskName /V /FO LIST"
Write-Output "Run now: schtasks /Run /TN $TaskName"
Write-Output "Delete : schtasks /Delete /TN $TaskName /F"
