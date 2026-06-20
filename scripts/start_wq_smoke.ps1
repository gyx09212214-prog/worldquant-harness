param(
    [string]$Expression = "rank(ts_delta(close,5))",
    [string]$Region = "USA",
    [string]$Universe = "TOP3000",
    [int]$Delay = 1,
    [string]$Neutralization = "SUBINDUSTRY",
    [int]$Decay = 0,
    [double]$Truncation = 0.08,
    [string]$Tag = "quantgpt-smoke"
)

$ErrorActionPreference = "Stop"

$Root = "D:\code\external\QuantGPT"
$Python = "C:\Users\guoyx\AppData\Local\Programs\Python\Python313\python.exe"
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
