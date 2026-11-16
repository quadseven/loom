#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Restores agent lanes after reboot from state/lanes-snapshot.json
.DESCRIPTION
    Idempotent restore: waits for workspace server, starts agents with resume flags,
    handles awaiting-decision lanes, lists one-shot runs as unfinished.
.NOTES
    Run at startup via Task Scheduler or similar mechanism.
#>

param(
    [Parameter(Mandatory = $false)]
    [string]$SnapshotPath = "state/lanes-snapshot.json",

    [Parameter(Mandatory = $false)]
    [string]$LanesManifest = "lanes/win/lanes.example.json",

    [Parameter(Mandatory = $false)]
    [string]$WorkspaceCli = "agent",

    [Parameter(Mandatory = $false)]
    [int]$ServerWaitTimeoutSec = 60,

    [Parameter(Mandatory = $false)]
    [int]$ServerPollIntervalSec = 2
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Resolve-Root {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
    return Resolve-Path (Join-Path $scriptDir "..\..")
}

function Read-JsonFile {
    param([string]$Path)
    $fullPath = Join-Path (Resolve-Root) $Path
    if (-not (Test-Path $fullPath)) {
        return $null
    }
    $content = Get-Content $fullPath -Raw
    return $content | ConvertFrom-Json
}

function Wait-WorkspaceServer {
    param(
        [string]$Cli,
        [int]$TimeoutSec,
        [int]$PollIntervalSec
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    Write-Host "Waiting for workspace server (timeout: ${TimeoutSec}s)..."

    while ((Get-Date) -lt $deadline) {
        try {
            $result = & $Cli list --json 2>$null
            if ($LASTEXITCODE -eq 0 -and $result) {
                Write-Host "Workspace server is ready"
                return $true
            }
        } catch {
            # Server not ready yet
        }
        Start-Sleep -Seconds $PollIntervalSec
    }

    Write-Error "Workspace server did not become ready within ${TimeoutSec}s"
    return $false
}

function Is-LaneRunning {
    param(
        [string]$Cli,
        [string]$LaneName,
        [string]$Identity,
        [string]$Kind
    )

    try {
        $result = & $Cli list --json 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $result) {
            return $false
        }

        $sessions = $result | ConvertFrom-Json
        foreach ($session in $sessions) {
            if ($session.name -eq $LaneName -or $session.identity -eq $Identity -or $session.kind -eq $Kind) {
                return $true
            }
        }
    } catch {
        Write-Warning "Failed to check if lane $LaneName is running: $_"
    }
    return $false
}

function Start-Agent {
    param(
        [string]$Cli,
        [string]$LaneName,
        [string]$Kind,
        [string]$Cwd,
        [string]$Identity,
        [string]$Model,
        [string[]]$LaunchFlags,
        [string]$SessionId,
        [string]$RestorePolicy
    )

    Write-Host "Restoring lane: $LaneName ($Kind)"

    if ($RestorePolicy -ne "resume") {
        Write-Host "  Restore policy is '$RestorePolicy', skipping resume"
        return @{ success = $true; skipped = $true; reason = "policy" }
    }

    if (-not $SessionId) {
        Write-Warning "  No session ID in snapshot for $LaneName; cannot resume"
        return @{ success = $false; skipped = $true; reason = "no-session-id" }
    }

    if (Is-LaneRunning $Cli $LaneName $Identity $Kind) {
        Write-Host "  Lane $LaneName already running; skipping"
        return @{ success = $true; skipped = $true; reason = "already-running" }
    }

    $args = @("start", "--name", $LaneName, "--cwd", $Cwd, "--identity", $Identity)

    if ($Kind -eq "codex") {
        $args += "--resume", $SessionId
    } elseif ($Kind -eq "antigravity") {
        $args += "--conversation", $SessionId
    } elseif ($Kind -eq "claude") {
        $args += "--resume", $SessionId
    } else {
        Write-Warning "  Unknown kind '$Kind' for lane $LaneName; using generic resume"
        $args += "--resume", $SessionId
    }

    $args += $LaunchFlags

    Write-Host "  Starting: $Cli $($args -join ' ')"

    try {
        $result = & $Cli $args 2>&1
        $exitCode = $LASTEXITCODE
        if ($exitCode -eq 0) {
            Write-Host "  Started successfully"
            return @{ success = $true; skipped = $false }
        } else {
            Write-Error "  Failed to start: $result"
            return @{ success = $false; skipped = $false; error = $result }
        }
    } catch {
        Write-Error "  Exception starting lane: $_"
        return @{ success = $false; skipped = $false; error = $_.ToString() }
    }
}

function Handle-AwaitingDecision {
    param(
        [string]$Cli,
        [string]$LaneName,
        [string]$Kind,
        [string]$Identity
    )

    Write-Host "Lane $LaneName ($Kind) is awaiting decision"
    Write-Host "  Operator must decide: continue, release claim, or mark failed"

    $result = @{
        lane = $LaneName
        kind = $Kind
        identity = $Identity
        status = "awaiting-decision"
        action_required = $true
    }

    return $result
}

function List-UnfinishedOneShots {
    param(
        [string]$Cli,
        [object[]]$SnapshotLanes
    )

    $unfinished = @()

    foreach ($lane in $SnapshotLanes) {
        if ($lane.restore_policy -eq "oneshot") {
            $running = Is-LaneRunning $Cli $lane.name $lane.identity $lane.kind
            if (-not $running) {
                $unfinished += @{
                    name = $lane.name
                    kind = $lane.kind
                    cwd = $lane.cwd
                    identity = $lane.identity
                    brief = $lane.brief
                    status = "unfinished-oneshot"
                }
            }
        }
    }

    if ($unfinished.Count -gt 0) {
        Write-Host "One-shot runs requiring operator decision:"
        foreach ($u in $unfinished) {
            Write-Host "  $($u.name) ($($u.kind)) - brief: $($u.brief)"
        }
    }

    return $unfinished
}

# Main
$root = Resolve-Root
Push-Location $root

try {
    $snapshot = Read-JsonFile $SnapshotPath
    if (-not $snapshot) {
        Write-Host "No snapshot found at $SnapshotPath; nothing to restore"
        exit 0
    }

    Write-Host "Restoring from snapshot dated $($snapshot.timestamp)"

    if (-not (Wait-WorkspaceServer $WorkspaceCli $ServerWaitTimeoutSec $ServerPollIntervalSec)) {
        exit 1
    }

    $manifest = Read-JsonFile $LanesManifest
    if (-not $manifest) {
        Write-Error "Could not read lanes manifest"
        exit 1
    }

    $results = @{
        restored = @()
        skipped = @()
        failed = @()
        awaiting_decision = @()
        unfinished_oneshots = @()
    }

    foreach ($lane in $snapshot.lanes) {
        $manifestLane = $manifest.lanes | Where-Object { $_.name -eq $lane.name }
        if (-not $manifestLane) {
            Write-Warning "Lane $($lane.name) not in manifest; skipping"
            continue
        }

        $mergedLane = @{
            name = $lane.name
            kind = $lane.kind
            cwd = $lane.cwd
            identity = $lane.identity
            model = $lane.model
            launch_flags = $lane.launch_flags
            restore_policy = $lane.restore_policy
            session_id = $lane.session_id
            brief = $lane.brief
        }

        if ($mergedLane.restore_policy -eq "awaiting-decision") {
            $result = Handle-AwaitingDecision $WorkspaceCli $mergedLane.name $mergedLane.kind $mergedLane.identity
            $results.awaiting_decision += $result
            continue
        }

        if ($mergedLane.restore_policy -eq "oneshot") {
            continue
        }

        $startResult = Start-Agent `
            -Cli $WorkspaceCli `
            -LaneName $mergedLane.name `
            -Kind $mergedLane.kind `
            -Cwd $mergedLane.cwd `
            -Identity $mergedLane.identity `
            -Model $mergedLane.model `
            -LaunchFlags $mergedLane.launch_flags `
            -SessionId $mergedLane.session_id `
            -RestorePolicy $mergedLane.restore_policy

        if ($startResult.success) {
            if ($startResult.skipped) {
                $results.skipped += @{
                    lane = $mergedLane.name
                    reason = $startResult.reason
                }
            } else {
                $results.restored += $mergedLane.name
            }
        } else {
            $results.failed += @{
                lane = $mergedLane.name
                error = $startResult.error
            }
        }
    }

    $results.unfinished_oneshots = List-UnfinishedOneShots $WorkspaceCli $snapshot.lanes

    Write-Host ""
    Write-Host "=== Restore Summary ==="
    Write-Host "Restored: $($results.restored.Count)"
    Write-Host "Skipped: $($results.skipped.Count)"
    Write-Host "Failed: $($results.failed.Count)"
    Write-Host "Awaiting Decision: $($results.awaiting_decision.Count)"
    Write-Host "Unfinished One-shots: $($results.unfinished_oneshots.Count)"

    if ($results.failed.Count -gt 0) {
        Write-Error "Some lanes failed to restore"
        exit 1
    }

    exit 0
}
finally {
    Pop-Location
}