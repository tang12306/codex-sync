param(
    [string]$Version = "0.2.0"
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

$Name = "CodexSync"
$SetupName = "CodexSyncSetup"
$InstallerName = "$Name" + "Setup-v$Version-windows-x64.exe"
$InstallerPath = Join-Path $Root "dist\$InstallerName"
$ShaPath = "$InstallerPath.sha256"

python -m PyInstaller `
    --clean `
    --noconfirm `
    --onefile `
    --noconsole `
    --name $SetupName `
    --icon "assets\CodexSync.ico" `
    --add-data "assets\CodexSync.ico;assets" `
    --add-data "codex_sync\sync_server.py;codex_sync" `
    --collect-all webview `
    --collect-data codex_sync `
    app_entry.py

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

Get-ChildItem -LiteralPath "dist" -Filter "CodexSyncSetup-v*-windows-x64.exe*" -ErrorAction SilentlyContinue | Remove-Item -Force
Get-ChildItem -LiteralPath "dist" -Filter "CodexSync-v*-windows-x64*" -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force
if (Test-Path -LiteralPath "dist\$Name.exe") {
    Remove-Item -LiteralPath "dist\$Name.exe" -Force
}
if (Test-Path -LiteralPath $InstallerPath) {
    Remove-Item -LiteralPath $InstallerPath -Force
}
Move-Item -LiteralPath "dist\$SetupName.exe" -Destination $InstallerPath

$Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $InstallerPath).Hash.ToLowerInvariant()
Set-Content -Encoding ASCII -LiteralPath $ShaPath -Value "$Hash  $InstallerName"

Write-Host "Built $InstallerPath"
Write-Host "SHA256 $Hash"
