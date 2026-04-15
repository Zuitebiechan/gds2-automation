# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the standalone J2534 worker executable.

Build this spec with both 32-bit and 64-bit Python interpreters.
Use the VCI_PROXY_WORKER_NAME environment variable to set the output name.
"""

import os

block_cipher = None
worker_name = os.environ.get("VCI_PROXY_WORKER_NAME", "VCI_Proxy_J2534_Worker")

a = Analysis(
    ['vci_proxy_worker_entry.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=[
        'vci_proxy',
        'vci_proxy.j2534_worker',
        'vci_proxy.j2534_driver',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'pystray',
        'PIL',
        'pywinauto',
        'pyautogui',
        'cv2',
        'opencv',
        'easyocr',
        'torch',
        'torchvision',
        'anthropic',
        'openai',
        'zhipuai',
        'flask',
        'flask_cors',
        'fastapi',
        'uvicorn',
        'pydantic',
        'pytest',
        'pytest_cov',
        'numpy',
        'scipy',
        'pandas',
        'matplotlib',
    ],
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
    name=worker_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
