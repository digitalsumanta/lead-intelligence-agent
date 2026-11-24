<#
.SYNOPSIS
  Back up the Lead Intelligence PostgreSQL database (Docker Compose stack) and prune old backups.

.DESCRIPTION
  Writes a compressed custom-format dump (restore with pg_restore) to backups\lead_agent_<timestamp>.dump,
  verifies it can be read, deletes backups older than -RetentionDays (always keeping the newest -KeepAtLeast),
  and appends a line to backups\backup.log. Exits non-zero on failure so Task Scheduler records it.

  If Google Drive for desktop is running, each dump is also copied to "My Drive\LeadIntelligenceBackups"
  (same retention). Override with -CopyTo <folder>, or disable with -CopyTo none.

  Store CREDENTIAL_ENCRYPTION_KEY separately from these files: provider keys inside the dump are encrypted
  with it.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\backup-db.ps1

.EXAMPLE
  Restore into the running stack (replaces current data):
  docker cp backups\lead_agent_<timestamp>.dump lead-intelligence-agent-database-1:/tmp/restore.dump
  docker exec lead-intelligence-agent-database-1 sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists /tmp/restore.dump'
#>
param(
    [int]$RetentionDays = 14,
    [int]$KeepAtLeast = 3,
    [string]$Container = "lead-intelligence-agent-database-1",
    # Off-site copy folder. Empty = auto-detect Google Drive for desktop ("<drive>:\My Drive").
    # Use -CopyTo none to disable.
    [string]$CopyTo = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$backupDir = Join-Path $root "backups"
$logFile = Join-Path $backupDir "backup.log"
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null

function Write-Log([string]$message) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $message
    Add-Content -Path $logFile -Value $line
    Write-Output $line
}

function Find-OffsiteFolder {
    if ($CopyTo -eq "none") { return $null }
    if ($CopyTo) { return $CopyTo }
    # Google Drive for desktop mounts a virtual drive containing "My Drive".
    foreach ($drive in Get-PSDrive -PSProvider FileSystem) {
        $myDrive = Join-Path $drive.Root "My Drive"
        if (Test-Path -LiteralPath $myDrive) { return (Join-Path $myDrive "LeadIntelligenceBackups") }
    }
    return $null
}

function Remove-OldDumps([string]$folder) {
    $dumps = Get-ChildItem -Path $folder -Filter "lead_agent_*.dump" | Sort-Object LastWriteTime -Descending
    $cutoff = (Get-Date).AddDays(-$RetentionDays)
    $dumps | Select-Object -Skip $KeepAtLeast | Where-Object { $_.LastWriteTime -lt $cutoff } | ForEach-Object {
        Remove-Item $_.FullName -Force
        Write-Log "DEL  $($_.Name) from $folder (older than $RetentionDays days)"
    }
}

try {
    $running = docker inspect -f "{{.State.Running}}" $Container 2>$null
    if ($LASTEXITCODE -ne 0 -or $running -ne "true") {
        throw "Database container '$Container' is not running (is Docker Desktop started?)"
    }

    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $name = "lead_agent_$stamp.dump"
    $target = Join-Path $backupDir $name

    # Dump inside the container and verify it there, then copy the file out
    # (copying avoids PowerShell mangling binary stdout).
    docker exec $Container sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f /tmp/backup.dump'
    if ($LASTEXITCODE -ne 0) { throw "pg_dump failed" }
    # No embedded double quotes: Windows PowerShell 5.1 strips them when calling native programs.
    $tables = docker exec $Container sh -c 'pg_restore -l /tmp/backup.dump | grep -c TABLE.DATA'
    if ($LASTEXITCODE -ne 0 -or [int]$tables -lt 1) { throw "Backup archive could not be read or contains no table data" }
    docker cp "${Container}:/tmp/backup.dump" $target | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "docker cp failed" }
    docker exec $Container rm -f /tmp/backup.dump | Out-Null
    $size = (Get-Item $target).Length
    if ($size -lt 1024) { throw "Backup file is unexpectedly small ($size bytes)" }

    $copyCheck = Get-Content -Path $target -Encoding Byte -TotalCount 5
    if ([System.Text.Encoding]::ASCII.GetString($copyCheck) -ne "PGDMP") { throw "Backup file is not a PostgreSQL custom-format archive" }

    Write-Log ("OK   {0} ({1:N0} KB, {2} tables)" -f $name, ($size / 1KB), $tables)

    # Retention: delete old dumps, but always keep the newest $KeepAtLeast.
    Remove-OldDumps $backupDir

    # Off-site copy. A failure here is logged but does not fail the backup itself.
    $offsite = Find-OffsiteFolder
    if ($null -eq $offsite) {
        if ($CopyTo -ne "none") { Write-Log "WARN off-site copy skipped: Google Drive (My Drive) not found - is Google Drive for desktop running?" }
    } else {
        try {
            New-Item -ItemType Directory -Force -Path $offsite | Out-Null
            Copy-Item -LiteralPath $target -Destination (Join-Path $offsite $name) -Force
            Write-Log "COPY $name -> $offsite"
            Remove-OldDumps $offsite
        } catch {
            Write-Log "WARN off-site copy to $offsite failed: $($_.Exception.Message)"
        }
    }
    exit 0
}
catch {
    Write-Log "FAIL $($_.Exception.Message)"
    exit 1
}
