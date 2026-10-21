# Туннель ноута на сервер (21.10.2026): облака и обратный ход без Tailscale.
#
# Одно SSH-подключение по публичному адресу сервера и два проброса:
#   -L 8888  прокси Jarvis на сервере (скилл relay): HTTPS_PROXY=http://127.0.0.1:8888
#            в .env — через него ходят OpenAI, Deepgram, Fish, Telegram и git;
#   -R 2222  обратно на sshd ноута: `ssh laptop` с сервера (логи, помощь).
# Tailscale для этого не годился: адрес сервера в нём сменился, и прокси лёг.
#
# Запускает его задание Планировщика (install-tunnel.ps1), окна нет. Оборвалось —
# через 10 секунд заново: сон, смена сети, перезагрузка сервера.

param(
    [string]$Server = "claude-work.duckdns.org",
    [string]$User = "root",
    [int]$Proxy = 8888,
    [int]$Back = 2222
)

$ssh = Join-Path $env:WINDIR "System32\OpenSSH\ssh.exe"
$log = Join-Path (Split-Path $PSScriptRoot -Parent) "logs\tunnel.log"
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

function Write-Log([string]$text) {
    $line = "{0:dd.MM.yy, HH:mm:ss} {1}" -f (Get-Date), $text
    Add-Content -Path $log -Value $line -Encoding utf8
}

$arguments = @(
    "-N",
    "-o", "BatchMode=yes",                 # спросить пароль некого
    "-o", "ExitOnForwardFailure=yes",      # порт занят — выйти и попробовать снова
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
    "-o", "StrictHostKeyChecking=accept-new",
    "-L", "127.0.0.1:${Proxy}:127.0.0.1:8888",
    "-R", "127.0.0.1:${Back}:127.0.0.1:22",
    "$User@$Server"
)

while ($true) {
    Write-Log "подключаюсь к $Server"
    $started = Get-Date
    $errors = & $ssh @arguments 2>&1 | Out-String
    $code = $LASTEXITCODE
    $lasted = [int]((Get-Date) - $started).TotalSeconds
    Write-Log ("оборвалось через {0} с, код {1}: {2}" -f $lasted, $code, $errors.Trim())
    Start-Sleep -Seconds 10
}
