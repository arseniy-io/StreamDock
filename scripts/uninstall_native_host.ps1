$ErrorActionPreference = "Stop"

$hostName = "com.streamdock.launcher"
$registryPath = "HKCU:\Software\Google\Chrome\NativeMessagingHosts\$hostName"
if (Test-Path -LiteralPath $registryPath) {
    Remove-Item -LiteralPath $registryPath -Recurse -Force
}

Write-Host "Локальный помощник StreamDock удалён из Chrome."
