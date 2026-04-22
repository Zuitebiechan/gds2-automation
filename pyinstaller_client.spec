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

from pathlib import Path
import sysconfig

import PIL
import tkinter
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

# Pillow needs native extension modules (e.g. PIL._imaging.pyd) and plugin data.
# Collect them explicitly to avoid runtime ImportError in packaged exe.
pil_datas = collect_data_files('PIL')
pil_binaries = collect_dynamic_libs('PIL')

# tkinter/Tcl/Tk packaging on Windows can be flaky with newer Python builds.
# Force-include the extension module, Tcl/Tk DLLs, and runtime script data so
# the packaged client can actually open its Tk windows.
python_root = Path(sysconfig.get_paths()['stdlib']).resolve().parent
python_dll_dir = python_root / 'DLLs'
tk_root = python_root / 'tcl'

def collect_tree_contents(source_dir: Path, dest_root: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    if not source_dir.exists():
        return entries
    for path in source_dir.rglob('*'):
        if not path.is_file():
            continue
        relative_parent = path.relative_to(source_dir).parent.as_posix()
        destination = dest_root if relative_parent == '.' else f'{dest_root}/{relative_parent}'
        entries.append((str(path), destination))
    return entries

tk_datas = (
    collect_tree_contents(tk_root / 'tcl8.6', '_tcl_data')
    + collect_tree_contents(tk_root / 'tk8.6', '_tk_data')
)

tk_binaries = []
for dll_name in ('_tkinter.pyd', 'tcl86t.dll', 'tk86t.dll'):
    source = python_dll_dir / dll_name
    if source.exists():
        tk_binaries.append((str(source), '.'))

# On some Windows/Python combinations (especially mixed global/venv installs),
# collect_dynamic_libs('PIL') may miss Pillow extension binaries.
# Force-include only ABI-matching PIL extension modules from the active
# interpreter so PIL.Image can import _imaging at runtime.
pil_pkg_dir = Path(PIL.__file__).resolve().parent
ext_suffix = sysconfig.get_config_var('EXT_SUFFIX') or '.pyd'

matching_pyds = list(pil_pkg_dir.glob(f'*{ext_suffix}'))
if not matching_pyds:
    raise RuntimeError(
        f"No Pillow extension matching active ABI suffix '{ext_suffix}' found in {pil_pkg_dir}. "
        "Recreate venv and reinstall Pillow to match current Python architecture/version."
    )

for pyd in matching_pyds:
    entry = (str(pyd), 'PIL')
    if entry not in pil_binaries:
        pil_binaries.append(entry)

a = Analysis(
    ['vci_proxy/client_gui.py'],
    pathex=['.'],
    binaries=pil_binaries + tk_binaries,
    datas=pil_datas + tk_datas,
    hiddenimports=[
        'vci_proxy',
        'vci_proxy.protocol',
        'vci_proxy.config',
        'vci_proxy.auth',
        'vci_proxy.j2534_driver',
        'vci_proxy.cache_vbatt',
        'vci_proxy.reverse_client',
        'vci_proxy.diagnostics_window',
        'pystray._win32',
        'tkinter',
        '_tkinter',
        'PIL',
        'PIL.Image',
        'PIL.ImageDraw',
        'PIL._imaging',
        'requests',
        'urllib3',
        'charset_normalizer',
        'certifi',
        'idna',
    ],
    hookspath=['pyinstaller_hooks'],
    hooksconfig={},
    runtime_hooks=['scripts/runtime_hook_tkinter.py'],
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
