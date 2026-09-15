$ErrorActionPreference = "Stop"

Push-Location $PSScriptRoot
try {
    Write-Host "Installing dependencies..."
    py -m pip install -r ".\requirements.txt"
    py -m pip install "pyinstaller>=6,<7"

    Write-Host "Building CodexUsageTray.exe..."
    py -m PyInstaller `
      --noconfirm `
      --clean `
      --onefile `
      --noconsole `
      --name "CodexUsageTray" `
      --icon ".\codex_usage.ico" `
      --hidden-import "pystray._win32" `
      ".\codex_usage_tray.py"

    Write-Host ""
    Write-Host "Done:"
    Write-Host "$PSScriptRoot\dist\CodexUsageTray.exe"
}
finally {
    Pop-Location
}
