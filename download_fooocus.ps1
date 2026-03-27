# Fooocus: скачать и распаковать (Windows)
# В релизах GitHub нет готовых .7z — качаем исходники и создаём .bat для запуска.

$ErrorActionPreference = "Stop"
$baseDir = $PSScriptRoot
$api = "https://api.github.com/repos/lllyasviel/Fooocus/releases/latest"
$release = Invoke-RestMethod -Uri $api

# Пробуем скачать готовый Windows-архив из assets
$asset = $release.assets | Where-Object { $_.name -match '\.(7z|zip)$' -and $_.name -match 'win|windows' } | Select-Object -First 1
if (-not $asset) { $asset = $release.assets | Where-Object { $_.name -match '\.(7z|zip)$' } | Select-Object -First 1 }

if ($asset) {
    $url = $asset.browser_download_url
    $out = Join-Path $baseDir $asset.name
    Write-Host "Downloading $($asset.name) ..."
    $ProgressPreference = 'SilentlyContinue'
    Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing
    Write-Host "Saved: $out"
    Write-Host "Unpack with 7-Zip to a folder, then run run_nvidia_gpu.bat or run_cpu.bat from that folder."
    exit 0
}

# Нет assets — качаем исходники (zipball)
$tag = $release.tag_name
$zipUrl = "https://github.com/lllyasviel/Fooocus/archive/refs/tags/$tag.zip"
$zipPath = Join-Path $baseDir "Fooocus-$tag.zip"
$extractDir = Join-Path $baseDir "Fooocus-$tag"

Write-Host "No Windows .7z in release; downloading source $tag..."
$ProgressPreference = 'SilentlyContinue'
Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath -UseBasicParsing

Write-Host "Extracting..."
Expand-Archive -Path $zipPath -DestinationPath $baseDir -Force
$ver = $tag.TrimStart('v')
$extractDir = (Resolve-Path (Join-Path $baseDir "Fooocus-$ver")).Path

# Создаём .bat для запуска (Python + launch.py)
$batGpu = @"
@echo off
cd /d "%~dp0"
if exist "fooocus_env\Scripts\activate.bat" (
    call fooocus_env\Scripts\activate.bat
) else (
    echo Creating venv and installing deps (first run may take a while)...
    python -m venv fooocus_env
    call fooocus_env\Scripts\activate.bat
    pip install -r requirements_versions.txt
)
python launch.py
pause
"@

$batCpu = @"
@echo off
cd /d "%~dp0"
if exist "fooocus_env\Scripts\activate.bat" (
    call fooocus_env\Scripts\activate.bat
) else (
    echo Creating venv and installing deps (first run may take a while)...
    python -m venv fooocus_env
    call fooocus_env\Scripts\activate.bat
    pip install -r requirements_versions.txt
)
python launch.py --always-cpu
pause
"@

[System.IO.File]::WriteAllText((Join-Path $extractDir "run_nvidia_gpu.bat"), $batGpu)
[System.IO.File]::WriteAllText((Join-Path $extractDir "run_cpu.bat"), $batCpu)

Write-Host ""
Write-Host "Done. Folder: $extractDir"
Write-Host "  - GPU: run_nvidia_gpu.bat"
Write-Host "  - CPU: run_cpu.bat"
Write-Host "Requires Python 3.10 (not 3.12). First run will create venv and install deps."
Remove-Item $zipPath -Force -ErrorAction SilentlyContinue
