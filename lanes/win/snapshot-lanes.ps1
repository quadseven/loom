#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Snapshots active agent lane sessions to state/lanes-snapshot.json
.DESCRIPTION
    Reads lanes.example.json, queries workspace CLI for each lane's active session,
    and writes an atomic snapshot. No tokens or credentials are ever written.
.NOTES
    Run periodically via Task Scheduler or similar mechanism.
#>

param(
    [Parameter(Mandatory = $false)]
    [string]$LanesManifest = "lanes/win/lanes.example.json",

    [Parameter(Mandatory = $false)]
    [string]$SnapshotPath = "state/lanes-snapshot.json",

    [Parameter(Mandatory = $false)]
    [string]$WorkspaceCli = "agent"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Resolve-Root {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
    return Resolve-Path (Join-Path $scriptDir "..\..")
}

function Read-LanesManifest {
    param([string]$Path)
    $fullPath = Join-Path (Resolve-Root) $Path
    if (-not (Test-Path $fullPath)) {
        Write-Error "Lanes manifest not found at $fullPath"
        exit 1
    }
    $content = Get-Content $fullPath -Raw
    return $content | ConvertFrom-Json
}

function Get-SessionId {
    param(
        [string]$LaneName,
        [string]$Kind,
        [string]$Identity
    )

    try {
        $result = & $WorkspaceCli list --json 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $result) {
            return $null
        }

        $sessions = $result | ConvertFrom-Json
        foreach ($session in $sessions) {
            if ($session.name -eq $LaneName -or $session.identity -eq $Identity -or $session.kind -eq $Kind) {
                return $session.id
            }
        }
    } catch {
        Write-Warning "Failed to query sessions for lane $LaneName: $_"
    }
    return $null
}

function Write-AtomicSnapshot {
    param(
        [string]$Path,
        [object]$Data
    )

    $fullPath = Join-Path (Resolve-Root) $Path
    $dir = Split-Path -Parent $fullPath
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }

    $json = $Data | ConvertTo-Json -Depth 10
    $tempPath = "$fullPath.tmp.$([Guid]::NewGuid())"

    try {
        Set-Content -Path $tempPath -Value $json -Encoding UTF8 -NoNewline
        Move-Item -Path $tempPath -Destination $fullPath -Force
    } catch {
        if (Test-Path $tempPath) {
            Remove-Item -Path $tempPath -Force -ErrorAction SilentlyContinue
        }
        throw
    }
}

# Main
$root = Resolve-Root
Push-Location $root

try {
    $manifest = Read-LanesManifest $LanesManifest
    $snapshot = @{
        timestamp = (Get-Date).ToString("o")
        lanes = @()
    }

    foreach ($lane in $manifest.lanes) {
        $sessionId = Get-SessionId $lane.name $lane.kind $lane.identity
        $laneSnapshot = @{
            name = $lane.name
            kind = $lane.kind
            cwd = $lane.cwd
            identity = $lane.identity
            model = $lane.model
            launch_flags = $lane.launch_flags
            restore_policy = $lane.restore_policy
            session_id = $sessionId
        }
        $snapshot.lanes += $laneSnapshot
    }

    Write-AtomicSnapshot $SnapshotPath $snapshot
    Write-Host "Snapshot written to $SnapshotPath"
}
finally {
    Pop-Location
}