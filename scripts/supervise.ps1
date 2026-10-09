<#
.SYNOPSIS
  Keep one taotrader command running (the tao-paper scheduled task; DESIGN.md section 12.1).

.DESCRIPTION
  Starts `scripts\run.cmd <args>` and restarts it 60 seconds after it exits, except when the exit code says a
  restart cannot help:
    0  clean stop (e.g. --max-minutes)      -> stop supervising
    2  usage / configuration / missing secret -> stop (fix the config first)
    3  live gate refused                      -> stop
    4  another instance holds the lock        -> stop (one instance per (mode, run))
    5  replay divergence                      -> stop (see docs\runbooks\crash-recovery.md)
    1  the run ended with an invariant breach or orphan -> stop (the books need a human)
  Any other exit (an unhandled crash, a killed process) is restarted: recovery is journal replay plus gap fill.

  Each restart is logged to logs\supervise.log. The script never changes system settings and never elevates.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\supervise.ps1 paper --config config\books.paper.toml
#>
# No param() block on purpose: every argument (including --options) is passed through verbatim via $args.
$TaoArgs = @($args)
$RestartDelaySeconds = 60
if ($env:TAOTRADER_SUPERVISE_DELAY_S) { $RestartDelaySeconds = [int]$env:TAOTRADER_SUPERVISE_DELAY_S }

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Run = Join-Path $PSScriptRoot 'run.cmd'
$LogDir = Join-Path $Root 'logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir 'supervise.log'
$NoRestart = @(0, 1, 2, 3, 4, 5)

if ($TaoArgs.Count -eq 0) {
    Write-Error 'supervise.ps1: give the taotrader command, e.g. paper --config config\books.paper.toml'
    exit 2
}

function Write-SupLog([string]$Message) {
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-ddTHH:mm:ssK'), $Message
    Add-Content -Path $Log -Value $line -Encoding utf8
}

while ($true) {
    Write-SupLog ("start: taotrader {0}" -f ($TaoArgs -join ' '))
    & $Run @TaoArgs
    $rc = $LASTEXITCODE
    Write-SupLog ("exit {0}" -f $rc)
    if ($NoRestart -contains $rc) {
        Write-SupLog ("exit code {0} is not restarted (see the taotrader log)" -f $rc)
        exit $rc
    }
    Start-Sleep -Seconds $RestartDelaySeconds
}
