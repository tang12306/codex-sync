param(
    [string]$Version = "0.1.9"
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

$Name = "CodexSync"
$InstallerName = "$Name" + "Setup-v$Version-windows-x64.exe"
$InstallerPath = Join-Path $Root "dist\$InstallerName"
$ShaPath = "$InstallerPath.sha256"

python -m PyInstaller `
    --clean `
    --noconfirm `
    --onefile `
    --noconsole `
    --name $Name `
    --icon "assets\CodexSync.ico" `
    --add-data "assets\CodexSync.ico;assets" `
    --add-data "codex_sync\sync_server.py;codex_sync" `
    --collect-all webview `
    --collect-data codex_sync `
    app_entry.py

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

if (Test-Path -LiteralPath $InstallerPath) {
    Remove-Item -LiteralPath $InstallerPath -Force
}
Copy-Item -LiteralPath "dist\$Name.exe" -Destination $InstallerPath

$Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $InstallerPath).Hash.ToLowerInvariant()
Set-Content -Encoding ASCII -LiteralPath $ShaPath -Value "$Hash  $InstallerName"

Write-Host "Built $InstallerPath"
Write-Host "SHA256 $Hash"
