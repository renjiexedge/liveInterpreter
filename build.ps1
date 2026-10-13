# Builds the one-folder release (guide/distribution_plan.md Phase 4) and checks it.
#   powershell -ExecutionPolicy Bypass -File build.ps1 [-SkipBuild]
# Output: dist\LiveInterpreter\ (Phase 5 packs it into the installer).
# Checks: the self-test passes (it also asserts the WinVaultKeyring backend and the
# bundled files), the real loop-test clip is bundled, and no .env or API key is in dist\.
param([switch]$SkipBuild)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$python = Join-Path $PSScriptRoot "env\Scripts\python.exe"
$dist = Join-Path $PSScriptRoot "dist\LiveInterpreter"
$exe = Join-Path $dist "LiveInterpreter.exe"
$failed = @()

if (-not $SkipBuild) {
    & $python -m PyInstaller LiveInterpreter.spec --noconfirm --clean
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (exit $LASTEXITCODE)" }
}
if (-not (Test-Path $exe)) { throw "$exe not found" }

# 1. Self-test. The exe is windowed (no console), so the report goes to a file.
$report = Join-Path $env:TEMP "LiveInterpreter-selftest.txt"
Remove-Item $report -ErrorAction SilentlyContinue
try {
    $proc = Start-Process -FilePath $exe -ArgumentList "--selftest", "--out", "`"$report`"" -Wait -PassThru
    if (Test-Path $report) { Get-Content $report | Write-Host }
    if ($proc.ExitCode -ne 0) { $failed += "self-test exited $($proc.ExitCode) (selftest.py: 1 import, 2 assets, 3 audio, 4 keyring, 5 UI)" }
} catch {
    # Smart App Control blocks unsigned exes it has no reputation for; it has no "Run anyway".
    $failed += "self-test could not start: $($_.Exception.Message) (Smart App Control: $((Get-MpComputerStatus).SmartAppControlState))"
}

# 2. The real loop-test clip, not the synthetic chirp fallback.
if (-not (Test-Path (Join-Path $dist "_internal\audio_test\loop_test.wav"))) { $failed += "loop_test.wav not bundled" }

# 3. No .env, python-dotenv or API key anywhere in the build.
$envFiles = Get-ChildItem $dist -Recurse -Force -Filter ".env*"
if ($envFiles) { $failed += ".env file(s) in dist: $($envFiles.FullName -join ', ')" }
if (Test-Path (Join-Path $dist "_internal\dotenv")) { $failed += "python-dotenv bundled" }
$keyPattern = [regex]"AIza[0-9A-Za-z_\-]{35}"
$latin1 = [Text.Encoding]::GetEncoding(28591)  # byte-for-byte, so binaries scan too
foreach ($file in Get-ChildItem $dist -Recurse -File -Force) {
    $text = $latin1.GetString([IO.File]::ReadAllBytes($file.FullName))
    if ($keyPattern.IsMatch($text)) { $failed += "API key pattern in $($file.FullName)" }
}

$size = (Get-ChildItem $dist -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
Write-Host ("dist\LiveInterpreter: {0:N0} MB" -f $size)
if ($failed) {
    $failed | ForEach-Object { Write-Host "FAILED: $_" -ForegroundColor Red }
    exit 1
}
Write-Host "Build OK: $exe" -ForegroundColor Green
