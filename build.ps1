# Builds the one-folder release (guide/distribution_plan.md Phase 4) and checks it.
#   powershell -ExecutionPolicy Bypass -File build.ps1 [-SkipBuild] [-Installer]
# Output: dist\LiveInterpreter\; with -Installer, also the Phase 5 Inno Setup installer
# dist\XedgeLiveInterpreter-<version>-setup.exe (needs Inno Setup 6), only if the checks pass.
# Checks: the self-test passes (it also asserts the WinVaultKeyring backend and the
# bundled files), the real loop-test clip is bundled, and no .env or API key is in dist\.
param([switch]$SkipBuild, [switch]$Installer)

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
if (-not $Installer) { exit 0 }

# 4. Installer (installer\LiveInterpreter.iss). Version defines come from version.py.
$iscc = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
if (-not $iscc) {
    $iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
              "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $iscc) { throw "Inno Setup 6 not found (ISCC.exe). Install it from https://jrsoftware.org/isdl.php" }
$v = & $python -c "import version as v; print(v.__version__); print(f'{v.DISPLAY_VERSION} (build {v.BUILD})'); print('.'.join(map(str, v.FILE_VERSION)))"
if ($LASTEXITCODE -ne 0 -or $v.Count -ne 3) { throw "Couldn't read version.py" }
$agreement = Join-Path $PSScriptRoot "installer\tester_agreement.txt"
$blanks = Select-String -Path $agreement -Pattern "\[[^\]]+\]" -AllMatches | ForEach-Object { $_.Matches.Value } | Sort-Object -Unique
if ($blanks) { Write-Host "WARNING: tester_agreement.txt still has blanks: $($blanks -join ', ')" -ForegroundColor Yellow }
& $iscc /Q "/DAppVersion=$($v[0])" "/DAppVerName=$($v[1])" "/DFileVersion=$($v[2])" (Join-Path $PSScriptRoot "installer\LiveInterpreter.iss")
if ($LASTEXITCODE -ne 0) { throw "ISCC failed (exit $LASTEXITCODE)" }
Write-Host "Installer OK: $(Join-Path $PSScriptRoot "dist\XedgeLiveInterpreter-$($v[0])-setup.exe")" -ForegroundColor Green
