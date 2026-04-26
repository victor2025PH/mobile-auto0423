# OpenClaw repository health check
# Pre-commit / pre-push sanity sweep:
#   - git status (which files dirty / untracked)
#   - branch vs main (commit ahead/behind)
#   - 4 runtime config files (intentionally not committed)
#   - config/launch.env (local override) presence
#   - large dirs that may need pruning (logs/_archive, debug, temp)
#   - vendor/ integrity (DLL count)
#
# Usage:
#   repo_health.bat
#   repo_health.bat -Verbose

param([switch]$Verbose)

$ErrorActionPreference = 'Continue'
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $ProjectRoot

$exitCode = 0
function Bump-Exit { param([int]$lvl) if ($lvl -gt $script:exitCode) { $script:exitCode = $lvl } }

Write-Host "==========================================="
Write-Host "  OpenClaw Repo Health"
Write-Host "==========================================="

# ---- [1/6] Branch + commits ahead/behind main ----
Write-Host ""
Write-Host "[1/6] Branch + commits"
$branch = (& git branch --show-current 2>$null).Trim()
if (-not $branch) {
    Write-Host "   [WARN] not on a branch (detached HEAD?)" -ForegroundColor Yellow
    Bump-Exit 1
} else {
    Write-Host ("   [OK]   branch: {0}" -f $branch) -ForegroundColor Green
    if ($branch -ne 'main') {
        $ahead = (& git rev-list --count "main..HEAD" 2>$null)
        $behind = (& git rev-list --count "HEAD..main" 2>$null)
        if ($ahead) { Write-Host ("          {0} commit(s) ahead of main" -f $ahead) -ForegroundColor Cyan }
        if ($behind -and $behind -ne '0') {
            Write-Host ("          {0} commit(s) behind main (pull rebase recommended)" -f $behind) -ForegroundColor Yellow
            Bump-Exit 1
        }
    }
}

# ---- [2/6] Working tree dirty ----
Write-Host ""
Write-Host "[2/6] Working tree status"
$dirty = & git status --porcelain 2>$null
$modCount = ($dirty | Where-Object { $_ -match '^\s*M' }).Count
$untrackedCount = ($dirty | Where-Object { $_ -match '^\?\?' }).Count
$stagedCount = ($dirty | Where-Object { $_ -match '^[AMR]' -and $_ -notmatch '^\?\?' }).Count

if ($modCount -eq 0 -and $untrackedCount -eq 0 -and $stagedCount -eq 0) {
    Write-Host "   [OK]   clean tree" -ForegroundColor Green
} else {
    Write-Host ("   [INFO] modified={0} untracked={1} staged={2}" -f $modCount, $untrackedCount, $stagedCount) -ForegroundColor Cyan
    if ($Verbose) {
        $dirty | Select-Object -First 20 | ForEach-Object { Write-Host ("       " + $_) -ForegroundColor DarkCyan }
    }
}

# ---- [3/6] 4 runtime config files (expected dirty after server start) ----
Write-Host ""
Write-Host "[3/6] Runtime config files (expected dirty after server start)"
$runtimeConfigs = @(
    'config/cluster_state.json',
    'config/device_aliases.json',
    'config/device_registry.json',
    'config/notify_config.json'
)
foreach ($cfg in $runtimeConfigs) {
    $isDirty = ($dirty | Where-Object { $_ -match [regex]::Escape($cfg) })
    if ($isDirty) {
        Write-Host ("   [INFO] dirty: $cfg") -ForegroundColor Cyan
    } else {
        Write-Host ("   [OK]   clean: $cfg") -ForegroundColor DarkGray
    }
}
Write-Host "       (these are runtime state; do NOT commit unless intentional)" -ForegroundColor DarkGray

# ---- [4/6] launch.env ----
Write-Host ""
Write-Host "[4/6] config/launch.env"
$envFile = Join-Path $ProjectRoot "config\launch.env"
$envExample = Join-Path $ProjectRoot "config\launch.env.example"
if (Test-Path $envFile) {
    $envSize = (Get-Item $envFile).Length
    Write-Host ("   [OK]   exists ({0} bytes)" -f $envSize) -ForegroundColor Green
} else {
    if (Test-Path $envExample) {
        Write-Host "   [WARN] not found. Run: cp config/launch.env.example config/launch.env" -ForegroundColor Yellow
        Bump-Exit 1
    } else {
        Write-Host "   [WARN] launch.env missing AND launch.env.example missing" -ForegroundColor Yellow
        Bump-Exit 1
    }
}

# ---- [5/6] Disk usage hotspots ----
Write-Host ""
Write-Host "[5/6] Disk usage hotspots"
$hotspots = @(
    'logs',
    'logs\_archive',
    'data',
    'data\_archive',
    'debug',
    'temp',
    'apk_repo'
)
foreach ($h in $hotspots) {
    $p = Join-Path $ProjectRoot $h
    if (Test-Path $p) {
        $size = (Get-ChildItem $p -Recurse -File -ErrorAction SilentlyContinue |
                 Measure-Object -Property Length -Sum).Sum
        $sizeMB = [Math]::Round(($size / 1MB), 1)
        $color = if ($sizeMB -gt 500) { 'Yellow' } elseif ($sizeMB -gt 100) { 'Cyan' } else { 'Green' }
        $tag = if ($sizeMB -gt 500) { '[WARN]' } elseif ($sizeMB -gt 100) { '[INFO]' } else { '[OK]  ' }
        Write-Host ("   {0} {1,8} MB   {2}" -f $tag, $sizeMB, $h) -ForegroundColor $color
        if ($sizeMB -gt 500) {
            Bump-Exit 1
            Write-Host "          consider: cleanup_logs.bat -Days 30" -ForegroundColor DarkYellow
        }
    }
}

# ---- [6/6] vendor/ integrity ----
Write-Host ""
Write-Host "[6/6] vendor/ integrity"
$vendor = Join-Path $ProjectRoot "vendor"
if (Test-Path $vendor) {
    $dllCount = (Get-ChildItem $vendor -Filter "*.dll").Count
    $hasScrcpy = Test-Path (Join-Path $vendor "scrcpy-server")
    Write-Host ("   [OK]   {0} DLL(s)  scrcpy-server: {1}" -f $dllCount, $(if ($hasScrcpy) { 'yes' } else { 'NO' })) -ForegroundColor Green
    if (-not $hasScrcpy) {
        Write-Host "   [WARN] scrcpy-server missing under vendor/" -ForegroundColor Yellow
        Bump-Exit 1
    }
} else {
    Write-Host "   [WARN] vendor/ directory missing" -ForegroundColor Yellow
    Bump-Exit 1
}

# ---- Summary ----
Write-Host ""
Write-Host "==========================================="
$verdict = switch ($exitCode) { 0 {'HEALTHY'} 1 {'NEEDS ATTENTION'} default {'UNKNOWN'} }
$color   = switch ($exitCode) { 0 {'Green'  } 1 {'Yellow'         } default {'White'  } }
Write-Host ("  >> Repo verdict: [{0}] {1}" -f $exitCode, $verdict) -ForegroundColor $color
Write-Host "==========================================="

exit $exitCode
