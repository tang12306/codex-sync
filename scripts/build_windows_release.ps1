# Build the Windows installer: PyInstaller (onedir) for the app, then Inno Setup for a standard setup wizard.
# Requires PyInstaller + pywebview (pip install pyinstaller pywebview) and Inno Setup 6 (winget install JRSoftware.InnoSetup).
# Keep this file ASCII-only: Windows PowerShell 5.1 reads BOM-less files in the ANSI code page.
param(
    [string]$Version = ""
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

if (-not $Version) {
    $Match = Select-String -LiteralPath "codex_sync\__init__.py" -Pattern '__version__\s*=\s*"([^"]+)"'
    if (-not $Match) { throw "Cannot read __version__ from codex_sync\__init__.py" }
    $Version = $Match.Matches[0].Groups[1].Value
}
if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "Version must look like 1.2.3, got '$Version'"
}

function Find-Iscc {
    $cmd = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
    )
    foreach ($path in $candidates) {
        if ($path -and (Test-Path -LiteralPath $path)) { return $path }
    }
    throw "Inno Setup 6 (ISCC.exe) not found. Install it with: winget install JRSoftware.InnoSetup"
}

$Iscc = Find-Iscc
$Name = "CodexSync"
$InstallerName = "CodexSyncSetup-v$Version-windows-x64.exe"
$InstallerPath = Join-Path $Root "dist\$InstallerName"
$ShaPath = "$InstallerPath.sha256"

# Version resource shown in the exe's Properties -> Details tab
$Parts = $Version.Split(".") + @("0")
$VersionTuple = ($Parts[0..3] -join ", ")
$VersionInfoPath = Join-Path $Root "build\version_info.txt"
New-Item -ItemType Directory -Force -Path (Split-Path $VersionInfoPath) | Out-Null
$VersionInfo = @"
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=($VersionTuple),
    prodvers=($VersionTuple),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', 'Codex Sync contributors'),
        StringStruct('FileDescription', 'Codex Sync'),
        StringStruct('FileVersion', '$Version'),
        StringStruct('InternalName', '$Name'),
        StringStruct('LegalCopyright', 'Copyright (c) 2026 Codex Sync contributors. MIT License.'),
        StringStruct('OriginalFilename', '$Name.exe'),
        StringStruct('ProductName', 'Codex Sync'),
        StringStruct('ProductVersion', '$Version')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"@
[IO.File]::WriteAllText($VersionInfoPath, $VersionInfo, (New-Object System.Text.UTF8Encoding($false)))

python -m PyInstaller `
    --clean `
    --noconfirm `
    --onedir `
    --noconsole `
    --name $Name `
    --icon "assets\CodexSync.ico" `
    --version-file $VersionInfoPath `
    --add-data "assets\CodexSync.ico;assets" `
    --add-data "codex_sync\sync_server.py;codex_sync" `
    --collect-all webview `
    --collect-data codex_sync `
    --hidden-import tkinter `
    --hidden-import tkinter.filedialog `
    app_entry.py

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

Get-ChildItem -LiteralPath "dist" -Filter "CodexSyncSetup-v*-windows-x64.exe*" -ErrorAction SilentlyContinue | Remove-Item -Force

& $Iscc /Qp "/DAppVersion=$Version" "/DSourceDir=$Root\dist\$Name" "/DOutputDir=$Root\dist" "installer\CodexSync.iss"
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup failed with exit code $LASTEXITCODE"
}

$Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $InstallerPath).Hash.ToLowerInvariant()
Set-Content -Encoding ASCII -LiteralPath $ShaPath -Value "$Hash  $InstallerName"

Write-Host "Built $InstallerPath"
Write-Host "SHA256 $Hash"
