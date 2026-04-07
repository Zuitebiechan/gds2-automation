from __future__ import annotations

from pathlib import Path

from src.discovery.vehicle_mapping import (
    VehicleDiscovery,
    VehicleMapping,
    discover_current_page,
)


def test_get_mapping_path_sanitizes_vehicle_identifier(tmp_path: Path) -> None:
    mapping = VehicleMapping(tmp_path)

    path = mapping.get_mapping_path("VIN:ABC/123*?#")

    assert path == tmp_path / "VIN_ABC_123___.json"


def test_save_load_and_lookup_helpers_use_persisted_mapping(tmp_path: Path) -> None:
    mapping = VehicleMapping(tmp_path)
    vehicle_id = "VIN123"
    persisted = {
        "vehicle_id": vehicle_id,
        "modules": {
            "ECM": {
                "index": 2,
                "data_categories": {
                    "Engine Data": 4,
                    "Fuel Data": {
                        "index": 6,
                        "has_sub": True,
                        "sub_categories": {
                            "Trim": 1,
                            "Injector": 2,
                        },
                    },
                },
            }
        },
    }

    mapping.save_mapping(vehicle_id, persisted)
    mapping._cache.clear()

    loaded = mapping.load_mapping(vehicle_id)

    assert loaded == persisted
    assert mapping.get_module_index(vehicle_id, "ECM") == 2
    assert mapping.get_data_category_index(vehicle_id, "ECM", "Engine Data") == 4
    assert mapping.get_data_category_index(vehicle_id, "ECM", "Fuel Data") == 6
    assert mapping.has_module_list(vehicle_id) is True
    assert mapping.has_data_categories(vehicle_id, "ECM") is True
    assert mapping.has_sub_categories(vehicle_id, "ECM", "Fuel Data") is True
    assert mapping.get_sub_categories(vehicle_id, "ECM", "Fuel Data") == {
        "Trim": 1,
        "Injector": 2,
    }
    assert mapping.get_sub_category_index(vehicle_id, "ECM", "Fuel Data", "Injector") == 2
    assert mapping.get_module_index(vehicle_id, "TCM") is None
    assert mapping.get_sub_category_index(vehicle_id, "ECM", "Fuel Data", "Unknown") is None


def test_update_helpers_create_and_promote_mapping_shapes(tmp_path: Path) -> None:
    mapping = VehicleMapping(tmp_path)
    vehicle_id = "VIN123"

    mapping.update_module_list(vehicle_id, {"ECM": 0})
    mapping.update_data_categories(vehicle_id, "ECM", {"Engine Data": 3})
    mapping.update_sub_categories(vehicle_id, "ECM", "Engine Data", {"Fuel Trim": 1})
    mapping.update_sub_categories(vehicle_id, "ECM", "Misfire Data", {"Cylinder 1": 4})

    loaded = mapping.load_mapping(vehicle_id)

    assert loaded == {
        "vehicle_id": vehicle_id,
        "modules": {
            "ECM": {
                "index": 0,
                "data_categories": {
                    "Engine Data": {
                        "index": 3,
                        "has_sub": True,
                        "sub_categories": {"Fuel Trim": 1},
                    },
                    "Misfire Data": {
                        "index": 0,
                        "has_sub": True,
                        "sub_categories": {"Cylinder 1": 4},
                    },
                },
            }
        },
    }


def test_vehicle_discovery_connect_returns_false_without_pywinauto(monkeypatch) -> None:
    monkeypatch.setattr("src.discovery.vehicle_mapping.Application", None)
    discovery = VehicleDiscovery()

    assert discovery.connect() is False
    assert discovery.get_list_items() == []


def test_vehicle_discovery_reads_list_items_and_visibility(monkeypatch) -> None:
    class _Rect:
        def __init__(self, width: int, height: int, left: int, top: int) -> None:
            self._width = width
            self._height = height
            self.left = left
            self.top = top

        def width(self) -> int:
            return self._width

        def height(self) -> int:
            return self._height

    class _Item:
        def __init__(self, text: str, rect: _Rect) -> None:
            self._text = text
            self._rect = rect

        def window_text(self) -> str:
            return self._text

        def rectangle(self) -> _Rect:
            return self._rect

    class _Window:
        def descendants(self, control_type: str):
            assert control_type == "ListItem"
            return [
                _Item("ECM", _Rect(200, 20, 10, 10)),
                _Item("", _Rect(200, 20, 10, 10)),
                _Item("Hidden TCM", _Rect(80, 5, -1, 0)),
            ]

    discovery = VehicleDiscovery()
    discovery._window = _Window()

    items = discovery.get_list_items()

    assert items == [
        {"index": 0, "name": "ECM", "visible": True},
        {"index": 2, "name": "Hidden TCM", "visible": False},
    ]
    assert discovery.discover_modules() == {"ECM": 0, "Hidden TCM": 2}
    assert discovery.discover_data_categories() == {"ECM": 0, "Hidden TCM": 2}


def test_discover_current_page_uses_connected_discovery(monkeypatch) -> None:
    class _FakeDiscovery:
        def connect(self) -> bool:
            return True

        def get_list_items(self):
            return [{"index": 0, "name": "ECM", "visible": True}]

    monkeypatch.setattr("src.discovery.vehicle_mapping.VehicleDiscovery", _FakeDiscovery)

    assert discover_current_page() == [{"index": 0, "name": "ECM", "visible": True}]
