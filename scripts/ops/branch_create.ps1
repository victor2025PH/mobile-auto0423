# OpenClaw branch creation helper
# One-line: create a new feat-ops-* branch from current main HEAD.
# Avoids the "I forgot to switch off main + accidentally committed" trap.
#
# Usage:
#   branch_create.bat                    # default: feat-ops-yyyy-MM-dd-HHmm
#   branch_create.bat my-name            # feat-ops-my-name-yyyy-MM-dd
#   branch_create.bat -Name "fix-x"      # feat-ops-fix-x-yyyy-MM-dd
#   branch_create.bat -FromMain          # ensure base is main (default), even if not on main now
#   branch_create.bat -DryRun            # show what would happen, do not create

param(
    [string]$Name = "",
    [switch]$FromMain,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $ProjectRoot

# Build branch name
$datestamp = Get-Date -Format 'yyyy-MM-dd-HHmm'
$datestampShort = Get-Date -Format 'yyyy-MM-dd'

if ($Name) {
    $clean = $Name -replace '[^a-zA-Z0-9-]', '-'
    $branchName = "feat-ops-$clean-$datestampShort"
} else {
    $branchName = "feat-ops-$datestamp"
}

Write-Host "==========================================="
Write-Host "  Create feature branch"
Write-Host "==========================================="
Write-Host ""

# Check working tree state
$currentBranch = (& git branch --show-current 2>$null).Trim()
$dirty = & git status --porcelain 2>$null

Write-Host ("Current branch: {0}" -f $currentBranch)
Write-Host ("New branch:     {0}" -f $branchName)
Write-Host ("Base:           main (HEAD)")
Write-Host ""

if ($dirty) {
    $modCount = ($dirty | Where-Object { $_ -match '^[\sM]M' }).Count
    if ($modCount -gt 0) {
        Write-Host ("[INFO] Working tree has $modCount modified file(s).") -ForegroundColor Cyan
        Write-Host "       They will be carried to the new branch (uncommitted)." -ForegroundColor DarkCyan
        Write-Host ""
    }
}

# Check branch already exists
$exists = & git rev-parse --verify --quiet "refs/heads/$branchName" 2>$null
if ($exists) {
    Write-Host ("[ERROR] Branch '{0}' already exists." -f $branchName) -ForegroundColor Red
    Write-Host "        Use a different name or git checkout that branch." -ForegroundColor DarkRed
    exit 1
}

if ($DryRun) {
    Write-Host "[DRY RUN] Would run:" -ForegroundColor Yellow
    Write-Host ("    git checkout -b {0} main" -f $branchName)
    exit 0
}

# Create branch off main
Write-Host ("Running: git checkout -b {0} main" -f $branchName)
& git checkout -b $branchName main 2>&1 | ForEach-Object { Write-Host "   $_" -ForegroundColor DarkGray }

if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] git checkout failed (see above)" -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host ("[OK] Now on branch: {0}" -f $branchName) -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:" -ForegroundColor DarkCyan
Write-Host "  - Make changes, commit normally"
Write-Host "  - When ready: git push -u origin $branchName && gh pr create"
