# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec cho Scanner Agent.
# Đóng thành 1 file .exe, chạy ngầm (không cửa sổ console), chỉ hiện icon khay.
# Build:  pyinstaller --noconfirm --clean ScanEcomAgent.spec

import os
import sys
import sysconfig

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

block_cipher = None

# --- BUNDLE TKINTER MANUAL ---
# PyInstaller onefile mode + Python 3.12+ đôi khi không tự bundle tcl/tk data.
# Chạy sẽ crash: "Tk data directory _tk_data / tk8.6 not found".
# Fix: copy TAY tcl/, tk/ (Lib) + tcl86t.dll, tk86t.dll (DLLs) từ Python install.
#
# Tìm Python install prefix từ sysconfig.
_py_prefix = sysconfig.get_config_var("prefix") or os.path.dirname(sys.executable)

# tcl/tk libraries (chứa init.tcl, tk.tcl, ...)
_tcl_dir = os.path.join(_py_prefix, "tcl")
_tk_datas = []
if os.path.isdir(_tcl_dir):
    # Bundle toàn bộ tcl/ giữ nguyên cấu trúc.
    # PyInstaller expect nó ở root MEIPASS (Tcl/Tk hook set TCL_LIBRARY=$MEIPASS/tcl/tclX.X).
    for root, dirs, files in os.walk(_tcl_dir):
        for f in files:
            src = os.path.join(root, f)
            rel = os.path.relpath(src, _py_prefix)  # giữ prefix "tcl/..." hoặc "tk/..."
            _tk_datas.append((src, os.path.dirname(rel)))

# DLLs (tcl86t.dll, tk86t.dll, tcl-libtommath.dll,...)
_dlls_dir = os.path.join(_py_prefix, "DLLs")
_tk_bins = []
if os.path.isdir(_dlls_dir):
    for f in os.listdir(_dlls_dir):
        lf = f.lower()
        if (lf.startswith("tcl") or lf.startswith("tk") or lf == "_tkinter.pyd") and lf.endswith((".dll", ".pyd")):
            _tk_bins.append((os.path.join(_dlls_dir, f), "."))

# Fallback thêm PyInstaller's collect helpers (đề phòng có thêm thứ gì bị bỏ sót).
_tk_datas += collect_data_files("tkinter", include_py_files=False)
_tk_bins += collect_dynamic_libs("tkinter")

# --- BUNDLE pyzbar (Windows cần libzbar-64.dll + libiconv.dll) ---
# pyzbar wheel embed sẵn 2 dll trong site-packages/pyzbar. PyInstaller thường
# tự pick nhưng an toàn hơn: dùng collect_dynamic_libs.
try:
    _tk_bins += collect_dynamic_libs("pyzbar")
except Exception:
    pass  # Không có pyzbar cũng OK (camera fallback không có sẽ tự skip).

# --- Bundle opencv (cv2 tự lo, nhưng đề phòng missing hook) ---
try:
    _tk_datas += collect_data_files("cv2", include_py_files=False)
except Exception:
    pass

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
    hiddenimports=[
        'pystray._win32', 'PIL._tkinter_finder', 'tkinter', 'tkinter.ttk', '_tkinter',
        # Camera scanner (optional, không crash nếu thiếu vì đã try/except trong code)
        'cv2', 'numpy', 'pyzbar', 'pyzbar.pyzbar', 'camera_scanner',
    ],
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
