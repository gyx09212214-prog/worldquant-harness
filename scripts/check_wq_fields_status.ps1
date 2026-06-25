$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
$StatusPath = Join-Path $Root "logs\wq_discover_fields_latest.json"

if (-not (Test-Path -LiteralPath $StatusPath)) {
    Write-Output "NO_WQ_DISCOVERY_RUN status_file=$StatusPath"
    exit 1
}

$status = Get-Content -LiteralPath $StatusPath -Raw | ConvertFrom-Json
$pidValue = [int]$status.pid
$proc = Get-Process -Id $pidValue -ErrorAction SilentlyContinue

if ($proc) {
    Write-Output "RUNNING pid=$pidValue output=$($status.output)"
    exit 0
}

if (Test-Path -LiteralPath $status.output) {
    $file = Get-Item -LiteralPath $status.output
    Write-Output "SUCCESS pid=$pidValue output=$($file.FullName) bytes=$($file.Length)"
    exit 0
}

Write-Output "FAILED pid=$pidValue output=$($status.output)"
Write-Output "stderr_tail:"
Get-Content -Path $status.stderr -ErrorAction SilentlyContinue -Tail 40
exit 2
