"""
UI Module for GDS2 RPA Automation.

Contains Tkinter-based user interfaces for:
- Module selection
- Data display results
- Discovery progress
"""

from .module_selector import ModuleDataDisplayUI, create_ui

__all__ = [
    "ModuleDataDisplayUI",
    "create_ui",
]
