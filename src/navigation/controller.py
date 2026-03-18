"""
Navigation Controller for GDS2.

Manages GDS2 application state and provides state-aware navigation.
Infers current page from visible buttons and list items via Java Agent.

No Java Agent changes required - page detection uses existing Agent APIs.
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)


class GDS2Page(Enum):
    """All known pages in GDS2 application."""
    UNKNOWN = "unknown"
    MAIN_MENU = "main_menu"
    DEVICE_EXPLORER = "device_explorer"     # Win32 dialog (not JavaFX)
    VEHICLE_SELECTION = "vehicle_selection"
    DIAGNOSTICS_MENU = "diagnostics_menu"
    MODULE_LIST = "module_list"
    MODULE_SUBMENU = "module_submenu"
    DATA_LIST = "data_list"
    SUB_DATA_LIST = "sub_data_list"         # Sub-categories for some data
    DATA_DISPLAY = "data_display"
    LOADING = "loading"
    J2534_DISCONNECT = "j2534_disconnect"


@dataclass
class NavigationResult:
    """Result of a navigation action."""
    success: bool
    page: GDS2Page
    choices: Optional[List[str]] = None     # Available items to select
    selected: Optional[str] = None          # What was just selected
    error: Optional[str] = None             # Error message if failed
    context: Dict[str, Any] = field(default_factory=dict)  # module, data_category, etc.

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "success": self.success,
            "page": self.page.value,
            "choices": self.choices,
            "selected": self.selected,
            "error": self.error,
            "context": self.context,
        }


class NavigationController:
    """
    State-aware navigation controller for GDS2.

    Manages navigation history and provides intelligent page detection
    using the Java Agent's button and list enumeration capabilities.
    """

    _MODULE_SUBMENU_REQUIRED_MARKERS = (
        "diagnostic trouble codes",
        "dtc",
        "module information",
        "special functions",
        "special function",
        "故障码",
        "模块信息",
        "特殊功能",
    )

    _MODULE_SUBMENU_DISPLAY_MARKERS = (
        "data display",
        "数据显示",
        "数据展示",
    )

    def __init__(self, nav=None):
        """
        Initialize navigation controller.

        Args:
            nav: AgentNavigator instance. If None, will create one.
        """
        self._nav = nav
        self._lock = threading.RLock()
        # IPC result cache — populated by page detection, consumed by get_snapshot()
        self._cached_button_texts: list[str] = []
        self._cached_items: list[str] = []
        self._cache_time: float = 0.0
        self._history: List[GDS2Page] = []
        self._current_page = GDS2Page.UNKNOWN
        self._context: Dict[str, Any] = {
            "module": None,
            "data_category": None,
            "sub_category": None,
            "device": None,
        }

    @property
    def nav(self):
        """Lazy-load AgentNavigator."""
        if self._nav is None:
            from ..streaming import AgentNavigator
            self._nav = AgentNavigator(timeout_sec=15.0)
        return self._nav

    @property
    def current_page(self) -> GDS2Page:
        """Get current page (cached)."""
        return self._current_page

    @property
    def current_module(self) -> Optional[str]:
        """Get currently selected module."""
        return self._context.get("module")

    @property
    def current_data_category(self) -> Optional[str]:
        """Get currently selected data category."""
        return self._context.get("data_category")

    @property
    def current_sub_category(self) -> Optional[str]:
        """Get currently selected sub-category."""
        return self._context.get("sub_category")

    @property
    def history(self) -> List[GDS2Page]:
        """Get navigation history (breadcrumb)."""
        return self._history.copy()

    def check_agent(self) -> bool:
        """Check if Java Agent is available."""
        return self.nav.check_agent()

    @staticmethod
    def _normalize_text(text: str) -> str:
        """Normalize UI labels for robust marker matching."""
        return " ".join(text.lower().strip().split())

    def _is_module_submenu_items(self, items: List[str]) -> bool:
        """Detect module-submenu by function markers, not by Data Display alone."""
        if not items:
            return False

        normalized_items = [self._normalize_text(item) for item in items if item]
        if not normalized_items:
            return False

        # Module list rows like [K20] should never be treated as submenu.
        if any("[" in item and "]" in item for item in items if item):
            return False

        has_display_marker = any(
            any(marker in label for marker in self._MODULE_SUBMENU_DISPLAY_MARKERS)
            for label in normalized_items
        )
        has_required_marker = any(
            any(marker in label for marker in self._MODULE_SUBMENU_REQUIRED_MARKERS)
            for label in normalized_items
        )

        # IMPORTANT: Data Display alone is not enough to classify as MODULE_SUBMENU.
        return has_display_marker and has_required_marker

    # =========================================================================
    # Page Detection
    # =========================================================================

    def detect_current_page(self, retries: int = 1, retry_delay: float = 1.5) -> GDS2Page:
        """
        Detect current page using Java Agent's get_page_id command.

        Primary method: get_page_id (scene graph analysis on Java side)
        Fallback: button/list heuristics (if Agent doesn't support get_page_id)

        Args:
            retries: Number of additional attempts if UNKNOWN (default: 1)
            retry_delay: Delay between retries in seconds

        Returns:
            Detected GDS2Page
        """
        with self._lock:
            for attempt in range(1 + retries):
                try:
                    # 0. Native Device Explorer (Win32 dialog) takes precedence.
                    # Java Agent cannot see this dialog, so without this check
                    # page detection may incorrectly return UNKNOWN.
                    try:
                        from ..native import DeviceExplorerController

                        if DeviceExplorerController().is_visible():
                            self._current_page = GDS2Page.DEVICE_EXPLORER
                            return self._current_page
                    except Exception as native_err:
                        logger.debug(f"Native dialog visibility check failed: {native_err}")

                    # 1. Try Agent-based detection (primary, most reliable)
                    page = self._detect_via_agent()
                    if page != GDS2Page.UNKNOWN:
                        self._current_page = page
                        return page

                    # 2. Fallback to heuristic (for older Agent JARs without get_page_id)
                    page = self._detect_via_heuristic()
                    if page != GDS2Page.UNKNOWN:
                        self._current_page = page
                        return page

                    # Still UNKNOWN - retry if attempts remain
                    if attempt < retries:
                        logger.debug("Page detection returned UNKNOWN, retrying in %.1fs", retry_delay)
                        time.sleep(retry_delay)
                        continue

                    self._current_page = GDS2Page.UNKNOWN
                    return GDS2Page.UNKNOWN

                except Exception as e:
                    logger.error(f"Page detection failed: {e}")
                    if attempt < retries:
                        time.sleep(retry_delay)
                        continue
                    self._current_page = GDS2Page.UNKNOWN
                    return GDS2Page.UNKNOWN

            self._current_page = GDS2Page.UNKNOWN
            return GDS2Page.UNKNOWN

    def _detect_via_agent(self) -> GDS2Page:
        """
        Use Java Agent's get_page_id command for reliable page detection.

        The Agent inspects the full JavaFX scene graph atomically:
        window title, all buttons (including disabled), list contents.
        """
        try:
            page_info = self.nav.get_page_id()
            if not page_info:
                logger.debug("get_page_id returned empty (Agent may not support it)")
                return GDS2Page.UNKNOWN

            page_id = page_info.get('page_id', 'unknown')
            confidence = page_info.get('confidence', 'none')

            logger.debug("Agent page detection: %s (confidence=%s)", page_id, confidence)

            # Pre-cache buttons and items for get_snapshot() reuse
            try:
                _buttons = self.nav.get_buttons()
                self._cached_button_texts = [b.get('text', '') for b in _buttons if b.get('text')]
                self._cached_items = self.nav.get_list_items(0) or []
                self._cache_time = time.time()
            except Exception:
                pass  # cache miss is OK, get_snapshot will fetch fresh

            # Map page_id string to GDS2Page enum
            page_map = {
                'main_menu': GDS2Page.MAIN_MENU,
                'device_explorer': GDS2Page.DEVICE_EXPLORER,
                'vehicle_selection': GDS2Page.VEHICLE_SELECTION,
                'diagnostics_menu': GDS2Page.DIAGNOSTICS_MENU,
                'module_list': GDS2Page.MODULE_LIST,
                'module_submenu': GDS2Page.MODULE_SUBMENU,
                'data_list': GDS2Page.DATA_LIST,
                'sub_data_list': GDS2Page.SUB_DATA_LIST,
                'data_display': GDS2Page.DATA_DISPLAY,
                'loading': GDS2Page.LOADING,
                'j2534_disconnect': GDS2Page.J2534_DISCONNECT,
            }

            return page_map.get(page_id, GDS2Page.UNKNOWN)

        except TimeoutError:
            logger.debug("get_page_id timed out, falling back to heuristic")
            return GDS2Page.UNKNOWN
        except Exception as e:
            logger.debug(f"get_page_id failed: {e}, falling back to heuristic")
            return GDS2Page.UNKNOWN

    def _detect_via_heuristic(self) -> GDS2Page:
        """
        Fallback page detection using button/list heuristics.

        This is the original detection logic with marker-based improvements,
        kept as fallback for when the Java Agent doesn't support get_page_id
        (older JAR versions).
        """
        try:
            buttons = self.nav.get_buttons()
            button_texts = {b.get('text', '') for b in buttons if b.get('text')}

            items = self.nav.get_list_items(0)

            # Cache IPC results for get_snapshot() reuse
            self._cached_button_texts = list(button_texts)
            self._cached_items = list(items) if items else []
            self._cache_time = time.time()

            logger.debug(f"Heuristic detection - Buttons: {button_texts}")
            logger.debug(f"Heuristic detection - List items: {len(items)}, first 5: {items[:5] if items else []}")

            # Detection rules - ORDER MATTERS!
            # More specific rules (with list content checks) come FIRST
            # Generic button-only rules come LAST

            # 1. DATA_DISPLAY: Has "Create Report" button (most specific)
            if "Create Report" in button_texts:
                return GDS2Page.DATA_DISPLAY

            # 1a. Transitional loading page: no list content and either no actionable
            # buttons or stale deep-page buttons while GDS2 is repainting.
            if not items:
                if not button_texts:
                    return GDS2Page.LOADING
                if "Enter" in button_texts and ("Back" in button_texts or "Vehicle Menu" in button_texts):
                    return GDS2Page.LOADING

            # 1b. Lost communication page variants: always has Back, may or may not have OK.
            if (
                "Back" in button_texts
                and not items
                and "Create Report" not in button_texts
                and "Diagnostics" not in button_texts
                and "Update" not in button_texts
                and "Enter" not in button_texts
            ):
                return GDS2Page.J2534_DISCONNECT

            # 2. MAIN_MENU: Has "Diagnostics" and "Update" buttons
            if "Diagnostics" in button_texts and "Update" in button_texts:
                return GDS2Page.MAIN_MENU

            # 3. MODULE_SUBMENU: Must look like function menu (not Data Display alone)
            if items and "Back" in button_texts and self._is_module_submenu_items(items):
                return GDS2Page.MODULE_SUBMENU

            # 4. DIAGNOSTICS_MENU: List contains "Module Diagnostics"
            if items and any("Module Diagnostics" in item for item in items):
                return GDS2Page.DIAGNOSTICS_MENU

            # 5. MODULE_LIST: List items look like modules (contain brackets like [K20])
            if items and any("[" in item and "]" in item for item in items):
                return GDS2Page.MODULE_LIST

            # 6. DATA_LIST: Has list items and Back button (but not specific markers above)
            if items and "Back" in button_texts:
                return GDS2Page.DATA_LIST

            # 7. VEHICLE_SELECTION: Has "Enter" button but NO list items
            #    Exclude deep pages where list hasn't loaded yet.
            #    "Back" and "Vehicle Menu" only appear on deep pages,
            #    never on vehicle_selection.
            if "Enter" in button_texts and not items:
                if "Back" not in button_texts and "Vehicle Menu" not in button_texts:
                    return GDS2Page.VEHICLE_SELECTION
                else:
                    logger.debug(
                        "Rule 7 blocked: Enter+empty list with deep-page buttons present"
                    )

            # 8. Also check for "Disconnect" or "Select Device" buttons for VEHICLE_SELECTION
            #    Guard against false positives on deep pages where toolbar buttons may persist.
            if (
                ("Disconnect" in button_texts or "Select Device" in button_texts)
                and not items
                and "Back" not in button_texts
                and "Vehicle Menu" not in button_texts
                and "Create Report" not in button_texts
            ):
                return GDS2Page.VEHICLE_SELECTION

            return GDS2Page.UNKNOWN

        except Exception as e:
            logger.error(f"Heuristic page detection failed: {e}")
            return GDS2Page.UNKNOWN
    def refresh_state(self) -> GDS2Page:
        """Refresh and return current page state."""
        with self._lock:
            return self.detect_current_page()

    def get_snapshot(self) -> Dict[str, Any]:
        """Get atomic page snapshot: page, buttons, items, context.

        Uses cached IPC results from the most recent detect_current_page()
        call if available (< 2s old), avoiding redundant round-trips.
        Returns fresh data if cache is stale.
        """
        with self._lock:
            page = self.detect_current_page()
            cache_age = time.time() - self._cache_time
            if cache_age < 2.0 and self._cache_time > 0:
                buttons = self._cached_button_texts
                items = self._cached_items
            else:
                buttons = self.get_visible_buttons()
                items = self.get_list_items(0)
            return {
                "page": page.value,
                "buttons": buttons,
                "lists": items,
                "context": self.get_context(),
            }

    # =========================================================================
    # Navigation Actions
    # =========================================================================

    def go_back(self) -> NavigationResult:
        """
        Click Back button and update state.

        Returns:
            NavigationResult with new page info
        """
        with self._lock:
            try:
                result = self.nav.click_button("Back")
                if not result.get('success'):
                    return NavigationResult(
                        success=False,
                        page=self._current_page,
                        error=f"Failed to click Back: {result.get('message')}",
                        context=self._context.copy(),
                    )

                # Wait for page to actually change instead of blind sleep
                old_page = self._current_page
                try:
                    new_page = self.wait_for_page_transition(old_page, timeout=15)
                except TimeoutError:
                    logger.warning("Page did not transition after Back click, detecting...")
                    new_page = self.detect_current_page()

                # Pop from history if possible
                if self._history:
                    self._history.pop()

                # Update context based on navigation
                if new_page == GDS2Page.DATA_LIST:
                    self._context["sub_category"] = None
                elif new_page == GDS2Page.MODULE_SUBMENU:
                    self._context["data_category"] = None
                    self._context["sub_category"] = None
                elif new_page == GDS2Page.MODULE_LIST:
                    self._context["module"] = None
                    self._context["data_category"] = None
                    self._context["sub_category"] = None

                return NavigationResult(
                    success=True,
                    page=new_page,
                    context=self._context.copy(),
                )

            except Exception as e:
                logger.exception(f"go_back failed: {e}")
                return NavigationResult(
                    success=False,
                    page=self._current_page,
                    error=str(e),
                    context=self._context.copy(),
                )

    def go_home(self) -> NavigationResult:
        """
        Click Home button to return to Main Menu.

        Returns:
            NavigationResult with Main Menu page
        """
        with self._lock:
            try:
                result = self.nav.click_button("Home")
                if not result.get('success'):
                    return NavigationResult(
                        success=False,
                        page=self._current_page,
                        error=f"Failed to click Home: {result.get('message')}",
                        context=self._context.copy(),
                    )

                # Wait for page transition to Main Menu
                old_page = self._current_page
                try:
                    self.wait_for_page_transition(old_page, timeout=15)
                except TimeoutError:
                    logger.warning("Page did not transition after Home click")

                # Clear history and context
                self._history.clear()
                self._context = {
                    "module": None,
                    "data_category": None,
                    "sub_category": None,
                    "device": self._context.get("device"),
                }

                self._current_page = GDS2Page.MAIN_MENU

                return NavigationResult(
                    success=True,
                    page=GDS2Page.MAIN_MENU,
                    context=self._context.copy(),
                )

            except Exception as e:
                logger.exception(f"go_home failed: {e}")
                return NavigationResult(
                    success=False,
                    page=self._current_page,
                    error=str(e),
                    context=self._context.copy(),
                )

    def go_vehicle_menu(self) -> NavigationResult:
        """
        Click Vehicle Menu to jump directly to Diagnostics Menu.

        Faster than going Home when changing modules.
        """
        with self._lock:
            try:
                result = self.nav.click_button("Vehicle Menu")
                if not result.get('success'):
                    return NavigationResult(
                        success=False,
                        page=self._current_page,
                        error="Vehicle Menu not available",
                        context=self._context.copy(),
                    )

                # Wait for page transition instead of blind sleep
                old_page = self._current_page
                try:
                    new_page = self.wait_for_page_transition(old_page, timeout=15)
                except TimeoutError:
                    logger.warning("Page did not transition after Vehicle Menu click")
                    new_page = self.detect_current_page()
                self._context["module"] = None
                self._context["data_category"] = None
                self._context["sub_category"] = None

                return NavigationResult(
                    success=True,
                    page=new_page,
                    context=self._context.copy(),
                )

            except Exception as e:
                logger.exception(f"go_vehicle_menu failed: {e}")
                return NavigationResult(
                    success=False,
                    page=self._current_page,
                    error=str(e),
                    context=self._context.copy(),
                )

    def navigate_to(self, target: GDS2Page) -> NavigationResult:
        """
        Navigate from current page to target page.

        Clicks Back button as needed to reach the target page.
        Only supports navigating "back" (up the hierarchy).

        Args:
            target: Target page to navigate to

        Returns:
            NavigationResult with target page info or error
        """
        # Page hierarchy (depth from Main Menu)
        with self._lock:
            page_depth = {
                GDS2Page.MAIN_MENU: 0,
                GDS2Page.DIAGNOSTICS_MENU: 1,
                GDS2Page.MODULE_LIST: 2,
                GDS2Page.MODULE_SUBMENU: 3,
                GDS2Page.DATA_LIST: 4,
                GDS2Page.SUB_DATA_LIST: 5,
                GDS2Page.DATA_DISPLAY: 5,  # Same depth as SUB_DATA_LIST
            }

            current = self.detect_current_page()
            current_depth = page_depth.get(current, 0)
            target_depth = page_depth.get(target, 0)

            if target_depth > current_depth:
                return NavigationResult(
                    success=False,
                    page=current,
                    error=f"Cannot navigate forward to {target.value}. Use specific selection methods.",
                    context=self._context.copy(),
                )

            # Navigate back until we reach target
            max_attempts = 10
            for _ in range(max_attempts):
                current = self.detect_current_page()
                if current == target:
                    return NavigationResult(
                        success=True,
                        page=current,
                        context=self._context.copy(),
                    )

                # Special case: Home for Main Menu
                if target == GDS2Page.MAIN_MENU:
                    return self.go_home()

                result = self.go_back()
                if not result.success:
                    return result

            return NavigationResult(
                success=False,
                page=self._current_page,
                error=f"Could not reach {target.value} after {max_attempts} attempts",
                context=self._context.copy(),
            )

    def wait_for_page_transition(
        self,
        from_page: GDS2Page,
        timeout: float = 30.0,
        poll_interval: float = 0.5,
    ) -> GDS2Page:
        """
        Wait until the current page changes from `from_page`.

        Polls detect_current_page until it returns something OTHER than
        from_page and UNKNOWN.  This replaces blind time.sleep() after
        navigation actions.

        Args:
            from_page: The page we are leaving
            timeout: Maximum seconds to wait
            poll_interval: Seconds between polls

        Returns:
            The new GDS2Page detected

        Raises:
            TimeoutError if page doesn't change within timeout
        """
        with self._lock:
            start = time.time()
            last_detected = from_page
            transition_candidate = GDS2Page.UNKNOWN
            transition_candidate_hits = 0
            while time.time() - start < timeout:
                current = self.detect_current_page(retries=0)
                last_detected = current
                if current != from_page and current != GDS2Page.UNKNOWN:
                    # Require two consecutive identical detections before accepting
                    # transition. This prevents transient misdetections during UI load.
                    if current == transition_candidate:
                        transition_candidate_hits += 1
                    else:
                        transition_candidate = current
                        transition_candidate_hits = 1

                    if transition_candidate_hits < 2:
                        time.sleep(poll_interval)
                        continue

                    logger.info(
                        "NAV %s → %s (%.1fs)",
                        from_page.value,
                        current.value,
                        time.time() - start,
                    )
                    self._current_page = current
                    return current
                # If UNKNOWN, try dismissing warning dialog (may be blocking detection)
                if current == GDS2Page.UNKNOWN:
                    self.dismiss_warning_dialog()
                transition_candidate = GDS2Page.UNKNOWN
                transition_candidate_hits = 0
                time.sleep(poll_interval)
            raise TimeoutError(
                f"Page did not transition from {from_page.value} within {timeout}s. "
                f"Last detected: {last_detected.value}"
            )

    def wait_for_page_stable(
        self,
        timeout: float = 30.0,
        stable_duration: float = 1.0,
        poll_interval: float = 0.3,
    ) -> GDS2Page:
        """
        Wait until the detected page stays the same for `stable_duration` seconds.

        Useful after actions where the target page is unknown, or when
        GDS2 might briefly show an intermediate state (loading).

        Args:
            timeout: Maximum seconds to wait
            stable_duration: How long the page must stay the same
            poll_interval: Seconds between polls

        Returns:
            The stable GDS2Page detected

        Raises:
            TimeoutError if no stable page within timeout
        """
        with self._lock:
            start = time.time()
            last_page = GDS2Page.UNKNOWN
            stable_since = start

            while time.time() - start < timeout:
                current = self.detect_current_page(retries=0)
                if current != last_page or current == GDS2Page.UNKNOWN:
                    last_page = current
                    stable_since = time.time()
                elif time.time() - stable_since >= stable_duration:
                    logger.debug(
                        "Page stable at %s after %.1fs",
                        current.value,
                        time.time() - start,
                    )
                    self._current_page = current
                    return current
                time.sleep(poll_interval)

            raise TimeoutError(
                f"Page did not stabilize within {timeout}s. "
                f"Last detected: {last_page.value}"
            )

    # =========================================================================
    # State Queries
    # =========================================================================

    def get_visible_buttons(self) -> List[str]:
        """Get list of visible button texts."""
        buttons = self.nav.get_buttons()
        return [b.get('text', '') for b in buttons if b.get('text')]

    def get_list_items(self, list_index: int = 0) -> List[str]:
        """Get list items from current page."""
        return self.nav.get_list_items(list_index)

    def wait_for_list(self, list_index: int = 0, max_attempts: int = 15,
                      previous_items: Optional[List[str]] = None) -> List[str]:
        """
        Wait for list items to load.

        Args:
            list_index: Index of the ListView to read
            max_attempts: Max polling attempts (1s apart)
            previous_items: If provided, waits until items DIFFER from these
                           (prevents returning stale items from a prior page)

        Returns:
            List of item texts, or [] on timeout
        """
        for attempt in range(max_attempts):
            items = self.nav.get_list_items(list_index)
            if items:
                # If caller told us what was on screen before, reject stale data
                if previous_items is not None and items == previous_items:
                    logger.debug(f"List unchanged (stale), waiting... ({attempt + 1}/{max_attempts})")
                    time.sleep(1)
                    continue
                return items
            logger.debug(f"Waiting for list... ({attempt + 1}/{max_attempts})")
            time.sleep(1)
        return []

    # =========================================================================
    # Selection Methods
    # =========================================================================

    def select_list_item(
        self,
        item_text: str,
        list_index: int = 0,
        double_click: bool = True
    ) -> NavigationResult:
        """
        Select a list item by text (partial match).

        Args:
            item_text: Text to search for
            list_index: Index of the list (0 = first)
            double_click: Whether to double-click

        Returns:
            NavigationResult
        """
        with self._lock:
            items = self.wait_for_list(list_index)
            if not items:
                return NavigationResult(
                    success=False,
                    page=self._current_page,
                    error="No list items found",
                    choices=items,
                    context=self._context.copy(),
                )

            # Find matching item
            target_index = None
            matched_item = None
            for i, item in enumerate(items):
                if item_text in item or item in item_text:
                    target_index = i
                    matched_item = item
                    break

            if target_index is None:
                return NavigationResult(
                    success=False,
                    page=self._current_page,
                    error=f"Item '{item_text}' not found",
                    choices=items,
                    context=self._context.copy(),
                )

            result = self.nav.select_list_item(list_index, target_index, double_click=double_click)
            if not result.get('success'):
                return NavigationResult(
                    success=False,
                    page=self._current_page,
                    error=f"Failed to select item: {result.get('message')}",
                    selected=matched_item,
                    context=self._context.copy(),
                )

            # Wait for page transition instead of blind sleep
            old_page = self._current_page
            try:
                new_page = self.wait_for_page_transition(old_page, timeout=15)
            except TimeoutError:
                logger.warning("Page did not transition after list item selection")
                new_page = self.detect_current_page()

            # Update history
            self._history.append(old_page)

            return NavigationResult(
                success=True,
                page=new_page,
                selected=matched_item,
                context=self._context.copy(),
            )

    def click_button(self, button_text: str) -> NavigationResult:
        """
        Click a button by text.

        Args:
            button_text: Button text to click

        Returns:
            NavigationResult
        """
        with self._lock:
            result = self.nav.click_button(button_text)
            if not result.get('success'):
                return NavigationResult(
                    success=False,
                    page=self._current_page,
                    error=f"Failed to click '{button_text}': {result.get('message')}",
                    context=self._context.copy(),
                )

            # Wait for page transition instead of blind sleep
            old_page = self._current_page
            try:
                new_page = self.wait_for_page_transition(old_page, timeout=15)
            except TimeoutError:
                logger.warning(f"Page did not transition after clicking '{button_text}'")
                new_page = self.detect_current_page()

            return NavigationResult(
                success=True,
                page=new_page,
                context=self._context.copy(),
            )

    def recover_data_display_connection(
        self,
        data_category: Optional[str] = None,
        *,
        soft_retry_attempts: int = 3,
        ok_timeout: float = 2.0,
        allow_backtrack: bool = True,
        backtrack_attempts: int = 2,
        retry_delays: Optional[list[float]] = None,
    ) -> NavigationResult:
        """Recover from the J2534 disconnect page back to Data Display.

        Strategy:
        1. Try OK several times to preserve the current Data Display context.
        2. If allowed, Back to Data List and re-enter the remembered data category.
        3. Return failure if recovery cannot safely restore Data Display.
        """
        with self._lock:
            current = self.detect_current_page(retries=0)
            if current == GDS2Page.DATA_DISPLAY:
                return NavigationResult(True, GDS2Page.DATA_DISPLAY, context=self._context.copy())

            if current == GDS2Page.LOADING:
                try:
                    current = self.wait_for_page_transition(GDS2Page.LOADING, timeout=10.0)
                except TimeoutError:
                    current = self.detect_current_page(retries=0)
                if current == GDS2Page.DATA_DISPLAY:
                    return NavigationResult(
                        success=True,
                        page=GDS2Page.DATA_DISPLAY,
                        context={**self._context.copy(), "recovery_method": "loading_wait"},
                    )

            if current != GDS2Page.J2534_DISCONNECT:
                return NavigationResult(
                    success=False,
                    page=current,
                    error=f"Expected J2534 disconnect page, got {current.value}",
                    context=self._context.copy(),
                )

            button_states = self.get_available_buttons()
            has_ok = button_states.get("OK", False)

            if has_ok:
                delays = retry_delays or [0.0, 1.5, 3.0]
                for attempt in range(min(soft_retry_attempts, len(delays))):
                    delay = delays[attempt]
                    if delay > 0:
                        time.sleep(delay)

                    result = self.nav.click_button("OK")
                    if not result.get('success'):
                        logger.warning("Failed to click OK on J2534 disconnect page: %s", result.get('message'))

                    try:
                        new_page = self.wait_for_page_transition(GDS2Page.J2534_DISCONNECT, timeout=ok_timeout)
                    except TimeoutError:
                        new_page = self.detect_current_page(retries=0)

                    if new_page == GDS2Page.DATA_DISPLAY:
                        self._current_page = GDS2Page.DATA_DISPLAY
                        return NavigationResult(
                            success=True,
                            page=GDS2Page.DATA_DISPLAY,
                            context={**self._context.copy(), "recovery_method": "soft_ok"},
                        )

                    # Re-sample visible buttons after each failed OK attempt.
                    # Some disconnect pages visually lose the OK action once GDS2
                    # determines the connection cannot be resumed in place.
                    button_states = self.get_available_buttons()
                    has_ok = button_states.get("OK", False)
                    if not has_ok:
                        logger.info("NAV recovery j2534_disconnect switching to backtrack")
                        break
            else:
                logger.info("NAV recovery j2534_disconnect using backtrack (OK unavailable)")

            if not allow_backtrack:
                return NavigationResult(
                    success=False,
                    page=GDS2Page.J2534_DISCONNECT,
                    error="Transient reconnect failed without leaving the disconnect page.",
                    context=self._context.copy(),
                )

            target_category = data_category or self._context.get("data_category")
            if not target_category:
                return NavigationResult(
                    success=False,
                    page=GDS2Page.J2534_DISCONNECT,
                    error="No data category available for reconnect backtrack.",
                    context=self._context.copy(),
                )

            last_failure: Optional[NavigationResult] = None
            for _ in range(max(1, backtrack_attempts)):
                back_result = self.go_back()
                if not back_result.success or back_result.page != GDS2Page.DATA_LIST:
                    return NavigationResult(
                        success=False,
                        page=back_result.page,
                        error=back_result.error or "Failed to return to Data List after disconnect.",
                        context=self._context.copy(),
                    )

                reenter = self.select_data_category(target_category)
                if not reenter.success:
                    return reenter

                if reenter.page == GDS2Page.SUB_DATA_LIST:
                    target_sub_category = self._context.get("sub_category")
                    if not target_sub_category:
                        return NavigationResult(
                            success=False,
                            page=GDS2Page.SUB_DATA_LIST,
                            error="Reconnect reached sub-category list but no prior sub-category was stored.",
                            context=self._context.copy(),
                        )
                    reenter = self.select_sub_category(target_sub_category)

                if reenter.success and reenter.page == GDS2Page.DATA_DISPLAY:
                    reenter.context["recovery_method"] = "backtrack"
                    return reenter

                if reenter.page != GDS2Page.J2534_DISCONNECT:
                    return NavigationResult(
                        success=False,
                        page=reenter.page,
                        error=reenter.error or "Failed to restore Data Display after reconnect backtrack.",
                        context=self._context.copy(),
                    )

                last_failure = reenter

            return NavigationResult(
                success=False,
                page=last_failure.page if last_failure else GDS2Page.J2534_DISCONNECT,
                error=(
                    last_failure.error if last_failure and last_failure.error
                    else "Failed to restore Data Display after reconnect backtrack."
                ),
                context=self._context.copy(),
            )

    def dismiss_warning_dialog(self) -> bool:
        """Dismiss warning dialog if present (OK button)."""
        with self._lock:
            for _ in range(3):
                buttons = self.nav.get_buttons()
                button_texts = [b.get('text', '') for b in buttons]
                if "OK" in button_texts:
                    logger.info("NAV warning dialog dismissed")
                    result = self.nav.click_button("OK")
                    if result.get('success'):
                        time.sleep(1)
                        return True
                time.sleep(0.3)
            return False

    # =========================================================================
    # Device Explorer Integration (Phase 2)
    # =========================================================================

    def start_diagnostics(self) -> NavigationResult:
        """
        Click Diagnostics button from Main Menu.

        If Device Explorer appears, returns the list of available devices.
        Otherwise, returns the current page for user to proceed manually.

        Returns:
            NavigationResult with:
            - page: DEVICE_EXPLORER if dialog appears, or detected page
            - choices: List of device names if DEVICE_EXPLORER, or menu items
        """
        with self._lock:
            try:
                result = self.nav.click_button("Diagnostics")
                if not result.get('success'):
                    return NavigationResult(
                        success=False,
                        page=self._current_page,
                        error=f"Failed to click Diagnostics: {result.get('message')}",
                        context=self._context.copy(),
                    )

                # Wait for page transition or Device Explorer
                # Use a short initial wait + page detection instead of blind 3s sleep
                time.sleep(1)  # Brief settle time for Device Explorer dialog

                # Check if Device Explorer appeared
                from ..native import DeviceExplorerController
                device_controller = DeviceExplorerController()

                if device_controller.find_dialog(timeout_sec=2.0):
                    # Device Explorer appeared - get device list
                    devices = device_controller.get_device_names()
                    self._current_page = GDS2Page.DEVICE_EXPLORER

                    return NavigationResult(
                        success=True,
                        page=GDS2Page.DEVICE_EXPLORER,
                        choices=devices,
                        context=self._context.copy(),
                    )

                # No Device Explorer - wait for page transition from MAIN_MENU
                try:
                    new_page = self.wait_for_page_transition(
                        GDS2Page.MAIN_MENU, timeout=30
                    )
                except TimeoutError:
                    logger.warning("Page did not transition after Diagnostics click")
                    new_page = self.detect_current_page(retries=3, retry_delay=1.5)
                self._history.append(GDS2Page.MAIN_MENU)

                # Get choices for the current page
                choices = None
                if new_page in (GDS2Page.DIAGNOSTICS_MENU, GDS2Page.MODULE_LIST, GDS2Page.DATA_LIST, GDS2Page.VEHICLE_SELECTION):
                    choices = self.get_list_items()

                return NavigationResult(
                    success=True,
                    page=new_page,
                    choices=choices,
                    context=self._context.copy(),
                )

            except Exception as e:
                logger.exception(f"start_diagnostics failed: {e}")
                return NavigationResult(
                    success=False,
                    page=self._current_page,
                    error=str(e),
                    context=self._context.copy(),
                )

    def select_device(self, device_name: str) -> NavigationResult:
        """
        Select a device in Device Explorer and click Continue.

        After device selection, returns Vehicle Selection page for user to proceed.

        Args:
            device_name: Name of device to select (e.g., "SM2 USB")

        Returns:
            NavigationResult with VEHICLE_SELECTION page
        """
        with self._lock:
            try:
                from ..native import DeviceExplorerController
                device_controller = DeviceExplorerController()

                if not device_controller.find_dialog(timeout_sec=2.0):
                    return NavigationResult(
                        success=False,
                        page=self._current_page,
                        error="Device Explorer not found",
                        context=self._context.copy(),
                    )

                # Select device
                if not device_controller.select_device_by_name(device_name):
                    devices = device_controller.get_device_names()
                    return NavigationResult(
                        success=False,
                        page=GDS2Page.DEVICE_EXPLORER,
                        error=f"Device '{device_name}' not found",
                        choices=devices,
                        context=self._context.copy(),
                    )

                time.sleep(0.3)

                # Click Continue
                if not device_controller.click_continue():
                    return NavigationResult(
                        success=False,
                        page=GDS2Page.DEVICE_EXPLORER,
                        error="Failed to click Continue",
                        context=self._context.copy(),
                    )

                # Wait for page transition from Device Explorer instead of blind sleep
                try:
                    new_page = self.wait_for_page_transition(
                        GDS2Page.DEVICE_EXPLORER, timeout=30
                    )
                except TimeoutError:
                    logger.warning("Page did not transition after device Continue click")
                    new_page = self.detect_current_page(retries=3, retry_delay=1.5)
                self._history.append(GDS2Page.DEVICE_EXPLORER)

                # Get choices for the new page
                choices = None
                if new_page in (GDS2Page.DIAGNOSTICS_MENU, GDS2Page.MODULE_LIST, GDS2Page.VEHICLE_SELECTION):
                    choices = self.get_list_items()

                return NavigationResult(
                    success=True,
                    page=new_page,
                    selected=device_name,
                    choices=choices,
                    context=self._context.copy(),
                )

            except Exception as e:
                logger.exception(f"select_device failed: {e}")
                return NavigationResult(
                    success=False,
                    page=self._current_page,
                    error=str(e),
                    context=self._context.copy(),
                )

    def disconnect_device(self) -> NavigationResult:
        """
        Disconnect current device from Vehicle Selection page.

        Click the "Disconnect" button to disconnect from VCI device.
        Must be at Vehicle Selection page.

        Returns:
            NavigationResult with success status
        """
        with self._lock:
            try:
                # First check if we're at Vehicle Selection
                current = self.detect_current_page()
                if current != GDS2Page.VEHICLE_SELECTION:
                    return NavigationResult(
                        success=False,
                        page=current,
                        error=f"Must be at Vehicle Selection page to disconnect. Current: {current.value}",
                        context=self._context.copy(),
                    )

                # Click Disconnect button
                result = self.nav.click_button("Disconnect")
                if not result.get('success'):
                    return NavigationResult(
                        success=False,
                        page=current,
                        error=f"Failed to click Disconnect: {result.get('message')}",
                        context=self._context.copy(),
                    )

                # Wait briefly for disconnect to process
                time.sleep(1)

                # Clear device from context
                self._context["device"] = None

                logger.info("Device disconnected")

                return NavigationResult(
                    success=True,
                    page=GDS2Page.VEHICLE_SELECTION,
                    context=self._context.copy(),
                )

            except Exception as e:
                logger.exception(f"disconnect_device failed: {e}")
                return NavigationResult(
                    success=False,
                    page=self._current_page,
                    error=str(e),
                    context=self._context.copy(),
                )

    def open_device_selector(self) -> NavigationResult:
        """
        Open Device Explorer from Vehicle Selection page.

        Click "Select Device" button to re-open Device Explorer dialog.
        Must be at Vehicle Selection page after disconnecting.

        Returns:
            NavigationResult with:
            - page: DEVICE_EXPLORER
            - choices: List of available devices
        """
        with self._lock:
            try:
                # First check if we're at Vehicle Selection
                current = self.detect_current_page()
                if current != GDS2Page.VEHICLE_SELECTION:
                    return NavigationResult(
                        success=False,
                        page=current,
                        error=f"Must be at Vehicle Selection page. Current: {current.value}",
                        context=self._context.copy(),
                    )

                # Click Select Device button
                result = self.nav.click_button("Select Device")
                if not result.get('success'):
                    return NavigationResult(
                        success=False,
                        page=current,
                        error=f"Failed to click Select Device: {result.get('message')}",
                        context=self._context.copy(),
                    )

                time.sleep(1)  # Brief settle for dialog to appear

                # Check if Device Explorer appeared
                from ..native import DeviceExplorerController
                device_controller = DeviceExplorerController()

                if device_controller.find_dialog(timeout_sec=5.0):
                    devices = device_controller.get_device_names()
                    self._current_page = GDS2Page.DEVICE_EXPLORER

                    return NavigationResult(
                        success=True,
                        page=GDS2Page.DEVICE_EXPLORER,
                        choices=devices,
                        context=self._context.copy(),
                    )
                else:
                    return NavigationResult(
                        success=False,
                        page=GDS2Page.VEHICLE_SELECTION,
                        error="Device Explorer dialog did not appear",
                        context=self._context.copy(),
                    )

            except Exception as e:
                logger.exception(f"open_device_selector failed: {e}")
                return NavigationResult(
                    success=False,
                    page=self._current_page,
                    error=str(e),
                    context=self._context.copy(),
                )

    def click_enter(self) -> NavigationResult:
        """
        Click Enter button (typically at Vehicle Selection page).

        Retries page detection if still at Vehicle Selection after clicking.
        If GDS2 auto-navigates to Module List (skipping Diagnostics Menu),
        clicks Back to return to Diagnostics Menu.

        Returns:
            NavigationResult with next page after clicking Enter
        """
        with self._lock:
            try:
                result = self.nav.click_button("Enter")
                if not result.get('success'):
                    return NavigationResult(
                        success=False,
                        page=self._current_page,
                        error=f"Failed to click Enter: {result.get('message')}",
                        context=self._context.copy(),
                    )

                # Wait for page transition from Vehicle Selection instead of blind sleep
                try:
                    new_page = self.wait_for_page_transition(
                        GDS2Page.VEHICLE_SELECTION, timeout=30
                    )
                except TimeoutError:
                    logger.warning("Page did not transition after Enter click")
                    new_page = self.detect_current_page(retries=2, retry_delay=1.5)

                # Dismiss any warning dialogs
                self.dismiss_warning_dialog()

                # Re-detect page after dismissing dialog (dialog may have obscured detection)
                if new_page == GDS2Page.UNKNOWN:
                    new_page = self.detect_current_page(retries=2, retry_delay=1.5)
                # If still at Vehicle Selection after first attempt, retry
                if new_page == GDS2Page.VEHICLE_SELECTION:
                    logger.info("NAV vehicle_selection retrying Enter")
                    self.nav.click_button("Enter")
                    try:
                        new_page = self.wait_for_page_transition(
                            GDS2Page.VEHICLE_SELECTION, timeout=30
                        )
                    except TimeoutError:
                        logger.warning("Page still did not transition after Enter retry")
                        new_page = self.detect_current_page(retries=2, retry_delay=1.5)
                    self.dismiss_warning_dialog()
                    if new_page == GDS2Page.UNKNOWN:
                        new_page = self.detect_current_page(retries=2, retry_delay=1.5)

                # If GDS2 auto-navigated to Module List (skipping Diagnostics Menu),
                # click Back to return to Diagnostics Menu so user can choose
                if new_page == GDS2Page.MODULE_LIST:
                    logger.info("NAV module_list auto-skip detected; returning to diagnostics_menu")
                    back_result = self.nav.click_button("Back")
                    if back_result.get('success'):
                        try:
                            new_page = self.wait_for_page_transition(
                                GDS2Page.MODULE_LIST, timeout=15
                            )
                        except TimeoutError:
                            new_page = self.detect_current_page(retries=2, retry_delay=1.0)

                # Get choices for the new page
                choices = None
                if new_page in (GDS2Page.DIAGNOSTICS_MENU, GDS2Page.MODULE_LIST, GDS2Page.DATA_LIST):
                    choices = self.get_list_items()

                return NavigationResult(
                    success=True,
                    page=new_page,
                    choices=choices,
                    context=self._context.copy(),
                )

            except Exception as e:
                logger.exception(f"click_enter failed: {e}")
                return NavigationResult(
                    success=False,
                    page=self._current_page,
                    error=str(e),
                    context=self._context.copy(),
                )

    def get_available_buttons(self) -> Dict[str, bool]:
        """
        Get available buttons and their enabled status for the current page.

        Returns:
            Dict mapping button name to enabled status
        """
        try:
            buttons = self.nav.get_buttons()
            button_states = {}

            for btn in buttons:
                name = btn.get('text', '')
                if name:
                    # Assume all visible buttons are enabled
                    # (Agent doesn't provide enabled state, so we infer from visibility)
                    button_states[name] = True

            return button_states

        except Exception as e:
            logger.error(f"Failed to get button states: {e}")
            return {}

    # =========================================================================
    # Context Management
    # =========================================================================

    # =========================================================================
    # Sub-category Support (Phase 3)
    # =========================================================================

    def select_data_category(self, data_category: str) -> NavigationResult:
        """
        Select a data category from the Data List.

        After selection, detects whether the page transitions to:
        - DATA_DISPLAY: Category has no sub-categories (Create Report visible)
        - SUB_DATA_LIST: Category has sub-categories (new list appears)

        Args:
            data_category: Name of data category to select

        Returns:
            NavigationResult with:
            - page: DATA_DISPLAY or SUB_DATA_LIST
            - choices: Sub-category list if SUB_DATA_LIST
        """
        with self._lock:
            items = self.wait_for_list()
            if not items:
                return NavigationResult(
                    success=False,
                    page=self._current_page,
                    error="No data categories found",
                    context=self._context.copy(),
                )

            # Find matching item
            target_index = None
            matched_item = None
            for i, item in enumerate(items):
                if data_category in item or item in data_category:
                    target_index = i
                    matched_item = item
                    break

            if target_index is None:
                return NavigationResult(
                    success=False,
                    page=self._current_page,
                    error=f"Data category '{data_category}' not found",
                    choices=items,
                    context=self._context.copy(),
                )

            # Select the item
            result = self.nav.select_list_item(0, target_index, double_click=True)
            if not result.get('success'):
                return NavigationResult(
                    success=False,
                    page=self._current_page,
                    error=f"Failed to select data category: {result.get('message')}",
                    context=self._context.copy(),
                )

            # Wait for page transition instead of blind sleep
            old_page = self._current_page
            try:
                new_page = self.wait_for_page_transition(old_page, timeout=20)
            except TimeoutError:
                logger.warning("Page did not transition after data category selection")
                new_page = self.detect_current_page()

            if new_page == GDS2Page.DATA_DISPLAY:
                self._history.append(self._current_page)
                self._current_page = GDS2Page.DATA_DISPLAY
                self._context["data_category"] = matched_item
                self._context["sub_category"] = None
                return NavigationResult(
                    success=True,
                    page=GDS2Page.DATA_DISPLAY,
                    selected=matched_item,
                    context=self._context.copy(),
                )

            # Check for sub-categories (list items but no Create Report)
            sub_items = self.nav.get_list_items(0)
            if sub_items:
                self._history.append(self._current_page)
                self._current_page = GDS2Page.SUB_DATA_LIST
                self._context["data_category"] = matched_item
                self._context["sub_category"] = None
                return NavigationResult(
                    success=True,
                    page=GDS2Page.SUB_DATA_LIST,
                    selected=matched_item,
                    choices=sub_items,
                    context=self._context.copy(),
                )

            # Fallback
            self._history.append(self._current_page)
            self._context["data_category"] = matched_item
            self._context["sub_category"] = None
            return NavigationResult(
                success=True,
                page=new_page,
                selected=matched_item,
                context=self._context.copy(),
            )

    def select_sub_category(self, sub_category: str) -> NavigationResult:
        """
        Select a sub-category from the Sub Data List.

        Args:
            sub_category: Name of sub-category to select

        Returns:
            NavigationResult with page info (should be DATA_DISPLAY)
        """
        with self._lock:
            items = self.wait_for_list()
            if not items:
                return NavigationResult(
                    success=False,
                    page=self._current_page,
                    error="No sub-categories found",
                    context=self._context.copy(),
                )

            # Find matching item
            target_index = None
            matched_item = None
            for i, item in enumerate(items):
                if sub_category in item or item in sub_category:
                    target_index = i
                    matched_item = item
                    break

            if target_index is None:
                return NavigationResult(
                    success=False,
                    page=self._current_page,
                    error=f"Sub-category '{sub_category}' not found",
                    choices=items,
                    context=self._context.copy(),
                )

            result = self.nav.select_list_item(0, target_index, double_click=True)
            if not result.get('success'):
                return NavigationResult(
                    success=False,
                    page=self._current_page,
                    error=f"Failed to select sub-category: {result.get('message')}",
                    context=self._context.copy(),
                )

            # Wait for page transition instead of blind sleep
            old_page = self._current_page
            try:
                new_page = self.wait_for_page_transition(old_page, timeout=20)
            except TimeoutError:
                logger.warning("Page did not transition after sub-category selection")
                new_page = self.detect_current_page()
            self._history.append(self._current_page)
            self._current_page = new_page
            self._context["sub_category"] = matched_item

            return NavigationResult(
                success=True,
                page=new_page,
                selected=matched_item,
                context=self._context.copy(),
            )

    def set_context(self, **kwargs):
        """Update navigation context."""
        with self._lock:
            for key, value in kwargs.items():
                if key in self._context:
                    self._context[key] = value

    def clear_context(self):
        """Clear all context except device."""
        with self._lock:
            device = self._context.get("device")
            self._context = {
                "module": None,
                "data_category": None,
                "sub_category": None,
                "device": device,
            }

    def get_context(self) -> Dict[str, Any]:
        """Get current navigation context."""
        return self._context.copy()

    # =========================================================================
    # String Representation
    # =========================================================================

    def __repr__(self) -> str:
        return (
            f"NavigationController("
            f"page={self._current_page.value}, "
            f"module={self._context.get('module')}, "
            f"data_category={self._context.get('data_category')})"
        )
