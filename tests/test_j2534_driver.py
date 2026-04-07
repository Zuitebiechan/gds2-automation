from __future__ import annotations

import sys
import types

import pytest

import vci_proxy.j2534_driver as driver_module
from vci_proxy.j2534_driver import J2534Driver, PASSTHRU_MSG, discover_j2534_drivers


def test_passthru_msg_round_trip_preserves_fields() -> None:
    original = {
        "protocol_id": 6,
        "rx_status": 1,
        "tx_flags": 2,
        "timestamp": 123,
        "data": b"\x10\x20\x30",
    }

    converted = PASSTHRU_MSG.from_dict(original).to_dict()

    assert converted == original


def test_discover_j2534_drivers_deduplicates_paths_and_prefers_scanmatik(monkeypatch) -> None:
    class _FakeWinreg:
        KEY_READ = 0x1
        KEY_WOW64_32KEY = 0x100
        HKEY_LOCAL_MACHINE = object()

        _roots = {
            KEY_READ: {
                "Scanmatik USB": {
                    "FunctionLibrary": r"C:\drivers\scanmatik.dll",
                    "Vendor": "Scanmatik",
                    "Name": "SM3",
                },
                "Bosch VCX": {
                    "FunctionLibrary": r"C:\drivers\bosch.dll",
                    "Vendor": "Bosch",
                    "Name": "Bosch VCX",
                },
            },
            KEY_READ | KEY_WOW64_32KEY: {
                "Scanmatik USB Dup": {
                    "FunctionLibrary": r"C:\drivers\scanmatik.dll",
                    "Vendor": "Scanmatik",
                    "Name": "SM3 Duplicate",
                },
            },
        }

        @classmethod
        def OpenKey(cls, root, subkey, reserved=0, access=0):
            if root is cls.HKEY_LOCAL_MACHINE and subkey == driver_module.J2534_REGISTRY_KEY:
                if access not in cls._roots:
                    raise OSError("missing root")
                return ("root", access)
            if isinstance(root, tuple) and root[0] == "root":
                data = cls._roots[root[1]]
                if subkey not in data:
                    raise OSError("missing subkey")
                return ("subkey", root[1], subkey)
            raise OSError("bad key")

        @classmethod
        def EnumKey(cls, root, index):
            data = cls._roots[root[1]]
            keys = list(data.keys())
            if index >= len(keys):
                raise OSError("done")
            return keys[index]

        @classmethod
        def QueryValueEx(cls, key, name):
            data = cls._roots[key[1]][key[2]]
            if name not in data:
                raise OSError("missing value")
            return data[name], None

        @staticmethod
        def CloseKey(_key):
            return None

    monkeypatch.setattr(driver_module.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", _FakeWinreg)
    monkeypatch.setattr(driver_module.os.path, "exists", lambda path: path in {r"C:\drivers\scanmatik.dll", r"C:\drivers\bosch.dll"})

    drivers = discover_j2534_drivers()

    assert [driver["dll_path"] for driver in drivers] == [
        r"C:\drivers\scanmatik.dll",
        r"C:\drivers\bosch.dll",
    ]
    assert drivers[0]["name"] == "SM3"


def test_j2534_driver_prefers_discovered_path_before_fallback(monkeypatch) -> None:
    class _FakeFn:
        def __call__(self, *args, **kwargs):
            return 0

    class _FakeDll:
        def __init__(self) -> None:
            self.PassThruOpen = _FakeFn()
            self.PassThruClose = _FakeFn()
            self.PassThruConnect = _FakeFn()
            self.PassThruDisconnect = _FakeFn()
            self.PassThruReadMsgs = _FakeFn()
            self.PassThruWriteMsgs = _FakeFn()
            self.PassThruStartMsgFilter = _FakeFn()
            self.PassThruStopMsgFilter = _FakeFn()
            self.PassThruReadVersion = _FakeFn()
            self.PassThruGetLastError = _FakeFn()
            self.PassThruIoctl = _FakeFn()

    fake_dll = _FakeDll()
    monkeypatch.setattr(driver_module, "discover_j2534_drivers", lambda: [{"name": "SM3", "dll_path": r"C:\drivers\scanmatik.dll", "vendor": "Scanmatik"}])
    monkeypatch.setattr(driver_module.os.path, "exists", lambda path: path == r"C:\drivers\scanmatik.dll")
    monkeypatch.setattr(driver_module.ctypes, "WinDLL", lambda path: fake_dll)

    driver = J2534Driver()

    assert driver.dll is fake_dll
    assert driver.dll_path == r"C:\drivers\scanmatik.dll"


def test_j2534_driver_raises_when_no_driver_path_can_be_loaded(monkeypatch) -> None:
    monkeypatch.setattr(driver_module, "discover_j2534_drivers", lambda: [])
    monkeypatch.setattr(driver_module.os.path, "exists", lambda path: False)

    with pytest.raises(RuntimeError, match="J2534 DLL"):
        J2534Driver()
