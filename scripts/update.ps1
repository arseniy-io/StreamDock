$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$ProgressPreference = "SilentlyContinue"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$healthUrl = "http://127.0.0.1:8765/api/health"

function Test-StreamDockOnline {
    try {
        $response = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 2
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
    if ((Test-StreamDockOnline) -or (Test-LocalPortOpen)) {
        throw "Сначала остановите StreamDock. Нельзя обновлять используемые приложением файлы."
    }

    Write-Host "StreamDock: обновление зависимостей текущей версии" -ForegroundColor Cyan
    Write-Host "Этот файл не скачивает исходный код. Сначала получите новую версию проекта, затем запустите update.bat."
    $installScript = Join-Path $PSScriptRoot "install.ps1"
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installScript -UpgradeDependencies
    if ($LASTEXITCODE -ne 0) {
        throw "Установщик зависимостей завершился с ошибкой."
    }
    exit 0
}
catch {
    Write-Host "Ошибка обновления: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
