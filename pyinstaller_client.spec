# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for VCI Proxy Client GUI.

Build:
    pip install -r requirements-client.txt
    pyinstaller pyinstaller_client.spec

Output:
    dist/VCI_Proxy_Client/VCI_Proxy_Client.exe
"""

block_cipher = None

a = Analysis(
    ['vci_proxy/client_gui.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=[
        'vci_proxy',
        'vci_proxy.protocol',
        'vci_proxy.config',
        'vci_proxy.auth',
        'vci_proxy.j2534_driver',
        'vci_proxy.cache_vbatt',
        'vci_proxy.reverse_client',
        'pystray._win32',
        'requests',
        'urllib3',
        'charset_normalizer',
        'certifi',
        'idna',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # RPA layer — not needed for proxy client
        'pywinauto',
        'pyautogui',
        # Image/OCR — not needed
        'cv2',
        'opencv',
        'easyocr',
        'torch',
        'torchvision',
        # AI/LLM — not needed
        'anthropic',
        'openai',
        'zhipuai',
        # Web framework — not needed
        'flask',
        'flask_cors',
        'fastapi',
        'uvicorn',
        'pydantic',
        # Testing — not needed
        'pytest',
        'pytest_cov',
        # Other heavy libs
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
    [],
    exclude_binaries=True,
    name='VCI_Proxy_Client',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # No console window — GUI app
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='VCI_Proxy_Client',
)
