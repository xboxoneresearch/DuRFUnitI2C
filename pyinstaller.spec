# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files

use_upx=False
# 'cmsis_svd' is a dependency of 'greatfet'
rfunit_py_datas = [('src/rfunit.py', '.'), *collect_data_files('cmsis_svd', includes=['schemas/*'])]
rfunit_hiddenimports = ["smbus2", "greatfet"]

a1 = Analysis(
    ['src/rfunit.py'],
    pathex=[],
    binaries=[],
    datas=rfunit_py_datas,
    hiddenimports=rfunit_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

a2 = Analysis(
    ['src/rfunit_gui.py'],
    pathex=[],
    binaries=[],
    datas=rfunit_py_datas,
    hiddenimports=rfunit_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

a3 = Analysis(
    ['src/vpe.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

a4 = Analysis(
    ['src/vpe_gui.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

a5 = Analysis(
    ['src/micropython_rfunit.py'],
    pathex=[],
    binaries=[],
    datas=rfunit_py_datas,
    hiddenimports=rfunit_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz1 = PYZ(a1.pure, a1.zipped_data)
pyz2 = PYZ(a2.pure, a2.zipped_data)
pyz3 = PYZ(a3.pure, a3.zipped_data)
pyz4 = PYZ(a4.pure, a4.zipped_data)
pyz5 = PYZ(a5.pure, a5.zipped_data)

exe_rfunit_cli = EXE(
    pyz1,
    a1.scripts,
    [],
    exclude_binaries=True,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=use_upx,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    name='rfunit-cli',
    console=True,
)

exe_rfunit_gui = EXE(
    pyz2,
    a2.scripts,
    [],
    exclude_binaries=True,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=use_upx,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    name='rfunit-gui',
    console=False,
)

exe_vpe_cli = EXE(
    pyz3,
    a3.scripts,
    [],
    exclude_binaries=True,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=use_upx,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    name='vpe-cli',
    console=True,
)

exe_vpe_gui = EXE(
    pyz4,
    a4.scripts,
    [],
    exclude_binaries=True,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=use_upx,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    name='vpe-gui',
    console=False,
)

exe_rfunit_cli_easy = EXE(
    pyz5,
    a5.scripts,
    [],
    exclude_binaries=True,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=use_upx,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    name='rfunit-micropython',
    console=True,
)

coll = COLLECT(
    exe_rfunit_cli,
    a1.binaries,
    a1.zipfiles,
    a1.datas,
    
    exe_rfunit_gui,
    a2.binaries,
    a2.zipfiles,
    a2.datas,

    exe_vpe_cli,
    a3.binaries,
    a3.zipfiles,
    a3.datas,

    exe_vpe_gui,
    a4.binaries,
    a4.zipfiles,
    a4.datas,

    exe_rfunit_cli_easy,        
    a5.binaries,
    a5.zipfiles,
    a5.datas,

    strip=False,
    upx=use_upx,
    upx_exclude=[],

    name="durango-rfunit-tools"
)