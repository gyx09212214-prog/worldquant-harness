$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$OutputEncoding = [System.Text.UTF8Encoding]::new()

$Root = "D:\code\external\QuantGPT"
$StatusPath = Join-Path $Root "logs\wq_smoke_latest.json"

if (-not (Test-Path -LiteralPath $StatusPath)) {
    Write-Output "NO_WQ_SMOKE_RUN status_file=$StatusPath"
    exit 1
}

$status = Get-Content -LiteralPath $StatusPath -Raw | ConvertFrom-Json
$pidValue = [int]$status.pid
$proc = Get-Process -Id $pidValue -ErrorAction SilentlyContinue

if ($proc) {
    $progress = if ($null -ne $status.progress) { $status.progress } else { "na" }
    $message = if ($status.message) { $status.message } else { "process alive" }
    if ($message -match "[^\x00-\x7F]") {
        $message = "WQ platform wait/retry; see status_file for raw message"
    }
    Write-Output "RUNNING pid=$pidValue status=$($status.status) progress=$progress message=$message expression=$($status.expression) output=$($status.output)"
    exit 0
}

if (Test-Path -LiteralPath $status.output) {
    $file = Get-Item -LiteralPath $status.output
    $payload = Get-Content -LiteralPath $status.output -Raw | ConvertFrom-Json
    $result = $payload.result
    if (-not $result.ok) {
        Write-Output "FAILED pid=$pidValue output=$($file.FullName) error=$($result.error)"
        exit 2
    }
    $alphaId = $result.alpha_id
    $metrics = $result.wq_brain
    Write-Output "SUCCESS pid=$pidValue output=$($file.FullName) alpha_id=$alphaId sharpe=$($metrics.wq_sharpe) fitness=$($metrics.wq_fitness) turnover=$($metrics.wq_turnover) submitted=$($result.submitted)"
    exit 0
}

Write-Output "FAILED pid=$pidValue expression=$($status.expression) output=$($status.output)"
Write-Output "stderr_tail:"
Get-Content -Path $status.stderr -ErrorAction SilentlyContinue -Tail 60
exit 2
