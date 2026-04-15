"""Out-of-process J2534 worker helpers for cross-architecture DLL support."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import logging
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from multiprocessing.connection import Client, Listener
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Optional

from .j2534_driver import (
    J2534Driver,
    discover_j2534_drivers,
    get_dll_architecture,
    get_python_architecture,
)


logger = logging.getLogger(__name__)

_PY_LIST_RE = re.compile(
    r"^\s*-V:(?P<version>\d+\.\d+)(?P<arch>-32)?\s+\*?\s*(?P<path>.+python(?:w)?\.exe)\s*$",
    re.IGNORECASE,
)
_WORKER_EXE_NAMES = {
    "x86": "VCI_Proxy_J2534_Worker_x86.exe",
    "x64": "VCI_Proxy_J2534_Worker_x64.exe",
}
_WORKER_HOST = "127.0.0.1"
_WORKER_CONNECT_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class WorkerLaunchSpec:
    dll_path: str
    target_arch: str
    mode: str
    command: list[str]


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _parse_py_launcher_paths(output: str) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    for line in output.splitlines():
        match = _PY_LIST_RE.match(line.strip())
        if not match:
            continue
        candidates.append(
            {
                "version": match.group("version"),
                "architecture": "x86" if match.group("arch") == "-32" else "x64",
                "path": match.group("path").strip(),
            }
        )
    return candidates


def select_python_executable(target_arch: str) -> str:
    """Select one Python executable that can host a worker for the target architecture."""
    current_arch = get_python_architecture()
    if current_arch == target_arch:
        return sys.executable

    env_key = f"VCI_PROXY_PYTHON_{target_arch.upper()}"
    configured = os.environ.get(env_key, "").strip()
    if configured:
        return configured

    result = subprocess.run(
        ["py", "-0p"],
        capture_output=True,
        text=True,
        check=False,
    )
    candidates = _parse_py_launcher_paths(result.stdout or "")
    matching = [c for c in candidates if c["architecture"] == target_arch]
    if matching:
        preferred_version = f"{sys.version_info.major}.{sys.version_info.minor}"
        matching.sort(
            key=lambda item: (
                item["version"] != preferred_version,
                tuple(-part for part in _version_key(item["version"])),
            )
        )
        return matching[0]["path"]

    raise RuntimeError(
        f"No Python runtime found for target architecture {target_arch}. "
        f"Install a {target_arch} Python interpreter or set {env_key}."
    )


def is_frozen_app() -> bool:
    """Return True when the current process is a packaged executable."""
    return bool(getattr(sys, "frozen", False))


def find_bundled_worker_executable(target_arch: str) -> Optional[Path]:
    """Return the bundled worker executable for one architecture, if present."""
    env_key = f"VCI_PROXY_J2534_WORKER_{target_arch.upper()}_EXE"
    configured = os.environ.get(env_key, "").strip()
    if configured:
        configured_path = Path(configured)
        if configured_path.exists():
            return configured_path
        return None

    if not is_frozen_app():
        return None

    worker_path = Path(sys.executable).resolve().parent / "workers" / _WORKER_EXE_NAMES[target_arch]
    if worker_path.exists():
        return worker_path
    return None


def resolve_j2534_worker_dll(dll_path: Optional[str]) -> tuple[str, str]:
    """Resolve one DLL path and target architecture for the worker process."""
    if dll_path:
        arch = get_dll_architecture(dll_path) or get_python_architecture()
        return dll_path, arch

    discovered = discover_j2534_drivers()
    if discovered:
        selected = discovered[0]
        dll_path = str(selected["dll_path"])
        arch = str(selected.get("architecture") or get_dll_architecture(dll_path) or get_python_architecture())
        return dll_path, arch

    for fallback in J2534Driver.FALLBACK_DLL_PATHS:
        if os.path.exists(fallback):
            arch = get_dll_architecture(fallback) or get_python_architecture()
            return fallback, arch

    raise RuntimeError("No J2534 DLL available for worker startup.")


def resolve_worker_launch_spec(dll_path: Optional[str]) -> WorkerLaunchSpec:
    """Resolve the preferred runtime for one local J2534 worker."""
    resolved_path, target_arch = resolve_j2534_worker_dll(dll_path)
    bundled_worker = find_bundled_worker_executable(target_arch)
    if bundled_worker is not None:
        return WorkerLaunchSpec(
            dll_path=resolved_path,
            target_arch=target_arch,
            mode="exe",
            command=[str(bundled_worker)],
        )

    if is_frozen_app():
        raise RuntimeError(
            f"Missing bundled {target_arch} worker executable for {resolved_path}. "
            "Reinstall the client package."
        )

    return WorkerLaunchSpec(
        dll_path=resolved_path,
        target_arch=target_arch,
        mode="python",
        command=[select_python_executable(target_arch), "-m", "vci_proxy.j2534_worker"],
    )


def _allocate_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((_WORKER_HOST, 0))
        return int(sock.getsockname()[1])


class RemoteJ2534Driver:
    """Thread-safe RPC proxy exposing the J2534Driver interface."""

    def __init__(self, address: tuple[str, int], authkey: bytes, *, dll_path: str):
        self._conn = Client(address, authkey=authkey)
        self._lock = Lock()
        self.dll_path = dll_path

    def _call(self, method: str, *args: Any) -> Any:
        with self._lock:
            self._conn.send({"method": method, "args": args})
            response = self._conn.recv()
        if not response.get("ok"):
            raise RuntimeError(str(response.get("error") or "worker call failed"))
        return response.get("result")

    def close_connection(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def open(self, device_name: Optional[str] = None):
        return self._call("open", device_name)

    def close(self, device_id: int):
        return self._call("close", device_id)

    def connect(self, device_id: int, protocol_id: int, flags: int, baudrate: int):
        return self._call("connect", device_id, protocol_id, flags, baudrate)

    def disconnect(self, channel_id: int):
        return self._call("disconnect", channel_id)

    def read_msgs(self, channel_id: int, num_msgs: int, timeout: int):
        return self._call("read_msgs", channel_id, num_msgs, timeout)

    def write_msgs(self, channel_id: int, messages: list[dict], timeout: int):
        return self._call("write_msgs", channel_id, messages, timeout)

    def read_version(self, device_id: int):
        return self._call("read_version", device_id)

    def start_msg_filter(
        self,
        channel_id: int,
        filter_type: int,
        mask_msg: Optional[dict],
        pattern_msg: Optional[dict],
        flow_control_msg: Optional[dict],
    ):
        return self._call(
            "start_msg_filter",
            channel_id,
            filter_type,
            mask_msg,
            pattern_msg,
            flow_control_msg,
        )

    def stop_msg_filter(self, channel_id: int, filter_id: int):
        return self._call("stop_msg_filter", channel_id, filter_id)

    def ioctl(self, channel_id: int, ioctl_id: int, input_data: Optional[bytes] = None):
        return self._call("ioctl", channel_id, ioctl_id, input_data)

    def get_error_name(self, code: int):
        return self._call("get_error_name", code)

    def get_last_error(self):
        return self._call("get_last_error")

    def shutdown_worker(self) -> None:
        self._call("__shutdown__")


class J2534WorkerController:
    """Launch and manage one architecture-matched J2534 worker process."""

    def __init__(self, dll_path: Optional[str] = None):
        launch_spec = resolve_worker_launch_spec(dll_path)
        self.dll_path = launch_spec.dll_path
        self.target_arch = launch_spec.target_arch
        self.launch_spec = launch_spec
        self.address = (_WORKER_HOST, _allocate_local_port())
        self.authkey = secrets.token_hex(16).encode("ascii")
        self.process: subprocess.Popen[str] | None = None
        self.driver: RemoteJ2534Driver | None = None

    def start(self) -> RemoteJ2534Driver:
        if self.driver is not None:
            return self.driver

        cmd = [
            *self.launch_spec.command,
            "--host",
            self.address[0],
            "--port",
            str(self.address[1]),
            "--authkey",
            self.authkey.decode("ascii"),
            "--dll",
            self.dll_path,
        ]
        logger.info(
            "Starting J2534 worker mode=%s target=%s arch=%s dll=%s address=%s:%s",
            self.launch_spec.mode,
            self.launch_spec.command[0],
            self.target_arch,
            self.dll_path,
            self.address[0],
            self.address[1],
        )
        self.process = subprocess.Popen(
            cmd,
            cwd=str(Path(__file__).resolve().parents[1]),
        )

        deadline = time.time() + _WORKER_CONNECT_TIMEOUT_S
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                self.driver = RemoteJ2534Driver(self.address, self.authkey, dll_path=self.dll_path)
                return self.driver
            except Exception as exc:  # pragma: no cover - retry loop
                last_error = exc
                time.sleep(0.1)

        self.stop()
        raise RuntimeError(
            f"J2534 worker failed to accept connections for {self.dll_path}: {last_error}"
        )

    def stop(self) -> None:
        driver = self.driver
        self.driver = None
        if driver is not None:
            try:
                driver.shutdown_worker()
            except Exception:
                pass
            finally:
                driver.close_connection()

        process = self.process
        self.process = None
        if process is None:
            return

        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def create_driver_runtime(dll_path: Optional[str]) -> tuple[RemoteJ2534Driver, Callable[[], None]]:
    """Create one remote driver plus a cleanup callback."""
    controller = J2534WorkerController(dll_path)
    driver = controller.start()
    return driver, controller.stop


def serve_worker(host: str, port: int, authkey: bytes, dll_path: str) -> None:
    """Serve one local RPC worker loop backed by a real J2534 DLL."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    logger.info("J2534 worker starting dll=%s host=%s port=%s", dll_path, host, port)
    driver = J2534Driver(dll_path)
    listener = Listener((host, port), authkey=authkey)
    try:
        conn = listener.accept()
        try:
            while True:
                request = conn.recv()
                method = str(request.get("method") or "")
                args = tuple(request.get("args") or ())
                if method == "__shutdown__":
                    conn.send({"ok": True, "result": None})
                    break
                try:
                    result = getattr(driver, method)(*args)
                    conn.send({"ok": True, "result": result})
                except Exception as exc:
                    conn.send(
                        {
                            "ok": False,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
        finally:
            conn.close()
    finally:
        listener.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one local J2534 worker process")
    parser.add_argument("--host", default=_WORKER_HOST)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--authkey", required=True)
    parser.add_argument("--dll", required=True)
    args = parser.parse_args()
    serve_worker(
        host=args.host,
        port=args.port,
        authkey=args.authkey.encode("ascii"),
        dll_path=args.dll,
    )


if __name__ == "__main__":
    main()
