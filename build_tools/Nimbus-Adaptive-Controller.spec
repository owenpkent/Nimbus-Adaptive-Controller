# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for Nimbus Adaptive Controller
This creates a standalone Windows executable with all dependencies bundled.
"""

import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

# Version info - keep in sync with src/__init__.py
VERSION = "1.4.3"
VERSION_TUPLE = (1, 4, 3, 0)

block_cipher = None

# Define the main script (use launcher.py for executable, not run.py)
# Get the project root directory (parent of build_tools)
SPEC_DIR = Path(__file__).parent if '__file__' in dir() else Path.cwd()
PROJECT_ROOT = SPEC_DIR.parent if SPEC_DIR.name == 'build_tools' else SPEC_DIR
main_script = str(PROJECT_ROOT / 'build_tools' / 'launcher.py')

# Collect all QML files and resources
qml_datas = []
qml_path = PROJECT_ROOT / 'qml'
if qml_path.exists():
    for qml_file in qml_path.rglob('*.qml'):
        rel_path = qml_file.relative_to(PROJECT_ROOT)
        qml_datas.append((str(qml_file), str(rel_path.parent)))

# Add logo
logo_datas = [(str(PROJECT_ROOT / 'logo.png'), '.')]

# Add controller config template
config_datas = [(str(PROJECT_ROOT / 'controller_config.json'), '.')]

# The virtual pad client (src/padbus_client.py) is pure ctypes over the bus
# driver's device interface, so there is no client DLL to bundle.

# Add profiles directory
profile_datas = []
profiles_path = PROJECT_ROOT / 'profiles'
if profiles_path.exists():
    for profile_file in profiles_path.rglob('*.json'):
        rel_path = profile_file.relative_to(PROJECT_ROOT)
        profile_datas.append((str(profile_file), str(rel_path.parent)))

# Add src package as data (so it can be imported)
src_datas = []
src_path = PROJECT_ROOT / 'src'
if src_path.exists():
    for py_file in src_path.rglob('*.py'):
        rel_path = py_file.relative_to(PROJECT_ROOT)
        src_datas.append((str(py_file), str(rel_path.parent)))
    # Spectator+ data: the per-game calibrations and the target assist allowlist
    # (src/spectator/calibrations/*.json, src/spectator/games/*.json). Read at
    # run time next to the package, so they ship beside it.
    for json_file in (src_path / 'spectator').rglob('*.json'):
        rel_path = json_file.relative_to(PROJECT_ROOT)
        src_datas.append((str(json_file), str(rel_path.parent)))

# Collect all numpy submodules — numpy 2.x restructured internals (e.g. numpy._core._exceptions)
# that PyInstaller cannot auto-detect; collect_all ensures nothing is missed.
numpy_datas, numpy_binaries, numpy_hidden = collect_all('numpy')

# Combine all data files
datas = qml_datas + logo_datas + config_datas + profile_datas + src_datas + numpy_datas

# Hidden imports for PySide6 and other dependencies
hiddenimports = [
    'PySide6.QtCore',
    'PySide6.QtGui',
    'PySide6.QtWidgets',
    'PySide6.QtQml',
    'PySide6.QtQuick',
    'PySide6.QtQuickControls2',
    'numpy',
    'numpy._core',
    'numpy._core._exceptions',
    'numpy._core._multiarray_umath',
    'numpy._core.multiarray',
    'numpy.lib',
    'numpy.lib.stride_tricks',
    'pyvjoy',
    'src.qt_qml_app',
    'src.qt_main',
    'src.bridge',
    'src.config',
    'src.vjoy_interface',
    'src.vigem_interface',
    'src.padbus_client',
    'src.qt_dialogs',
    'src.qt_widgets',
    'src.borderless',
    'src.mouse_hider',
    'src.window_utils',
]

a = Analysis(
    [main_script],
    pathex=[str(PROJECT_ROOT)],  # Add project root so 'src' package is found
    binaries=numpy_binaries,
    datas=datas,
    hiddenimports=hiddenimports + numpy_hidden,
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
    name='Nimbus-Adaptive-Controller-1.4.3',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # Set to False for GUI app (no console window)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    manifest=str(PROJECT_ROOT / 'build_tools' / 'Nimbus-Adaptive-Controller.manifest'),
    icon=str(PROJECT_ROOT / 'build_tools' / 'Nimbus-Adaptive-Controller.ico'),
    version=str(PROJECT_ROOT / 'build_tools' / 'version_info.txt'),  # Windows version resource
)
