from __future__ import annotations

import importlib
import logging
import sys
import types
import json
from pathlib import Path

import pytest

from diagnostic_platform.observability import flush_product_log_writers
import vci_proxy.j2534_worker as worker_module
from vci_proxy.j2534_worker import (
    J2534WorkerController,
    RemoteJ2534Driver,
    WorkerLaunchSpec,
    resolve_worker_launch_spec,
    resolve_worker_launch_specs,
    select_python_executable,
)


def _read_local_events(tmp_path: Path) -> list[dict[str, object]]:
    flush_product_log_writers()
    raw_dir = tmp_path / "VCI_Proxy" / "observability" / "raw"
    rows: list[dict[str, object]] = []
    for path in sorted(raw_dir.glob("*.jsonl")):
        rows.extend(
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    return rows


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
    worker_exe_x64 = tmp_path / "workers" / "VCI_Proxy_J2534_Worker_x64.exe"
    worker_exe.parent.mkdir(parents=True)
    worker_exe.write_bytes(b"")
    worker_exe_x64.write_bytes(b"")

    monkeypatch.setattr(
        worker_module,
        "resolve_j2534_worker_dll",
        lambda dll_path: (r"C:\drivers\sm2.dll", "x86"),
    )
    monkeypatch.setattr(
        worker_module,
        "find_bundled_worker_executable",
        lambda arch: worker_exe if arch == "x86" else worker_exe_x64,
    )
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
        assert "Client package is incomplete" in str(exc)
        assert "x86" in str(exc)
    else:  # pragma: no cover - assertion style for clearer failure text
        raise AssertionError("expected RuntimeError when bundled worker exe is missing")


def test_resolve_worker_launch_specs_try_both_architectures_when_target_is_unknown(monkeypatch) -> None:
    monkeypatch.setattr(
        worker_module,
        "resolve_j2534_worker_dll",
        lambda dll_path: (r"C:\drivers\mystery.dll", "unknown"),
    )
    monkeypatch.setattr(worker_module, "is_frozen_app", lambda: False)
    monkeypatch.setattr(worker_module, "get_python_architecture", lambda: "x64")
    monkeypatch.setattr(
        worker_module,
        "select_python_executable",
        lambda arch: rf"C:\Python-{arch}\python.exe",
    )

    launch_specs = resolve_worker_launch_specs(r"C:\drivers\mystery.dll")

    assert launch_specs == [
        WorkerLaunchSpec(
            dll_path=r"C:\drivers\mystery.dll",
            target_arch="x64",
            mode="python",
            command=[r"C:\Python-x64\python.exe", "-m", "vci_proxy.j2534_worker"],
        ),
        WorkerLaunchSpec(
            dll_path=r"C:\drivers\mystery.dll",
            target_arch="x86",
            mode="python",
            command=[r"C:\Python-x86\python.exe", "-m", "vci_proxy.j2534_worker"],
        ),
    ]


def test_resolve_worker_launch_specs_rejects_incomplete_frozen_bundle(monkeypatch, tmp_path) -> None:
    x64_worker = tmp_path / "VCI_Proxy_J2534_Worker_x64.exe"
    x64_worker.write_bytes(b"")

    monkeypatch.setattr(
        worker_module,
        "resolve_j2534_worker_dll",
        lambda dll_path: (r"C:\drivers\sm2.dll", "x64"),
    )
    monkeypatch.setattr(worker_module, "is_frozen_app", lambda: True)
    monkeypatch.setattr(
        worker_module,
        "find_bundled_worker_executable",
        lambda arch: x64_worker if arch == "x64" else None,
    )

    with pytest.raises(RuntimeError, match="Client package is incomplete"):
        resolve_worker_launch_specs(r"C:\drivers\sm2.dll")


def test_worker_entry_wrapper_imports_package_main() -> None:
    entry_module = importlib.import_module("vci_proxy_worker_entry")

    assert entry_module.main is worker_module.main


def test_worker_spec_uses_top_level_entry_wrapper() -> None:
    spec_text = Path("pyinstaller_j2534_worker.spec").read_text(encoding="utf-8")

    assert "['vci_proxy_worker_entry.py']" in spec_text


def test_serve_worker_exits_cleanly_when_parent_disconnects_while_waiting(monkeypatch, caplog) -> None:
    observed: dict[str, bool] = {}

    class _FakeConn:
        def recv(self):
            raise ConnectionResetError(10054, "forcibly closed")

        def close(self) -> None:
            observed["conn_closed"] = True

    class _FakeListener:
        def __init__(self, address, authkey=None):
            observed["listener_created"] = True

        def accept(self):
            return _FakeConn()

        def close(self) -> None:
            observed["listener_closed"] = True

    monkeypatch.setattr(worker_module, "_configure_worker_logging", lambda log_file: None)
    monkeypatch.setattr(worker_module, "J2534Driver", lambda dll_path: object())
    monkeypatch.setattr(worker_module, "Listener", _FakeListener)

    with caplog.at_level(logging.INFO):
        worker_module.serve_worker("127.0.0.1", 8259, b"secret", r"C:\drivers\sm2.dll")

    assert "J2534 worker parent disconnected while waiting for requests" in caplog.text
    assert observed == {
        "listener_created": True,
        "conn_closed": True,
        "listener_closed": True,
    }


def test_serve_worker_exits_cleanly_when_parent_disconnects_during_response_send(monkeypatch, caplog) -> None:
    observed: dict[str, object] = {}

    class _FakeDriver:
        def ping(self, value: str) -> str:
            observed["driver_call"] = value
            return f"pong:{value}"

    class _FakeConn:
        def __init__(self) -> None:
            self._requests = [
                {"method": "ping", "args": ("hello",)},
            ]

        def recv(self):
            if not self._requests:
                raise AssertionError("worker should stop after the send failure")
            return self._requests.pop(0)

        def send(self, payload) -> None:
            observed["payload"] = payload
            raise BrokenPipeError("broken pipe")

        def close(self) -> None:
            observed["conn_closed"] = True

    class _FakeListener:
        def __init__(self, address, authkey=None):
            pass

        def accept(self):
            return _FakeConn()

        def close(self) -> None:
            observed["listener_closed"] = True

    monkeypatch.setattr(worker_module, "_configure_worker_logging", lambda log_file: None)
    monkeypatch.setattr(worker_module, "J2534Driver", lambda dll_path: _FakeDriver())
    monkeypatch.setattr(worker_module, "Listener", _FakeListener)

    with caplog.at_level(logging.INFO):
        worker_module.serve_worker("127.0.0.1", 8260, b"secret", r"C:\drivers\sm2.dll")

    assert observed["driver_call"] == "hello"
    assert observed["payload"] == {"ok": True, "result": "pong:hello"}
    assert observed["conn_closed"] is True
    assert observed["listener_closed"] is True
    assert "J2534 worker parent disconnected while sending response" in caplog.text


def test_serve_worker_exits_cleanly_when_parent_disconnects_during_shutdown_ack(monkeypatch, caplog) -> None:
    observed: dict[str, bool] = {}

    class _FakeConn:
        def recv(self):
            return {"method": "__shutdown__", "args": ()}

        def send(self, payload) -> None:
            raise EOFError("pipe closed")

        def close(self) -> None:
            observed["conn_closed"] = True

    class _FakeListener:
        def __init__(self, address, authkey=None):
            pass

        def accept(self):
            return _FakeConn()

        def close(self) -> None:
            observed["listener_closed"] = True

    monkeypatch.setattr(worker_module, "_configure_worker_logging", lambda log_file: None)
    monkeypatch.setattr(worker_module, "J2534Driver", lambda dll_path: object())
    monkeypatch.setattr(worker_module, "Listener", _FakeListener)

    with caplog.at_level(logging.INFO):
        worker_module.serve_worker("127.0.0.1", 8261, b"secret", r"C:\drivers\sm2.dll")

    assert observed["conn_closed"] is True
    assert observed["listener_closed"] is True
    assert "J2534 worker parent disconnected during shutdown ack" in caplog.text


def test_remote_driver_call_with_context_sends_log_context(monkeypatch) -> None:
    observed: dict[str, object] = {}

    class _FakeConn:
        def send(self, payload) -> None:
            observed["payload"] = payload

        def recv(self):
            return {"ok": True, "result": (0, 11)}

    monkeypatch.setattr(worker_module, "Client", lambda address, authkey=None: _FakeConn())

    driver = RemoteJ2534Driver(("127.0.0.1", 8259), b"secret", dll_path=r"C:\drivers\sm2.dll")
    result = driver.call_with_context(
        "connect",
        (1, 6, 0, 500000),
        log_context={"proxy_seq": 77, "worker_request_id": "wrk-1", "msg_name": "CONNECT_REQ"},
    )

    assert result == (0, 11)
    assert observed["payload"] == {
        "method": "connect",
        "args": (1, 6, 0, 500000),
        "log_context": {"proxy_seq": 77, "worker_request_id": "wrk-1", "msg_name": "CONNECT_REQ"},
    }


def test_serve_worker_emits_rpc_events_with_log_context(monkeypatch, tmp_path: Path) -> None:
    observed: dict[str, object] = {}
    monkeypatch.setenv("APPDATA", str(tmp_path))

    class _FakeDriver:
        def ping(self, value: str) -> str:
            observed["driver_call"] = value
            return f"pong:{value}"

    class _FakeConn:
        def __init__(self) -> None:
            self._requests = [
                {
                    "method": "ping",
                    "args": ("hello",),
                    "log_context": {"proxy_seq": 77, "worker_request_id": "wrk-77", "msg_name": "PING_REQ"},
                },
                EOFError("done"),
            ]

        def recv(self):
            item = self._requests.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item

        def send(self, payload) -> None:
            observed.setdefault("responses", []).append(payload)

        def close(self) -> None:
            observed["conn_closed"] = True

    class _FakeListener:
        def __init__(self, address, authkey=None):
            pass

        def accept(self):
            return _FakeConn()

        def close(self) -> None:
            observed["listener_closed"] = True

    monkeypatch.setattr(worker_module, "_configure_worker_logging", lambda log_file: None)
    monkeypatch.setattr(worker_module, "J2534Driver", lambda dll_path: _FakeDriver())
    monkeypatch.setattr(worker_module, "Listener", _FakeListener)

    worker_module.serve_worker("127.0.0.1", 8262, b"secret", r"C:\drivers\sm2.dll")

    rows = _read_local_events(tmp_path)
    event_types = [row["event_type"] for row in rows]
    assert "worker.lifecycle.starting" in event_types
    assert "worker.lifecycle.listener_ready" in event_types
    assert "worker.rpc.received" in event_types
    assert "worker.rpc.returned" in event_types
    rpc_received = next(row for row in rows if row["event_type"] == "worker.rpc.received")
    assert rpc_received["proxy_seq"] == 77
    assert rpc_received["worker_request_id"] == "wrk-77"


def test_worker_controller_emits_spawn_failed_event(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(
        worker_module,
        "resolve_worker_launch_specs",
        lambda dll_path: [
            WorkerLaunchSpec(
                dll_path=r"C:\drivers\sm2.dll",
                target_arch="x64",
                mode="python",
                command=[sys.executable, "-m", "vci_proxy.j2534_worker"],
            )
        ],
    )
    monkeypatch.setattr(
        worker_module.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("spawn boom")),
    )

    controller = J2534WorkerController(r"C:\drivers\sm2.dll")
    with pytest.raises(RuntimeError, match="failed to start"):
        controller.start()

    rows = _read_local_events(tmp_path)
    event_types = [row["event_type"] for row in rows]
    assert "worker.lifecycle.spawn_started" in event_types
    assert "worker.lifecycle.spawn_failed" in event_types
