param(
    [string]$Expression = "rank(ts_delta(close,5))",
    [string]$Region = "USA",
    [string]$Universe = "TOP3000",
    [int]$Delay = 1,
    [string]$Neutralization = "SUBINDUSTRY",
    [int]$Decay = 0,
    [double]$Truncation = 0.08,
    [string]$Tag = "worldquant_harness-smoke"
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
$Python = if ($env:PYTHON) { $env:PYTHON } else { "python" }
$Launcher = Join-Path $Root "scripts\start_wq_smoke_job.py"

& $Python $Launcher `
    --expression $Expression `
    --region $Region `
    --universe $Universe `
    --delay $Delay `
    --decay $Decay `
    --neutralization $Neutralization `
    --truncation $Truncation `
    --tag $Tag
exit $LASTEXITCODE
