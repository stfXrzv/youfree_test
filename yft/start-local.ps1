param(
  [string]$DataRoot = (Join-Path $PSScriptRoot '..\..\work\yft-lab'),
  [int]$Port = 8183
)
$ErrorActionPreference = 'Stop'
$labRoot = [IO.Path]::GetFullPath($DataRoot)
$env:YFT_ROOT = $labRoot
$env:YFT_YTAPI_ROOT = Join-Path $labRoot 'ytapi'
$env:YFT_STATIC_ROOT = $PSScriptRoot
$env:YFT_TMP = Join-Path $labRoot 'tmp'
$env:YFT_COOKIES = Join-Path $labRoot 'ytapi\cookies.txt'
$env:YFT_YTDLP_PROXY = ''
$env:YFT_POT_PROVIDER_URL = ''
$env:YFT_PORT = [string]$Port
$env:YFT_HOST = '127.0.0.1'
$ffmpegCommand = Get-Command ffmpeg -ErrorAction SilentlyContinue
if ($ffmpegCommand) {
  $env:YFT_FFMPEG = $ffmpegCommand.Source
} else {
  $ffmpegBuild = Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages\*FFmpeg*\*\bin\ffmpeg.exe') -File -ErrorAction SilentlyContinue | Select-Object -First 1
  if (-not $ffmpegBuild) { throw 'FFmpeg was not found on this laptop.' }
  $env:YFT_FFMPEG = $ffmpegBuild.FullName
}
$pythonExe = Join-Path $labRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { throw 'The local YFT virtual environment is missing.' }
Write-Output "YFT: http://localhost:$Port/yft/"
& $pythonExe (Join-Path $PSScriptRoot 'server.py')
