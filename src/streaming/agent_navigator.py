"""
Agent-based Navigator for GDS2.

Sends navigation commands to the Java Agent instead of using PyAutoGUI/OpenCV.

Benefits over PyAutoGUI:
- Works with invisible list items (no scrolling needed)
- Faster button clicks (~1ms vs ~100ms)
- No screen coordinate dependency
- More reliable list selection

Usage:
    nav = AgentNavigator()

    # Check connection
    if nav.check_agent():
        # Navigate
        nav.click_button("Diagnostics")
        nav.wait(2000)
        nav.select_list_item(0, 5)  # Select 6th item in first list
"""

import json
import os
import time
import uuid
import logging
import threading
from pathlib import Path
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)


class AgentNavigator:
    """
    Navigator that sends commands to the GDS2 Java Agent.

    Commands are sent via JSON file and results are read from result file.
    """

    _command_roundtrip_lock = threading.Lock()
    _command_replace_retries = 5
    _command_replace_retry_delay_s = 0.02

    def __init__(self, data_dir: Optional[Path] = None, timeout_sec: float = 10.0):
        if data_dir is None:
            self._data_dir = Path.home() / 'gds2-data'
        else:
            self._data_dir = Path(data_dir)

        self._command_file = self._data_dir / 'command.json'
        self._result_file = self._data_dir / 'result.json'
        self._timeout_sec = timeout_sec

        logger.info(f"AgentNavigator initialized: {self._data_dir}")

    def check_agent(self) -> bool:
        """Check if Java Agent is running and responsive."""
        try:
            result = self._send_command("get_window", {})
            return result.get('success', False)
        except Exception as e:
            logger.debug(f"Agent check failed: {e}")
            return False

    def click_button(self, text: str) -> Dict[str, Any]:
        """
        Click a button by its text label.

        Args:
            text: Exact text on the button (e.g., "Diagnostics", "Enter")

        Returns:
            Result dict with success, message, and data
        """
        logger.info(f"Clicking button: {text}")
        return self._send_command("click_button", {"text": text})

    def select_list_item(
        self,
        list_index: int,
        item_index: int,
        double_click: bool = True
    ) -> Dict[str, Any]:
        """
        Select a list item by index.

        Works even if the item is not visible - JavaFX will auto-scroll.

        Args:
            list_index: Index of the ListView (0 = first list on screen)
            item_index: Index of the item to select
            double_click: If True, also fires Enter key to activate

        Returns:
            Result dict
        """
        logger.info(f"Selecting list item: list={list_index}, item={item_index}")
        return self._send_command("select_list", {
            "list_index": list_index,
            "item_index": item_index,
            "double_click": double_click
        })

    def select_list_item_by_text(
        self,
        list_index: int,
        item_text: str,
        double_click: bool = True
    ) -> Dict[str, Any]:
        """
        Select a list item by text content (contains match).

        Args:
            list_index: Index of the ListView
            item_text: Text to search for (partial match)
            double_click: If True, also fires Enter key to activate

        Returns:
            Result dict
        """
        logger.info(f"Selecting list item by text: list={list_index}, text='{item_text}'")
        return self._send_command("select_list", {
            "list_index": list_index,
            "item_text": item_text,
            "double_click": double_click
        })

    def get_list_items(self, list_index: int = 0) -> List[str]:
        """
        Get all items from a ListView.

        Args:
            list_index: Index of the ListView

        Returns:
            List of item texts
        """
        result = self._send_command("get_list", {"list_index": list_index})
        if result.get('success'):
            return result.get('data', {}).get('items', [])
        return []

    def get_buttons(self) -> List[Dict[str, Any]]:
        """
        Get all visible, enabled buttons on screen.

        Returns:
            List of button info dicts with 'text', 'id', 'enabled'
        """
        result = self._send_command("get_buttons", {})
        if result.get('success'):
            return result.get('data', {}).get('buttons', [])
        return []

    def get_window_info(self) -> List[Dict[str, Any]]:
        """
        Get information about all visible windows.

        Returns:
            List of window info dicts with 'title', 'focused', 'width', 'height'
        """
        result = self._send_command("get_window", {})
        if result.get('success'):
            return result.get('data', {}).get('windows', [])
        return []

    def get_page_id(self) -> Dict[str, Any]:
        """
        Identify the current GDS2 page using Java-side scene graph analysis.

        This is more reliable than Python-side button heuristics because
        the Java Agent has direct access to the full JavaFX scene graph,
        including window titles, disabled buttons, and list contents,
        all in a single atomic snapshot.

        Returns:
            Dict with:
              - page_id: str (e.g., 'main_menu', 'module_list')
              - confidence: str ('high', 'medium', 'low', 'none')
              - evidence: str (why this page was identified)
              - window_title: str
              - buttons: List[str] (enabled button texts)
              - list_item_count: int
              - has_modal: bool
            Returns empty dict if command fails.
        """
        result = self._send_command("get_page_id", {})
        if result.get('success'):
            return result.get('data', {})
        logger.warning(f"get_page_id failed: {result.get('message')}")
        return {}

    def get_navigation_path(self) -> List[str]:
        """
        Get the current clickable Navigation Path items from the JavaFX breadcrumb table.

        Returns:
            Ordered breadcrumb items, for example
            ["Module Diagnostics", "Engine Control Module", "Control Functions"].
            Returns an empty list if the agent command fails or the table is not present.
        """
        result = self._send_command("get_navigation_path", {})
        if result.get('success'):
            return result.get('data', {}).get('items', [])
        logger.warning(f"get_navigation_path failed: {result.get('message')}")
        return []

    def click_navigation_path_item(self, text: str) -> Dict[str, Any]:
        """
        Click one item in the Navigation Path breadcrumb table by text.

        Args:
            text: Exact or contains-match breadcrumb label, such as
                  "Module Diagnostics" or "Engine Control Module".

        Returns:
            Result dict with success, message, and data.
        """
        logger.info(f"Clicking Navigation Path item: {text}")
        return self._send_command("click_navigation_path_item", {"text": text})

    def debug_navigation_path(self) -> Dict[str, Any]:
        """
        Ask the Java Agent for detailed Navigation Path diagnostics.

        Returns:
            Result dict with candidate tables, chosen table, extracted items,
            and label/bounds information when supported by the agent.
        """
        return self._send_command("debug_navigation_path", {})

    def get_clear_dtcs_selection_state(self) -> Dict[str, Any]:
        """
        Get Clear DTCs module selection panel state from the Java Agent.

        Returns:
            Result dict with availableModules, selectedModules, and button states.
        """
        return self._send_command("get_clear_dtcs_selection_state", {})

    def wait(self, ms: int) -> Dict[str, Any]:
        """
        Wait for specified milliseconds (executed by Java Agent).

        Args:
            ms: Milliseconds to wait

        Returns:
            Result dict
        """
        return self._send_command("wait", {"ms": ms})

    def inspect_controls(self, max_depth: int = 0) -> Dict[str, Any]:
        """
        Inspect all controls on screen - for debugging UI structure.

        Args:
            max_depth: Maximum depth to traverse (0 = unlimited)

        Returns:
            Result dict with controls, typeCounts, totalCount
        """
        return self._send_command("inspect", {"max_depth": max_depth})

    # ========== Swing/AWT Methods (for non-JavaFX dialogs) ==========

    def inspect_swing(self) -> Dict[str, Any]:
        """
        Inspect Swing/AWT controls (for dialogs like Device Explorer).

        Returns:
            Result dict with Swing controls
        """
        return self._send_command("inspect_swing", {})

    def click_swing_button(self, text: str) -> Dict[str, Any]:
        """
        Click a Swing button by text.

        Args:
            text: Button text (e.g., "Continue", "Cancel")

        Returns:
            Result dict
        """
        logger.info(f"Clicking Swing button: {text}")
        return self._send_command("click_swing_button", {"text": text})

    def select_swing_table_row(self, table_index: int, row_index: int) -> Dict[str, Any]:
        """
        Select a row in a Swing JTable.

        Args:
            table_index: Index of the JTable (0 = first found)
            row_index: Row to select

        Returns:
            Result dict
        """
        logger.info(f"Selecting Swing table row: table={table_index}, row={row_index}")
        return self._send_command("select_swing_table_row", {
            "table_index": table_index,
            "row_index": row_index
        })

    def get_swing_table(self, table_index: int = 0) -> Dict[str, Any]:
        """
        Get data from a Swing JTable.

        Args:
            table_index: Index of the JTable

        Returns:
            Result dict with columns, rows, rowCount
        """
        return self._send_command("get_swing_table", {"table_index": table_index})

    def _send_command(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Send a command to the Java Agent and wait for result.

        Args:
            action: Command action name
            params: Command parameters

        Returns:
            Result dict from Agent

        Raises:
            TimeoutError: If no result within timeout
            RuntimeError: If command processing fails
        """
        with self._command_roundtrip_lock:
            cmd_id = str(uuid.uuid4())[:8]
            command = {
                "id": cmd_id,
                "action": action,
                "params": params
            }

            logger.debug(f"Sending command: {command}")

            self._data_dir.mkdir(parents=True, exist_ok=True)

            tmp_file = self._command_file.with_suffix('.tmp')
            with open(tmp_file, 'w', encoding='utf-8') as f:
                json.dump(command, f)
            for attempt in range(self._command_replace_retries):
                try:
                    os.replace(str(tmp_file), str(self._command_file))
                    break
                except PermissionError:
                    if attempt + 1 >= self._command_replace_retries:
                        raise
                    time.sleep(self._command_replace_retry_delay_s)

            start_time = time.time()
            while time.time() - start_time < self._timeout_sec:
                if self._result_file.exists():
                    try:
                        with open(self._result_file, 'r', encoding='utf-8') as f:
                            result = json.load(f)

                        if result.get('id') == cmd_id:
                            logger.debug(f"Received result: {result}")
                            return result

                    except (json.JSONDecodeError, PermissionError, OSError):
                        pass

                time.sleep(0.05)

        raise TimeoutError(f"No response from Agent within {self._timeout_sec}s")


# ========== Convenience Functions ==========

def test_agent_navigation():
    """Test basic Agent navigation capabilities."""
    print("=" * 60)
    print("Agent Navigator Test")
    print("=" * 60)

    nav = AgentNavigator()

    # Check agent
    print("\n[1] Checking Agent connection...")
    if not nav.check_agent():
        print("  [ERROR] Agent not responding!")
        print("  Make sure GDS2 is started with the Agent:")
        print("    launch-gds2-with-agent.bat")
        return False

    print("  [OK] Agent is responding")

    # Get window info
    print("\n[2] Getting window info...")
    windows = nav.get_window_info()
    for w in windows:
        print(f"  Window: {w.get('title')} (focused={w.get('focused')})")

    # Get buttons
    print("\n[3] Getting visible buttons...")
    buttons = nav.get_buttons()
    print(f"  Found {len(buttons)} buttons:")
    for btn in buttons[:10]:  # Show first 10
        print(f"    - {btn.get('text')}")
    if len(buttons) > 10:
        print(f"    ... and {len(buttons) - 10} more")

    # Get list items (if any list is visible)
    print("\n[4] Getting list items (list 0)...")
    items = nav.get_list_items(0)
    if items:
        print(f"  Found {len(items)} items:")
        for i, item in enumerate(items[:5]):
            print(f"    [{i}] {item}")
        if len(items) > 5:
            print(f"    ... and {len(items) - 5} more")
    else:
        print("  No list found or list is empty")

    print("\n" + "=" * 60)
    print("Agent Navigator Test Complete")
    print("=" * 60)

    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    test_agent_navigation()
