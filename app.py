"""
GDS2 Automation Web UI - Flask Version

Simple web interface for GDS2 vehicle diagnostics automation.
"""

from flask import Flask, render_template, jsonify, request, Response
from flask_cors import CORS
import logging
import re
import time
import json
import queue
import threading
from pathlib import Path
from enum import Enum
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, asdict

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler('gds2_web.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)


# =============================================================================
# State Machine
# =============================================================================

class GDS2State(Enum):
    """GDS2 application state."""
    UNKNOWN = "unknown"
    MAIN_MENU = "main_menu"
    MODULE_LIST = "module_list"
    DATA_LIST = "data_list"
    DATA_DISPLAY = "data_display"


@dataclass
class AppState:
    """Application state container."""
    gds2_state: str = GDS2State.MAIN_MENU.value
    current_module: Optional[str] = None
    current_data_category: Optional[str] = None
    data_list_focus_index: int = 0


# Global state
app_state = AppState()


# =============================================================================
# GDS2 Controller
# =============================================================================

class GDS2Controller:
    """Controller for GDS2 automation."""

    def __init__(self):
        """Initialize controller."""
        self._mapping = None
        self._discovery = None

    @property
    def mapping(self):
        """Lazy-load vehicle mapping."""
        if self._mapping is None:
            from src.discovery import VehicleMapping
            self._mapping = VehicleMapping()
        return self._mapping

    @property
    def discovery(self):
        """Lazy-load vehicle discovery."""
        if self._discovery is None:
            from src.discovery import VehicleDiscovery
            self._discovery = VehicleDiscovery()
        return self._discovery

    def get_module_list(self, vehicle_id: str = "current_vehicle") -> List[str]:
        """Get list of available modules."""
        mapping = self.mapping.load_mapping(vehicle_id)
        if mapping and "modules" in mapping:
            return list(mapping["modules"].keys())
        return []

    def get_data_categories(self, vehicle_id: str, module_name: str) -> List[str]:
        """Get data categories for a module."""
        mapping = self.mapping.load_mapping(vehicle_id)
        if not mapping:
            return []

        modules = mapping.get("modules", {})
        module_info = modules.get(module_name, {})
        data_categories = module_info.get("data_categories", {})
        return list(data_categories.keys())

    def discover_data_categories(self, vehicle_id: str, module_name: str) -> List[str]:
        """Discover data categories for a module."""
        logger.info(f"Discovering data categories for {module_name}...")

        if self.discovery.connect():
            data_categories = self.discovery.discover_data_categories()
            logger.info(f"Found {len(data_categories)} data categories")
            self.mapping.update_data_categories(vehicle_id, module_name, data_categories)
            return list(data_categories.keys())

        return []

    def discover_modules(self, vehicle_id: str = "current_vehicle") -> List[str]:
        """
        Discover all modules by navigating to Module List page.

        Returns list of discovered module names.
        """
        import pyautogui
        import cv2
        import numpy as np
        from PIL import ImageGrab

        IMAGES_DIR = Path(r"C:\Users\shsww\projects\RPA_demo\images")
        BUTTONS_DIR = IMAGES_DIR / "buttons"
        LIST_ITEMS_DIR = IMAGES_DIR / "list_items"
        DEVICES_DIR = IMAGES_DIR / "devices"

        def find_and_click(button_name: str, confidence: float = 0.8, timeout: float = 10) -> bool:
            image_path = BUTTONS_DIR / f"{button_name}.png"
            if not image_path.exists():
                image_path = LIST_ITEMS_DIR / f"{button_name}.png"
            if not image_path.exists():
                logger.error(f"Template not found: {button_name}")
                return False

            start_time = time.time()
            while time.time() - start_time < timeout:
                template = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
                if template is None:
                    return False

                screenshot = ImageGrab.grab()
                screenshot_gray = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2GRAY)
                result = cv2.matchTemplate(screenshot_gray, template, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(result)

                if max_val >= confidence:
                    h, w = template.shape
                    x, y = max_loc[0] + w // 2, max_loc[1] + h // 2
                    pyautogui.click(x, y)
                    return True
                time.sleep(0.5)
            return False

        def find_button(button_name: str, confidence: float = 0.8) -> bool:
            image_path = BUTTONS_DIR / f"{button_name}.png"
            if not image_path.exists():
                return False

            template = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
            if template is None:
                return False

            screenshot = ImageGrab.grab()
            screenshot_gray = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2GRAY)
            result = cv2.matchTemplate(screenshot_gray, template, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv2.minMaxLoc(result)
            return max_val >= confidence

        def focus_gds2():
            try:
                from pywinauto import Application
                app = Application(backend="uia").connect(title_re=".*GDS 2.*")
                window = app.top_window()
                window.set_focus()
                time.sleep(0.3)
            except Exception as e:
                logger.warning(f"Could not focus GDS2: {e}")

        try:
            logger.info("=== Discovering Modules ===")

            # Step 1: Click Diagnostics
            logger.info("Step 1: Clicking Diagnostics...")
            if not find_and_click("diagnostics", confidence=0.8, timeout=10):
                logger.error("Failed to click Diagnostics")
                return []
            time.sleep(3)

            # Step 2: Handle Device Explorer (if appears)
            logger.info("Step 2: Checking Device Explorer...")
            if find_button("continue", confidence=0.9):
                logger.info("  Device Explorer popup detected")

                # Try to click SM2 USB device using template matching
                # First try highlighted version, then normal version
                screenshot = ImageGrab.grab()
                screenshot_gray = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2GRAY)

                device_matched = False
                for device_name in ["sm2_usb_highlight", "sm2_usb"]:
                    device_path = DEVICES_DIR / f"{device_name}.png"
                    if device_path.exists():
                        template = cv2.imread(str(device_path), cv2.IMREAD_GRAYSCALE)
                        result = cv2.matchTemplate(screenshot_gray, template, cv2.TM_CCOEFF_NORMED)
                        _, max_val, _, max_loc = cv2.minMaxLoc(result)
                        logger.info(f"  {device_name} template match confidence: {max_val:.3f}")

                        if max_val >= 0.85:
                            h, w = template.shape
                            x, y = max_loc[0] + w // 2, max_loc[1] + h // 2
                            pyautogui.click(x, y)
                            time.sleep(1)
                            device_matched = True
                            logger.info(f"  Successfully matched {device_name}")
                            break

                if not device_matched:
                    logger.warning("  Failed to match any SM2 USB template")

                find_and_click("continue", confidence=0.9, timeout=5)
                time.sleep(3)
            else:
                logger.info("  No Device Explorer popup")

            # Step 3: Click Enter for vehicle selection
            logger.info("Step 3: Clicking Enter...")
            if not find_and_click("enter", confidence=0.85, timeout=10):
                logger.error("Failed to click Enter")
                return []
            time.sleep(3)

            # Handle warning dialog
            if find_button("ok", confidence=0.85):
                find_and_click("ok", confidence=0.85, timeout=5)
                time.sleep(1)

            # Step 4: Click Module Diagnostics
            logger.info("Step 4: Clicking Module Diagnostics...")
            if not find_and_click("module_diagnostics", confidence=0.85, timeout=10):
                logger.error("Failed to click Module Diagnostics")
                return []
            time.sleep(2)

            # Step 5: Discover modules using pywinauto
            logger.info("Step 5: Discovering modules with pywinauto...")
            if self.discovery.connect():
                modules = self.discovery.discover_modules()
                logger.info(f"Found {len(modules)} modules")

                # Save to mapping
                module_indices = {name: idx for name, idx in modules.items()}
                self.mapping.update_module_list(vehicle_id, module_indices)

                logger.info("=== Module Discovery Complete ===")
                return list(modules.keys())
            else:
                logger.error("Failed to connect to GDS2 for discovery")
                return []

        except Exception as e:
            logger.exception(f"Module discovery failed: {e}")
            return []

    def navigate_to_data_list_from_module_list(self, module_name: str) -> bool:
        """
        Navigate from Module List page to Data List page.
        Assumes GDS2 is already at Module List page.
        """
        import pyautogui
        import cv2
        import numpy as np
        from PIL import ImageGrab

        IMAGES_DIR = Path(r"C:\Users\shsww\projects\RPA_demo\images")
        BUTTONS_DIR = IMAGES_DIR / "buttons"
        LIST_ITEMS_DIR = IMAGES_DIR / "list_items"

        def find_and_click(button_name: str, confidence: float = 0.8, timeout: float = 10) -> bool:
            image_path = BUTTONS_DIR / f"{button_name}.png"
            if not image_path.exists():
                image_path = LIST_ITEMS_DIR / f"{button_name}.png"
            if not image_path.exists():
                logger.error(f"Template not found: {button_name}")
                return False

            start_time = time.time()
            while time.time() - start_time < timeout:
                template = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
                if template is None:
                    return False

                screenshot = ImageGrab.grab()
                screenshot_gray = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2GRAY)
                result = cv2.matchTemplate(screenshot_gray, template, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(result)

                if max_val >= confidence:
                    h, w = template.shape
                    x, y = max_loc[0] + w // 2, max_loc[1] + h // 2
                    pyautogui.click(x, y)
                    return True
                time.sleep(0.5)
            return False

        def focus_gds2():
            try:
                from pywinauto import Application
                app = Application(backend="uia").connect(title_re=".*GDS 2.*")
                window = app.top_window()
                window.set_focus()
                time.sleep(0.3)
            except Exception as e:
                logger.warning(f"Could not focus GDS2: {e}")

        try:
            logger.info(f"=== Navigating from Module List to Data List ===")
            logger.info(f"Target module: {module_name}")

            # Step 1: Select module using keyboard
            module_index = self.mapping.get_module_index("current_vehicle", module_name)
            if module_index is None:
                logger.error(f"Module {module_name} not found in mapping")
                return False

            logger.info(f"Step 1: Selecting module at index {module_index}...")
            focus_gds2()
            if module_index > 0:
                for _ in range(module_index):
                    pyautogui.press('down')
                    time.sleep(0.1)
            time.sleep(0.5)
            pyautogui.press('enter')
            time.sleep(3)

            # Step 2: Click Data Display
            logger.info("Step 2: Clicking Data Display...")
            if not find_and_click("data_display", confidence=0.90, timeout=10):
                logger.error("Failed to click Data Display")
                return False
            time.sleep(3)

            logger.info("=== Navigation to Data List Complete ===")
            return True

        except Exception as e:
            logger.exception(f"Navigation failed: {e}")
            return False

    def navigate_to_data_list(self, module_name: str) -> bool:
        """Navigate from Main Menu to Data List page."""
        import pyautogui
        import cv2
        import numpy as np
        from PIL import ImageGrab

        IMAGES_DIR = Path(r"C:\Users\shsww\projects\RPA_demo\images")
        BUTTONS_DIR = IMAGES_DIR / "buttons"
        LIST_ITEMS_DIR = IMAGES_DIR / "list_items"
        DEVICES_DIR = IMAGES_DIR / "devices"

        def find_and_click(button_name: str, confidence: float = 0.8, timeout: float = 10) -> bool:
            # Try buttons directory first
            image_path = BUTTONS_DIR / f"{button_name}.png"

            # If not found, try list_items directory
            if not image_path.exists():
                image_path = LIST_ITEMS_DIR / f"{button_name}.png"

            if not image_path.exists():
                logger.error(f"Template not found: {button_name}")
                return False

            start_time = time.time()
            while time.time() - start_time < timeout:
                template = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
                if template is None:
                    return False

                screenshot = ImageGrab.grab()
                screenshot_gray = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2GRAY)
                result = cv2.matchTemplate(screenshot_gray, template, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(result)

                if max_val >= confidence:
                    h, w = template.shape
                    x, y = max_loc[0] + w // 2, max_loc[1] + h // 2
                    pyautogui.click(x, y)
                    return True
                time.sleep(0.5)
            return False

        def find_button(button_name: str, confidence: float = 0.8) -> bool:
            image_path = BUTTONS_DIR / f"{button_name}.png"
            if not image_path.exists():
                return False

            template = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
            if template is None:
                return False

            screenshot = ImageGrab.grab()
            screenshot_gray = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2GRAY)
            result = cv2.matchTemplate(screenshot_gray, template, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv2.minMaxLoc(result)
            return max_val >= confidence

        def focus_gds2():
            try:
                from pywinauto import Application
                app = Application(backend="uia").connect(title_re=".*GDS 2.*")
                window = app.top_window()
                window.set_focus()
                time.sleep(0.3)
            except Exception as e:
                logger.warning(f"Could not focus GDS2: {e}")

        try:
            logger.info("Step 1: Clicking Diagnostics...")
            if not find_and_click("diagnostics", confidence=0.8, timeout=10):
                return False
            time.sleep(3)

            logger.info("Step 2: Checking Device Explorer...")
            if find_button("continue", confidence=0.9):
                # Try to click SM2 USB device using template matching
                # First try highlighted version, then normal version
                screenshot = ImageGrab.grab()
                screenshot_gray = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2GRAY)

                device_matched = False
                for device_name in ["sm2_usb_highlight", "sm2_usb"]:
                    device_path = DEVICES_DIR / f"{device_name}.png"
                    if device_path.exists():
                        template = cv2.imread(str(device_path), cv2.IMREAD_GRAYSCALE)
                        result = cv2.matchTemplate(screenshot_gray, template, cv2.TM_CCOEFF_NORMED)
                        _, max_val, _, max_loc = cv2.minMaxLoc(result)

                        if max_val >= 0.85:
                            h, w = template.shape
                            x, y = max_loc[0] + w // 2, max_loc[1] + h // 2
                            pyautogui.click(x, y)
                            time.sleep(1)
                            device_matched = True
                            break

                find_and_click("continue", confidence=0.9, timeout=5)
                time.sleep(3)

            logger.info("Step 3: Clicking Enter...")
            if not find_and_click("enter", confidence=0.85, timeout=10):
                return False
            time.sleep(3)

            if find_button("ok", confidence=0.85):
                find_and_click("ok", confidence=0.85, timeout=5)
                time.sleep(1)

            logger.info("Step 4: Clicking Module Diagnostics...")
            if not find_and_click("module_diagnostics", confidence=0.85, timeout=10):
                return False
            time.sleep(2)

            logger.info(f"Step 5: Selecting module {module_name}...")
            module_index = self.mapping.get_module_index("current_vehicle", module_name)
            if module_index is None:
                return False

            focus_gds2()
            if module_index > 0:
                for _ in range(module_index):
                    pyautogui.press('down')
                    time.sleep(0.1)
            time.sleep(0.5)
            pyautogui.press('enter')
            time.sleep(3)

            logger.info("Step 6: Clicking Data Display...")
            if not find_and_click("data_display", confidence=0.90, timeout=10):
                return False
            time.sleep(3)

            return True

        except Exception as e:
            logger.exception(f"Navigation failed: {e}")
            return False

    def navigate_to_data_display(self, module_name: str, data_category: str, current_focus: int) -> Optional[str]:
        """Navigate from Data List to Data Display and create report."""
        import pyautogui
        import cv2
        import numpy as np
        from PIL import ImageGrab

        IMAGES_DIR = Path(r"C:\Users\shsww\projects\RPA_demo\images")
        BUTTONS_DIR = IMAGES_DIR / "buttons"
        LIST_ITEMS_DIR = IMAGES_DIR / "list_items"

        def find_and_click(button_name: str, confidence: float = 0.8, timeout: float = 10) -> bool:
            # Try buttons directory first
            image_path = BUTTONS_DIR / f"{button_name}.png"

            # If not found, try list_items directory
            if not image_path.exists():
                image_path = LIST_ITEMS_DIR / f"{button_name}.png"

            if not image_path.exists():
                logger.error(f"Template not found: {button_name}")
                return False

            start_time = time.time()
            while time.time() - start_time < timeout:
                template = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
                if template is None:
                    return False

                screenshot = ImageGrab.grab()
                screenshot_gray = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2GRAY)
                result = cv2.matchTemplate(screenshot_gray, template, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(result)

                if max_val >= confidence:
                    h, w = template.shape
                    x, y = max_loc[0] + w // 2, max_loc[1] + h // 2
                    pyautogui.click(x, y)
                    return True
                time.sleep(0.5)
            return False

        def focus_gds2():
            try:
                from pywinauto import Application
                app = Application(backend="uia").connect(title_re=".*GDS 2.*")
                window = app.top_window()
                window.set_focus()
                time.sleep(0.3)
            except:
                pass

        try:
            target_index = self.mapping.get_data_category_index("current_vehicle", module_name, data_category)
            if target_index is None:
                return None

            relative_steps = target_index - current_focus

            logger.info(f"Navigating to {data_category} (target={target_index}, current={current_focus}, relative={relative_steps})")

            focus_gds2()
            if relative_steps > 0:
                for _ in range(relative_steps):
                    pyautogui.press('down')
                    time.sleep(0.1)
            elif relative_steps < 0:
                for _ in range(abs(relative_steps)):
                    pyautogui.press('up')
                    time.sleep(0.1)

            time.sleep(0.5)
            pyautogui.press('enter')
            time.sleep(5)

            logger.info("Clicking Create Report...")
            if not find_and_click("create_report", confidence=0.7, timeout=30):
                return None

            time.sleep(2)

            report_dir = Path.home() / "AppData" / "Local" / "Temp" / "GDS 2"
            if report_dir.exists():
                reports = list(report_dir.glob("Data Display_*.html"))
                if reports:
                    latest_report = max(reports, key=lambda p: p.stat().st_mtime)
                    return str(latest_report)

            return None

        except Exception as e:
            logger.exception(f"Navigation failed: {e}")
            return None

    def click_back_button(self) -> bool:
        """Click Back button."""
        import pyautogui
        import cv2
        import numpy as np
        from PIL import ImageGrab

        BUTTONS_DIR = Path(r"C:\Users\shsww\projects\RPA_demo\images\buttons")
        image_path = BUTTONS_DIR / "back.png"

        if not image_path.exists():
            return False

        try:
            template = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
            if template is None:
                return False

            start_time = time.time()
            while time.time() - start_time < 10:
                screenshot = ImageGrab.grab()
                screenshot_gray = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2GRAY)
                result = cv2.matchTemplate(screenshot_gray, template, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(result)

                if max_val >= 0.8:
                    h, w = template.shape
                    x, y = max_loc[0] + w // 2, max_loc[1] + h // 2
                    pyautogui.click(x, y)
                    time.sleep(1.5)
                    return True
                time.sleep(0.5)

            return False

        except Exception as e:
            logger.exception(f"Back button failed: {e}")
            return False

    def parse_report(self, report_path: str) -> Dict[str, Any]:
        """Parse HTML report."""
        result = {"vehicle_info": {}, "data_items": []}

        try:
            with open(report_path, 'r', encoding='iso-8859-1') as f:
                html_content = f.read()

            vin_match = re.search(r'Vehicle Identification Number \(VIN\)</td><td>([^<]+)</td>', html_content)
            if vin_match:
                result["vehicle_info"]["vin"] = vin_match.group(1)

            make_match = re.search(r'<td>Make</td><td>([^<]+)</td>', html_content)
            if make_match:
                result["vehicle_info"]["make"] = make_match.group(1)

            model_match = re.search(r'<td>Model</td><td>([^<]+)</td>', html_content)
            if model_match:
                result["vehicle_info"]["model"] = model_match.group(1)

            year_match = re.search(r'<td>Model Year</td><td>([^<]+)</td>', html_content)
            if year_match:
                result["vehicle_info"]["year"] = year_match.group(1)

            data_pattern = r'<tr><td>([^<]+)</td><td>([^<]*)</td><td>([^<]*)</td></tr>'
            matches = re.findall(data_pattern, html_content)

            for match in matches:
                parameter, value, units = match
                if parameter not in ["Parameter", "Control Module", "DTC Type"]:
                    result["data_items"].append({
                        "parameter": parameter.strip(),
                        "value": value.strip(),
                        "units": units.strip(),
                    })

        except Exception as e:
            logger.error(f"Error parsing report: {e}")

        return result


# Global controller
controller = GDS2Controller()


# =============================================================================
# Flask Routes
# =============================================================================

@app.route('/test')
def test():
    """Simple test endpoint."""
    return "<h1>Flask is working!</h1><p>If you see this, the server is running correctly.</p>"


@app.route('/')
def index():
    """Render main page."""
    return render_template('index.html')


@app.route('/api/fetch_modules', methods=['POST'])
def fetch_modules():
    """
    Step 1: Fetch all modules.
    Assumes GDS2 is at Main Menu.
    Navigates to Module List and discovers all modules.
    """
    try:
        logger.info("=== Step 1: Fetch Modules ===")
        logger.info("Assumption: GDS2 is at Main Menu")

        # Discover modules (navigates from Main Menu to Module List)
        modules = controller.discover_modules("current_vehicle")

        if modules:
            # Update state
            app_state.gds2_state = GDS2State.MODULE_LIST.value
            app_state.current_module = None
            app_state.current_data_category = None

            logger.info(f"Found {len(modules)} modules. GDS2 is now at Module List.")

            return jsonify({
                "success": True,
                "modules": modules,
                "state": asdict(app_state)
            })
        else:
            return jsonify({"error": "Failed to discover modules"}), 500

    except Exception as e:
        logger.exception("Fetch modules failed")
        return jsonify({"error": str(e)}), 500


@app.route('/api/fetch_categories', methods=['POST'])
def fetch_categories():
    """
    Step 2: Fetch data categories for selected module.
    Assumes GDS2 is at Module List.
    Selects module, navigates to Data List, and discovers all data categories.
    """
    data = request.json
    module_name = data.get('module')

    if not module_name:
        return jsonify({"error": "Module name required"}), 400

    try:
        logger.info(f"=== Step 2: Fetch Categories for {module_name} ===")
        logger.info("Assumption: GDS2 is at Module List")

        # Navigate from Module List to Data List
        success = controller.navigate_to_data_list_from_module_list(module_name)
        if not success:
            return jsonify({"error": "Failed to navigate to data list"}), 500

        # Discover data categories
        categories = controller.discover_data_categories("current_vehicle", module_name)

        if not categories:
            return jsonify({"error": "Failed to discover data categories"}), 500

        # Update state
        app_state.gds2_state = GDS2State.DATA_LIST.value
        app_state.current_module = module_name
        app_state.data_list_focus_index = 0

        logger.info(f"Found {len(categories)} data categories. GDS2 is now at Data List.")

        return jsonify({
            "success": True,
            "data_categories": categories,
            "state": asdict(app_state)
        })

    except Exception as e:
        logger.exception("Fetch categories failed")
        return jsonify({"error": str(e)}), 500


@app.route('/api/get_dtcs', methods=['POST'])
def get_dtcs():
    """
    Step 3: Get DTCs (Diagnostic Trouble Codes).
    Assumes GDS2 is at Data List.
    Selects "Vehicle DTC Information", creates report, parses DTCs, clicks Back.
    """
    try:
        logger.info("=== Step 3: Get DTCs ===")
        logger.info("Assumption: GDS2 is at Data List")

        if app_state.gds2_state != GDS2State.DATA_LIST.value:
            return jsonify({
                "error": f"GDS2 must be at Data List page. Current state: {app_state.gds2_state}"
            }), 400

        if not app_state.current_module:
            return jsonify({"error": "No module selected. Please complete Step 2 first."}), 400

        module_name = app_state.current_module

        # Find "Vehicle DTC Information" data category
        # This is the standard data category for reading DTCs
        dtc_category = "Vehicle DTC Information"
        target_index = controller.mapping.get_data_category_index("current_vehicle", module_name, dtc_category)

        if target_index is None:
            # Try alternative names
            for alt_name in ["Vehicle DTC and ID Information", "DTC Information", "Vehicle DTCs"]:
                target_index = controller.mapping.get_data_category_index("current_vehicle", module_name, alt_name)
                if target_index is not None:
                    dtc_category = alt_name
                    break

        if target_index is None:
            return jsonify({"error": "Could not find DTC data category. Please ensure vehicle supports DTC reading."}), 400

        logger.info(f"Found DTC category: {dtc_category} at index {target_index}")

        # Navigate to DTC display
        report_path = controller.navigate_to_data_display(
            module_name, dtc_category, app_state.data_list_focus_index
        )

        if not report_path:
            return jsonify({"error": "Failed to create DTC report"}), 500

        # Parse DTC report
        from src.utils.report_parser import GDS2ReportParser
        parser = GDS2ReportParser()
        dtc_data = parser.parse_dtc_report(report_path)

        # Click Back to return to Data List
        logger.info("Clicking Back to return to Data List...")
        success = controller.click_back_button()
        if not success:
            return jsonify({"error": "Failed to click Back button"}), 500

        time.sleep(1.5)

        # Update state
        app_state.gds2_state = GDS2State.DATA_LIST.value
        app_state.data_list_focus_index = target_index

        logger.info(f"Get DTCs complete. Found {len(dtc_data['dtc_list'])} DTCs. GDS2 returned to Data List.")

        return jsonify({
            "success": True,
            "vehicle_info": dtc_data.get("vehicle_info", {}),
            "dtc_list": dtc_data.get("dtc_list", []),
            "module_status": dtc_data.get("module_status", []),
            "report_path": report_path,
            "state": asdict(app_state)
        })

    except Exception as e:
        logger.exception("Get DTCs failed")
        return jsonify({"error": str(e)}), 500


@app.route('/api/search_data', methods=['POST'])
def search_data():
    """
    Get DTCs: Search data for selected category.
    Assumes GDS2 is at Data List.
    Selects data category, creates report, parses DTCs, clicks Back to return to Data List.
    """
    data = request.json
    module_name = data.get('module')
    data_category = data.get('data_category')

    if not module_name or not data_category:
        return jsonify({"error": "Module and data category required"}), 400

    try:
        logger.info(f"=== Get DTCs: Search {data_category} ===")
        logger.info("Assumption: GDS2 is at Data List")

        # Get target index
        target_index = controller.mapping.get_data_category_index("current_vehicle", module_name, data_category)
        if target_index is None:
            return jsonify({"error": f"Data category {data_category} not found"}), 400

        # Navigate to data display (from current focus position)
        report_path = controller.navigate_to_data_display(
            module_name, data_category, app_state.data_list_focus_index
        )

        if not report_path:
            return jsonify({"error": "Failed to create report"}), 500

        # Parse report for regular data
        report_data = controller.parse_report(report_path)

        # Also parse for DTCs
        from src.utils.report_parser import GDS2ReportParser
        dtc_parser = GDS2ReportParser()
        dtc_data = dtc_parser.parse_dtc_report(report_path)

        # Click Back to return to Data List
        logger.info("Clicking Back to return to Data List...")
        success = controller.click_back_button()
        if not success:
            return jsonify({"error": "Failed to click Back button"}), 500

        time.sleep(1.5)

        # Update state - back at Data List with focus on the item we just viewed
        app_state.gds2_state = GDS2State.DATA_LIST.value
        app_state.current_data_category = data_category
        app_state.data_list_focus_index = target_index

        logger.info(f"Search complete. GDS2 returned to Data List (focus at index {target_index}).")
        logger.info(f"Parsed {len(dtc_data.get('dtc_list', []))} DTCs from report.")

        return jsonify({
            "success": True,
            "report_path": report_path,
            "report_data": report_data,
            "vehicle_info": dtc_data.get("vehicle_info", {}),
            "dtc_list": dtc_data.get("dtc_list", []),
            "module_status": dtc_data.get("module_status", []),
            "state": asdict(app_state)
        })

    except Exception as e:
        logger.exception("Search data failed")
        return jsonify({"error": str(e)}), 500


@app.route('/api/discover_modules', methods=['POST'])
def discover_modules():
    """Discover all modules from GDS2."""
    try:
        logger.info("Starting module discovery...")

        # Discover modules
        modules = controller.discover_modules("current_vehicle")

        if modules:
            # Update state
            app_state.gds2_state = GDS2State.MODULE_LIST.value
            app_state.current_module = None
            app_state.current_data_category = None

            return jsonify({
                "success": True,
                "modules": modules,
                "state": asdict(app_state)
            })
        else:
            return jsonify({"error": "Failed to discover modules"}), 500

    except Exception as e:
        logger.exception("Module discovery failed")
        return jsonify({"error": str(e)}), 500


@app.route('/api/modules')
def get_modules():
    """Get list of modules."""
    modules = controller.get_module_list()
    return jsonify({"modules": modules})


@app.route('/api/data_categories')
def get_data_categories():
    """Get data categories for a module."""
    module_name = request.args.get('module')
    if not module_name:
        return jsonify({"error": "Module name required"}), 400

    categories = controller.get_data_categories("current_vehicle", module_name)
    return jsonify({"data_categories": categories})


@app.route('/api/fetch_and_discover', methods=['POST'])
def fetch_and_discover():
    """
    Fetch first data (Engine Data at index 0) then discover all categories.
    Assumes GDS2 is at Main Menu.
    """
    data = request.json
    module_name = data.get('module')

    if not module_name:
        return jsonify({"error": "Module name required"}), 400

    try:
        logger.info(f"=== Fetch & Discover: {module_name} ===")
        logger.info("Assumption: GDS2 is at Main Menu")

        # Step 1: Navigate to Data List (full path from Main Menu)
        logger.info("Step 1: Navigating from Main Menu to Data List...")
        success = controller.navigate_to_data_list(module_name)
        if not success:
            return jsonify({"error": "Failed to navigate to data list"}), 500

        app_state.gds2_state = GDS2State.DATA_LIST.value
        app_state.current_module = module_name
        app_state.data_list_focus_index = 0

        # Step 2: Select first item (Engine Data at index 0) and create report
        logger.info("Step 2: Selecting Engine Data (index 0) and creating report...")
        report_path = controller.navigate_to_data_display(
            module_name, "Engine Data", 0  # Always index 0 for first item
        )

        if not report_path:
            return jsonify({"error": "Failed to create report"}), 500

        # Update state
        app_state.gds2_state = GDS2State.DATA_DISPLAY.value
        app_state.current_data_category = "Engine Data"

        # Parse report
        report_data = controller.parse_report(report_path)

        # Step 3: Click Back to return to Data List
        logger.info("Step 3: Clicking Back to return to Data List...")
        success = controller.click_back_button()
        if not success:
            return jsonify({"error": "Failed to click Back button"}), 500

        time.sleep(2)

        # Step 4: Discover all data categories
        logger.info("Step 4: Discovering all data categories...")
        categories = controller.discover_data_categories("current_vehicle", module_name)

        if not categories:
            return jsonify({"error": "Failed to discover data categories"}), 500

        # Update state
        app_state.gds2_state = GDS2State.DATA_LIST.value
        app_state.data_list_focus_index = 0

        logger.info(f"=== Fetch & Discover Complete: {len(categories)} categories ===")

        return jsonify({
            "success": True,
            "data_categories": categories,
            "report_data": report_data,
            "state": asdict(app_state)
        })

    except Exception as e:
        logger.exception("Fetch & Discover failed")
        return jsonify({"error": str(e)}), 500


@app.route('/api/discover_data_simple', methods=['POST'])
def discover_data_simple():
    """
    Simplified: Discover data categories assuming GDS2 is at Data Display page.
    Just click Back once, then discover.
    """
    data = request.json
    module_name = data.get('module')

    if not module_name:
        return jsonify({"error": "Module name required"}), 400

    try:
        logger.info(f"=== Simple Discovery: {module_name} ===")
        logger.info("Assumption: GDS2 is at Data Display page")

        # Step 1: Click Back to return to Data List
        logger.info("Step 1: Clicking Back button...")
        success = controller.click_back_button()
        if not success:
            return jsonify({"error": "Failed to click Back button"}), 500

        time.sleep(2)

        # Step 2: Discover data categories using pywinauto
        logger.info("Step 2: Discovering data categories...")
        categories = controller.discover_data_categories("current_vehicle", module_name)

        if not categories:
            return jsonify({"error": "Failed to discover data categories"}), 500

        # Update state
        app_state.gds2_state = GDS2State.DATA_LIST.value
        app_state.current_module = module_name
        app_state.data_list_focus_index = 0

        logger.info(f"Discovered {len(categories)} data categories")

        return jsonify({
            "success": True,
            "data_categories": categories,
            "state": asdict(app_state)
        })

    except Exception as e:
        logger.exception("Simple discovery failed")
        return jsonify({"error": str(e)}), 500


@app.route('/api/fetch_data_simple', methods=['POST'])
def fetch_data_simple():
    """
    Simplified: Fetch data assuming GDS2 is at Main Menu.
    Always do full navigation from Main Menu.
    """
    data = request.json
    module_name = data.get('module')
    data_category = data.get('data_category')

    if not module_name or not data_category:
        return jsonify({"error": "Module and data category required"}), 400

    try:
        logger.info(f"=== Simple Fetch: {module_name} -> {data_category} ===")
        logger.info("Assumption: GDS2 is at Main Menu")

        # Step 1: Navigate to Data List (full path from Main Menu)
        logger.info("Step 1: Navigating from Main Menu to Data List...")
        success = controller.navigate_to_data_list(module_name)
        if not success:
            return jsonify({"error": "Failed to navigate to data list"}), 500

        app_state.gds2_state = GDS2State.DATA_LIST.value
        app_state.current_module = module_name
        app_state.data_list_focus_index = 0

        # Step 2: Navigate to Data Display and create report
        logger.info("Step 2: Selecting data category and creating report...")
        report_path = controller.navigate_to_data_display(
            module_name, data_category, app_state.data_list_focus_index
        )

        if not report_path:
            return jsonify({"error": "Failed to create report"}), 500

        # Update state
        app_state.gds2_state = GDS2State.DATA_DISPLAY.value
        app_state.current_data_category = data_category

        # Parse report
        report_data = controller.parse_report(report_path)

        logger.info("=== Fetch Complete ===")

        return jsonify({
            "success": True,
            "report_path": report_path,
            "report_data": report_data,
            "state": asdict(app_state)
        })

    except Exception as e:
        logger.exception("Simple fetch failed")
        return jsonify({"error": str(e)}), 500


@app.route('/api/discover_data', methods=['POST'])
def discover_data():
    """Discover data categories for a module using smart navigation."""
    data = request.json
    module_name = data.get('module')

    if not module_name:
        return jsonify({"error": "Module name required"}), 400

    try:
        logger.info(f"=== Discovering data categories for {module_name} ===")
        logger.info(f"Current GDS2 state: {app_state.gds2_state}")

        # Smart navigation based on current state
        if app_state.gds2_state == GDS2State.MAIN_MENU.value:
            # Full navigation from Main Menu
            logger.info("State: MAIN_MENU -> navigating full path")
            success = controller.navigate_to_data_list(module_name)
            if not success:
                return jsonify({"error": "Failed to navigate to data list"}), 500

        elif app_state.gds2_state == GDS2State.MODULE_LIST.value:
            # Already at Module List -> just select module and go to Data Display
            logger.info("State: MODULE_LIST -> navigating from module list")
            success = controller.navigate_to_data_list_from_module_list(module_name)
            if not success:
                return jsonify({"error": "Failed to navigate from module list"}), 500

        elif app_state.gds2_state == GDS2State.DATA_LIST.value:
            # Already at Data List
            if app_state.current_module == module_name:
                # Same module, already in correct place
                logger.info("State: DATA_LIST (same module) -> already at correct location")
            else:
                # Different module, need to go back and navigate
                logger.info("State: DATA_LIST (different module) -> clicking Back twice")
                controller.click_back_button()  # Back to module submenu
                time.sleep(1)
                controller.click_back_button()  # Back to module list
                time.sleep(1)
                app_state.gds2_state = GDS2State.MODULE_LIST.value
                success = controller.navigate_to_data_list_from_module_list(module_name)
                if not success:
                    return jsonify({"error": "Failed to navigate to different module"}), 500

        elif app_state.gds2_state == GDS2State.DATA_DISPLAY.value:
            # At Data Display -> click Back to get to Data List
            if app_state.current_module == module_name:
                logger.info("State: DATA_DISPLAY (same module) -> clicking Back")
                controller.click_back_button()
                time.sleep(1)
            else:
                # Different module, need multiple backs
                logger.info("State: DATA_DISPLAY (different module) -> clicking Back multiple times")
                controller.click_back_button()  # Back to data list
                time.sleep(1)
                controller.click_back_button()  # Back to module submenu
                time.sleep(1)
                controller.click_back_button()  # Back to module list
                time.sleep(1)
                app_state.gds2_state = GDS2State.MODULE_LIST.value
                success = controller.navigate_to_data_list_from_module_list(module_name)
                if not success:
                    return jsonify({"error": "Failed to navigate to different module"}), 500

        # Discover data categories
        categories = controller.discover_data_categories("current_vehicle", module_name)

        # Update state
        app_state.gds2_state = GDS2State.DATA_LIST.value
        app_state.current_module = module_name
        app_state.data_list_focus_index = 0

        logger.info(f"Discovered {len(categories)} data categories")

        return jsonify({
            "success": True,
            "data_categories": categories,
            "state": asdict(app_state)
        })

    except Exception as e:
        logger.exception("Discovery failed")
        return jsonify({"error": str(e)}), 500


@app.route('/api/fetch_data', methods=['POST'])
def fetch_data():
    """Fetch data for a module/category."""
    data = request.json
    module_name = data.get('module')
    data_category = data.get('data_category')

    if not module_name or not data_category:
        return jsonify({"error": "Module and data category required"}), 400

    try:
        # Handle navigation based on current state
        if app_state.gds2_state == GDS2State.MAIN_MENU.value:
            # Navigate to data list
            success = controller.navigate_to_data_list(module_name)
            if not success:
                return jsonify({"error": "Failed to navigate to data list"}), 500

            app_state.gds2_state = GDS2State.DATA_LIST.value
            app_state.current_module = module_name
            app_state.data_list_focus_index = 0

        elif app_state.gds2_state == GDS2State.DATA_DISPLAY.value:
            # Click back to return to data list
            success = controller.click_back_button()
            if not success:
                return jsonify({"error": "Failed to click back"}), 500

            # Update focus index
            prev_index = controller.mapping.get_data_category_index(
                "current_vehicle", app_state.current_module, app_state.current_data_category
            )
            if prev_index is not None:
                app_state.data_list_focus_index = prev_index

            app_state.gds2_state = GDS2State.DATA_LIST.value

        # Navigate to data display
        report_path = controller.navigate_to_data_display(
            module_name, data_category, app_state.data_list_focus_index
        )

        if not report_path:
            return jsonify({"error": "Failed to create report"}), 500

        # Update state
        app_state.gds2_state = GDS2State.DATA_DISPLAY.value
        app_state.current_data_category = data_category

        # Parse report
        report_data = controller.parse_report(report_path)

        return jsonify({
            "success": True,
            "report_path": report_path,
            "report_data": report_data,
            "state": asdict(app_state)
        })

    except Exception as e:
        logger.exception("Fetch data failed")
        return jsonify({"error": str(e)}), 500


@app.route('/api/back', methods=['POST'])
def back():
    """Click back button."""
    try:
        success = controller.click_back_button()

        if success:
            if app_state.gds2_state == GDS2State.DATA_DISPLAY.value:
                prev_index = controller.mapping.get_data_category_index(
                    "current_vehicle", app_state.current_module, app_state.current_data_category
                )
                if prev_index is not None:
                    app_state.data_list_focus_index = prev_index

                app_state.gds2_state = GDS2State.DATA_LIST.value

            return jsonify({"success": True, "state": asdict(app_state)})
        else:
            return jsonify({"error": "Failed to click back"}), 500

    except Exception as e:
        logger.exception("Back button failed")
        return jsonify({"error": str(e)}), 500


@app.route('/api/state')
def get_state():
    """Get current application state."""
    return jsonify(asdict(app_state))


@app.route('/api/reset', methods=['POST'])
def reset_state():
    """Reset state to Main Menu (when user manually navigated GDS2 back to main menu)."""
    try:
        app_state.gds2_state = GDS2State.MAIN_MENU.value
        app_state.current_module = None
        app_state.current_data_category = None
        app_state.data_list_focus_index = 0

        logger.info("State reset to MAIN_MENU")
        return jsonify({"success": True, "state": asdict(app_state)})

    except Exception as e:
        logger.exception("Reset failed")
        return jsonify({"error": str(e)}), 500


# =============================================================================
# Real-time Data Streaming (SSE)
# =============================================================================

# Global streaming state
streaming_collector = None
streaming_clients: List[queue.Queue] = []
streaming_lock = threading.Lock()


def broadcast_to_clients(event_type: str, data: dict):
    """Broadcast data to all connected SSE clients."""
    message = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    with streaming_lock:
        dead_clients = []
        for client_queue in streaming_clients:
            try:
                client_queue.put_nowait(message)
            except queue.Full:
                dead_clients.append(client_queue)
        # Remove dead clients
        for dead in dead_clients:
            streaming_clients.remove(dead)


def on_realtime_data_change(changes):
    """Callback when parameter values change."""
    change_data = [c.to_dict() for c in changes]
    broadcast_to_clients("changes", {
        "type": "changes",
        "count": len(changes),
        "changes": change_data,
        "timestamp": time.time()
    })
    logger.info(f"Broadcast {len(changes)} parameter changes")


def on_realtime_full_data(params):
    """Callback with all parameters on each collection."""
    param_data = [p.to_dict() for p in params]
    broadcast_to_clients("data", {
        "type": "full_data",
        "count": len(params),
        "parameters": param_data,
        "timestamp": time.time()
    })
    logger.debug(f"Broadcast {len(params)} parameters")


def on_realtime_error(error):
    """Callback when an error occurs."""
    broadcast_to_clients("error", {
        "type": "error",
        "message": error,
        "timestamp": time.time()
    })
    logger.error(f"Streaming error: {error}")


@app.route('/api/stream/start', methods=['POST'])
def start_streaming():
    """Start real-time data streaming."""
    global streaming_collector

    data = request.json or {}
    interval = data.get('interval', 3.0)
    data_category = data.get('data_category')  # Get data category from frontend

    try:
        if streaming_collector and streaming_collector.is_running:
            return jsonify({"error": "Streaming already running"}), 400

        # Check if data category is provided
        if not data_category:
            return jsonify({
                "error": "Please select a data category to monitor"
            }), 400

        # Check if we have module info from Step 2
        if not app_state.current_module:
            return jsonify({
                "error": "Please complete Step 2 first: fetch categories"
            }), 400

        # Update app state with selected data category
        app_state.current_data_category = data_category

        # Check current GDS2 state
        if app_state.gds2_state != GDS2State.DATA_LIST.value:
            return jsonify({
                "error": f"GDS2 must be at Data List page. Current state: {app_state.gds2_state}"
            }), 400

        logger.info(f"=== Starting Monitoring for {app_state.current_data_category} ===")

        # Navigate from Data List to Data Display
        # Get target index for the data category
        target_index = controller.mapping.get_data_category_index(
            "current_vehicle", app_state.current_module, app_state.current_data_category
        )
        if target_index is None:
            return jsonify({
                "error": f"Data category '{app_state.current_data_category}' not found"
            }), 400

        # Navigate using keyboard (from current focus position)
        import pyautogui

        def focus_gds2():
            try:
                from pywinauto import Application
                app_conn = Application(backend="uia").connect(title_re=".*GDS 2.*")
                window = app_conn.top_window()
                window.set_focus()
                time.sleep(0.3)
            except Exception as e:
                logger.warning(f"Could not focus GDS2: {e}")

        focus_gds2()

        # Calculate relative steps from current focus position
        relative_steps = target_index - app_state.data_list_focus_index
        logger.info(f"Navigating to {app_state.current_data_category} (target={target_index}, current={app_state.data_list_focus_index}, steps={relative_steps})")

        if relative_steps > 0:
            for _ in range(relative_steps):
                pyautogui.press('down')
                time.sleep(0.1)
        elif relative_steps < 0:
            for _ in range(abs(relative_steps)):
                pyautogui.press('up')
                time.sleep(0.1)

        time.sleep(0.5)
        pyautogui.press('enter')
        time.sleep(3)  # Wait for Data Display page to load

        # Update state
        app_state.gds2_state = GDS2State.DATA_DISPLAY.value
        app_state.data_list_focus_index = target_index

        logger.info("Now at Data Display page, starting collector...")

        # Start the collector
        from src.streaming import RealtimeDataCollector

        streaming_collector = RealtimeDataCollector(
            on_data_change=on_realtime_data_change,
            on_full_data=on_realtime_full_data,
            on_error=on_realtime_error,
            interval_seconds=interval
        )
        streaming_collector.start()

        logger.info(f"Started real-time streaming with interval {interval}s")
        return jsonify({
            "success": True,
            "message": f"Monitoring started for {app_state.current_data_category}",
            "interval": interval,
            "data_category": app_state.current_data_category,
            "state": asdict(app_state)
        })

    except Exception as e:
        logger.exception("Failed to start streaming")
        return jsonify({"error": str(e)}), 500


@app.route('/api/stream/stop', methods=['POST'])
def stop_streaming():
    """Stop real-time data streaming and navigate back to Data List."""
    global streaming_collector

    try:
        if streaming_collector:
            streaming_collector.stop()
            streaming_collector = None
            logger.info("Stopped real-time streaming")

            # Click Back button to return to Data List
            if app_state.gds2_state == GDS2State.DATA_DISPLAY.value:
                logger.info("Clicking Back to return to Data List...")
                success = controller.click_back_button()
                if success:
                    time.sleep(1.5)
                    app_state.gds2_state = GDS2State.DATA_LIST.value
                    logger.info("Returned to Data List")
                else:
                    logger.warning("Failed to click Back button")

            return jsonify({
                "success": True,
                "message": "Streaming stopped and returned to Data List",
                "state": asdict(app_state)
            })
        else:
            return jsonify({"message": "Streaming was not running"})

    except Exception as e:
        logger.exception("Failed to stop streaming")
        return jsonify({"error": str(e)}), 500


@app.route('/api/stream/status')
def streaming_status():
    """Get streaming status."""
    global streaming_collector

    is_running = streaming_collector is not None and streaming_collector.is_running
    collection_count = streaming_collector.collection_count if streaming_collector else 0
    client_count = len(streaming_clients)

    return jsonify({
        "running": is_running,
        "collection_count": collection_count,
        "connected_clients": client_count
    })


@app.route('/api/stream/events')
def stream_events():
    """SSE endpoint for real-time data streaming."""
    def generate():
        # Create a queue for this client
        client_queue = queue.Queue(maxsize=100)

        with streaming_lock:
            streaming_clients.append(client_queue)

        logger.info(f"SSE client connected. Total clients: {len(streaming_clients)}")

        try:
            # Send initial connection message
            yield f"event: connected\ndata: {json.dumps({'message': 'Connected to real-time stream'})}\n\n"

            while True:
                try:
                    # Wait for data with timeout (allows for connection check)
                    message = client_queue.get(timeout=30)
                    yield message
                except queue.Empty:
                    # Send keepalive
                    yield f": keepalive\n\n"

        except GeneratorExit:
            pass
        finally:
            with streaming_lock:
                if client_queue in streaming_clients:
                    streaming_clients.remove(client_queue)
            logger.info(f"SSE client disconnected. Total clients: {len(streaming_clients)}")

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no'
        }
    )


@app.route('/api/stream/latest')
def get_latest_data():
    """Get the latest collected data (non-streaming)."""
    global streaming_collector

    if not streaming_collector:
        return jsonify({"error": "Streaming not started"}), 400

    last_values = streaming_collector.last_values
    data = [v.to_dict() for v in last_values.values()]

    return jsonify({
        "count": len(data),
        "parameters": data,
        "collection_count": streaming_collector.collection_count
    })


if __name__ == '__main__':
    import sys
    port = 8080
    if '--port' in sys.argv:
        idx = sys.argv.index('--port')
        if idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])

    print(f"\n{'='*60}")
    print(f"  GDS2 Automation Web UI")
    print(f"{'='*60}")
    print(f"\n  Open in browser: http://localhost:{port}")
    print(f"\n  If using proxy, add 'localhost' to bypass list")
    print(f"{'='*60}\n")

    app.run(debug=True, host='127.0.0.1', port=port, use_reloader=False)
