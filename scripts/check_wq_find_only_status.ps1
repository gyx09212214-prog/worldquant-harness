$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$OutputEncoding = [System.Text.UTF8Encoding]::new()

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
$Python = if ($env:PYTHON) { $env:PYTHON } else { "python" }

& $Python (Join-Path $Root "scripts\wq_status.py") --kind find-only @args
exit $LASTEXITCODE
