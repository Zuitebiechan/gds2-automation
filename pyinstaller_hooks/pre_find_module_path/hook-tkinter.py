"""
Local override for PyInstaller's pre-find tkinter hook.

On this Windows host, PyInstaller's Tcl/Tk probe reports the installation as
"broken" and empties hook_api.search_dirs, which excludes the pure-Python
`tkinter` package from the bundle even when `_tkinter`, Tcl, and Tk resources
are present and usable.

We intentionally keep this hook as a no-op and provide the required Tcl/Tk
runtime files explicitly from `pyinstaller_client.spec`.
"""


def pre_find_module_path(hook_api):
    del hook_api
