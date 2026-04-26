# OpenClaw weekly business report
# Wraps scripts/phase8_funnel_report.py and writes a markdown file under logs/reports/
#
# Usage:
#   weekly_report.bat               # default 7 days
#   weekly_report.bat --days 1      # last 24 hours
#   weekly_report.bat --actor agent_a   # only A side

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $ProjectRoot

$reportDir = Join-Path $ProjectRoot "logs\reports"
if (-not (Test-Path $reportDir)) {
    New-Item -ItemType Directory -Path $reportDir -Force | Out-Null
}

$ts = Get-Date -Format 'yyyy-MM-dd_HHmm'
$reportFile = Join-Path $reportDir "weekly_$ts.md"

Write-Host "==========================================="
Write-Host "  OpenClaw Weekly Report"
Write-Host "==========================================="
Write-Host ""
Write-Host "Running phase8_funnel_report.py with args: $args"
Write-Host ""

# Run the python report and capture stdout
$header = @"
# OpenClaw Weekly Report

**Generated**: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
**Args**: $($args -join ' ')

---

"@

$header | Out-File -FilePath $reportFile -Encoding UTF8

try {
    # phase8_funnel_report.py is at scripts/phase8_funnel_report.py
    $pyScript = Join-Path $ProjectRoot "scripts\phase8_funnel_report.py"
    if (-not (Test-Path $pyScript)) {
        Write-Host "[ERROR] scripts/phase8_funnel_report.py not found" -ForegroundColor Red
        exit 1
    }

    $output = & python $pyScript @args 2>&1
    $output | Out-File -FilePath $reportFile -Append -Encoding UTF8

    Write-Host "Report written to: $reportFile" -ForegroundColor Green
    Write-Host ""
    Write-Host "--- Preview (first 30 lines) ---" -ForegroundColor DarkCyan
    Get-Content $reportFile -TotalCount 30
    Write-Host ""
    Write-Host "Open the full report: $reportFile" -ForegroundColor Cyan
} catch {
    Write-Host "[ERROR] Report failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
