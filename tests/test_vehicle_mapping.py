"""
Tests for VehicleMapping with sub-category support.

Tests both old format (backward compatibility) and new format with sub-categories.
"""

import json
import pytest
import tempfile
from pathlib import Path

from src.discovery.vehicle_mapping import VehicleMapping


@pytest.fixture
def temp_dir():
    """Create a temporary directory for test mappings."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def mapping(temp_dir):
    """Create VehicleMapping with temp directory."""
    return VehicleMapping(mappings_dir=temp_dir)


class TestOldFormatBackwardCompat:
    """Tests for backward compatibility with old format."""

    def test_old_format_int_index(self, mapping, temp_dir):
        """Test reading old format where data_categories maps to int."""
        # Create old format mapping
        old_mapping = {
            "vehicle_id": "test_vehicle",
            "modules": {
                "[K20] Engine Control Module": {
                    "index": 5,
                    "data_categories": {
                        "Engine Data": 0,
                        "Fuel System Data": 1,
                        "Misfire Data": 2
                    }
                }
            }
        }

        mapping_path = temp_dir / "test_vehicle.json"
        with open(mapping_path, "w") as f:
            json.dump(old_mapping, f)

        # Test reading old format
        idx = mapping.get_data_category_index(
            "test_vehicle", "[K20] Engine Control Module", "Misfire Data"
        )
        assert idx == 2

    def test_old_format_has_data_categories(self, mapping, temp_dir):
        """Test has_data_categories with old format."""
        old_mapping = {
            "vehicle_id": "test_vehicle",
            "modules": {
                "[K20] Engine Control Module": {
                    "index": 5,
                    "data_categories": {"Engine Data": 0}
                }
            }
        }

        mapping_path = temp_dir / "test_vehicle.json"
        with open(mapping_path, "w") as f:
            json.dump(old_mapping, f)

        assert mapping.has_data_categories("test_vehicle", "[K20] Engine Control Module")

    def test_old_format_no_sub_categories(self, mapping, temp_dir):
        """Test that old format reports no sub-categories."""
        old_mapping = {
            "vehicle_id": "test_vehicle",
            "modules": {
                "[K20] Engine Control Module": {
                    "index": 5,
                    "data_categories": {"Engine Data": 0}
                }
            }
        }

        mapping_path = temp_dir / "test_vehicle.json"
        with open(mapping_path, "w") as f:
            json.dump(old_mapping, f)

        assert not mapping.has_sub_categories(
            "test_vehicle", "[K20] Engine Control Module", "Engine Data"
        )


class TestNewFormatWithSubCategories:
    """Tests for new format with sub-category support."""

    def test_new_format_no_sub(self, mapping, temp_dir):
        """Test new format without sub-categories."""
        new_mapping = {
            "vehicle_id": "test_vehicle",
            "modules": {
                "[K20] Engine Control Module": {
                    "index": 5,
                    "data_categories": {
                        "Engine Data": {"index": 0, "has_sub": False},
                        "Misfire Data": {"index": 2, "has_sub": False}
                    }
                }
            }
        }

        mapping_path = temp_dir / "test_vehicle.json"
        with open(mapping_path, "w") as f:
            json.dump(new_mapping, f)

        idx = mapping.get_data_category_index(
            "test_vehicle", "[K20] Engine Control Module", "Misfire Data"
        )
        assert idx == 2

        assert not mapping.has_sub_categories(
            "test_vehicle", "[K20] Engine Control Module", "Misfire Data"
        )

    def test_new_format_with_sub(self, mapping, temp_dir):
        """Test new format with sub-categories."""
        new_mapping = {
            "vehicle_id": "test_vehicle",
            "modules": {
                "[K20] Engine Control Module": {
                    "index": 5,
                    "data_categories": {
                        "Fuel System Data": {
                            "index": 12,
                            "has_sub": True,
                            "sub_categories": {
                                "Fuel Injector Data": 0,
                                "Fuel Pump Data": 1,
                                "Fuel Trim Data": 2
                            }
                        }
                    }
                }
            }
        }

        mapping_path = temp_dir / "test_vehicle.json"
        with open(mapping_path, "w") as f:
            json.dump(new_mapping, f)

        # Check index
        idx = mapping.get_data_category_index(
            "test_vehicle", "[K20] Engine Control Module", "Fuel System Data"
        )
        assert idx == 12

        # Check has sub-categories
        assert mapping.has_sub_categories(
            "test_vehicle", "[K20] Engine Control Module", "Fuel System Data"
        )

        # Get sub-categories
        sub_cats = mapping.get_sub_categories(
            "test_vehicle", "[K20] Engine Control Module", "Fuel System Data"
        )
        assert sub_cats == {
            "Fuel Injector Data": 0,
            "Fuel Pump Data": 1,
            "Fuel Trim Data": 2
        }


class TestGetSubCategories:
    """Tests for sub-category retrieval."""

    def test_get_sub_categories_exists(self, mapping, temp_dir):
        """Test getting sub-categories when they exist."""
        new_mapping = {
            "vehicle_id": "test_vehicle",
            "modules": {
                "Module": {
                    "index": 0,
                    "data_categories": {
                        "Category": {
                            "index": 0,
                            "has_sub": True,
                            "sub_categories": {"Sub1": 0, "Sub2": 1}
                        }
                    }
                }
            }
        }

        mapping_path = temp_dir / "test_vehicle.json"
        with open(mapping_path, "w") as f:
            json.dump(new_mapping, f)

        sub_cats = mapping.get_sub_categories("test_vehicle", "Module", "Category")
        assert sub_cats == {"Sub1": 0, "Sub2": 1}

    def test_get_sub_categories_none(self, mapping, temp_dir):
        """Test getting sub-categories when category has none."""
        new_mapping = {
            "vehicle_id": "test_vehicle",
            "modules": {
                "Module": {
                    "index": 0,
                    "data_categories": {
                        "Category": {"index": 0, "has_sub": False}
                    }
                }
            }
        }

        mapping_path = temp_dir / "test_vehicle.json"
        with open(mapping_path, "w") as f:
            json.dump(new_mapping, f)

        sub_cats = mapping.get_sub_categories("test_vehicle", "Module", "Category")
        assert sub_cats is None

    def test_get_sub_categories_old_format(self, mapping, temp_dir):
        """Test getting sub-categories returns None for old format."""
        old_mapping = {
            "vehicle_id": "test_vehicle",
            "modules": {
                "Module": {
                    "index": 0,
                    "data_categories": {"Category": 0}
                }
            }
        }

        mapping_path = temp_dir / "test_vehicle.json"
        with open(mapping_path, "w") as f:
            json.dump(old_mapping, f)

        sub_cats = mapping.get_sub_categories("test_vehicle", "Module", "Category")
        assert sub_cats is None

    def test_get_sub_category_index(self, mapping, temp_dir):
        """Test getting index of a specific sub-category."""
        new_mapping = {
            "vehicle_id": "test_vehicle",
            "modules": {
                "Module": {
                    "index": 0,
                    "data_categories": {
                        "Category": {
                            "index": 0,
                            "has_sub": True,
                            "sub_categories": {"Sub1": 0, "Sub2": 1, "Sub3": 2}
                        }
                    }
                }
            }
        }

        mapping_path = temp_dir / "test_vehicle.json"
        with open(mapping_path, "w") as f:
            json.dump(new_mapping, f)

        idx = mapping.get_sub_category_index("test_vehicle", "Module", "Category", "Sub2")
        assert idx == 1

    def test_get_sub_category_index_not_found(self, mapping, temp_dir):
        """Test getting index of non-existent sub-category."""
        new_mapping = {
            "vehicle_id": "test_vehicle",
            "modules": {
                "Module": {
                    "index": 0,
                    "data_categories": {
                        "Category": {
                            "index": 0,
                            "has_sub": True,
                            "sub_categories": {"Sub1": 0}
                        }
                    }
                }
            }
        }

        mapping_path = temp_dir / "test_vehicle.json"
        with open(mapping_path, "w") as f:
            json.dump(new_mapping, f)

        idx = mapping.get_sub_category_index("test_vehicle", "Module", "Category", "NonExistent")
        assert idx is None


class TestUpdateSubCategories:
    """Tests for updating sub-categories."""

    def test_update_sub_categories_new_category(self, mapping):
        """Test adding sub-categories to a new category."""
        mapping.update_sub_categories(
            "test_vehicle", "Module", "Category",
            {"Sub1": 0, "Sub2": 1}
        )

        loaded = mapping.load_mapping("test_vehicle")
        cat_info = loaded["modules"]["Module"]["data_categories"]["Category"]

        assert cat_info["has_sub"] is True
        assert cat_info["sub_categories"] == {"Sub1": 0, "Sub2": 1}

    def test_update_sub_categories_upgrade_old_format(self, mapping, temp_dir):
        """Test upgrading old format to new format when adding sub-categories."""
        # Create old format
        old_mapping = {
            "vehicle_id": "test_vehicle",
            "modules": {
                "Module": {
                    "index": 0,
                    "data_categories": {"Category": 5}  # Old format: int
                }
            }
        }

        mapping_path = temp_dir / "test_vehicle.json"
        with open(mapping_path, "w") as f:
            json.dump(old_mapping, f)

        # Clear cache
        mapping._cache.clear()

        # Add sub-categories
        mapping.update_sub_categories(
            "test_vehicle", "Module", "Category",
            {"Sub1": 0}
        )

        # Clear cache to force reload
        mapping._cache.clear()

        loaded = mapping.load_mapping("test_vehicle")
        cat_info = loaded["modules"]["Module"]["data_categories"]["Category"]

        # Should be upgraded to new format, preserving index
        assert isinstance(cat_info, dict)
        assert cat_info["index"] == 5
        assert cat_info["has_sub"] is True
        assert cat_info["sub_categories"] == {"Sub1": 0}

    def test_update_sub_categories_existing_new_format(self, mapping, temp_dir):
        """Test updating sub-categories in existing new format."""
        new_mapping = {
            "vehicle_id": "test_vehicle",
            "modules": {
                "Module": {
                    "index": 0,
                    "data_categories": {
                        "Category": {"index": 3, "has_sub": False}
                    }
                }
            }
        }

        mapping_path = temp_dir / "test_vehicle.json"
        with open(mapping_path, "w") as f:
            json.dump(new_mapping, f)

        mapping._cache.clear()

        mapping.update_sub_categories(
            "test_vehicle", "Module", "Category",
            {"NewSub": 0}
        )

        mapping._cache.clear()

        loaded = mapping.load_mapping("test_vehicle")
        cat_info = loaded["modules"]["Module"]["data_categories"]["Category"]

        assert cat_info["index"] == 3  # Preserved
        assert cat_info["has_sub"] is True
        assert cat_info["sub_categories"] == {"NewSub": 0}


class TestMixedFormat:
    """Tests for mixed old and new format in same mapping."""

    def test_mixed_format(self, mapping, temp_dir):
        """Test mapping with both old and new format categories."""
        mixed_mapping = {
            "vehicle_id": "test_vehicle",
            "modules": {
                "Module": {
                    "index": 0,
                    "data_categories": {
                        "OldCategory": 0,  # Old format
                        "NewCategory": {"index": 1, "has_sub": False},  # New format, no sub
                        "SubCategory": {  # New format with sub
                            "index": 2,
                            "has_sub": True,
                            "sub_categories": {"Sub1": 0}
                        }
                    }
                }
            }
        }

        mapping_path = temp_dir / "test_vehicle.json"
        with open(mapping_path, "w") as f:
            json.dump(mixed_mapping, f)

        # Test old format
        assert mapping.get_data_category_index("test_vehicle", "Module", "OldCategory") == 0
        assert not mapping.has_sub_categories("test_vehicle", "Module", "OldCategory")

        # Test new format without sub
        assert mapping.get_data_category_index("test_vehicle", "Module", "NewCategory") == 1
        assert not mapping.has_sub_categories("test_vehicle", "Module", "NewCategory")

        # Test new format with sub
        assert mapping.get_data_category_index("test_vehicle", "Module", "SubCategory") == 2
        assert mapping.has_sub_categories("test_vehicle", "Module", "SubCategory")
        assert mapping.get_sub_categories("test_vehicle", "Module", "SubCategory") == {"Sub1": 0}
