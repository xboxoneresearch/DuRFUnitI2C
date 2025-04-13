
# -*- mode: python ; coding: utf-8 -*-

added_files = [
         ( 'rfunit.py', '.' ),
         ]

a = Analysis(
    ['micropython_rfunit.py'],
    datas=added_files,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    name='micropython_rfunit',
    debug=False,
    strip=False,
    upx=True,
    console=True,
)
