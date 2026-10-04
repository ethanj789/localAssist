# Builds the InkRecognizer CLI tool to a stable, predictable path.
#
#   winink/bin/InkRecognizer.exe
#
# The Python wrapper (tool_apps/notes/winink_recognizer.py) looks for the exe
# at that path. Re-run this script after changing any C# source.
#
# Usage:  pwsh winink/build.ps1                        (Release, default)
#         pwsh winink/build.ps1 -Configuration Debug

[CmdletBinding()]
param(
    [ValidateSet("Debug", "Release")]
    [string]$Configuration = "Release"
)

$ErrorActionPreference = "Stop"

$here      = Split-Path -Parent $MyInvocation.MyCommand.Path
$project   = Join-Path $here "InkRecognizer\InkRecognizer.csproj"
$outDir    = Join-Path $here "bin"
$config    = $Configuration

Write-Host "Publishing InkRecognizer ($config) -> $outDir" -ForegroundColor Cyan

# Framework-dependent publish: small output, requires the .NET 8+ Windows
# runtime (already present on dev machine). Self-contained is overkill here.
dotnet publish $project `
    -c $config `
    -o $outDir `
    --nologo

if ($LASTEXITCODE -ne 0) {
    throw "dotnet publish failed with exit code $LASTEXITCODE"
}

$exe = Join-Path $outDir "InkRecognizer.exe"
if (-not (Test-Path $exe)) {
    throw "Expected exe not found at $exe"
}

Write-Host "OK: $exe" -ForegroundColor Green
