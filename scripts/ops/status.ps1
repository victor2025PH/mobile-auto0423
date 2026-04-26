# OpenClaw status check (5 items)
# Usage: status.bat  (or directly: powershell -File status.ps1)
# For details on each item, see docs/SYSTEM_RUNBOOK.md
#
# Exit codes (for cron / monitor consumption):
#   0 = GO        all 5 items healthy
#   1 = DEGRADED  service up but with warnings (loopback bind / partial devices / errors in log)
#   2 = DOWN      no process / no port / /health not 200 / 0 devices
#
# When invoked from another .ps1 (start.ps1 / migrate.ps1), pass -NoExit so the
# verdict-based 'exit' does not propagate up and kill the caller.

param([switch]$NoExit)

$ErrorActionPreference = 'Continue'
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $ProjectRoot
. "$PSScriptRoot\_lib.ps1"

# Track worst severity. Helper: bump exit code if worse.
$script:exitCode = 0
function Bump-Exit { param([int]$lvl) if ($lvl -gt $script:exitCode) { $script:exitCode = $lvl } }

Write-Host "==========================================="
Write-Host "  OpenClaw Status Check (5 items)"
Write-Host "==========================================="

# ---- [1/5] Process ----
Write-Host ""
Write-Host "[1/5] Process"
$procs = Get-OpenClawProcesses

if ($procs) {
    foreach ($p in $procs) {
        $tag = Get-OpenClawLaunchTag $p.CommandLine
        Write-Host ("   [OK]   {0}  PID={1}" -f $tag, $p.ProcessId) -ForegroundColor Green
    }
    # Warn if not started via wrapper (no auto-restart)
    $hasWrapper = $procs | Where-Object { $_.CommandLine -match 'service_wrapper\.py' }
    if (-not $hasWrapper) {
        Write-Host "   [WARN] Not started via service_wrapper - no auto-restart on crash" -ForegroundColor Yellow
        Write-Host "          (see RUNBOOK F8) Recommend: migrate.bat" -ForegroundColor DarkYellow
        Bump-Exit 1
    }
} else {
    Write-Host "   [DOWN] No OpenClaw process running" -ForegroundColor Red
    Bump-Exit 2
}

# ---- [2/5] Port listening ----
Write-Host ""
Write-Host "[2/5] Port listening"
$foundPort = $null
foreach ($port in @(8000, 18080)) {
    $listening = netstat -ano | Select-String "LISTENING" | Select-String ":$port "
    if ($listening) {
        foreach ($line in $listening) {
            $parts = $line.Line -split '\s+' | Where-Object { $_ }
            $localAddr = $parts[1]
            $pidVal = $parts[-1]
            $bindNote = if ($localAddr -match '^127\.0\.0\.1:') { ' (loopback only - LAN unreachable, see F1)' }
                        elseif ($localAddr -match '^0\.0\.0\.0:') { ' (LAN reachable)' }
                        else { '' }
            $color = if ($localAddr -match '^127\.0\.0\.1:') { 'Yellow' } else { 'Green' }
            Write-Host ("   [OK]   :{0}  PID={1}  bind={2}{3}" -f $port, $pidVal, $localAddr, $bindNote) -ForegroundColor $color
            if (-not $foundPort) { $foundPort = $port }
            if ($localAddr -match '^127\.0\.0\.1:') { Bump-Exit 1 }
        }
    }
}
if (-not $foundPort) {
    Write-Host "   [DOWN] :8000 / :18080 not listening" -ForegroundColor Red
    Bump-Exit 2
}

# ---- [3/5] /health endpoint ----
Write-Host ""
Write-Host "[3/5] /health endpoint"
foreach ($port in @(8000, 18080)) {
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$port/health" -TimeoutSec 3 `
                -UseBasicParsing -ErrorAction Stop
        Write-Host ("   [OK]   http://127.0.0.1:{0}/health  ->  {1}" -f $port, $resp.StatusCode) -ForegroundColor Green
    } catch {
        $code = $_.Exception.Response.StatusCode.value__
        if ($code) {
            Write-Host ("   [DEGRADED] http://127.0.0.1:{0}/health  ->  {1}" -f $port, $code) -ForegroundColor Yellow
            Bump-Exit 1
        }
    }
}

# ---- [4/5] Devices ----
Write-Host ""
Write-Host "[4/5] Devices"
if ($foundPort) {
    try {
        $devicesJson = Invoke-WebRequest -Uri "http://127.0.0.1:$foundPort/devices" `
                       -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
        $devices = $devicesJson.Content | ConvertFrom-Json
        $connected = @($devices | Where-Object { $_.status -eq 'connected' }).Count
        $unauth = @($devices | Where-Object { $_.usb_issue -eq 'unauthorized' }).Count
        $total = $devices.Count

        $summaryColor = 'Green'
        $summaryTag = '[OK]   '
        if ($connected -lt $total -and $connected -gt 0) {
            $summaryColor = 'Yellow'
            $summaryTag = '[PART] '
            Bump-Exit 1
        } elseif ($connected -eq 0) {
            $summaryColor = 'Red'
            $summaryTag = '[DOWN] '
            Bump-Exit 2
        }
        Write-Host ("   {0}{1}/{2} connected ({3} unauthorized)" -f $summaryTag, $connected, $total, $unauth) -ForegroundColor $summaryColor

        foreach ($d in $devices) {
            $icon = if ($d.status -eq 'connected') { '[OK  ]' }
                    elseif ($d.usb_issue -eq 'unauthorized') { '[AUTH]' }
                    else { '[DOWN]' }
            $color = if ($d.status -eq 'connected') { 'Green' } else { 'Yellow' }
            Write-Host ("       {0}  {1}  ({2})" -f $icon, $d.display_name, $d.device_id) -ForegroundColor $color
        }
    } catch {
        Write-Host ("   [DOWN] /devices query failed: {0}" -f $_.Exception.Message) -ForegroundColor Red
    }
} else {
    Write-Host "   [SKIP] no listening port, skipped" -ForegroundColor DarkGray
}

# ---- [5/5] Recent errors ----
Write-Host ""
Write-Host "[5/5] Recent errors (last 200 lines of logs/openclaw.log)"
$logFile = Join-Path $ProjectRoot "logs\openclaw.log"
if (Test-Path $logFile) {
    try {
        # Open with FileShare.ReadWrite so uvicorn/server can keep writing while we read.
        # Read only the tail (~64KB is enough for 200 JSON lines).
        $fs = [System.IO.File]::Open($logFile, [System.IO.FileMode]::Open,
                                      [System.IO.FileAccess]::Read,
                                      [System.IO.FileShare]::ReadWrite)
        try {
            $tailBytes = [Math]::Min(65536, $fs.Length)
            $fs.Seek(-$tailBytes, [System.IO.SeekOrigin]::End) | Out-Null
            $buf = New-Object byte[] $tailBytes
            [void]$fs.Read($buf, 0, $tailBytes)
            $text = [System.Text.Encoding]::UTF8.GetString($buf)
        } finally {
            $fs.Close()
        }
        $allLines = $text -split "`r?`n" | Where-Object { $_ }
        $tail = $allLines | Select-Object -Last 200
        $errs = $tail | Select-String -Pattern '"level":\s*"ERROR"' | Select-Object -Last 3
        if ($errs) {
            Write-Host "   [WARN] last 3 ERROR lines:" -ForegroundColor Yellow
            foreach ($e in $errs) {
                $line = $e.Line
                if ($line.Length -gt 200) { $line = $line.Substring(0, 200) + '...' }
                Write-Host ("       " + $line) -ForegroundColor DarkYellow
            }
            Bump-Exit 1
        } else {
            Write-Host "   [OK]   no ERROR in last 200 lines" -ForegroundColor Green
        }
    } catch {
        Write-Host ("   [SKIP] read log failed: {0}" -f $_.Exception.Message) -ForegroundColor DarkGray
    }
} else {
    Write-Host "   [SKIP] $logFile not found" -ForegroundColor DarkGray
}

# ---- Summary ----
Write-Host ""
Write-Host "==========================================="
if ($foundPort) {
    Write-Host ("  >> Dashboard: http://localhost:{0}/dashboard" -f $foundPort) -ForegroundColor Cyan
    Write-Host "  >> Use 'localhost' NOT 192.168.x.x   (see RUNBOOK F1)" -ForegroundColor DarkCyan
} else {
    Write-Host "  >> Service is down. Run start.bat" -ForegroundColor Yellow
}
$verdict = switch ($script:exitCode) {
    0 { 'GO       all 5 items healthy' }
    1 { 'DEGRADED service up with warnings' }
    2 { 'DOWN     critical failure' }
}
$verdictColor = switch ($script:exitCode) { 0 { 'Green' } 1 { 'Yellow' } 2 { 'Red' } }
Write-Host ("  >> Verdict: [{0}] {1}" -f $script:exitCode, $verdict) -ForegroundColor $verdictColor
Write-Host "==========================================="

if ($NoExit) { return $script:exitCode }
exit $script:exitCode
