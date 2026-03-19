# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['desktop_webserver\\desktop_air_purifier_webserver.py'],
    pathex=['.', 'desktop_app'],
    binaries=[],
    datas=[('desktop_app/desktop_air_purifier_app.py', 'desktop_air_purifier_app.py')],
    hiddenimports=['desktop_air_purifier_app'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='AirPurifierWeb',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
