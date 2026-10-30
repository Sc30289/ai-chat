param(
    [int]$Port = 8765,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$venvPy = Join-Path $root "..\minimind-vscode\.venv\Scripts\python.exe"
$venvPy = [System.IO.Path]::GetFullPath($venvPy)

$candidates = @()
if ($Python) { $candidates += $Python }
if ($env:AI_CHAT_PYTHON) { $candidates += $env:AI_CHAT_PYTHON }
if (Test-Path $venvPy) { $candidates += $venvPy }

$chosen = $null
foreach ($c in $candidates) {
    if ($c -and (Test-Path $c)) { $chosen = $c; break }
}
if (-not $chosen) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $chosen = $cmd.Source }
}
if (-not $chosen) {
    Write-Host "找不到 Python。请安装 Python 3.9+，或用 -Python 指定解释器路径。" -ForegroundColor Red
    exit 1
}

$env:PYTHONIOENCODING = "utf-8"
Write-Host "使用解释器: $chosen" -ForegroundColor DarkGray
& $chosen (Join-Path $root "server.py") --port $Port
