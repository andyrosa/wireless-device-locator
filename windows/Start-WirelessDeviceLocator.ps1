$ErrorActionPreference = 'Stop'
$finderRoot = Split-Path -Parent $PSScriptRoot
$finderUrl = 'http://127.0.0.1:8731'
try {
    $finderStatus = Invoke-RestMethod "$finderUrl/api/status" -TimeoutSec 2
    if ($finderStatus.app -eq 'bluetooth-finder') {
        Start-Process $finderUrl
        exit
    }
} catch {}
$finderPython = (Get-Command python.exe).Source
if (-not (Test-Path (Join-Path $finderRoot '.vendor\bleak\__init__.py'))) {
    & $finderPython -m pip install --target (Join-Path $finderRoot '.vendor') -r (Join-Path $finderRoot 'requirements-wireless-device-locator.txt') --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) { throw 'Could not install the Wireless Device Locator dependencies.' }
}
$logDirectory = Join-Path $finderRoot 'logs'
[IO.Directory]::CreateDirectory($logDirectory) | Out-Null
Start-Process -FilePath $finderPython -ArgumentList @('"' + (Join-Path $finderRoot 'wireless_device_locator_server.py') + '"') -WorkingDirectory $finderRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDirectory 'wireless-device-locator-server.log') -RedirectStandardError (Join-Path $logDirectory 'wireless-device-locator-server-error.log')
for ($finderAttempt = 0; $finderAttempt -lt 20; $finderAttempt++) {
    Start-Sleep -Milliseconds 500
    try {
        $finderStatus = Invoke-RestMethod "$finderUrl/api/status" -TimeoutSec 1
        if ($finderStatus.app -eq 'bluetooth-finder') { Start-Process $finderUrl; exit }
    } catch {}
}
throw 'The local reader did not start. Check logs\wireless-device-locator-server-error.log.'
