$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$OutputEncoding = [System.Text.UTF8Encoding]::new()

$Root = "D:\code\external\QuantGPT"
$Python = if ($env:PYTHON) { $env:PYTHON } else { "python" }

& $Python (Join-Path $Root "scripts\wq_status.py") --kind find-only @args
exit $LASTEXITCODE
