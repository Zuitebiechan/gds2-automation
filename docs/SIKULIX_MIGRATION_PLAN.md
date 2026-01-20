# SikuliX Migration Plan

**Status:** Planned (Not Started)
**Created:** 2026-01-20
**Purpose:** Migrate from pywinauto to SikuliX for more reliable JavaFX automation

---

## Why Migrate?

### Current Issues with pywinauto + JavaFX

| Issue | Impact |
|-------|--------|
| Dynamic auto_ids | Cannot use auto_id for element identification |
| List selection fails | Had to use keyboard workarounds |
| Some elements not exposed | UIA doesn't see all JavaFX controls |
| Unreliable invoke() | Some buttons don't respond |

### Why SikuliX is Better for JavaFX

- Image-based matching works regardless of UI technology
- Fuzzy matching handles minor UI variations
- Built-in OCR for text verification
- More robust for complex JavaFX UIs
- Wait functions, region targeting built-in

---

## Migration Scope

### What Changes vs What Stays

```
┌─────────────────────────────────────────┐
│         Workflow Layer                  │  ← NO CHANGE
│   ReadVehicleDTCWorkflow                │
└─────────────────┬───────────────────────┘
                  │
┌─────────────────▼───────────────────────┐
│         Page Object Layer               │  ← MINOR CHANGES
│   MainMenuPage, DTCPage, etc.           │
└─────────────────┬───────────────────────┘
                  │
┌─────────────────▼───────────────────────┐
│         Driver Layer                    │  ← REPLACE
│   GDS2Driver (pywinauto → SikuliX)      │
└─────────────────┬───────────────────────┘
                  │
┌─────────────────▼───────────────────────┐
│         Locators                        │  ← REPLACE
│   (title, type) → image paths           │
└─────────────────────────────────────────┘
```

### Change Summary

| Component | Change Required | Effort |
|-----------|-----------------|--------|
| `src/workflows/*.py` | None | 0% |
| `src/pages/*.py` | Minor - same methods, same returns | 10% |
| `src/core/driver.py` | Replace - new SikuliX driver | 40% |
| `src/core/locators.py` | Replace - image paths instead of text | 20% |
| `images/` folder | New - capture element screenshots | 30% |

---

## Screenshots Required

### Estimated Count: ~30 images

```
images/
├── buttons/              # Navigation buttons (~10)
│   ├── diagnostics.png
│   ├── enter.png
│   ├── back.png
│   ├── home.png
│   ├── continue.png
│   ├── ok.png
│   ├── create_report.png
│   ├── clear_dtcs.png
│   ├── refresh.png
│   └── module.png
│
├── list_items/           # Menu items (~10)
│   ├── module_diagnostics.png
│   ├── vehicle_diagnostics.png
│   ├── system_diagnostics.png
│   ├── session_manager.png
│   ├── vehicle_dtc_info.png
│   ├── clear_vehicle_dtcs.png
│   ├── supported_modules.png
│   ├── dtc_information.png
│   ├── data_display.png
│   └── special_functions.png
│
├── pages/                # Page identifiers (~6)
│   ├── main_menu_header.png
│   ├── device_explorer_title.png
│   ├── vehicle_selection_banner.png
│   ├── diagnostics_menu_header.png
│   ├── vehicle_diagnostics_header.png
│   └── dtc_page_header.png
│
└── devices/              # Device selection (~2)
    ├── sm2_usb.png
    └── sm2_usb_selected.png
```

---

## New Code Structure

### New Locators (src/core/locators.py)

```python
from dataclasses import dataclass
from pathlib import Path

IMAGES_DIR = Path(__file__).parent.parent.parent / "images"

@dataclass
class ImageLocator:
    image_path: Path
    description: str
    confidence: float = 0.8

class GDS2Locators:
    class MainMenu:
        DIAGNOSTICS_BTN = ImageLocator(
            IMAGES_DIR / "buttons/diagnostics.png",
            "Main diagnostics button",
            confidence=0.85
        )
        HOME_BTN = ImageLocator(
            IMAGES_DIR / "buttons/home.png",
            "Return to main menu"
        )

    class DeviceExplorer:
        CONTINUE_BTN = ImageLocator(
            IMAGES_DIR / "buttons/continue.png",
            "Continue with selected device"
        )
        SM2_USB = ImageLocator(
            IMAGES_DIR / "devices/sm2_usb.png",
            "SM2 USB device item"
        )

    class DiagnosticsMenu:
        VEHICLE_DIAGNOSTICS = ImageLocator(
            IMAGES_DIR / "list_items/vehicle_diagnostics.png",
            "Vehicle diagnostics menu item"
        )
        ENTER_BTN = ImageLocator(
            IMAGES_DIR / "buttons/enter.png",
            "Enter selected menu"
        )

    class DTCPage:
        CLEAR_DTCS_BTN = ImageLocator(
            IMAGES_DIR / "buttons/clear_dtcs.png",
            "Clear DTCs button"
        )
        CREATE_REPORT_BTN = ImageLocator(
            IMAGES_DIR / "buttons/create_report.png",
            "Create HTML report"
        )

    class Pages:
        MAIN_MENU = ImageLocator(
            IMAGES_DIR / "pages/main_menu_header.png",
            "Main menu page identifier"
        )
        DTC_PAGE = ImageLocator(
            IMAGES_DIR / "pages/dtc_page_header.png",
            "DTC page identifier"
        )

Loc = GDS2Locators
```

### New SikuliX Driver (src/core/sikulix_driver.py)

```python
from sikuli import *
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .locators import ImageLocator

class GDS2Driver:
    """SikuliX-based driver for GDS2 automation"""

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.default_timeout = self.config.get("default_timeout", 30)
        self.default_confidence = self.config.get("confidence", 0.8)

        # SikuliX settings
        Settings.MinSimilarity = self.default_confidence
        Settings.MoveMouseDelay = 0.5

    def find_element(self, locator: 'ImageLocator',
                     timeout: float = None) -> Match:
        """Find element by image"""
        timeout = timeout or self.default_timeout
        pattern = Pattern(str(locator.image_path)).similar(locator.confidence)
        return wait(pattern, timeout)

    def click_button(self, locator: 'ImageLocator') -> bool:
        """Click a button"""
        pattern = Pattern(str(locator.image_path)).similar(locator.confidence)
        click(pattern)
        return True

    def click_list_item(self, locator: 'ImageLocator') -> bool:
        """Click a list item"""
        pattern = Pattern(str(locator.image_path)).similar(locator.confidence)
        click(pattern)
        return True

    def element_exists(self, locator: 'ImageLocator',
                       timeout: float = 0.5) -> bool:
        """Check if element exists on screen"""
        pattern = Pattern(str(locator.image_path)).similar(locator.confidence)
        return exists(pattern, timeout) is not None

    def wait_for_element(self, locator: 'ImageLocator',
                         timeout: float = None) -> bool:
        """Wait for element to appear"""
        timeout = timeout or self.default_timeout
        pattern = Pattern(str(locator.image_path)).similar(locator.confidence)
        try:
            wait(pattern, timeout)
            return True
        except FindFailed:
            return False

    def wait_for_element_vanish(self, locator: 'ImageLocator',
                                 timeout: float = None) -> bool:
        """Wait for element to disappear (loading screens)"""
        timeout = timeout or self.default_timeout
        pattern = Pattern(str(locator.image_path)).similar(locator.confidence)
        return waitVanish(pattern, timeout)

    def wait_for_element_enabled(self, locator: 'ImageLocator',
                                  timeout: float = None) -> bool:
        """
        Wait for element to be enabled.
        For SikuliX, we need separate images for enabled/disabled states,
        or use visual difference detection.
        """
        # Simply wait for the element to appear
        # For enabled state, use a separate image of the enabled button
        return self.wait_for_element(locator, timeout)

    def read_text(self, region: tuple) -> str:
        """OCR text from region (x, y, width, height)"""
        r = Region(*region)
        return r.text()

    def capture_region(self, region: tuple, save_path: str) -> str:
        """Capture a region of the screen"""
        r = Region(*region)
        return capture(r, save_path)

    # Context manager support
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False
```

---

## Automated Screenshot Capture Tool

### Script: scripts/capture_elements.py

```python
"""
Automated GDS2 UI element capture tool.

Usage:
    python capture_elements.py --auto      # Auto-navigate and capture
    python capture_elements.py --current   # Capture current page only
    python capture_elements.py --element   # Interactive element capture
"""

import argparse
import json
import time
from pathlib import Path
from datetime import datetime

import pyautogui
from pywinauto import Application, Desktop

IMAGES_DIR = Path(__file__).parent.parent / "images"
MANIFEST_FILE = IMAGES_DIR / "manifest.json"


class ElementCapture:
    def __init__(self):
        self.app = None
        self.manifest = {}
        self._load_manifest()

    def _load_manifest(self):
        if MANIFEST_FILE.exists():
            self.manifest = json.loads(MANIFEST_FILE.read_text())

    def _save_manifest(self):
        MANIFEST_FILE.write_text(json.dumps(self.manifest, indent=2))

    def connect(self):
        """Connect to GDS2"""
        self.app = Application(backend="uia").connect(title="GDS 2")
        return self.app.window(title="GDS 2")

    def capture_element(self, element, page_name: str, element_name: str):
        """Capture screenshot of a single element"""
        rect = element.rectangle()

        # Add padding
        padding = 5
        region = (
            rect.left - padding,
            rect.top - padding,
            rect.width() + padding * 2,
            rect.height() + padding * 2
        )

        # Create directory
        page_dir = IMAGES_DIR / page_name
        page_dir.mkdir(parents=True, exist_ok=True)

        # Capture
        filename = f"{element_name}.png"
        filepath = page_dir / filename
        screenshot = pyautogui.screenshot(region=region)
        screenshot.save(filepath)

        # Update manifest
        key = f"{page_name}/{filename}"
        self.manifest[key] = {
            "title": element.window_text(),
            "type": element.element_info.control_type,
            "page": page_name,
            "position": {
                "x": rect.left,
                "y": rect.top,
                "width": rect.width(),
                "height": rect.height()
            },
            "captured_at": datetime.now().isoformat()
        }

        print(f"  Captured: {key}")
        return filepath

    def capture_page_elements(self, window, page_name: str):
        """Capture all interactive elements on current page"""
        print(f"\nCapturing page: {page_name}")

        # Find buttons
        buttons = window.descendants(control_type="Button")
        for btn in buttons:
            title = btn.window_text()
            if title and len(title) > 1:
                safe_name = title.lower().replace(" ", "_") + "_btn"
                self.capture_element(btn, page_name, safe_name)

        # Find list items
        list_items = window.descendants(control_type="ListItem")
        for item in list_items:
            title = item.window_text()
            if title and len(title) > 1:
                safe_name = title.lower().replace(" ", "_") + "_item"
                self.capture_element(item, page_name, safe_name)

        self._save_manifest()

    def capture_current_page(self, page_name: str = None):
        """Capture all elements on the current page"""
        window = self.connect()

        if not page_name:
            page_name = input("Enter page name (e.g., main_menu): ").strip()

        self.capture_page_elements(window, page_name)
        print(f"\nDone! Images saved to: {IMAGES_DIR / page_name}")

    def auto_capture(self):
        """Automatically navigate and capture pages"""
        window = self.connect()

        # Page 1: Main Menu
        self.capture_page_elements(window, "main_menu")

        # Navigate to Device Explorer
        try:
            diag_btn = window.child_window(title="Diagnostics", control_type="Button")
            if diag_btn.exists(timeout=2):
                diag_btn.invoke()
                time.sleep(2)

                # Page 2: Device Explorer
                desktop = Desktop(backend="uia")
                explorer = desktop.window(title_re=".*Device.*")
                if explorer.exists(timeout=5):
                    self.capture_page_elements(explorer, "device_explorer")
        except Exception as e:
            print(f"Could not capture Device Explorer: {e}")

        print(f"\nAuto-capture complete! Check: {IMAGES_DIR}")

    def interactive_capture(self):
        """Interactive mode: click elements to capture"""
        print("Interactive capture mode")
        print("1. Position mouse over element")
        print("2. Press Enter to capture")
        print("3. Type 'done' to finish")

        window = self.connect()
        page_name = input("Enter page name: ").strip()

        while True:
            element_name = input("\nElement name (or 'done'): ").strip()
            if element_name.lower() == 'done':
                break

            input("Position mouse over element, then press Enter...")

            # Get mouse position
            x, y = pyautogui.position()

            # Capture region around mouse
            size = 100
            region = (x - size//2, y - size//2, size, size)

            page_dir = IMAGES_DIR / page_name
            page_dir.mkdir(parents=True, exist_ok=True)

            filepath = page_dir / f"{element_name}.png"
            screenshot = pyautogui.screenshot(region=region)
            screenshot.save(filepath)

            print(f"Captured: {filepath}")

        print("Interactive capture complete!")


def main():
    parser = argparse.ArgumentParser(description="GDS2 Element Capture Tool")
    parser.add_argument("--auto", action="store_true", help="Auto-navigate and capture")
    parser.add_argument("--current", action="store_true", help="Capture current page")
    parser.add_argument("--element", action="store_true", help="Interactive element capture")
    parser.add_argument("--page", type=str, help="Page name for --current mode")

    args = parser.parse_args()

    capturer = ElementCapture()

    if args.auto:
        capturer.auto_capture()
    elif args.current:
        capturer.capture_current_page(args.page)
    elif args.element:
        capturer.interactive_capture()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
```

---

## Migration Steps

| Step | Task | Estimated Time |
|------|------|----------------|
| 1 | Install SikuliX + Java runtime | 30 min |
| 2 | Run capture tool on each GDS2 page | 1-2 hours |
| 3 | Review and organize captured images | 30 min |
| 4 | Create new `locators.py` with image paths | 30 min |
| 5 | Create `sikulix_driver.py` | 2 hours |
| 6 | Update `base_page.py` for new driver | 30 min |
| 7 | Test each page object | 2-3 hours |
| 8 | Run full workflow test | 1 hour |
| 9 | Clean up old pywinauto code | 30 min |

**Total Estimated Time: ~1 day**

---

## SikuliX Installation

### Prerequisites
1. Java JDK 8+ installed
2. JAVA_HOME environment variable set

### Install SikuliX
```bash
# Download SikuliX
# https://raiman.github.io/SikuliX1/downloads.html

# Or use pip for Python bindings
pip install sikulix4python

# Alternative: Use Jython with SikuliX
# Download sikulixide-2.0.5.jar
java -jar sikulixide-2.0.5.jar
```

### Python Integration Options

**Option A: sikulix4python (easiest)**
```python
pip install sikulix4python
from sikulix4python import *
```

**Option B: py4j bridge**
```python
pip install py4j
# Requires SikuliX server running
```

**Option C: Jython scripts**
```bash
# Run scripts directly with Jython
java -jar sikulix.jar -r script.py
```

---

## Fallback Strategy

If SikuliX has issues, consider hybrid approach:

```python
class HybridDriver:
    def __init__(self):
        self.pywinauto_driver = PywinautoDriver()
        self.sikulix_driver = SikulixDriver()

    def click_button(self, locator):
        # Try pywinauto first (faster)
        try:
            return self.pywinauto_driver.click_button(locator.title)
        except:
            pass

        # Fallback to SikuliX (more reliable)
        return self.sikulix_driver.click_button(locator.image_path)
```

---

## Files to Create

- [ ] `src/core/sikulix_driver.py` - New driver implementation
- [ ] `src/core/locators_sikulix.py` - Image-based locators
- [ ] `scripts/capture_elements.py` - Screenshot capture tool
- [ ] `images/` - Directory for element screenshots
- [ ] `images/manifest.json` - Element metadata

## Files to Modify

- [ ] `src/pages/base_page.py` - Import new driver
- [ ] `src/pages/*.py` - Minimal changes (same method signatures)
- [ ] `requirements.txt` - Add SikuliX dependencies

## Files Unchanged

- `src/workflows/*.py` - No changes needed
- `src/utils/report_parser.py` - No changes needed
- `docs/CLAUDE.md` - Update after migration complete

---

## Notes

- Keep pywinauto code as backup until SikuliX is fully tested
- Capture screenshots at the same resolution/DPI as production
- Consider capturing both enabled and disabled states for buttons
- Test with different Windows themes/scaling settings

---

**This plan will be executed later. Current pywinauto implementation works for the demo.**
