param(
  [string]$SshTarget = 'ubuntu@code2it.com',
  [string]$KeyPath = (Join-Path $env:USERPROFILE '.ssh\ovh_vps')
)
$ErrorActionPreference = 'Stop'
$health = Invoke-RestMethod -Uri 'http://127.0.0.1:8183/yft/api/health' -TimeoutSec 5
if (-not $health.ok) { throw 'Start the local YFT server first.' }
Write-Output 'YFT public forwarding: code2it.com/yft -> laptop:8183'
& ssh -N -T -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=20 -o ServerAliveCountMax=3 -i $KeyPath -R '127.0.0.1:18183:127.0.0.1:8183' $SshTarget
