<#
.SYNOPSIS
Weekly local backup of Automation Foundry: stop the runtime, back up, verify, start it again.

.DESCRIPTION
install    Registers the per-user weekly task "AutomationFoundryBackup" (Sunday 22:00 local
           time by default). Needs no elevation. Use -Destination to write bundles to a
           directory outside the system disk, for example an external drive.
uninstall  Removes the task. Existing bundles are never touched.
status     Shows the task, the bundle count and size, the newest bundle and free disk space.
run        What the task executes. It never deletes a bundle.

The application refuses to back up while the runtime supervisor is active, so "run" stops the
runtime, creates the bundle, verifies it independently and starts the runtime again, even when
the backup fails. The runtime is restarted only if it was running when the backup began. On any
failure it opens a local alert on the "platform-health" automation; the next successful backup
resolves it. Nothing leaves this computer.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('install', 'uninstall', 'status', 'run')]
    [string]$Action,

    [string]$Destination = '',

    [ValidateSet('Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday')]
    [string]$Day = 'Sunday',

    [string]$Time = '22:00'
)

$ErrorActionPreference = 'Stop'

$TaskName = 'AutomationFoundryBackup'
$RuntimeTaskName = 'AutomationFoundryRuntime'
$RepositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Runtime = Join-Path $RepositoryRoot '.venv\Scripts\automation-foundry-runtime.exe'
$Cli = Join-Path $RepositoryRoot '.venv\Scripts\automation-foundry.exe'
$LogFile = Join-Path $RepositoryRoot 'storage\runtime\logs\backup.log'
$LockFile = Join-Path $RepositoryRoot 'storage\runtime\backup.lock'
$DefaultBundleRoot = Join-Path $RepositoryRoot 'storage\backups'
$MinimumFreeGigabytes = 5
$AlertKey = 'backup-failed'
$AlertAutomation = 'platform-health'
$ActiveRunAttempts = 6
$ActiveRunWaitSeconds = 300

function Write-Log([string]$Message) {
    $directory = Split-Path $LogFile
    if (-not (Test-Path $directory)) { New-Item -ItemType Directory -Path $directory | Out-Null }
    $line = '{0} {1}' -f (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'), $Message
    Add-Content -Path $LogFile -Value $line
}

function Get-BundleRoot {
    if ($Destination) { return $Destination }
    return $DefaultBundleRoot
}

function Get-DatabaseSettings {
    $settings = @{ User = 'automation_foundry'; Database = 'automation_foundry' }
    $envFile = Join-Path $RepositoryRoot '.env'
    if (Test-Path $envFile) {
        foreach ($line in Get-Content $envFile) {
            if ($line -match '^POSTGRES_USER=(.+)$') { $settings.User = $Matches[1].Trim() }
            if ($line -match '^POSTGRES_DB=(.+)$') { $settings.Database = $Matches[1].Trim() }
        }
    }
    return $settings
}

function Invoke-Sql([string]$Statement) {
    $database = Get-DatabaseSettings
    $output = & docker exec automation-foundry-postgres psql -U $database.User -d $database.Database -At -c $Statement 2>&1
    if ($LASTEXITCODE -ne 0) { throw "database query failed" }
    return ($output | Out-String).Trim()
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

function Get-RuntimeState {
    return (& $Runtime status 2>$null | Select-Object -First 1)
}

function Wait-ForRuntimeStopped([int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if ((Get-RuntimeState) -eq 'runtime: stopped') { return $true }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Wait-ForRuntimeTaskIdle([int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        $task = Get-ScheduledTask -TaskName $RuntimeTaskName -ErrorAction SilentlyContinue
        if ($null -eq $task -or $task.State -ne 'Running') { return }
        Start-Sleep -Seconds 2
    }
}

function Test-ActiveRuns {
    $count = Invoke-Sql "select count(*) from runs where status in ('running','queued')"
    return [int]$count
}

function Send-FailureAlert([string]$Summary) {
    try {
        $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
        & $Cli record-alert --automation-slug $AlertAutomation --deduplication-key $AlertKey `
            --idempotency-key "$AlertKey`:$stamp" --title 'Backup semanal falhou' `
            --summary $Summary --severity error --source 'backup-task' *> $null
        if ($LASTEXITCODE -ne 0) { Write-Log 'could not record the failure alert' }
    } catch {
        Write-Log 'could not record the failure alert'
    }
}

function Resolve-FailureAlert {
    try {
        $id = Invoke-Sql "select id from platform_alerts where deduplication_key = '$AlertKey' and status <> 'resolved' order by id desc limit 1"
        if ($id) {
            & $Cli set-alert --alert-id $id --resolve --actor 'backup-task' `
                --reason 'Um backup posterior foi criado e verificado.' *> $null
            Write-Log "resolved failure alert $id"
        }
    } catch {
        Write-Log 'could not resolve the failure alert'
    }
}

function Invoke-Run {
    $wasRunning = $false
    $stoppedRuntime = $false
    $lockHeld = $false
    $failure = $null
    try {
        if (-not (Test-Path $Runtime) -or -not (Test-Path $Cli)) { throw 'application executables not found' }
        $bundleRoot = Get-BundleRoot
        if (-not (Test-Path $bundleRoot)) {
            if ($Destination) { throw "backup destination does not exist: $bundleRoot" }
            New-Item -ItemType Directory -Path $bundleRoot | Out-Null
        }
        $free = (Get-Item $bundleRoot).PSDrive.Free / 1GB
        if ($free -lt $MinimumFreeGigabytes) {
            throw ('only {0:N1} GB free on the backup drive; at least {1} GB required' -f $free, $MinimumFreeGigabytes)
        }
        if (-not (Wait-ForDocker 120)) { throw 'docker engine not ready' }

        $wasRunning = ((Get-RuntimeState) -eq 'runtime: running')
        Write-Log "backup started (runtime running: $wasRunning, destination: $bundleRoot)"

        if ($wasRunning) {
            $active = -1
            for ($attempt = 1; $attempt -le $ActiveRunAttempts; $attempt++) {
                $active = Test-ActiveRuns
                if ($active -eq 0) { break }
                Write-Log "$active run(s) still running or queued (check $attempt of $ActiveRunAttempts)"
                if ($attempt -lt $ActiveRunAttempts) { Start-Sleep -Seconds $ActiveRunWaitSeconds }
            }
            if ($active -ne 0) { throw 'runs stayed active; backup skipped so no work is interrupted' }
        }

        Set-Content -Path $LockFile -Value (Get-Date).ToUniversalTime().ToString('o')
        $lockHeld = $true

        if ($wasRunning) {
            & $Runtime stop *> $null
            $stoppedRuntime = $true
            if (-not (Wait-ForRuntimeStopped 180)) { throw 'runtime did not stop in time' }
            Wait-ForRuntimeTaskIdle 60
            Write-Log 'runtime stopped'
        }

        $arguments = @('backup', '--json')
        if ($Destination) { $arguments += @('--destination', $Destination) }
        $started = Get-Date
        $created = (& $Cli @arguments | Out-String) | ConvertFrom-Json
        if (-not $created.ok) { throw 'backup command reported failure' }
        $verified = (& $Cli verify-backup $created.bundle_path --json | Out-String) | ConvertFrom-Json
        if (-not $verified.ok -or $verified.backup_id -ne $created.backup_id) {
            throw 'independent verification failed'
        }
        Write-Log ('backup {0} verified: {1} artifacts, {2:N1} MB, {3:N1}s' -f `
            $created.backup_id, $created.artifact_count, ($created.total_bytes / 1MB), ((Get-Date) - $started).TotalSeconds)
    } catch {
        $failure = $_.Exception.Message
        Write-Log "backup failed: $failure"
    } finally {
        if ($lockHeld) { Remove-Item -Path $LockFile -Force -ErrorAction SilentlyContinue }
        if ($stoppedRuntime) {
            Start-ScheduledTask -TaskName $RuntimeTaskName
            Write-Log 'runtime start requested'
        }
    }
    if ($null -ne $failure) {
        Send-FailureAlert "O backup semanal falhou e nenhum bundle novo foi publicado: $failure. Consulte storage/runtime/logs/backup.log."
        exit 1
    }
    Resolve-FailureAlert
    exit 0
}

function Invoke-Install {
    $user = '{0}\{1}' -f $env:USERDOMAIN, $env:USERNAME
    $argument = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" run' -f $PSCommandPath
    if ($Destination) { $argument += (' -Destination "{0}"' -f $Destination) }
    $taskAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argument `
        -WorkingDirectory $RepositoryRoot
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Day -At $Time
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $taskAction -Trigger $trigger `
        -Settings $settings -Principal $principal -Force `
        -Description 'Weekly verified backup of Automation Foundry (stops the runtime briefly).' | Out-Null
    $where = if ($Destination) { $Destination } else { $DefaultBundleRoot }
    Write-Output "Task '$TaskName' registered: every $Day at $Time, bundles in $where."
}

function Invoke-Uninstall {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "Task '$TaskName' removed. Existing bundles were not touched."
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
        Write-Output ("Task '{0}': {1}; next run {2}; last run {3}; last result {4}" -f `
            $TaskName, $task.State, $info.NextRunTime, $info.LastRunTime, $info.LastTaskResult)
    }
    $bundleRoot = Get-BundleRoot
    if (Test-Path $bundleRoot) {
        $bundles = @(Get-ChildItem -Path $bundleRoot -Directory | Where-Object { $_.Name -notlike '.*' })
        $bytes = ($bundles | ForEach-Object { (Get-ChildItem $_.FullName -File | Measure-Object Length -Sum).Sum } | Measure-Object -Sum).Sum
        Write-Output ('Bundles in {0}: {1} ({2:N0} MB)' -f $bundleRoot, $bundles.Count, ($bytes / 1MB))
        $newest = $bundles | Sort-Object Name | Select-Object -Last 1
        if ($newest) { Write-Output ('Newest: {0} (written {1})' -f $newest.Name, $newest.LastWriteTime) }
        Write-Output ('Free space on that drive: {0:N1} GB' -f ((Get-Item $bundleRoot).PSDrive.Free / 1GB))
    } else {
        Write-Output "Bundle directory not found: $bundleRoot"
    }
}

switch ($Action) {
    'install' { Invoke-Install }
    'uninstall' { Invoke-Uninstall }
    'status' { Invoke-Status }
    'run' { Invoke-Run }
}
