param([int]$Port = 8503, [string]$Workspace = "workspace")
$ErrorActionPreference = "Stop"
$projectDirectory = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $projectDirectory
$pythonExecutable = Join-Path $projectDirectory ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonExecutable)) {
    throw "Create .venv and install the project first; see README.md."
}
$existingModelPython = "D:\Project\.venv\Scripts\python.exe"
if (-not $env:DATA_DETECTIVE_MODEL_PYTHON -and (Test-Path -LiteralPath $existingModelPython)) {
    $env:DATA_DETECTIVE_MODEL_PYTHON = $existingModelPython
}
$env:DATA_DETECTIVE_WORKSPACE = $Workspace
& $pythonExecutable -m streamlit run app.py --server.port $Port --server.address 127.0.0.1
