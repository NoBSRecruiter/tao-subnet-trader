<#
.SYNOPSIS
  Register (or remove) the taotrader Windows Task Scheduler entries (DESIGN.md section 12.1). RUN BY THE USER.

.DESCRIPTION
  Registers, for the CURRENT user, without elevation and without storing a password (logon type Interactive: the
  tasks run while you are logged on, which is when paper trading runs anyway):

    tao-collect   daily 04:00 and at logon      scripts\run.cmd collect --catch-up
    tao-paper     at logon, kept running         scripts\supervise.ps1 paper --config config\books.paper.toml
                                                 (restarted 60 s after a crash; Task Scheduler also restarts the task
                                                 every minute on failure, up to 999 times)
    tao-nightly   daily 02:30                    scripts\nightly.ps1 -BackupRoot <BackupRoot>
                                                 (verify-journal, verify-lake, paper<->offline replay equality,
                                                 VACUUM INTO backups of journals and state to a second disk)
    tao-weekly    Sundays 05:00                  scripts\weekly.ps1 -BackupRoot <BackupRoot>
                                                 (robocopy of immutable lake chunks + verify-lake on the mirror,
                                                 weekly paper report)
    tao-doctor    daily 08:00                    scripts\run.cmd doctor

  Nothing else is changed: no power, Defender, Windows Update or firewall settings (the bot never changes system
  settings; docs\runbooks and README list the host settings you may apply by hand). Re-running the script replaces
  the tasks with the same names. -Unregister removes them. -WhatIf shows what would happen.

  The live adapter is NOT registered here: it runs on Linux/WSL under systemd (scripts\taotrader-live.service).

.PARAMETER BackupRoot
  A directory on a SECOND disk for nightly/weekly backups, e.g. D:\taotrader-backup. Required unless -Unregister.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register-tasks.ps1 -BackupRoot D:\taotrader-backup -WhatIf
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register-tasks.ps1 -BackupRoot D:\taotrader-backup
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register-tasks.ps1 -Unregister
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$BackupRoot,
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$PaperConfig = 'config\books.paper.toml',
    [string]$TaskPath = '\',
    [switch]$NoPaper,
    [switch]$Unregister
)

$ErrorActionPreference = 'Stop'
$Names = @('tao-collect', 'tao-paper', 'tao-nightly', 'tao-weekly', 'tao-doctor')

if ($Unregister) {
    foreach ($n in $Names) {
        $t = Get-ScheduledTask -TaskName $n -TaskPath $TaskPath -ErrorAction SilentlyContinue
        if ($null -ne $t -and $PSCmdlet.ShouldProcess($n, 'Unregister-ScheduledTask')) {
            Unregister-ScheduledTask -TaskName $n -TaskPath $TaskPath -Confirm:$false
            Write-Host "removed $n"
        }
    }
    exit 0
}

if (-not $BackupRoot) {
    Write-Error 'register-tasks.ps1: -BackupRoot <dir on a second disk> is required (e.g. D:\taotrader-backup)'
    exit 2
}
$RepoRoot = (Resolve-Path $RepoRoot).Path
$Run = Join-Path $RepoRoot 'scripts\run.cmd'
if (-not (Test-Path (Join-Path $RepoRoot '.venv\Scripts\python.exe'))) {
    Write-Error "no .venv in ${RepoRoot}: run 'uv sync --frozen --all-extras' first"
    exit 2
}
if ((Split-Path -Qualifier $BackupRoot) -eq (Split-Path -Qualifier $RepoRoot)) {
    Write-Warning "BackupRoot $BackupRoot is on the same drive as the repository; use a second disk for real backups"
}
if ($RepoRoot -match 'OneDrive') {
    Write-Warning 'the repository (and data\) is inside OneDrive: move it out (SQLite WAL and Parquet)'
}

$User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
$PowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

function New-PsAction([string]$Script, [string]$Arguments) {
    $file = Join-Path $RepoRoot "scripts\$Script"
    New-ScheduledTaskAction -Execute $PowerShell -WorkingDirectory $RepoRoot `
        -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$file`" $Arguments"
}

function Register-Tao([string]$Name, $Action, $Triggers, $Settings, [string]$Description) {
    if ($PSCmdlet.ShouldProcess("$TaskPath$Name", 'Register-ScheduledTask')) {
        Register-ScheduledTask -TaskName $Name -TaskPath $TaskPath -Action $Action -Trigger $Triggers `
            -Settings $Settings -Principal $Principal -Description $Description -Force | Out-Null
        Write-Host "registered $TaskPath$Name"
    }
}

$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $User

# ---- tao-collect: daily 04:00 and at logon; resumable (committed chunks are skipped)
$collect = New-ScheduledTaskAction -Execute $Run -Argument 'collect --catch-up --quiet' -WorkingDirectory $RepoRoot
$collectSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 23) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-Tao 'tao-collect' $collect @((New-ScheduledTaskTrigger -Daily -At '04:00'), $atLogon) $collectSettings `
    'taotrader: resume the archive collector to the finalized head (read-only JSON-RPC)'

# ---- tao-paper: at logon, kept running, restarted on failure every minute
if (-not $NoPaper) {
    $paper = New-PsAction 'supervise.ps1' "paper --config `"$PaperConfig`" --quiet"
    $paperSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -DontStopOnIdleEnd
    Register-Tao 'tao-paper' $paper @($atLogon) $paperSettings `
        'taotrader: paper trading on the live finalized feed (no real funds; single instance per run)'
}

# ---- tao-nightly: checks and VACUUM INTO backups
$nightly = New-PsAction 'nightly.ps1' "-BackupRoot `"$BackupRoot`" -PaperConfig `"$PaperConfig`""
$nightlySettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4) -AllowStartIfOnBatteries
Register-Tao 'tao-nightly' $nightly @((New-ScheduledTaskTrigger -Daily -At '02:30')) $nightlySettings `
    'taotrader: verify-journal, verify-lake, paper<->offline replay equality, VACUUM INTO backups'

# ---- tao-weekly: lake mirror + verify, weekly report
$weekly = New-PsAction 'weekly.ps1' "-BackupRoot `"$BackupRoot`" -PaperConfig `"$PaperConfig`""
$weeklySettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 8) -AllowStartIfOnBatteries
Register-Tao 'tao-weekly' $weekly @((New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At '05:00')) $weeklySettings `
    'taotrader: robocopy immutable lake chunks + verify-lake on the mirror, weekly paper report'

# ---- tao-doctor: daily health summary (the Linux live host runs `doctor --live` from its systemd timer instead)
$doctor = New-ScheduledTaskAction -Execute $Run -Argument 'doctor --quiet' -WorkingDirectory $RepoRoot
$doctorSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -AllowStartIfOnBatteries
Register-Tao 'tao-doctor' $doctor @((New-ScheduledTaskTrigger -Daily -At '08:00')) $doctorSettings `
    'taotrader: environment, heartbeat and run health checks'

Write-Host ''
Write-Host 'Host settings are yours to apply by hand (the bot never changes them): keep data\ outside OneDrive;'
Write-Host 'optionally exclude data\ from Defender real-time scanning; disable sleep on AC for the paper machine'
Write-Host '(powercfg /change standby-timeout-ac 0); set Windows Update active hours.'
