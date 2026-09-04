# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec для PuTTY-AI (Windows 10/11)
# Сборка:  python -m PyInstaller --clean --noconfirm PuTTY-AI.spec

a = Analysis(
    ['putty_ai.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('u_boot_errors_kb.md', '.'),
        ('learned_cases.md', '.'),
        ('skills.json', '.'),
        ('learned_rules.json', '.'),
        ('user_patches.py', '.'),
        ('app.ico', '.'),
    ],
    hiddenimports=[
        'serial',
        'serial.tools.list_ports',
        'paramiko',
        'PyQt6',
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'PyQt6.QtWidgets',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='PuTTY-AI',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='app.ico',
)
