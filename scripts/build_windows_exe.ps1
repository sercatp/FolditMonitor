[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$buildRoot = Join-Path $projectRoot "build"
$distRoot = Join-Path $projectRoot "dist"
$releaseRoot = Join-Path $projectRoot "release"
$packageRoot = Join-Path $distRoot "FolditMonitor"
$archivePath = Join-Path $releaseRoot "FolditMonitor-windows-x64.zip"
$alertPath = Join-Path $projectRoot "alert.wav"
$defaultsProfilePath = Join-Path $projectRoot "Foldit Monitor.defaults.json"

Set-Location $projectRoot

& python -m PyInstaller --version
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing the EXE build dependency (PyInstaller)..."
    & python -m pip install --upgrade pyinstaller
    if ($LASTEXITCODE -ne 0) {
        throw "Could not install PyInstaller."
    }
}

& python -m PyInstaller --noconfirm --clean --windowed --onedir --name "FolditMonitor" --paths $projectRoot --add-data "$alertPath;." --add-data "$defaultsProfilePath;." --collect-all frida --workpath $buildRoot --distpath $distRoot --specpath $buildRoot "Foldit Monitor.pyw"
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller did not complete successfully."
}

New-Item -ItemType Directory -Path $releaseRoot -Force | Out-Null
if (Test-Path -LiteralPath $archivePath) {
    Remove-Item -LiteralPath $archivePath -Force
}
Compress-Archive -Path $packageRoot -DestinationPath $archivePath -Force

Write-Host "Build complete:"
Write-Host "  EXE: $packageRoot\FolditMonitor.exe"
Write-Host "  Archive: $archivePath"
