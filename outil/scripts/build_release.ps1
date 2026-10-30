param(
    [string]$Output = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    $python = "python"
}
if ($Output) {
    & $python (Join-Path $PSScriptRoot "build_release.py") --output $Output
} else {
    & $python (Join-Path $PSScriptRoot "build_release.py")
}
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
