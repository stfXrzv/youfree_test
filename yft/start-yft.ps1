param([string]$DataRoot = (Join-Path $PSScriptRoot '..\..\work\yft-lab'))
$ErrorActionPreference = 'Stop'
$labRoot = [IO.Path]::GetFullPath($DataRoot)
$shellExe = (Get-Process -Id $PID).Path
$serverReady = $false
try { $serverReady = (Invoke-RestMethod 'http://127.0.0.1:8183/yft/api/health' -TimeoutSec 3).service -eq 'yft' } catch {}
if (-not $serverReady) {
  $localScript = Join-Path $PSScriptRoot 'start-local.ps1'
  $localArgs = '-NoProfile -ExecutionPolicy Bypass -File "' + $localScript + '" -DataRoot "' + $labRoot + '"'
  $server = Start-Process -FilePath $shellExe -ArgumentList $localArgs -WindowStyle Hidden -RedirectStandardOutput (Join-Path $labRoot 'server.stdout.log') -RedirectStandardError (Join-Path $labRoot 'server.stderr.log') -PassThru
  $server.Id | Set-Content -LiteralPath (Join-Path $labRoot 'server-launch.pid')
  for ($attempt = 0; $attempt -lt 20; $attempt++) {
    try { $serverReady = (Invoke-RestMethod 'http://127.0.0.1:8183/yft/api/health' -TimeoutSec 1).ok; if ($serverReady) { break } } catch {}
    Start-Sleep -Milliseconds 250
  }
  if (-not $serverReady) { throw 'YFT did not start. See server.stderr.log in the local lab folder.' }
}
$tunnel = Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" | Where-Object { $_.CommandLine -like '*127.0.0.1:18183:127.0.0.1:8183*' } | Select-Object -First 1
if (-not $tunnel) {
  $tunnelScript = Join-Path $PSScriptRoot 'start-public-tunnel.ps1'
  $tunnelArgs = '-NoProfile -ExecutionPolicy Bypass -File "' + $tunnelScript + '"'
  $forward = Start-Process -FilePath $shellExe -ArgumentList $tunnelArgs -WindowStyle Hidden -RedirectStandardOutput (Join-Path $labRoot 'tunnel.stdout.log') -RedirectStandardError (Join-Path $labRoot 'tunnel.stderr.log') -PassThru
  $forward.Id | Set-Content -LiteralPath (Join-Path $labRoot 'tunnel-launch.pid')
}
Write-Output 'Local: http://localhost:8183/yft/'
Write-Output 'Public: https://code2it.com/yft/'
