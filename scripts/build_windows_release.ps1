param(
    [string]$Version = "0.1.6"
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

$Name = "CodexSync"
$PackageName = "$Name-v$Version-windows-x64"
$PackageDir = Join-Path $Root "dist\$PackageName"
$ZipPath = Join-Path $Root "dist\$PackageName.zip"
$ShaPath = "$ZipPath.sha256"

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

if (Test-Path -LiteralPath $PackageDir) {
    Remove-Item -LiteralPath $PackageDir -Recurse -Force
}
New-Item -ItemType Directory -Path $PackageDir | Out-Null

Copy-Item -LiteralPath "dist\$Name.exe" -Destination (Join-Path $PackageDir "$Name.exe")
Copy-Item -LiteralPath "README.md" -Destination (Join-Path $PackageDir "README.md")
Copy-Item -LiteralPath "README.en.md" -Destination (Join-Path $PackageDir "README.en.md")
Copy-Item -LiteralPath "LICENSE" -Destination (Join-Path $PackageDir "LICENSE")
Copy-Item -LiteralPath "SECURITY.md" -Destination (Join-Path $PackageDir "SECURITY.md")

if (Test-Path -LiteralPath $ZipPath) {
    Remove-Item -LiteralPath $ZipPath -Force
}
Compress-Archive -Path (Join-Path $PackageDir "*") -DestinationPath $ZipPath -Force

$Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ZipPath).Hash.ToLowerInvariant()
Set-Content -Encoding ASCII -LiteralPath $ShaPath -Value "$Hash  $PackageName.zip"

Write-Host "Built $ZipPath"
Write-Host "SHA256 $Hash"
