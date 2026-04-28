# -*- mode: python ; coding: utf-8 -*-
import os

block_cipher = None

import customtkinter
ctk_path = os.path.dirname(customtkinter.__file__)

SPEC_DIR = os.path.dirname(SPEC)
ROOT = os.path.join(SPEC_DIR, os.pardir)

icon_path = os.path.join(ROOT, 'smoke_icon.ico')
if not os.path.exists(icon_path):
    icon_path = os.path.join(ROOT, 'icon.ico')

a = Analysis(
    [os.path.join(ROOT, 'smokinghazy.py')],
    pathex=[ROOT],
    binaries=[],
    datas=[
        (ctk_path, 'customtkinter/'),
        (icon_path, '.'),
    ],
    hiddenimports=[
        'pystray._win32',
        'PIL._tkinter_finder',
        'customtkinter',
        'cryptography.hazmat.primitives.ciphers',
        'cryptography.hazmat.primitives.ciphers.algorithms',
        'cryptography.hazmat.primitives.ciphers.modes',
        'cryptography.hazmat.backends.openssl',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'PIL._avif',
        'PIL._webp',
        'PIL._imagingtk',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

_PIL_EXCLUDE_PYDS = {
    '_avif', '_webp', '_imagingtk',
    'FpxImagePlugin', 'MicImagePlugin',
}
a.binaries = [
    (name, path, typ)
    for name, path, typ in a.binaries
    if not any(ex in name for ex in _PIL_EXCLUDE_PYDS)
]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='Teleproxy',
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
    icon=icon_path if os.path.exists(icon_path) else None,
)
