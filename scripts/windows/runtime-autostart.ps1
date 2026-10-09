<#
.SYNOPSIS
Manages the per-user logon task that starts the Automation Foundry runtime.

.DESCRIPTION
install    Registers the task "AutomationFoundryRuntime" for the current user. It needs
           no elevation, changes nothing outside Task Scheduler and does not start the
           runtime by itself.
uninstall  Removes the task. Runtime processes that are already running are untouched.
status     Shows the registered task and the runtime status.
run        What the task executes: waits for the Docker engine, then starts the
           supervisor, retrying a failed start a bounded number of times. A clean exit
           (for example after "automation-foundry-runtime stop") is never restarted.

The runtime still binds only to loopback and every external effect keeps its human
approval gate; this script only decides when the supervisor starts.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('install', 'uninstall', 'status', 'run')]
    [string]$Action
)

$ErrorActionPreference = 'Stop'

$TaskName = 'AutomationFoundryRuntime'
$RepositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Runtime = Join-Path $RepositoryRoot '.venv\Scripts\automation-foundry-runtime.exe'
$LogFile = Join-Path $RepositoryRoot 'storage\runtime\logs\autostart.log'
$BackupLock = Join-Path $RepositoryRoot 'storage\runtime\backup.lock'
$BackupWaitSeconds = 1800
$DockerWaitSeconds = 600
$MaxStartAttempts = 5

function Write-Log([string]$Message) {
    $directory = Split-Path $LogFile
    if (-not (Test-Path $directory)) { New-Item -ItemType Directory -Path $directory | Out-Null }
    $line = '{0} {1}' -f (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'), $Message
    Add-Content -Path $LogFile -Value $line
}

function Wait-ForBackup([int]$Seconds) {
    # The backup stops the runtime on purpose; starting it again mid-backup would corrupt it.
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Test-Path $BackupLock) -and ((Get-Date) -lt $deadline)) {
        $age = ((Get-Date) - (Get-Item $BackupLock).LastWriteTime).TotalMinutes
        if ($age -gt 120) { Write-Log 'stale backup lock ignored'; return }
        Write-Log 'backup in progress; waiting before starting the runtime'
        Start-Sleep -Seconds 20
    }
}

function Wait-ForDocker([int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        & docker info *> $null
        if ($LASTEXITCODE -eq 0) { return $true }
        Start-Sleep -Seconds 10
    }
    return $false
}

function Invoke-Run {
    if (-not (Test-Path $Runtime)) {
        Write-Log 'runtime executable not found; run the project setup first'
        exit 2
    }
    Set-Location $RepositoryRoot
    $current = (& $Runtime status 2>$null | Select-Object -First 1)
    if ($current -eq 'runtime: running') {
        Write-Log 'runtime already running; nothing to do'
        exit 0
    }
    Wait-ForBackup $BackupWaitSeconds
    if (-not (Wait-ForDocker $DockerWaitSeconds)) {
        Write-Log "docker engine not ready after $DockerWaitSeconds s; giving up"
        exit 1
    }
    for ($attempt = 1; $attempt -le $MaxStartAttempts; $attempt++) {
        Write-Log "starting runtime (attempt $attempt of $MaxStartAttempts)"
        & $Runtime start | ForEach-Object { Write-Log "runtime: $_" }
        $code = $LASTEXITCODE
        Write-Log "runtime exited with code $code"
        if ($code -eq 0) { exit 0 }
        if ($attempt -lt $MaxStartAttempts) { Start-Sleep -Seconds (30 * $attempt) }
    }
    Write-Log 'runtime did not start; see the service logs in storage/runtime/logs'
    exit 1
}

function Invoke-Install {
    $user = '{0}\{1}' -f $env:USERDOMAIN, $env:USERNAME
    $argument = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" run' -f $PSCommandPath
    $taskAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argument `
        -WorkingDirectory $RepositoryRoot
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $trigger.Delay = 'PT1M'
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $taskAction -Trigger $trigger `
        -Settings $settings -Principal $principal -Force `
        -Description 'Starts the Automation Foundry local runtime at logon (loopback only).' | Out-Null
    Write-Output "Task '$TaskName' registered for $user (runs one minute after logon)."
}

function Invoke-Uninstall {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "Task '$TaskName' removed."
    } else {
        Write-Output "Task '$TaskName' is not registered."
    }
}

function Invoke-Status {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Output "Task '$TaskName' is not registered."
    } else {
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        Write-Output ("Task '{0}': {1}; last run {2}; last result {3}" -f `
            $TaskName, $task.State, $info.LastRunTime, $info.LastTaskResult)
    }
    if (Test-Path $Runtime) { & $Runtime status }
}

switch ($Action) {
    'install' { Invoke-Install }
    'uninstall' { Invoke-Uninstall }
    'status' { Invoke-Status }
    'run' { Invoke-Run }
}
