$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$setupRoot = Join-Path $repoRoot 'ui\setup'
$nodeModules = Join-Path $setupRoot 'node_modules'
$output = Join-Path $repoRoot 'src\codex_bridge\assets\codexbridge_setup_app.html'

Push-Location $setupRoot
try {
    & npm ci
    if ($LASTEXITCODE -ne 0) {
        throw "npm ci failed with exit code $LASTEXITCODE."
    }
    & npm run build
    if ($LASTEXITCODE -ne 0) {
        throw "Setup app build failed with exit code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}

if (-not (Test-Path -LiteralPath $output -PathType Leaf)) {
    throw "Expected setup app output was not created: $output"
}
Write-Output "Setup app HTML: $output"
Write-Output "Node modules: $nodeModules"
