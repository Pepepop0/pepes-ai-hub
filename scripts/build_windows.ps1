<#
.SYNOPSIS
    Build the Windows release folder for Pepito's AI Hub.

.DESCRIPTION
    Produces dist\PepitosAIHub\PepitosAIHub.exe plus its _internal data from
    the current checkout, using the checkout's own .venv and a pinned
    PyInstaller. Cleans only its own previous output, verifies the entrypoint
    exists, checks that every resource directory was copied, and runs the
    built executable's --smoke self-check. See BUILD_WINDOWS.md.

.PARAMETER SkipSmoke
    Build without running the packaged smoke test afterwards.

.PARAMETER Version
    Print the version the build will carry and exit.
#>
[CmdletBinding()]
param(
    [switch]$SkipSmoke,
    [switch]$Version
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$Spec = Join-Path $Root 'packaging\pepitos_ai_hub.spec'
$AppName = 'PepitosAIHub'
$DistDir = Join-Path $Root 'dist'
$BuildDir = Join-Path $Root 'build'
$OutDir = Join-Path $DistDir $AppName
$Exe = Join-Path $OutDir "$AppName.exe"
$CliExe = Join-Path $OutDir "$AppName-CLI.exe"
$Internal = Join-Path $OutDir '_internal'

function Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }
function Fail([string]$Message) { Write-Host "BUILD FAILED: $Message" -ForegroundColor Red; exit 1 }

# Native tools (PyInstaller, pip) log progress to stderr. Under 'Stop', Windows
# PowerShell 5.1 turns the first such line into a terminating error, so native
# calls run with the preference relaxed and are judged by their exit code only.
function Invoke-Native([string]$Exe, [string[]]$Arguments) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        # Write-Host, not the pipeline: the pipeline is this function's return value.
        & $Exe @Arguments 2>&1 | ForEach-Object { Write-Host "$_" }
        return [int]$LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
}

if (-not (Test-Path $Python)) {
    Fail "No .venv at $Root. Run bootstrap-dev.bat first."
}
if (-not (Test-Path $Spec)) {
    Fail "Spec not found: $Spec"
}

$BuildVersion = & $Python -c "import local_ai_stack; print(local_ai_stack.__version__)"
if ($LASTEXITCODE -ne 0) { Fail 'Could not read local_ai_stack.__version__ from the venv.' }
if ($Version) { Write-Host "Pepito's AI Hub $BuildVersion"; exit 0 }

Step "Building Pepito's AI Hub $BuildVersion from $Root"

# --- 1. Build dependencies and renderer ------------------------------------
Step "Ensuring build and desktop dependencies in the venv"
$code = Invoke-Native $Python @('-m', 'pip', 'install', '--disable-pip-version-check', '--quiet', '-e', "$Root[build]")
if ($code -ne 0) { Fail "Could not install build dependencies." }
if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) { Fail 'Node.js 22+ and npm are required to build the desktop UI.' }
Push-Location (Join-Path $Root 'desktop-ui')
try {
    Step 'Installing locked desktop dependencies'
    $code = Invoke-Native 'npm.cmd' @('ci')
    if ($code -ne 0) { Fail 'npm ci failed.' }
    Step 'Building the desktop renderer'
    $code = Invoke-Native 'npm.cmd' @('run', 'build')
    if ($code -ne 0) { Fail 'Desktop renderer build failed.' }
} finally {
    Pop-Location
}

# --- 2. Clean only what this script produced --------------------------------
# Never `Remove-Item dist` wholesale: only the app folder and PyInstaller's
# work directory for this spec are ours to delete.
foreach ($path in @($OutDir, (Join-Path $BuildDir 'pepitos_ai_hub'))) {
    if (Test-Path $path) {
        Step "Removing previous output: $path"
        Remove-Item -Recurse -Force $path
    }
}

# --- 3. Build ---------------------------------------------------------------
Step 'Running PyInstaller'
$code = Invoke-Native $Python @('-m', 'PyInstaller', '--noconfirm', '--clean', '--distpath', $DistDir, '--workpath', $BuildDir, $Spec)
if ($code -ne 0) { Fail 'PyInstaller reported an error.' }

# --- 4. Verify the output ---------------------------------------------------
Step 'Verifying the entrypoint and bundled resources'
if (-not (Test-Path $Exe)) { Fail "Entrypoint missing: $Exe" }
if (-not (Test-Path $CliExe)) { Fail "CLI entrypoint missing: $CliExe" }
$missing = @()
foreach ($name in @('profiles', 'manifests', 'workflows', 'assets')) {
    $dir = Join-Path $Internal $name
    if (-not (Test-Path $dir)) { $missing += $name }
}
if ($missing.Count -gt 0) { Fail "Resource directories missing from the bundle: $($missing -join ', ')" }
foreach ($sentinel in @('profiles\capella.yaml', 'manifests\artifacts.yaml', 'workflows\flux2-klein-t2i-api.json', 'assets\themes', 'assets\app.ico', 'desktop-ui\dist\index.html')) {
    if (-not (Test-Path (Join-Path $Internal $sentinel))) { Fail "Bundled resource incomplete: $sentinel" }
}

$size = (Get-ChildItem $OutDir -Recurse -File | Measure-Object Length -Sum).Sum
Write-Host ("Output: {0}  ({1:N1} MB)" -f $OutDir, ($size / 1MB))

# --- 5. Smoke the built executable -------------------------------------------
if (-not $SkipSmoke) {
    Step 'Running the packaged smoke check (PepitosAIHub-CLI.exe --smoke)'
    $code = Invoke-Native $CliExe @('--smoke')
    if ($code -ne 0) { Fail "Packaged smoke check failed (exit $code)." }
    Step 'Checking --version on the built executable'
    $code = Invoke-Native $CliExe @('--version')
    if ($code -ne 0) { Fail '--version failed on the built executable.' }
}

Write-Host ''
Write-Host "BUILD OK: $Exe" -ForegroundColor Green
Write-Host "Run it by double-clicking the .exe or from a terminal: `"$Exe`""
exit 0
