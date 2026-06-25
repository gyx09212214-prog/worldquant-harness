param(
    [string[]]$Regions = @("USA", "CHN"),
    [string[]]$Universes = @("TOP3000"),
    [int[]]$Delays = @(1),
    [int]$Limit = 50,
    [int]$MaxDatasets = 25
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
$Python = if ($env:PYTHON) { $env:PYTHON } else { "python" }
$EnvPath = Join-Path $Root ".env"
$LogsDir = Join-Path $Root "logs"
$ReportsDir = Join-Path $Root "reports"

New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null
New-Item -ItemType Directory -Force -Path $ReportsDir | Out-Null

if (-not (Test-Path -LiteralPath $EnvPath)) {
    Write-Output "FAILED reason=missing_env path=$EnvPath"
    exit 1
}

$emailLine = Select-String -LiteralPath $EnvPath -Pattern "^WQ_BRAIN_EMAIL=" -ErrorAction SilentlyContinue
$passwordLine = Select-String -LiteralPath $EnvPath -Pattern "^WQ_BRAIN_PASSWORD=" -ErrorAction SilentlyContinue
$emailSet = $false
$passwordSet = $false
if ($emailLine) {
    $emailSet = (($emailLine.Line -replace "^WQ_BRAIN_EMAIL=", "").Trim().Length -gt 0)
}
if ($passwordLine) {
    $passwordSet = (($passwordLine.Line -replace "^WQ_BRAIN_PASSWORD=", "").Trim().Length -gt 0)
}

if (-not $emailSet -or -not $passwordSet) {
    Write-Output "FAILED reason=missing_wq_credentials email_set=$emailSet password_set=$passwordSet"
    exit 2
}

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$output = Join-Path $ReportsDir "wq_available_fields_$timestamp.json"
$stdout = Join-Path $LogsDir "wq_discover_fields_$timestamp.out.log"
$stderr = Join-Path $LogsDir "wq_discover_fields_$timestamp.err.log"
$status = Join-Path $LogsDir "wq_discover_fields_latest.json"

$argsList = @(
    "scripts\wq_discover_fields.py",
    "--regions"
) + $Regions + @(
    "--universes"
) + $Universes + @(
    "--delays"
) + ($Delays | ForEach-Object { [string]$_ }) + @(
    "--limit", [string]$Limit,
    "--max-datasets", [string]$MaxDatasets,
    "--output", $output
)

$process = Start-Process `
    -FilePath $Python `
    -ArgumentList $argsList `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -PassThru

$state = [ordered]@{
    kind = "wq_discover_fields"
    status = "RUNNING"
    pid = $process.Id
    started_at = (Get-Date).ToString("s")
    regions = $Regions
    universes = $Universes
    delays = $Delays
    output = $output
    stdout = $stdout
    stderr = $stderr
}
$state | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $status -Encoding UTF8

Write-Output "STARTED pid=$($process.Id) status_file=$status output=$output"
exit 0
