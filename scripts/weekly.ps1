<#
.SYNOPSIS
  taotrader weekly lake mirror and report (the tao-weekly scheduled task; DESIGN.md section 12.1).

.DESCRIPTION
  1. robocopy the immutable lake chunks (data\lake and data\paper\lake) to -BackupRoot\lake and \paper-lake. Only
     new files are copied (/XC /XN /XO); nothing is ever deleted on either side.
  2. a fresh VACUUM INTO copy of each lake's manifest database (data\state.sqlite, data\paper\state.sqlite) next to
     the mirror, then `taotrader verify-lake` on the MIRROR (proves the backup is complete and uncorrupted).
  3. the weekly paper run summary (data quality, model drift, prune watch, fills and failures):
     reports\output\weekly-<yyyy-MM-dd>\summary.json and summary.html.
  The transcript is logs\weekly-<yyyy-MM-dd>.log; exit 1 when any step failed.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\weekly.ps1 -BackupRoot D:\taotrader-backup
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BackupRoot,
    [string]$PaperConfig = 'config\books.paper.toml',
    [string]$PaperRun = 'paper-main'
)

$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Run = Join-Path $PSScriptRoot 'run.cmd'
$Py = Join-Path $Root '.venv\Scripts\python.exe'
$Day = Get-Date -Format 'yyyy-MM-dd'
New-Item -ItemType Directory -Force -Path (Join-Path $Root 'logs') | Out-Null
$Log = Join-Path $Root "logs\weekly-$Day.log"
$failed = @()

function Step([string]$Name, [scriptblock]$Body, [int]$OkBelow = 1) {
    Add-Content -Path $Log -Encoding utf8 -Value ("== {0} {1}" -f (Get-Date -Format 'HH:mm:ss'), $Name)
    $out = & $Body 2>&1
    $rc = $LASTEXITCODE
    $out | ForEach-Object { Add-Content -Path $Log -Encoding utf8 -Value "$_" }
    Add-Content -Path $Log -Encoding utf8 -Value ("-- exit {0}" -f $rc)
    if ($rc -ge $OkBelow) { $script:failed += "$Name (exit $rc)" }
}

$pairs = @(
    @{ Key = 'main'; Src = 'data\lake'; Dst = (Join-Path $BackupRoot 'lake'); State = 'data\state.sqlite'; DstState = (Join-Path $BackupRoot 'state.sqlite') },
    @{ Key = 'paper'; Src = 'data\paper\lake'; Dst = (Join-Path $BackupRoot 'paper\lake'); State = 'data\paper\state.sqlite'; DstState = (Join-Path $BackupRoot 'paper\state.sqlite') }
)
foreach ($p in $pairs) {
    if (-not (Test-Path $p.Src)) { continue }
    # robocopy: exit codes 0-7 are success (8+ = failure)
    Step "robocopy $($p.Src)" { robocopy $p.Src $p.Dst /E /XC /XN /XO /R:2 /W:5 /NP /NFL /NDL } 8
    if (Test-Path $p.State) {
        $tmp = Join-Path $BackupRoot "_weekly\$Day\$($p.Key)"
        Step "manifest copy $($p.State)" {
            & $Py (Join-Path $PSScriptRoot 'backup_sqlite.py') --dest $tmp --base (Split-Path -Parent $p.State) $p.State
            if ($LASTEXITCODE -eq 0) { Copy-Item -Force (Join-Path $tmp 'state.sqlite') $p.DstState }
        }
    }
    Step "verify-lake mirror $($p.Dst)" { & $Run verify-lake --lake $p.Dst --quiet }
}
if (Test-Path "data\runs\$PaperRun\journal.sqlite") {
    Step 'weekly paper report' {
        & $Run report --run $PaperRun --config $PaperConfig --out "reports\output\weekly-$Day" --quiet
    }
}

if ($failed.Count -gt 0) {
    Add-Content -Path $Log -Encoding utf8 -Value ("WEEKLY FAILED: {0}" -f ($failed -join '; '))
    exit 1
}
Add-Content -Path $Log -Encoding utf8 -Value 'WEEKLY OK'
exit 0
