$ErrorActionPreference = "Stop"

$Port = 8003
$HealthUrl = "http://127.0.0.1:$Port/api/v1/health"

$listeners = netstat -ano | Select-String ":$Port\s+.*LISTENING"
$pidValue = $null
if ($listeners) {
    $line = ($listeners | Select-Object -First 1).ToString().Trim()
    $pidValue = ($line -split "\s+")[-1]
}

try {
    $health = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 3
    Write-Output "RUNNING port=$Port pid=$pidValue health=$($health.status) active_tasks=$($health.active_tasks) total_tasks=$($health.total_tasks)"
    exit 0
}
catch {
    if ($pidValue) {
        Write-Output "LISTENING_BUT_UNHEALTHY port=$Port pid=$pidValue error=$($_.Exception.Message)"
        exit 2
    }
    Write-Output "STOPPED port=$Port"
    exit 1
}

