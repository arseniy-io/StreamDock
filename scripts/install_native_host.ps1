$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$sourcePath = Join-Path $projectRoot "scripts\native-host\StreamDockHost.cs"
$outputDirectory = Join-Path $projectRoot "scripts\native-host\bin"
$hostExecutable = Join-Path $outputDirectory "StreamDockHost.exe"
$hostManifest = Join-Path $outputDirectory "com.streamdock.launcher.json"
$extensionId = "pkodbfmcfgicbhcbmigbdkmkchppgeki"
$hostName = "com.streamdock.launcher"

New-Item -ItemType Directory -Force -Path $outputDirectory | Out-Null

$compilerCandidates = @(
    (Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"),
    (Join-Path $env:WINDIR "Microsoft.NET\Framework\v4.0.30319\csc.exe")
)
$compiler = $compilerCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $compiler) {
    throw "Не найден системный компилятор Windows для локального помощника."
}

$frameworkDirectory = Split-Path -Parent $compiler
$webExtensions = Join-Path $frameworkDirectory "System.Web.Extensions.dll"
& $compiler /nologo /target:exe /optimize+ /platform:anycpu /utf8output "/reference:$webExtensions" "/out:$hostExecutable" $sourcePath
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $hostExecutable)) {
    throw "Не удалось собрать локальный помощник StreamDock."
}

$manifestData = [ordered]@{
    name = $hostName
    description = "Безопасно запускает и останавливает локальное приложение StreamDock"
    path = $hostExecutable
    type = "stdio"
    allowed_origins = @("chrome-extension://$extensionId/")
}
$json = $manifestData | ConvertTo-Json -Depth 4
$utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($hostManifest, $json, $utf8WithoutBom)

$registryPath = "HKCU:\Software\Google\Chrome\NativeMessagingHosts\$hostName"
New-Item -Path $registryPath -Force | Out-Null
Set-Item -Path $registryPath -Value $hostManifest

Write-Host "StreamDock установлен."
Write-Host "Локальный помощник зарегистрирован только для текущего пользователя Windows."
Write-Host "Теперь перезагрузите расширение на странице chrome://extensions."
