<#
.SYNOPSIS
  taotrader nightly checks and backups (the tao-nightly scheduled task; DESIGN.md section 12.1).

.DESCRIPTION
  1. verify-journal --all            hash chain of every run journal (and the heartbeat head)
  2. verify-lake                     chunk files against the manifest, for data\lake and data\paper\lake
  3. replay (paper <-> offline)      re-verifies the paper run's journaled decisions on a COPY of its journal from the
                                     recorded snapshots (byte-identical decisions or exit 5); nothing is written
  4. VACUUM INTO backups             journals and state databases to -BackupRoot\<yyyy-MM-dd>\ (a second disk)

  Every step runs even if an earlier one failed; the script exits 1 when any step failed, and the transcript is in
  logs\nightly-<yyyy-MM-dd>.log. A replay divergence after you changed code or config is expected until the paper
  run is restarted with --accept-drift (docs\runbooks\crash-recovery.md).

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\nightly.ps1 -BackupRoot D:\taotrader-backup
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BackupRoot,
    [string]$PaperConfig = 'config\books.paper.toml'
)

$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Run = Join-Path $PSScriptRoot 'run.cmd'
$Py = Join-Path $Root '.venv\Scripts\python.exe'
$Day = Get-Date -Format 'yyyy-MM-dd'
New-Item -ItemType Directory -Force -Path (Join-Path $Root 'logs') | Out-Null
$Log = Join-Path $Root "logs\nightly-$Day.log"
$failed = @()

function Step([string]$Name, [scriptblock]$Body) {
    Add-Content -Path $Log -Encoding utf8 -Value ("== {0} {1}" -f (Get-Date -Format 'HH:mm:ss'), $Name)
    $out = & $Body 2>&1
    $rc = $LASTEXITCODE
    $out | ForEach-Object { Add-Content -Path $Log -Encoding utf8 -Value "$_" }
    Add-Content -Path $Log -Encoding utf8 -Value ("-- exit {0}" -f $rc)
    if ($rc -ne 0) { $script:failed += "$Name (exit $rc)" }
}

Step 'verify-journal' { & $Run verify-journal --all --quiet }
foreach ($lake in @('data\lake', 'data\paper\lake')) {
    if (Test-Path $lake) { Step "verify-lake $lake" { & $Run verify-lake --lake $lake --quiet } }
}
if (Test-Path 'data\runs') {
    Step 'replay paper (verify copy)' { & $Run replay --config $PaperConfig --quiet }
}
$dest = Join-Path $BackupRoot $Day
Step "backup -> $dest" {
    & $Py (Join-Path $PSScriptRoot 'backup_sqlite.py') --dest $dest --base $Root `
        'data\runs\*\journal.sqlite' 'data\runs\*\state.sqlite' 'data\state.sqlite' 'data\paper\state.sqlite'
}

if ($failed.Count -gt 0) {
    Add-Content -Path $Log -Encoding utf8 -Value ("NIGHTLY FAILED: {0}" -f ($failed -join '; '))
    exit 1
}
Add-Content -Path $Log -Encoding utf8 -Value 'NIGHTLY OK'
exit 0
