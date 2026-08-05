# Install FFmpeg essentials into <repo>/tools/ffmpeg (Pinokio + local use).
# Idempotent: exits 0 if tools/ffmpeg/bin/ffmpeg.exe already exists.
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Tools = Join-Path $Root "tools"
$ZipName = "ffmpeg-release-essentials.zip"
$ZipPath = Join-Path $Tools $ZipName
$FfmpegDir = Join-Path $Tools "ffmpeg"
$FfmpegExe = Join-Path $FfmpegDir "bin\ffmpeg.exe"
$Url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

if (Test-Path -LiteralPath $FfmpegExe) {
    Write-Host "FFmpeg already present: $FfmpegExe"
    exit 0
}

New-Item -ItemType Directory -Force -Path $Tools | Out-Null

if (-not (Test-Path -LiteralPath $ZipPath)) {
    Write-Host "Downloading $Url ..."
    Invoke-WebRequest -Uri $Url -OutFile $ZipPath
}

if (-not (Test-Path -LiteralPath $ZipPath)) {
    throw "ZIP missing after download: $ZipPath"
}

$zipSize = (Get-Item -LiteralPath $ZipPath).Length
if ($zipSize -lt 1MB) {
    throw "ZIP looks too small ($zipSize bytes); download may be an HTML error page: $ZipPath"
}

if (Test-Path -LiteralPath $FfmpegDir) {
    Remove-Item -LiteralPath $FfmpegDir -Recurse -Force
}

Write-Host "Extracting $ZipPath ..."
Expand-Archive -LiteralPath $ZipPath -DestinationPath $Tools -Force

$extracted = Get-ChildItem -LiteralPath $Tools -Directory -Filter "ffmpeg-*" | Select-Object -First 1
if ($null -eq $extracted) {
    throw "No ffmpeg-* folder after extract in $Tools"
}

Rename-Item -LiteralPath $extracted.FullName -NewName "ffmpeg"

if (-not (Test-Path -LiteralPath $FfmpegExe)) {
    throw "ffmpeg.exe missing after extract: $FfmpegExe"
}

Remove-Item -LiteralPath $ZipPath -Force
Write-Host "FFmpeg ready: $FfmpegExe"
