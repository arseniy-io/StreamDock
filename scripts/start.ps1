$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$ProgressPreference = "SilentlyContinue"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$appUrl = "http://127.0.0.1:8765"
$healthUrl = "$appUrl/api/health"

function Test-StreamDockOnline {
    try {
        $response = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 2 -Headers @{ "Cache-Control" = "no-cache" }
        return $response.StatusCode -eq 200 -and $response.Headers["X-StreamDock-App"] -eq "1"
    }
    catch {
        return $false
    }
}

function Test-LocalPortOpen {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $connection = $client.ConnectAsync("127.0.0.1", 8765)
        return $connection.Wait(500) -and $client.Connected
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

try {
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        throw "Не найдена .venv. Сначала запустите install.bat."
    }
    if (-not (Test-Path -LiteralPath (Join-Path $projectRoot "app\main.py") -PathType Leaf)) {
        throw "Не найдены файлы приложения. Распакуйте весь архив StreamDock в одну папку."
    }

    if (Test-StreamDockOnline) {
        Write-Host "StreamDock уже запущен. Вторая копия не создаётся." -ForegroundColor Green
        Start-Process $appUrl
        exit 0
    }
    if (Test-LocalPortOpen) {
        throw "Порт 8765 занят другой программой. Закройте её и повторите запуск."
    }

    & $venvPython (Join-Path $PSScriptRoot "system_check.py")
    if ($LASTEXITCODE -ne 0) {
        throw "Python-компоненты StreamDock установлены не полностью. Запустите update.bat."
    }

    Write-Host ""
    Write-Host "Запускаем StreamDock только на локальном адресе $appUrl" -ForegroundColor Cyan
    Write-Host "Чтобы остановить ручной запуск, нажмите Ctrl+C в этом окне."

    $openerScript = @"
`$ProgressPreference = 'SilentlyContinue'
for (`$attempt = 0; `$attempt -lt 50; `$attempt++) {
    try {
        `$response = Invoke-WebRequest -Uri '$healthUrl' -UseBasicParsing -TimeoutSec 1
        if (`$response.StatusCode -eq 200 -and `$response.Headers['X-StreamDock-App'] -eq '1') {
            Start-Process '$appUrl'
            break
        }
    } catch {}
    Start-Sleep -Milliseconds 200
}
"@
    $encodedOpener = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($openerScript))
    $opener = Start-Process -FilePath "powershell.exe" -ArgumentList @(
        "-NoProfile", "-WindowStyle", "Hidden", "-EncodedCommand", $encodedOpener
    ) -WindowStyle Hidden -PassThru

    Push-Location $projectRoot
    try {
        & $venvPython -m uvicorn app.main:app --host 127.0.0.1 --port 8765
        $serverExitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
        if ($opener -and -not $opener.HasExited) {
            Stop-Process -Id $opener.Id -Force -ErrorAction SilentlyContinue
        }
    }

    if ($serverExitCode -ne 0) {
        throw "Локальный сервер завершился с кодом $serverExitCode. Подробности смотрите в logs\app.log."
    }
    exit 0
}
catch {
    Write-Host "Ошибка запуска: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
