@echo off
REM ============================================================
REM  Build ScanEcomAgent.exe (chay tren Windows)
REM  Yeu cau: da cai Python (tick "Add to PATH" khi cai).
REM ============================================================
setlocal

echo [1/4] Tao moi truong ao...
python -m venv .venv
call .venv\Scripts\activate.bat

echo [2/4] Cai thu vien...
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install pyinstaller==6.11.1

echo [3/4] Dong goi thanh 1 file .exe (chay ngam, chi hien icon khay)...
pyinstaller --noconfirm --clean ScanEcomAgent.spec

echo [4/4] Xong!
echo    File EXE nam tai: dist\ScanEcomAgent.exe
echo    Nho de config.ini CANH BEN file .exe truoc khi chay.
echo.
pause
