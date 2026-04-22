from __future__ import annotations

import os
import time
import shutil
import tempfile
import uuid
from pathlib import Path

import pytest
import _pytest.tmpdir as pytest_tmpdir
import _pytest.pathlib as pytest_pathlib


_ORIGINAL_CLEANUP_DEAD_SYMLINKS = pytest_pathlib.cleanup_dead_symlinks
_TMP_ROOT = (
    Path.home()
    / ".codex"
    / "memories"
    / f"pytest-root-{os.getpid()}"
)
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["TMP"] = str(_TMP_ROOT)
os.environ["TEMP"] = str(_TMP_ROOT)
os.environ["TMPDIR"] = str(_TMP_ROOT)
tempfile.tempdir = str(_TMP_ROOT)


def _safe_cleanup_dead_symlinks(root):
    try:
        return _ORIGINAL_CLEANUP_DEAD_SYMLINKS(root)
    except PermissionError:
        if os.name == "nt":
            return None
        raise


pytest_pathlib.cleanup_dead_symlinks = _safe_cleanup_dead_symlinks
pytest_tmpdir.cleanup_dead_symlinks = _safe_cleanup_dead_symlinks


def pytest_configure(config):
    basetemp = _TMP_ROOT / "basetemp"
    basetemp.mkdir(parents=True, exist_ok=True)
    config.option.basetemp = str(basetemp)


@pytest.fixture
def tmp_path():
    path = _TMP_ROOT / f"tmp-{os.getpid()}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=False)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
