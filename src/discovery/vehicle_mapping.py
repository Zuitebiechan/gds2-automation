"""
Vehicle Mapping Discovery

Discovers and stores module/data mappings for vehicles using pywinauto.
Uses keyboard navigation (UP/DOWN/ENTER) instead of coordinate-based clicking.

Strategy:
- Module list: Discovered once when entering Module List page
- Data list: On-demand discovery when first accessing a module's data
- Sub-categories: Discovered when a data category has nested items

Data Format (v2):
- Old format: "data_categories": {"Engine Data": 0}
- New format: "data_categories": {
    "Engine Data": {"index": 0, "has_sub": false},
    "Fuel System Data": {
        "index": 12,
        "has_sub": true,
        "sub_categories": {"Fuel Injector Data": 0, "Fuel Pump Data": 1}
    }
}
"""

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any, Union
try:
    from pywinauto import Application
except ImportError:
    Application = None

logger = logging.getLogger(__name__)

# Default mappings directory
MAPPINGS_DIR = Path(__file__).parent.parent.parent / "mappings"


class VehicleMapping:
    """Manages vehicle module/data mappings for keyboard navigation."""

    def __init__(self, mappings_dir: Optional[Path] = None):
        """Initialize vehicle mapping manager."""
        self.mappings_dir = mappings_dir or MAPPINGS_DIR
        self.mappings_dir.mkdir(parents=True, exist_ok=True)
        self._cache: Dict[str, Any] = {}

    def get_mapping_path(self, vehicle_id: str) -> Path:
        """Get path to mapping file for a vehicle."""
        # Sanitize vehicle_id for filename
        safe_name = "".join(c if c.isalnum() or c in "._- " else "_" for c in vehicle_id)
        return self.mappings_dir / f"{safe_name}.json"

    def load_mapping(self, vehicle_id: str) -> Optional[Dict]:
        """Load mapping for a vehicle from JSON file."""
        if vehicle_id in self._cache:
            return self._cache[vehicle_id]

        mapping_path = self.get_mapping_path(vehicle_id)
        if mapping_path.exists():
            with open(mapping_path, "r", encoding="utf-8") as f:
                mapping = json.load(f)
                self._cache[vehicle_id] = mapping
                return mapping
        return None

    def save_mapping(self, vehicle_id: str, mapping: Dict):
        """Save mapping for a vehicle to JSON file."""
        mapping_path = self.get_mapping_path(vehicle_id)
        with open(mapping_path, "w", encoding="utf-8") as f:
            json.dump(mapping, f, indent=2, ensure_ascii=False)
        self._cache[vehicle_id] = mapping
        logger.info(f"Saved mapping to {mapping_path}")

    def get_module_index(self, vehicle_id: str, module_name: str) -> Optional[int]:
        """Get DOWN key presses needed to select a module."""
        mapping = self.load_mapping(vehicle_id)
        if not mapping:
            return None

        modules = mapping.get("modules", {})
        module_info = modules.get(module_name)
        if module_info:
            return module_info.get("index")
        return None

    def get_data_category_index(
        self, vehicle_id: str, module_name: str, data_category: str
    ) -> Optional[int]:
        """
        Get DOWN key presses needed to select a data category.

        Handles both old format (int) and new format (dict with index).
        """
        mapping = self.load_mapping(vehicle_id)
        if not mapping:
            return None

        modules = mapping.get("modules", {})
        module_info = modules.get(module_name)
        if not module_info:
            return None

        data_categories = module_info.get("data_categories", {})
        category_info = data_categories.get(data_category)

        if category_info is None:
            return None

        # Handle both old format (int) and new format (dict)
        if isinstance(category_info, int):
            return category_info
        elif isinstance(category_info, dict):
            return category_info.get("index")
        return None

    def has_module_list(self, vehicle_id: str) -> bool:
        """Check if module list has been discovered for this vehicle."""
        mapping = self.load_mapping(vehicle_id)
        if not mapping:
            return False
        return len(mapping.get("modules", {})) > 0

    def has_data_categories(self, vehicle_id: str, module_name: str) -> bool:
        """Check if data categories have been discovered for this module."""
        mapping = self.load_mapping(vehicle_id)
        if not mapping:
            return False

        modules = mapping.get("modules", {})
        module_info = modules.get(module_name)
        if not module_info:
            return False

        return "data_categories" in module_info and len(module_info["data_categories"]) > 0

    def has_sub_categories(self, vehicle_id: str, module_name: str, data_category: str) -> bool:
        """
        Check if a data category has sub-categories.

        Args:
            vehicle_id: Vehicle identifier
            module_name: Module name
            data_category: Data category name

        Returns:
            True if has sub-categories, False otherwise
        """
        mapping = self.load_mapping(vehicle_id)
        if not mapping:
            return False

        modules = mapping.get("modules", {})
        module_info = modules.get(module_name)
        if not module_info:
            return False

        data_categories = module_info.get("data_categories", {})
        category_info = data_categories.get(data_category)

        if category_info is None:
            return False

        # Only new format supports sub-categories
        if isinstance(category_info, dict):
            return category_info.get("has_sub", False)
        return False

    def get_sub_categories(
        self, vehicle_id: str, module_name: str, data_category: str
    ) -> Optional[Dict[str, int]]:
        """
        Get sub-categories for a data category.

        Args:
            vehicle_id: Vehicle identifier
            module_name: Module name
            data_category: Data category name

        Returns:
            Dict mapping sub-category name to index, or None if not found
        """
        mapping = self.load_mapping(vehicle_id)
        if not mapping:
            return None

        modules = mapping.get("modules", {})
        module_info = modules.get(module_name)
        if not module_info:
            return None

        data_categories = module_info.get("data_categories", {})
        category_info = data_categories.get(data_category)

        if category_info is None:
            return None

        # Only new format supports sub-categories
        if isinstance(category_info, dict):
            return category_info.get("sub_categories")
        return None

    def get_sub_category_index(
        self, vehicle_id: str, module_name: str, data_category: str, sub_category: str
    ) -> Optional[int]:
        """
        Get index of a sub-category within its parent data category.

        Args:
            vehicle_id: Vehicle identifier
            module_name: Module name
            data_category: Parent data category name
            sub_category: Sub-category name

        Returns:
            Index (DOWN key presses) for the sub-category, or None
        """
        sub_cats = self.get_sub_categories(vehicle_id, module_name, data_category)
        if sub_cats is None:
            return None
        return sub_cats.get(sub_category)

    def update_module_list(self, vehicle_id: str, modules: Dict[str, int]):
        """
        Update module list for a vehicle.

        Args:
            vehicle_id: Vehicle identifier
            modules: Dict mapping module name to index
        """
        mapping = self.load_mapping(vehicle_id) or {
            "vehicle_id": vehicle_id,
            "modules": {}
        }

        for module_name, index in modules.items():
            if module_name not in mapping["modules"]:
                mapping["modules"][module_name] = {"index": index}
            else:
                mapping["modules"][module_name]["index"] = index

        self.save_mapping(vehicle_id, mapping)

    def update_data_categories(
        self, vehicle_id: str, module_name: str, data_categories: Dict[str, Union[int, Dict]]
    ):
        """
        Update data categories for a specific module.

        Accepts both old format (name -> int) and new format (name -> dict).

        Args:
            vehicle_id: Vehicle identifier
            module_name: Module name
            data_categories: Dict mapping data category name to index or info dict
        """
        mapping = self.load_mapping(vehicle_id) or {
            "vehicle_id": vehicle_id,
            "modules": {}
        }

        if module_name not in mapping["modules"]:
            mapping["modules"][module_name] = {"index": 0}

        mapping["modules"][module_name]["data_categories"] = data_categories
        self.save_mapping(vehicle_id, mapping)

    def update_sub_categories(
        self, vehicle_id: str, module_name: str, data_category: str,
        sub_categories: Dict[str, int]
    ):
        """
        Update sub-categories for a specific data category.

        Converts old format (int) to new format (dict) if needed.

        Args:
            vehicle_id: Vehicle identifier
            module_name: Module name
            data_category: Parent data category name
            sub_categories: Dict mapping sub-category name to index
        """
        mapping = self.load_mapping(vehicle_id) or {
            "vehicle_id": vehicle_id,
            "modules": {}
        }

        if module_name not in mapping["modules"]:
            mapping["modules"][module_name] = {"index": 0}

        module_info = mapping["modules"][module_name]
        data_cats = module_info.setdefault("data_categories", {})

        category_info = data_cats.get(data_category)

        if category_info is None:
            # Category not yet registered
            data_cats[data_category] = {
                "index": 0,
                "has_sub": True,
                "sub_categories": sub_categories
            }
        elif isinstance(category_info, int):
            # Upgrade from old format (int) to new format (dict)
            data_cats[data_category] = {
                "index": category_info,
                "has_sub": True,
                "sub_categories": sub_categories
            }
        elif isinstance(category_info, dict):
            # Update existing new-format entry
            category_info["has_sub"] = True
            category_info["sub_categories"] = sub_categories
        else:
            logger.warning(f"Unexpected category info type: {type(category_info)}")
            return

        self.save_mapping(vehicle_id, mapping)


class VehicleDiscovery:
    """Discovers module/data structure for a vehicle using pywinauto."""

    def __init__(self):
        """Initialize discovery."""
        self._app = None
        self._window = None

    def connect(self) -> bool:
        """Connect to GDS2 window."""
        try:
            self._app = Application(backend='uia').connect(title_re='GDS 2')
            self._window = self._app.window(title_re='GDS 2')
            return True
        except Exception as e:
            logger.error(f"Failed to connect to GDS2: {e}")
            return False

    def get_list_items(self) -> List[Dict]:
        """
        Get all list items from current page.

        Returns:
            List of dicts with 'index', 'name', 'visible' keys
        """
        if not self._window:
            if not self.connect():
                return []

        items = []
        all_items = self._window.descendants(control_type='ListItem')

        for i, item in enumerate(all_items):
            text = item.window_text()
            if text:
                rect = item.rectangle()
                is_visible = (
                    rect.width() > 100 and
                    rect.height() > 10 and
                    rect.left > 0 and
                    rect.top > 0
                )
                items.append({
                    "index": i,
                    "name": text,
                    "visible": is_visible
                })

        return items

    def discover_modules(self) -> Dict[str, int]:
        """
        Discover all modules on current Module List page.

        Returns:
            Dict mapping module name to index (DOWN key presses)
        """
        items = self.get_list_items()
        return {item["name"]: item["index"] for item in items}

    def discover_data_categories(self) -> Dict[str, int]:
        """
        Discover all data categories on current Data List page.

        Returns:
            Dict mapping data category name to index (DOWN key presses)
        """
        items = self.get_list_items()
        return {item["name"]: item["index"] for item in items}


def discover_current_page() -> List[Dict]:
    """
    Utility function to discover list items on current GDS2 page.

    Returns:
        List of items with index, name, and visibility
    """
    discovery = VehicleDiscovery()
    if discovery.connect():
        return discovery.get_list_items()
    return []


def print_current_page():
    """Print all list items on current GDS2 page."""
    items = discover_current_page()
    print(f"Found {len(items)} items:")
    print("=" * 60)
    for item in items:
        status = "VISIBLE" if item["visible"] else "hidden"
        print(f"{item['index']:2}. {item['name']:50} {status}")
