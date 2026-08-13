# ============================================================
#  build_exe.ps1  -  Tu cai Python (neu thieu) roi build ScanEcomAgent.exe
#  Chay 1 lenh trong PowerShell (dung tai thu muc agent):
#      powershell -ExecutionPolicy Bypass -File build_exe.ps1
#  Hoac nhap chuot phai build_exe.ps1 -> "Run with PowerShell".
#
#  LUU Y: file nay chi dung ASCII (khong dau) de tuong thich moi PowerShell
#  version (5.1 doc script khong BOM = ANSI; giu ASCII de khong bao gio loi
#  encoding).
# ============================================================

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Write-Step($n, $msg) { Write-Host "`n[$n] $msg" -ForegroundColor Cyan }

# --- Tim Python 3.12; neu chua co thi cai qua winget ---
# PHAI dung Python 3.12 (KHONG 3.13): tkinter tren 3.13 co bug voi PyInstaller
# -> exe crash "Tk data directory not found". 3.12 stable, verify OK.
function Get-Python312 {
    # Uu tien py -3.12 (Python Launcher) - specific.
    try {
        $v = & py -3.12 --version 2>&1
        if ($LASTEXITCODE -eq 0 -and "$v" -match "Python 3\.12") { return @("py", "-3.12") }
    } catch {}
    # Fallback: python neu tinh co la 3.12.
    try {
        $v = & python --version 2>&1
        if ($LASTEXITCODE -eq 0 -and "$v" -match "Python 3\.12") { return @("python") }
    } catch {}
    return $null
}

Write-Step 1 "Kiem tra Python 3.12..."
$pyArgs = Get-Python312
if (-not $pyArgs) {
    Write-Host "    Chua co Python 3.12. Tu cai now..." -ForegroundColor Yellow

    $installed = $false

    # Cach A: winget (Windows 10 22H2+ / 11).
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "    Thu winget..." -ForegroundColor Yellow
        winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
        if ($LASTEXITCODE -eq 0) { $installed = $true }
    }

    # Cach B: tai .exe tu python.org (Windows Server / Windows cu khong co winget).
    if (-not $installed) {
        Write-Host "    Khong co winget hoac winget fail -> tai installer tu python.org..." -ForegroundColor Yellow
        $pyExe = "$env:TEMP\python-3.12.8-amd64.exe"
        $pyUrl = "https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe"
        try {
            # Windows Server 2016 / Windows cu mac dinh TLS 1.0/1.1 -> python.org
            # tu choi. Ep TLS 1.2 cho session hien tai.
            [Net.ServicePointManager]::SecurityProtocol = `
                [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls13
            Invoke-WebRequest -Uri $pyUrl -OutFile $pyExe -UseBasicParsing
            Write-Host "    Cai Python 3.12.8 silent (co Add to PATH, Python Launcher)..." -ForegroundColor Yellow
            $proc = Start-Process -Wait -PassThru -FilePath $pyExe -ArgumentList @(
                "/quiet",
                "InstallAllUsers=1",
                "PrependPath=1",
                "Include_launcher=1",
                "Include_pip=1",
                "Include_test=0",
                "Include_doc=0"
            )
            if ($proc.ExitCode -eq 0) { $installed = $true }
            else { Write-Host "    Installer ExitCode = $($proc.ExitCode)" -ForegroundColor Red }
        } catch {
            Write-Host "    [LOI] Khong tai duoc installer: $_" -ForegroundColor Red
        }
    }

    if (-not $installed) {
        Write-Host "    [LOI] Khong cai duoc Python 3.12 tu dong." -ForegroundColor Red
        Write-Host "    Cai tay: https://www.python.org/downloads/release/python-3128/" -ForegroundColor Red
        Write-Host "    Nho tick 'Add python.exe to PATH' khi cai." -ForegroundColor Red
        exit 1
    }

    # Nap lai PATH cho phien hien tai de thay python vua cai.
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")
    $pyArgs = Get-Python312
    if (-not $pyArgs) {
        Write-Host "    Da cai Python 3.12 nhung phien PowerShell nay chua thay." -ForegroundColor Yellow
        Write-Host "    DONG PowerShell, MO LAI, roi chay lai script nay." -ForegroundColor Yellow
        exit 1
    }
}
Write-Host "    OK: $(& $pyArgs[0] $pyArgs[1..99] --version 2>&1)" -ForegroundColor Green

Write-Step 2 "Tao moi truong ao (.venv)..."
& $pyArgs[0] $pyArgs[1..99] -m venv .venv
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

Write-Step 3 "Cai thu vien + PyInstaller..."
& $venvPy -m pip install --upgrade pip
& $venvPy -m pip install -r requirements.txt
# PyInstaller 6.10+ co bug tren Python 3.12 runtime hook pyi_rth__tkinter:
# no tim _MEIPASS/_tk_data khong thay -> exe crash.
# PyInstaller 6.6.0 la ban stable cuoi khong dinh bug nay. Cai kem
# pyinstaller-hooks-contrib PHIEN BAN TUONG UNG de dam bao tkinter hook chay dung.
& $venvPy -m pip install "pyinstaller==6.6.0" "pyinstaller-hooks-contrib==2024.8"

Write-Step 4 "Dong goi thanh 1 file .exe (chay ngam, chi icon khay)..."
# Xoa cache PYZ va build folder de PyInstaller khong dung file cu.
# --clean cua PyInstaller KHONG xoa het cache -> phai xoa thu cong.
$pyiCache = Join-Path $env:LOCALAPPDATA "pyinstaller"
if (Test-Path $pyiCache) {
    Write-Host "    Xoa cache PyInstaller: $pyiCache" -ForegroundColor Yellow
    Remove-Item $pyiCache -Recurse -Force -ErrorAction SilentlyContinue
}
if (Test-Path "build") {
    Remove-Item "build" -Recurse -Force -ErrorAction SilentlyContinue
}
if (Test-Path "dist\ScanEcomAgent.exe") {
    Remove-Item "dist\ScanEcomAgent.exe" -Force -ErrorAction SilentlyContinue
}
# Verify VERSION source truoc khi build de log ro
$srcVer = (Get-Content "VERSION" -Raw).Trim()
Write-Host "    Build voi VERSION source = $srcVer" -ForegroundColor Cyan

& $venvPy -m PyInstaller --noconfirm --clean ScanEcomAgent.spec

$exe = Join-Path $PSScriptRoot "dist\ScanEcomAgent.exe"
if (Test-Path $exe) {
    Write-Host "`n==================================================" -ForegroundColor Green
    Write-Host " XONG! File EXE: $exe" -ForegroundColor Green
    Write-Host " Buoc tiep: chep ScanEcomAgent.exe + config.ini vao 1 thu muc," -ForegroundColor Green
    Write-Host " sua config.ini (url, api_key), roi nhap dup de chay." -ForegroundColor Green
    Write-Host " Muon tu chay khi mo may: chay install_autostart.bat" -ForegroundColor Green
    Write-Host "==================================================" -ForegroundColor Green
} else {
    Write-Host "`n[LOI] Khong thay file .exe sau khi build. Xem log phia tren." -ForegroundColor Red
    exit 1
}
