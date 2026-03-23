"""Vehicle/module/data mapping helpers for keyboard-driven navigation."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

try:
    from pywinauto import Application
except ImportError:  # pragma: no cover - optional runtime dependency
    Application = None

logger = logging.getLogger(__name__)

MAPPINGS_DIR = Path(__file__).resolve().parents[2] / "mappings"


class VehicleMapping:
    """Manage persisted module and data-category indexes for a vehicle."""

    def __init__(self, mappings_dir: Optional[Path] = None) -> None:
        self.mappings_dir = mappings_dir or MAPPINGS_DIR
        self.mappings_dir.mkdir(parents=True, exist_ok=True)
        self._cache: Dict[str, Any] = {}

    def get_mapping_path(self, vehicle_id: str) -> Path:
        safe_name = "".join(
            char if char.isalnum() or char in "._- " else "_"
            for char in vehicle_id
        )
        return self.mappings_dir / f"{safe_name}.json"

    def load_mapping(self, vehicle_id: str) -> Optional[Dict[str, Any]]:
        if vehicle_id in self._cache:
            return self._cache[vehicle_id]

        mapping_path = self.get_mapping_path(vehicle_id)
        if not mapping_path.exists():
            return None

        with mapping_path.open("r", encoding="utf-8") as handle:
            mapping = json.load(handle)

        self._cache[vehicle_id] = mapping
        return mapping

    def save_mapping(self, vehicle_id: str, mapping: Dict[str, Any]) -> None:
        mapping_path = self.get_mapping_path(vehicle_id)
        with mapping_path.open("w", encoding="utf-8") as handle:
            json.dump(mapping, handle, indent=2, ensure_ascii=False)

        self._cache[vehicle_id] = mapping
        logger.info("Saved mapping to %s", mapping_path)

    def get_module_index(self, vehicle_id: str, module_name: str) -> Optional[int]:
        mapping = self.load_mapping(vehicle_id)
        if not mapping:
            return None

        module_info = mapping.get("modules", {}).get(module_name)
        if module_info is None:
            return None

        return module_info.get("index")

    def get_data_category_index(
        self,
        vehicle_id: str,
        module_name: str,
        data_category: str,
    ) -> Optional[int]:
        mapping = self.load_mapping(vehicle_id)
        if not mapping:
            return None

        module_info = mapping.get("modules", {}).get(module_name)
        if not module_info:
            return None

        category_info = module_info.get("data_categories", {}).get(data_category)
        if category_info is None:
            return None

        if isinstance(category_info, int):
            return category_info
        if isinstance(category_info, dict):
            return category_info.get("index")
        return None

    def has_module_list(self, vehicle_id: str) -> bool:
        mapping = self.load_mapping(vehicle_id)
        if not mapping:
            return False
        return len(mapping.get("modules", {})) > 0

    def has_data_categories(self, vehicle_id: str, module_name: str) -> bool:
        mapping = self.load_mapping(vehicle_id)
        if not mapping:
            return False

        module_info = mapping.get("modules", {}).get(module_name)
        if not module_info:
            return False

        data_categories = module_info.get("data_categories", {})
        return len(data_categories) > 0

    def has_sub_categories(
        self,
        vehicle_id: str,
        module_name: str,
        data_category: str,
    ) -> bool:
        mapping = self.load_mapping(vehicle_id)
        if not mapping:
            return False

        module_info = mapping.get("modules", {}).get(module_name)
        if not module_info:
            return False

        category_info = module_info.get("data_categories", {}).get(data_category)
        if isinstance(category_info, dict):
            return bool(category_info.get("has_sub", False))
        return False

    def get_sub_categories(
        self,
        vehicle_id: str,
        module_name: str,
        data_category: str,
    ) -> Optional[Dict[str, int]]:
        mapping = self.load_mapping(vehicle_id)
        if not mapping:
            return None

        module_info = mapping.get("modules", {}).get(module_name)
        if not module_info:
            return None

        category_info = module_info.get("data_categories", {}).get(data_category)
        if isinstance(category_info, dict):
            return category_info.get("sub_categories")
        return None

    def get_sub_category_index(
        self,
        vehicle_id: str,
        module_name: str,
        data_category: str,
        sub_category: str,
    ) -> Optional[int]:
        sub_categories = self.get_sub_categories(vehicle_id, module_name, data_category)
        if sub_categories is None:
            return None
        return sub_categories.get(sub_category)

    def update_module_list(self, vehicle_id: str, modules: Dict[str, int]) -> None:
        mapping = self.load_mapping(vehicle_id) or {
            "vehicle_id": vehicle_id,
            "modules": {},
        }

        for module_name, index in modules.items():
            module_info = mapping["modules"].setdefault(module_name, {})
            module_info["index"] = index

        self.save_mapping(vehicle_id, mapping)

    def update_data_categories(
        self,
        vehicle_id: str,
        module_name: str,
        data_categories: Dict[str, Union[int, Dict[str, Any]]],
    ) -> None:
        mapping = self.load_mapping(vehicle_id) or {
            "vehicle_id": vehicle_id,
            "modules": {},
        }

        module_info = mapping["modules"].setdefault(module_name, {"index": 0})
        module_info["data_categories"] = data_categories
        self.save_mapping(vehicle_id, mapping)

    def update_sub_categories(
        self,
        vehicle_id: str,
        module_name: str,
        data_category: str,
        sub_categories: Dict[str, int],
    ) -> None:
        mapping = self.load_mapping(vehicle_id) or {
            "vehicle_id": vehicle_id,
            "modules": {},
        }

        module_info = mapping["modules"].setdefault(module_name, {"index": 0})
        data_categories = module_info.setdefault("data_categories", {})
        category_info = data_categories.get(data_category)

        if category_info is None:
            data_categories[data_category] = {
                "index": 0,
                "has_sub": True,
                "sub_categories": sub_categories,
            }
        elif isinstance(category_info, int):
            data_categories[data_category] = {
                "index": category_info,
                "has_sub": True,
                "sub_categories": sub_categories,
            }
        elif isinstance(category_info, dict):
            category_info["has_sub"] = True
            category_info["sub_categories"] = sub_categories
        else:
            logger.warning("Unexpected category info type: %s", type(category_info))
            return

        self.save_mapping(vehicle_id, mapping)


class VehicleDiscovery:
    """Discover list items from the live GDS2 window using pywinauto."""

    def __init__(self) -> None:
        self._app = None
        self._window = None

    def connect(self) -> bool:
        if Application is None:
            logger.error("pywinauto is not installed")
            return False

        try:
            self._app = Application(backend="uia").connect(title_re="GDS 2")
            self._window = self._app.window(title_re="GDS 2")
            return True
        except Exception as exc:  # pragma: no cover - runtime integration wrapper
            logger.error("Failed to connect to GDS2: %s", exc)
            return False

    def get_list_items(self) -> List[Dict[str, Any]]:
        if self._window is None and not self.connect():
            return []

        items: List[Dict[str, Any]] = []
        for index, item in enumerate(self._window.descendants(control_type="ListItem")):
            text = item.window_text()
            if not text:
                continue

            rect = item.rectangle()
            is_visible = (
                rect.width() > 100
                and rect.height() > 10
                and rect.left > 0
                and rect.top > 0
            )
            items.append(
                {
                    "index": index,
                    "name": text,
                    "visible": is_visible,
                }
            )

        return items

    def discover_modules(self) -> Dict[str, int]:
        return {item["name"]: item["index"] for item in self.get_list_items()}

    def discover_data_categories(self) -> Dict[str, int]:
        return {item["name"]: item["index"] for item in self.get_list_items()}


def discover_current_page() -> List[Dict[str, Any]]:
    discovery = VehicleDiscovery()
    if discovery.connect():
        return discovery.get_list_items()
    return []


def print_current_page() -> None:
    items = discover_current_page()
    print(f"Found {len(items)} items:")
    print("=" * 60)
    for item in items:
        status = "VISIBLE" if item["visible"] else "hidden"
        print(f"{item['index']:2}. {item['name']:50} {status}")
