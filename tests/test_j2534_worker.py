from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import vci_proxy.j2534_worker as worker_module
from vci_proxy.j2534_worker import WorkerLaunchSpec, resolve_worker_launch_spec, select_python_executable


def test_select_python_executable_prefers_current_process_when_arch_matches(monkeypatch) -> None:
    monkeypatch.setattr(worker_module, "get_python_architecture", lambda: "x64")
    monkeypatch.setattr(sys, "executable", r"C:\Python313\python.exe")

    selected = select_python_executable("x64")

    assert selected == r"C:\Python313\python.exe"


def test_select_python_executable_uses_py_launcher_listing_for_other_arch(monkeypatch) -> None:
    monkeypatch.setattr(worker_module, "get_python_architecture", lambda: "x64")
    monkeypatch.setattr(sys, "executable", r"C:\Python313\python.exe")
    monkeypatch.setattr(
        worker_module.subprocess,
        "run",
        lambda *args, **kwargs: types.SimpleNamespace(
            returncode=0,
            stdout=(
                " -V:3.13 *        C:\\Python313\\python.exe\n"
                " -V:3.13-32       C:\\Python313-32\\python.exe\n"
            ),
            stderr="",
        ),
    )

    selected = select_python_executable("x86")

    assert selected == r"C:\Python313-32\python.exe"


def test_resolve_worker_launch_spec_prefers_bundled_worker_exe(monkeypatch, tmp_path) -> None:
    worker_exe = tmp_path / "workers" / "VCI_Proxy_J2534_Worker_x86.exe"
    worker_exe.parent.mkdir(parents=True)
    worker_exe.write_bytes(b"")

    monkeypatch.setattr(
        worker_module,
        "resolve_j2534_worker_dll",
        lambda dll_path: (r"C:\drivers\sm2.dll", "x86"),
    )
    monkeypatch.setattr(worker_module, "find_bundled_worker_executable", lambda arch: worker_exe if arch == "x86" else None)
    monkeypatch.setattr(worker_module, "is_frozen_app", lambda: True)
    monkeypatch.setattr(
        worker_module,
        "select_python_executable",
        lambda _arch: (_ for _ in ()).throw(AssertionError("python fallback should not be used")),
    )

    launch_spec = resolve_worker_launch_spec(r"C:\drivers\sm2.dll")

    assert launch_spec == WorkerLaunchSpec(
        dll_path=r"C:\drivers\sm2.dll",
        target_arch="x86",
        mode="exe",
        command=[str(worker_exe)],
    )


def test_resolve_worker_launch_spec_uses_python_fallback_in_source_mode(monkeypatch) -> None:
    monkeypatch.setattr(
        worker_module,
        "resolve_j2534_worker_dll",
        lambda dll_path: (r"C:\drivers\sm2.dll", "x86"),
    )
    monkeypatch.setattr(worker_module, "find_bundled_worker_executable", lambda arch: None)
    monkeypatch.setattr(worker_module, "is_frozen_app", lambda: False)
    monkeypatch.setattr(worker_module, "select_python_executable", lambda arch: rf"C:\Python313-{arch}\python.exe")

    launch_spec = resolve_worker_launch_spec(r"C:\drivers\sm2.dll")

    assert launch_spec == WorkerLaunchSpec(
        dll_path=r"C:\drivers\sm2.dll",
        target_arch="x86",
        mode="python",
        command=[r"C:\Python313-x86\python.exe", "-m", "vci_proxy.j2534_worker"],
    )


def test_resolve_worker_launch_spec_raises_clear_error_when_bundled_worker_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        worker_module,
        "resolve_j2534_worker_dll",
        lambda dll_path: (r"C:\drivers\sm2.dll", "x86"),
    )
    monkeypatch.setattr(worker_module, "find_bundled_worker_executable", lambda arch: None)
    monkeypatch.setattr(worker_module, "is_frozen_app", lambda: True)

    try:
        resolve_worker_launch_spec(r"C:\drivers\sm2.dll")
    except RuntimeError as exc:
        assert "Missing bundled x86 worker executable" in str(exc)
        assert r"C:\drivers\sm2.dll" in str(exc)
    else:  # pragma: no cover - assertion style for clearer failure text
        raise AssertionError("expected RuntimeError when bundled worker exe is missing")


def test_worker_entry_wrapper_imports_package_main() -> None:
    entry_module = importlib.import_module("vci_proxy_worker_entry")

    assert entry_module.main is worker_module.main


def test_worker_spec_uses_top_level_entry_wrapper() -> None:
    spec_text = Path("pyinstaller_j2534_worker.spec").read_text(encoding="utf-8")

    assert "['vci_proxy_worker_entry.py']" in spec_text
