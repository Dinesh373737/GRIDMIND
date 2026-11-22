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
$npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $npm) { throw 'Install Node.js 22.12+ LTS and reopen PowerShell to build the React dashboard.' }
Push-Location 'frontend'
try {
    $lockHash = (Get-FileHash package-lock.json -Algorithm SHA256).Hash
    $stamp = 'node_modules\.gridmind-lock'
    if ((-not (Test-Path 'node_modules\vite\package.json')) -or
        (-not (Test-Path $stamp)) -or ((Get-Content -Raw $stamp).Trim() -ne $lockHash)) {
        & $npm.Source ci --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { throw 'Unable to install locked dashboard dependencies.' }
        Set-Content -Path $stamp -Value $lockHash
    }
    # Local startup never embeds AWS configuration into the dashboard bundle.
    $cloudVars = @('VITE_API_BASE_URL', 'VITE_COGNITO_DOMAIN', 'VITE_COGNITO_CLIENT_ID')
    $savedEnv = @{}
    foreach ($name in $cloudVars) {
        $savedEnv[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        Set-Item -Path "Env:$name" -Value ''
    }
    $env:VITE_GRIDMIND_LOCAL_ONLY = '1'
    try {
        & $npm.Source run build
        if ($LASTEXITCODE -ne 0) { throw 'Unable to build the React dashboard.' }
    } finally {
        Remove-Item Env:VITE_GRIDMIND_LOCAL_ONLY -ErrorAction SilentlyContinue
        foreach ($name in $cloudVars) {
            [Environment]::SetEnvironmentVariable($name, $savedEnv[$name], 'Process')
        }
    }
} finally { Pop-Location }

if (-not $env:GRIDMIND_DATABASE_URL) { & "$PSScriptRoot\mysql.ps1" -Port $MySqlPort }
& $python -m uvicorn gridmind.api:app --host 127.0.0.1 --port $Port
