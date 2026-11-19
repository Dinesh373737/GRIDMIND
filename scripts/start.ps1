param([int]$Port = 8000, [int]$MySqlPort = 3307)

$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Unable to create the Python environment.' }
}
$python = (Resolve-Path '.venv\Scripts\python.exe').Path
& $python -c 'import fastapi, sqlalchemy, pymysql, uvicorn, cryptography'
if ($LASTEXITCODE -ne 0) {
    & $python -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Unable to install project requirements.' }
}
if (-not $env:GRIDMIND_DATABASE_URL) { & "$PSScriptRoot\mysql.ps1" -Port $MySqlPort }
& $python -m uvicorn gridmind.api:app --host 127.0.0.1 --port $Port
