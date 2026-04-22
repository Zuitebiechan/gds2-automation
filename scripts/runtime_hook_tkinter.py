from __future__ import annotations

import os
import sys
from pathlib import Path


def _candidate_bundle_roots() -> list[Path]:
    roots: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
        roots.append(Path(meipass).parent)
    exe_dir = Path(sys.executable).resolve().parent
    roots.append(exe_dir)
    roots.append(exe_dir / "_internal")

    unique: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        unique.append(root)
    return unique


def _configure_tcl_tk() -> None:
    for root in _candidate_bundle_roots():
        tcl_dir = root / "_tcl_data"
        tk_dir = root / "_tk_data"
        if not (tcl_dir.exists() and tk_dir.exists()):
            tcl_dir = root / "tcl" / "tcl8.6"
            tk_dir = root / "tcl" / "tk8.6"
        if tcl_dir.exists() and tk_dir.exists():
            os.environ.setdefault("TCL_LIBRARY", str(tcl_dir))
            os.environ.setdefault("TK_LIBRARY", str(tk_dir))
            return


_configure_tcl_tk()
