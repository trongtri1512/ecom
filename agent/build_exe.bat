@echo off
REM ============================================================
REM  Build ScanEcomAgent.exe (chay tren Windows)
REM  Tu do tim Python (uu tien 'py'), dung ngay neu co loi.
REM ============================================================
setlocal
cd /d "%~dp0"

REM --- Tim lenh Python: uu tien 'py' (luon co sau khi cai Python) ---
set "PY="
py --version >nul 2>&1 && set "PY=py"
if not defined PY (
  python --version >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo [LOI] Khong tim thay Python.
  echo   - Neu vua cai Python: DONG cua so nay, MO LAI roi chay lai.
  echo   - Hoac cai lai Python va TICK "Add Python to PATH".
  echo   - Hoac dung file build_exe.ps1 ^(PowerShell^) de tu cai Python.
  pause
  exit /b 1
)
echo Dung Python: %PY%
%PY% --version

echo.
echo [1/4] Tao moi truong ao...
%PY% -m venv .venv || goto :err
set "VPY=.venv\Scripts\python.exe"

echo [2/4] Cai thu vien...
"%VPY%" -m pip install --upgrade pip || goto :err
"%VPY%" -m pip install -r requirements.txt || goto :err
"%VPY%" -m pip install pyinstaller==6.11.1 || goto :err

echo [3/4] Dong goi thanh 1 file .exe (chay ngam, chi hien icon khay)...
"%VPY%" -m PyInstaller --noconfirm --clean ScanEcomAgent.spec || goto :err

if not exist "dist\ScanEcomAgent.exe" goto :err
echo.
echo [4/4] XONG! File EXE: dist\ScanEcomAgent.exe
echo    Nho de config.ini CANH BEN file .exe truoc khi chay.
echo.
pause
exit /b 0

:err
echo.
echo [LOI] Build that bai o buoc tren. Xem thong bao loi phia tren.
pause
exit /b 1
