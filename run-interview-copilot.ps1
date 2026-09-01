$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendDir = Join-Path $projectDir "backend"
$python = Join-Path $backendDir ".venv\Scripts\python.exe"
$configFile = Join-Path $projectDir ".env.local"

if (-not (Test-Path -LiteralPath $python)) {
    Write-Host "[Interview Copilot] 未找到 backend\.venv，请先完成 Python 环境安装。" -ForegroundColor Red
    Read-Host "按回车键退出"
    exit 1
}

if (Test-Path -LiteralPath $configFile) {
    foreach ($line in Get-Content -LiteralPath $configFile -Encoding UTF8) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) { continue }
        $separator = $trimmed.IndexOf("=")
        if ($separator -lt 1) { continue }
        $name = $trimmed.Substring(0, $separator).Trim()
        $value = $trimmed.Substring($separator + 1)
        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
    Write-Host "[Interview Copilot] 已载入本机私密配置。" -ForegroundColor Green
} else {
    Write-Host "[Interview Copilot] 尚未找到 .env.local，将以演示配置启动。" -ForegroundColor Yellow
}

$serverCommand = "Set-Location -LiteralPath '$($backendDir.Replace("'", "''"))'; & '$($python.Replace("'", "''"))' -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000"
Write-Host "[Interview Copilot] 正在启动 http://127.0.0.1:8000"
Start-Process powershell.exe -ArgumentList @("-NoExit", "-NoProfile", "-Command", $serverCommand)
Start-Sleep -Seconds 3
Start-Process "http://127.0.0.1:8000"
