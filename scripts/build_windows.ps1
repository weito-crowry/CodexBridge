$ErrorActionPreference = 'Stop'

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'The Windows GUI package must be built on Windows.'
}

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$buildRoot = [IO.Path]::GetFullPath((Join-Path $repoRoot 'build\windows-package'))
$distRoot = [IO.Path]::GetFullPath((Join-Path $repoRoot 'dist'))
$packageDir = [IO.Path]::GetFullPath((Join-Path $distRoot 'CodexBridge'))
$zipPath = [IO.Path]::GetFullPath((Join-Path $distRoot 'CodexBridge-windows.zip'))
$specPath = Join-Path $repoRoot 'packaging\windows\CodexBridge.spec'
$separator = [IO.Path]::DirectorySeparatorChar

foreach ($outputPath in @($buildRoot, $packageDir, $zipPath)) {
    if (-not $outputPath.StartsWith($repoRoot.TrimEnd('\') + $separator, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean output outside the repository: $outputPath"
    }
}

foreach ($outputPath in @($buildRoot, $packageDir, $zipPath)) {
    if (Test-Path -LiteralPath $outputPath) {
        Remove-Item -LiteralPath $outputPath -Recurse -Force
    }
}
New-Item -ItemType Directory -Path $distRoot -Force | Out-Null

Push-Location $repoRoot
try {
    & uv run pyinstaller --noconfirm --clean --distpath $distRoot --workpath $buildRoot $specPath
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}

$executablePath = Join-Path $packageDir 'CodexBridge.exe'
if (-not (Test-Path -LiteralPath $executablePath -PathType Leaf)) {
    throw "Expected packaged executable was not created: $executablePath"
}

Add-Type -AssemblyName System.IO.Compression.FileSystem
[IO.Compression.ZipFile]::CreateFromDirectory(
    $packageDir,
    $zipPath,
    [IO.Compression.CompressionLevel]::Optimal,
    $true
)

Write-Output "Package directory: $packageDir"
Write-Output "Package ZIP: $zipPath"
