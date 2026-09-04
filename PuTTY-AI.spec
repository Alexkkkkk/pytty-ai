# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules


hiddenimports = [
    "serial",
    "serial.tools.list_ports",
    "paramiko",
    *collect_submodules("PyQt6"),
]


a = Analysis(
    ["putty_ai.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("u_boot_errors_kb.md", "."),
        ("learned_cases.md", "."),
        ("skills.json", "."),
        ("learned_rules.json", "."),
        ("user_patches.py", "."),
    ],
    hiddenimports=hiddenimports,
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
    name="PuTTY-AI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon="app.ico",
)