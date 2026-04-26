# OpenClaw start script
# Usage: start.bat  (or directly: powershell -File start.ps1)
# Detailed runbook: docs/SYSTEM_RUNBOOK.md

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $ProjectRoot
. "$PSScriptRoot\_lib.ps1"

Write-Host "==========================================="
Write-Host "  OpenClaw - starting"
Write-Host "==========================================="
Write-Host ""

# ---- 0. Load config/launch.env (KEY=VAL) into process env ----
$envFile = Join-Path $ProjectRoot "config\launch.env"
if (Test-Path $envFile) {
    Write-Host "Loading config/launch.env..."
    $loaded = @()
    Get-Content $envFile | ForEach-Object {
        $line = $_
        if ($line -match '^\s*#' -or $line -match '^\s*$') { return }
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
            $key = $matches[1].Trim()
            $val = $matches[2].Trim().Trim('"').Trim("'")
            [Environment]::SetEnvironmentVariable($key, $val, 'Process')
            $loaded += "$key=$val"
        }
    }
    if ($loaded.Count -gt 0) {
        Write-Host ("   [OK] {0} var(s): {1}" -f $loaded.Count, ($loaded -join ', ')) -ForegroundColor DarkGray
    }
} else {
    Write-Host "[INFO] config/launch.env not found (defaults: port=18080, host=0.0.0.0)" -ForegroundColor DarkGray
    $exampleFile = Join-Path $ProjectRoot "config\launch.env.example"
    if (Test-Path $exampleFile) {
        Write-Host "       template available: cp config/launch.env.example config/launch.env" -ForegroundColor DarkGray
    }
}

# ---- 0b. Warn if config/ has uncommitted changes (visibility, not blocking) ----
try {
    $dirtyConfig = & git status --porcelain config/ 2>$null | Where-Object { $_ -match '^\s*M\s' }
    if ($dirtyConfig) {
        Write-Host ""
        Write-Host "[INFO] Uncommitted changes in config/ (will be loaded as-is):" -ForegroundColor Cyan
        $dirtyConfig | Select-Object -First 5 | ForEach-Object {
            Write-Host ("   $_") -ForegroundColor DarkCyan
        }
        if ($dirtyConfig.Count -gt 5) {
            Write-Host ("   ... and {0} more" -f ($dirtyConfig.Count - 5)) -ForegroundColor DarkCyan
        }
        Write-Host "       (commit / stash if you want a clean baseline)" -ForegroundColor DarkCyan
    }
} catch {
    # git not available or not a repo - ignore
}

# ---- 1. Check existing processes ----
$existing = Get-OpenClawProcesses

if ($existing) {
    Write-Host ""
    Write-Host "[WARN] OpenClaw is already running:" -ForegroundColor Yellow
    foreach ($p in $existing) {
        $tag = Get-OpenClawLaunchTag $p.CommandLine
        Write-Host ("   PID={0}  {1}" -f $p.ProcessId, $tag)
    }
    Write-Host ""
    Write-Host "Run stop.bat first, or status.bat to check."
    Write-Host "Press Ctrl+C to cancel, Enter to continue (may cause port conflict)..." -ForegroundColor Yellow
    [void](Read-Host)
}

# ---- 2. Clean stale sentinel ----
$sentinel = Join-Path $ProjectRoot ".restart-required"
if (Test-Path $sentinel) {
    Write-Host "[INFO] Removed stale .restart-required sentinel"
    Remove-Item $sentinel -Force -ErrorAction SilentlyContinue
}

# ---- 3. Launch service_wrapper ----
Write-Host ""
Write-Host "Launching service_wrapper.py..."
$wrapperScript = Join-Path $ProjectRoot "service_wrapper.py"
if (-not (Test-Path $wrapperScript)) {
    Write-Host "[ERROR] $wrapperScript not found" -ForegroundColor Red
    exit 1
}

# Show effective env that wrapper will inherit
$effPort = if ($env:OPENCLAW_PORT) { $env:OPENCLAW_PORT } else { '18080 (default)' }
$effHost = if ($env:OPENCLAW_HOST) { $env:OPENCLAW_HOST } else { '0.0.0.0 (default)' }
Write-Host ("   port=$effPort  host=$effHost") -ForegroundColor DarkGray

Start-Process -WindowStyle Minimized -WorkingDirectory $ProjectRoot `
    -FilePath "python" -ArgumentList "service_wrapper.py"

Write-Host ""
Write-Host "Waiting 8 seconds for server.py to come up..."
Start-Sleep -Seconds 8

# ---- 4. Auto-run status check (use -NoExit so we don't kill our caller) ----
Write-Host ""
& (Join-Path $PSScriptRoot "status.ps1") -NoExit
