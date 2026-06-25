$ErrorActionPreference = "Stop"

$Port = 8003
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
$LogDir = Join-Path $Root "logs"
$OutLog = Join-Path $LogDir "worldquant_harness_8003.out.log"
$ErrLog = Join-Path $LogDir "worldquant_harness_8003.err.log"
$HealthUrl = "http://127.0.0.1:$Port/api/v1/health"

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

$listeners = netstat -ano | Select-String ":$Port\s+.*LISTENING"
if ($listeners) {
    foreach ($listener in $listeners) {
        $line = $listener.ToString().Trim()
        $pidValue = ($line -split "\s+")[-1]
        if ($pidValue -match "^\d+$") {
            Write-Output "Stopping existing worldquant-harness listener pid=$pidValue port=$Port"
            Stop-Process -Id ([int]$pidValue) -Force -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Seconds 2
}

$env:WORLDQUANT_HARNESS_USE_WIND = "1"
$env:WORLDQUANT_HARNESS_DATA_SOURCE = "wind,baostock"

$serverArgs = "-m worldquant_harness --transport http --host 127.0.0.1 --port $Port"
Start-Process `
    -FilePath "python" `
    -ArgumentList $serverArgs `
    -WorkingDirectory $Root `
    -RedirectStandardOutput $OutLog `
    -RedirectStandardError $ErrLog `
    -WindowStyle Hidden

Start-Sleep -Seconds 4

try {
    $health = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 5
    Write-Output "RUNNING port=$Port health=$($health.status) active_tasks=$($health.active_tasks) total_tasks=$($health.total_tasks)"
}
catch {
    Write-Output "FAILED port=$Port error=$($_.Exception.Message)"
    Write-Output "stdout=$OutLog"
    Write-Output "stderr=$ErrLog"
    exit 1
}
