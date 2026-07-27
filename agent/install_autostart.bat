@echo off
REM ============================================================
REM  Cai ScanEcomAgent tu chay khi mo may (Windows Startup).
REM  Chay file nay 1 LAN. No tao shortcut trong thu muc Startup
REM  tro toi dist\ScanEcomAgent.exe (chay ngam, chi hien icon khay).
REM ============================================================
setlocal

set "EXE=%~dp0dist\ScanEcomAgent.exe"

if not exist "%EXE%" (
  echo [LOI] Chua thay %EXE%
  echo       Hay chay build_exe.bat truoc de tao file .exe.
  pause
  exit /b 1
)

set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"

REM Tao shortcut bang PowerShell.
powershell -NoProfile -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%STARTUP%\ScanEcomAgent.lnk');" ^
  "$s.TargetPath='%EXE%';" ^
  "$s.WorkingDirectory='%~dp0dist';" ^
  "$s.WindowStyle=7;" ^
  "$s.Description='Scan Ecom Agent';" ^
  "$s.Save()"

echo Da cai tu khoi dong: %STARTUP%\ScanEcomAgent.lnk
echo Tu lan khoi dong may sau, agent se tu chay ngam.
echo (Muon go: xoa file .lnk do trong thu muc Startup.)
pause
