# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec cho Scanner Agent.
# Đóng thành 1 file .exe, chạy ngầm (không cửa sổ console), chỉ hiện icon khay.
# Build:  pyinstaller --noconfirm --clean ScanEcomAgent.spec

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

block_cipher = None

# Bundle rõ ràng tcl/tk data + binaries. Trên 1 số máy Windows, PyInstaller
# 6.11+ không tự bundle _tk_data -> exe crash "Tk data directory not found".
_tk_datas = collect_data_files('tkinter', include_py_files=False)
_tk_bins = collect_dynamic_libs('tkinter')

a = Analysis(
    ['scanner_agent.py'],
    pathex=[],
    binaries=_tk_bins,
    # KHÔNG bundle logos vào .exe: để thư mục logos/ nằm CẠNH .exe (như config.ini)
    # để người dùng tự thêm/đổi logo mà không cần build lại.
    # BUNDLE VERSION: file version phải nằm TRONG .exe để _read_version() đọc được
    # (khớp GitHub Actions tag → auto-update mới compare đúng).
    datas=[('VERSION', '.')] + _tk_datas,
    # pystray/PIL nạp backend động; tkinter cho cửa sổ GUI -> khai báo để gom đủ.
    hiddenimports=['pystray._win32', 'PIL._tkinter_finder', 'tkinter', 'tkinter.ttk'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='ScanEcomAgent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,      # QUAN TRỌNG: không hiện cửa sổ console đen
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon='icon.ico',  # (tuỳ chọn) bỏ comment nếu có file icon.ico
)
