param([int]$Port = 3307)

$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $project '.runtime'
$binary = $env:GRIDMIND_MYSQL_BIN
if (-not $binary) {
    $found = Get-Command mysqld.exe -ErrorAction SilentlyContinue
    if ($found) { $binary = Split-Path -Parent $found.Source }
    else { $binary = 'C:\Program Files\MySQL\MySQL Server 8.0\bin' }
}
$mysqld = Join-Path $binary 'mysqld.exe'
if (-not (Test-Path -LiteralPath $mysqld -PathType Leaf)) {
    throw 'MySQL Server 8 is required. Set GRIDMIND_MYSQL_BIN to its bin directory or GRIDMIND_DATABASE_URL to an existing schema.'
}
New-Item -ItemType Directory -Force -Path $runtime | Out-Null
$data = Join-Path $runtime 'mysql'
$pidFile = Join-Path $runtime 'mysql.pid'
$urlFile = Join-Path $runtime 'database.url'
$initFile = Join-Path $runtime 'mysql-initialize.sql'
$logFile = Join-Path $runtime 'mysql.log'

function Test-LocalPort {
    $connection = [System.Net.Sockets.TcpClient]::new()
    try { $connection.Connect('127.0.0.1', $Port); return $true }
    catch { return $false }
    finally { $connection.Dispose() }
}

if (Test-LocalPort) {
    if ((Test-Path -LiteralPath $pidFile) -and (Test-Path -LiteralPath $urlFile)) {
        $owned = Get-Process -Id ([int](Get-Content -LiteralPath $pidFile -TotalCount 1)) -ErrorAction SilentlyContinue
        if ($owned -and $owned.ProcessName -eq 'mysqld') { return }
    }
    throw "Port $Port is already in use. Set GRIDMIND_DATABASE_URL or choose a free port."
}

if (-not (Test-Path -LiteralPath (Join-Path $data 'mysql'))) {
    New-Item -ItemType Directory -Force -Path $data | Out-Null
    & $mysqld --no-defaults --initialize-insecure "--datadir=$data" "--log-error=$logFile"
    if ($LASTEXITCODE -ne 0) { throw "MySQL initialization failed. See $logFile." }
    $rootPassword = [guid]::NewGuid().ToString('N')
    $appPassword = [guid]::NewGuid().ToString('N')
    @(
        "ALTER USER 'root'@'localhost' IDENTIFIED BY '$rootPassword';"
        "CREATE DATABASE gridmind CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"
        "CREATE USER 'gridmind'@'127.0.0.1' IDENTIFIED BY '$appPassword';"
        "GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, REFERENCES, INDEX, CREATE VIEW, SHOW VIEW, DROP ON gridmind.* TO 'gridmind'@'127.0.0.1';"
    ) | Set-Content -LiteralPath $initFile -Encoding ascii
    "mysql+pymysql://gridmind:${appPassword}@127.0.0.1:${Port}/gridmind?charset=utf8mb4" |
        Set-Content -LiteralPath $urlFile -Encoding ascii
}
if (-not (Test-Path -LiteralPath $urlFile)) { throw 'Project database URL is missing. Set GRIDMIND_DATABASE_URL.' }
$serverOptions = @(
    '--no-defaults', "--datadir=`"$data`"", "--port=$Port", '--bind-address=127.0.0.1',
    '--mysqlx=OFF', "--pid-file=`"$pidFile`"", "--log-error=`"$logFile`""
)
if (Test-Path -LiteralPath $initFile) { $serverOptions += "--init-file=`"$initFile`"" }
$server = Start-Process -FilePath $mysqld -ArgumentList $serverOptions -WindowStyle Hidden -PassThru
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    Start-Sleep -Milliseconds 500
    $server.Refresh()
    if ($server.HasExited) { throw "MySQL exited during startup. See $logFile." }
    if (Test-LocalPort) {
        if (Test-Path -LiteralPath $initFile) { Remove-Item -LiteralPath $initFile }
        return
    }
}
throw "MySQL did not start in time. See $logFile."
