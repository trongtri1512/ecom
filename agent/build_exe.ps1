# ============================================================
#  build_exe.ps1  —  Tự cài Python (nếu thiếu) rồi build ScanEcomAgent.exe
#  Chạy 1 lệnh trong PowerShell (đứng tại thư mục agent):
#      powershell -ExecutionPolicy Bypass -File build_exe.ps1
#  Hoặc nhấp chuột phải build_exe.ps1 -> "Run with PowerShell".
# ============================================================

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Write-Step($n, $msg) { Write-Host "`n[$n] $msg" -ForegroundColor Cyan }

# --- Tìm Python; nếu chưa có thì cài qua winget ---
function Get-PythonCmd {
    foreach ($c in @("python", "py")) {
        try {
            $v = & $c --version 2>&1
            if ($LASTEXITCODE -eq 0 -and $v -match "Python 3") { return $c }
        } catch {}
    }
    return $null
}

Write-Step 1 "Kiểm tra Python..."
$py = Get-PythonCmd
if (-not $py) {
    Write-Host "    Chưa có Python. Đang cài bằng winget..." -ForegroundColor Yellow
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host "    [LỖI] Máy không có winget (App Installer)." -ForegroundColor Red
        Write-Host "    Cách khác: tải Python tại https://www.python.org/downloads/ (nhớ tick 'Add to PATH')." -ForegroundColor Red
        exit 1
    }
    winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
    # Nạp lại PATH cho phiên hiện tại để thấy python vừa cài.
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")
    $py = Get-PythonCmd
    if (-not $py) {
        Write-Host "    Đã cài Python nhưng phiên PowerShell này chưa thấy." -ForegroundColor Yellow
        Write-Host "    ĐÓNG PowerShell, MỞ LẠI, rồi chạy lại script này." -ForegroundColor Yellow
        exit 1
    }
}
Write-Host "    OK: $(& $py --version)" -ForegroundColor Green

Write-Step 2 "Tạo môi trường ảo (.venv)..."
& $py -m venv .venv
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

Write-Step 3 "Cài thư viện + PyInstaller..."
& $venvPy -m pip install --upgrade pip
& $venvPy -m pip install -r requirements.txt
& $venvPy -m pip install pyinstaller==6.11.1

Write-Step 4 "Đóng gói thành 1 file .exe (chạy ngầm, chỉ icon khay)..."
& $venvPy -m PyInstaller --noconfirm --clean ScanEcomAgent.spec

$exe = Join-Path $PSScriptRoot "dist\ScanEcomAgent.exe"
if (Test-Path $exe) {
    Write-Host "`n==================================================" -ForegroundColor Green
    Write-Host " XONG! File EXE: $exe" -ForegroundColor Green
    Write-Host " Bước tiếp: chép ScanEcomAgent.exe + config.ini vào 1 thư mục," -ForegroundColor Green
    Write-Host " sửa config.ini (url, api_key), rồi nhấp đúp để chạy." -ForegroundColor Green
    Write-Host " Muốn tự chạy khi mở máy: chạy install_autostart.bat" -ForegroundColor Green
    Write-Host "==================================================" -ForegroundColor Green
} else {
    Write-Host "`n[LỖI] Không thấy file .exe sau khi build. Xem log phía trên." -ForegroundColor Red
    exit 1
}
