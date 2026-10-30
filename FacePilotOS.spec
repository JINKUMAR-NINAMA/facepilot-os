# -*- mode: python ; coding: utf-8 -*-
# Build from the project root:  pyinstaller FacePilotOS.spec --noconfirm
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

datas = collect_data_files('mediapipe') + [('facepilot_os/models/face_landmarker.task', 'models')]
binaries = collect_dynamic_libs('mediapipe')
hiddenimports = [
    'config',
    'core.expression_engine',
    'core.magnetic_snap',
    'core.one_euro_filter',
    'core.pose_mapper',
    'core.vision_thread',
    'core.win32_input',
    'ui.glass_hud',
    'keyboard',
    'matplotlib',
    'matplotlib.pyplot',
] + collect_submodules('comtypes')

a = Analysis(
    ['facepilot_os/main.py'],
    pathex=['facepilot_os'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='FacePilotOS',
    debug=False,
    strip=False,
    upx=False,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='FacePilotOS',
)