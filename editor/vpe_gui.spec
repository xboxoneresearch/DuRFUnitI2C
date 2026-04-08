# -*- mode: python ; coding: utf-8 -*-

added_files = [
         ( 'vpe.py', '.' ),
         ]

a = Analysis(
    ['vpe_gui.py'],
    datas=added_files,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    name='vpe_gui',
    debug=False,
    strip=False,
    upx=True,
    console=False,
)
