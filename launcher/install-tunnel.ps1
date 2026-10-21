# Поставить туннель ноута на сервер (21.10.2026). Запускать один раз, в
# PowerShell от администратора:
#
#   powershell -ExecutionPolicy Bypass -File D:\Jarvis\launcher\install-tunnel.ps1
#
# Что делает:
#   1. задание Планировщика «Jarvis tunnel» — tunnel.ps1 при старте Windows, без
#      окна и без входа (S4U), не останавливается от батареи и по времени;
#   2. в .env адрес прокси из Tailscale меняется на 127.0.0.1:8888 (копия рядом);
#   3. проверяет, что через туннель отвечает GitHub.
# Убрать: Unregister-ScheduledTask -TaskName "Jarvis tunnel"

#Requires -RunAsAdministrator

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$name = "Jarvis tunnel"

# 1. Задание.
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSScriptRoot\tunnel.ps1`""
$triggers = @(
    (New-ScheduledTaskTrigger -AtStartup),
    (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME)
)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable
Register-ScheduledTask -TaskName $name -Action $action -Trigger $triggers -Principal $principal `
    -Settings $settings -Force | Out-Null
Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName $name
Write-Host "Задание «$name» поставлено и запущено."

# 2. Прокси в .env.
$envFile = Join-Path $root ".env"
if (Test-Path $envFile) {
    $text = [IO.File]::ReadAllText($envFile)
    $fixed = [regex]::Replace($text, '(?m)^(\s*HTTPS?_PROXY\s*=\s*http://)[^\s/:]+:8888', '${1}127.0.0.1:8888')
    if ($fixed -ne $text) {
        Copy-Item $envFile "$envFile.bak-tailscale" -Force
        [IO.File]::WriteAllText($envFile, $fixed, (New-Object Text.UTF8Encoding $false))
        Write-Host ".env: прокси теперь 127.0.0.1:8888 (копия — .env.bak-tailscale)."
    } else {
        Write-Host ".env: строк HTTPS_PROXY с портом 8888 не нашлось или уже 127.0.0.1 — не трогал."
    }
}

# 3. Проверка.
Start-Sleep -Seconds 8
$code = & curl.exe -s -o NUL -w "%{http_code}" -m 15 -x http://127.0.0.1:8888 https://api.github.com/
if ($code -eq "200") {
    Write-Host "Туннель работает: GitHub ответил через сервер."
} else {
    Write-Host "GitHub через туннель не ответил (код '$code'). Смотри $root\logs\tunnel.log"
}
