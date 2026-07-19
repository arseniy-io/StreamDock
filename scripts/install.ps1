param(
    [switch]$UpgradeDependencies,
    [switch]$SkipNativeHost
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$ProgressPreference = "SilentlyContinue"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$venvDirectory = Join-Path $projectRoot ".venv"
$venvPython = Join-Path $venvDirectory "Scripts\python.exe"
$requirements = Join-Path $projectRoot "requirements.txt"
$constraints = Join-Path $projectRoot "constraints.txt"

function Invoke-CheckedCommand {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$FailureMessage
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw $FailureMessage
    }
}

function Get-PythonCandidate {
    $candidates = @(
        [pscustomobject]@{ Command = "py.exe"; Prefix = @("-3") },
        [pscustomobject]@{ Command = "python.exe"; Prefix = @() },
        [pscustomobject]@{ Command = "python3.exe"; Prefix = @() }
    )

    foreach ($candidate in $candidates) {
        $resolved = Get-Command $candidate.Command -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $resolved) {
            continue
        }

        try {
            $probeArguments = @($candidate.Prefix) + @(
                "-c",
                "import struct,sys; print(f'{sys.version_info.major}|{sys.version_info.minor}|{struct.calcsize(`"P`") * 8}')"
            )
            $probeOutput = & $resolved.Source @probeArguments 2>$null
            if ($LASTEXITCODE -ne 0) {
                continue
            }
            $versionLine = $probeOutput | Where-Object { $_ -match '^\d+\|\d+\|\d+$' } | Select-Object -Last 1
            if (-not $versionLine) {
                continue
            }
            $parts = $versionLine -split '\|'
            if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 11)) {
                continue
            }
            if ([int]$parts[2] -ne 64) {
                continue
            }
            return [pscustomobject]@{
                File = $resolved.Source
                Prefix = @($candidate.Prefix)
                Version = "$($parts[0]).$($parts[1])"
            }
        }
        catch {
            continue
        }
    }

    throw @"
Не найден 64-битный Python 3.11 или новее.
Установите Python с https://www.python.org/downloads/windows/
Во время установки включите пункт Add Python to PATH, затем снова запустите install.bat.
"@
}

function Test-VenvPython {
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        return $false
    }
    & $venvPython -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
    return $LASTEXITCODE -eq 0
}

try {
    Write-Host ""
    Write-Host "StreamDock: установка локального приложения" -ForegroundColor Cyan
    Write-Host "Папка проекта: $projectRoot"

    if (-not (Test-Path -LiteralPath $requirements -PathType Leaf)) {
        throw "Не найден requirements.txt. Распакуйте весь архив проекта в одну папку."
    }
    if (-not (Test-Path -LiteralPath $constraints -PathType Leaf)) {
        throw "Не найден constraints.txt. Распакуйте весь архив проекта в одну папку."
    }

    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        $python = Get-PythonCandidate
        Write-Host "Найден Python $($python.Version), создаём .venv..." -ForegroundColor Green
        $venvArguments = @($python.Prefix) + @("-m", "venv", $venvDirectory)
        Invoke-CheckedCommand -FilePath $python.File -Arguments $venvArguments -FailureMessage "Не удалось создать .venv."
    }
    elseif (-not (Test-VenvPython)) {
        throw "Существующая .venv повреждена или создана Python ниже 3.11. Удалите только папку .venv и снова запустите install.bat."
    }
    else {
        Write-Host "Используем существующее окружение .venv." -ForegroundColor Green
    }

    Write-Host "Обновляем установщик Python-пакетов..."
    Invoke-CheckedCommand -FilePath $venvPython -Arguments @("-m", "pip", "install", "--upgrade", "pip") -FailureMessage "Не удалось обновить pip. Проверьте интернет-соединение."

    Write-Host "Устанавливаем зависимости StreamDock. Это может занять несколько минут..."
    $pipArguments = @("-m", "pip", "install")
    if ($UpgradeDependencies) {
        $pipArguments += "--upgrade"
    }
    $pipArguments += @("-r", $requirements, "-c", $constraints)
    Invoke-CheckedCommand -FilePath $venvPython -Arguments $pipArguments -FailureMessage "Не удалось установить зависимости. Проверьте интернет-соединение и свободное место."
    Invoke-CheckedCommand -FilePath $venvPython -Arguments @("-m", "pip", "check") -FailureMessage "Установленные Python-пакеты несовместимы между собой."

    foreach ($directoryName in @("downloads", "models", "logs")) {
        $directory = Join-Path $projectRoot $directoryName
        New-Item -ItemType Directory -Force -Path $directory | Out-Null
    }

    Write-Host ""
    & $venvPython (Join-Path $PSScriptRoot "system_check.py")
    if ($LASTEXITCODE -ne 0) {
        throw "Проверка Python-компонентов завершилась ошибкой."
    }

    if (-not $SkipNativeHost) {
        $nativeInstaller = Join-Path $PSScriptRoot "install_native_host.ps1"
        if (Test-Path -LiteralPath $nativeInstaller -PathType Leaf) {
            Write-Host ""
            Write-Host "Регистрируем локальный помощник расширения Chrome..."
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $nativeInstaller
            if ($LASTEXITCODE -ne 0) {
                Write-Warning "Помощник Chrome не установился. Само приложение можно запускать через start.bat."
            }
        }
    }

    Write-Host ""
    Write-Host "Готово. Запускайте приложение через start.bat." -ForegroundColor Green
    Write-Host "Модели заранее загружать не обязательно. Для этого есть download_models.bat."
    exit 0
}
catch {
    Write-Host ""
    Write-Host "Ошибка установки: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
