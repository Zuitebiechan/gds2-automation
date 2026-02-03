"""
Module Data Display UI

Tkinter-based UI for selecting modules and viewing data.
Designed for users unfamiliar with GDS2 software.
"""

import json
import logging
import threading
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
from pathlib import Path
from typing import Dict, Any, List, Optional, Callable

logger = logging.getLogger(__name__)


class ModuleDataDisplayUI:
    """
    Main UI for Module Data Display workflow.

    Features:
    - Vehicle/registry selection
    - Module search and selection
    - Data item selection (when applicable)
    - Run workflow and display results
    - Discovery progress tracking
    """

    def __init__(self, root: tk.Tk = None):
        """
        Initialize the UI.

        Args:
            root: Optional existing Tk root. Creates new one if not provided.
        """
        self.root = root or tk.Tk()
        self.root.title("GDS2 Module Data Display")
        self.root.geometry("800x600")
        self.root.minsize(600, 400)

        # State
        self.driver = None
        self.workflow = None
        self.registry = None
        self.current_vin = None
        self.modules: List[str] = []
        self.data_items: List[str] = []

        # Callbacks
        self.on_run_callback: Optional[Callable] = None
        self.on_discover_callback: Optional[Callable] = None

        # Build UI
        self._create_widgets()
        self._setup_layout()

    def _create_widgets(self):
        """Create all UI widgets."""
        # Main container with padding
        self.main_frame = ttk.Frame(self.root, padding="10")

        # === Header Section ===
        self.header_frame = ttk.Frame(self.main_frame)
        self.title_label = ttk.Label(
            self.header_frame,
            text="GDS2 Module Data Display",
            font=("Helvetica", 16, "bold")
        )
        self.status_label = ttk.Label(
            self.header_frame,
            text="Status: Not connected",
            foreground="gray"
        )

        # === Vehicle Section ===
        self.vehicle_frame = ttk.LabelFrame(self.main_frame, text="Vehicle", padding="5")

        self.vin_label = ttk.Label(self.vehicle_frame, text="VIN:")
        self.vin_var = tk.StringVar()
        self.vin_combo = ttk.Combobox(
            self.vehicle_frame,
            textvariable=self.vin_var,
            state="readonly",
            width=30
        )
        self.vin_combo.bind("<<ComboboxSelected>>", self._on_vin_selected)

        self.discover_btn = ttk.Button(
            self.vehicle_frame,
            text="Discover New Vehicle",
            command=self._on_discover_click
        )
        self.refresh_btn = ttk.Button(
            self.vehicle_frame,
            text="Refresh",
            command=self._refresh_registries
        )

        # === Module Selection Section ===
        self.module_frame = ttk.LabelFrame(self.main_frame, text="Module Selection", padding="5")

        # Module search
        self.search_label = ttk.Label(self.module_frame, text="Search:")
        self.search_var = tk.StringVar()
        self.search_var.trace("w", self._on_search_changed)
        self.search_entry = ttk.Entry(self.module_frame, textvariable=self.search_var, width=30)

        # Module list
        self.module_list_frame = ttk.Frame(self.module_frame)
        self.module_listbox = tk.Listbox(
            self.module_list_frame,
            height=8,
            exportselection=False
        )
        self.module_scrollbar = ttk.Scrollbar(
            self.module_list_frame,
            orient="vertical",
            command=self.module_listbox.yview
        )
        self.module_listbox.config(yscrollcommand=self.module_scrollbar.set)
        self.module_listbox.bind("<<ListboxSelect>>", self._on_module_selected)

        # === Data Item Section ===
        self.data_frame = ttk.LabelFrame(self.main_frame, text="Data Item (if required)", padding="5")

        self.data_list_frame = ttk.Frame(self.data_frame)
        self.data_listbox = tk.Listbox(
            self.data_list_frame,
            height=5,
            exportselection=False
        )
        self.data_scrollbar = ttk.Scrollbar(
            self.data_list_frame,
            orient="vertical",
            command=self.data_listbox.yview
        )
        self.data_listbox.config(yscrollcommand=self.data_scrollbar.set)

        self.data_hint_label = ttk.Label(
            self.data_frame,
            text="Select a module first",
            foreground="gray"
        )

        # === Action Section ===
        self.action_frame = ttk.Frame(self.main_frame)

        self.run_btn = ttk.Button(
            self.action_frame,
            text="Get Data",
            command=self._on_run_click,
            state="disabled"
        )
        self.cancel_btn = ttk.Button(
            self.action_frame,
            text="Cancel",
            command=self._on_cancel_click,
            state="disabled"
        )

        # Progress bar
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(
            self.action_frame,
            variable=self.progress_var,
            maximum=100
        )
        self.progress_label = ttk.Label(self.action_frame, text="")

        # === Results Section ===
        self.results_frame = ttk.LabelFrame(self.main_frame, text="Results", padding="5")

        self.results_text = scrolledtext.ScrolledText(
            self.results_frame,
            height=10,
            wrap=tk.WORD,
            state="disabled"
        )

    def _setup_layout(self):
        """Setup widget layout using grid."""
        self.main_frame.pack(fill="both", expand=True)

        # Header
        self.header_frame.pack(fill="x", pady=(0, 10))
        self.title_label.pack(side="left")
        self.status_label.pack(side="right")

        # Vehicle section
        self.vehicle_frame.pack(fill="x", pady=(0, 10))
        self.vin_label.grid(row=0, column=0, padx=5, pady=5)
        self.vin_combo.grid(row=0, column=1, padx=5, pady=5)
        self.refresh_btn.grid(row=0, column=2, padx=5, pady=5)
        self.discover_btn.grid(row=0, column=3, padx=5, pady=5)

        # Module section
        self.module_frame.pack(fill="both", expand=True, pady=(0, 10))
        self.search_label.pack(anchor="w")
        self.search_entry.pack(fill="x", pady=(0, 5))
        self.module_list_frame.pack(fill="both", expand=True)
        self.module_listbox.pack(side="left", fill="both", expand=True)
        self.module_scrollbar.pack(side="right", fill="y")

        # Data item section
        self.data_frame.pack(fill="x", pady=(0, 10))
        self.data_hint_label.pack(anchor="w")
        self.data_list_frame.pack(fill="both", expand=True)
        self.data_listbox.pack(side="left", fill="both", expand=True)
        self.data_scrollbar.pack(side="right", fill="y")

        # Action section
        self.action_frame.pack(fill="x", pady=(0, 10))
        self.run_btn.pack(side="left", padx=5)
        self.cancel_btn.pack(side="left", padx=5)
        self.progress_label.pack(side="right", padx=5)
        self.progress_bar.pack(side="right", fill="x", expand=True, padx=5)

        # Results section
        self.results_frame.pack(fill="both", expand=True)
        self.results_text.pack(fill="both", expand=True)

    # === Public Methods ===

    def set_driver(self, driver):
        """Set the ImageDriver instance."""
        self.driver = driver
        self._update_status("Connected to GDS2")

    def set_callbacks(
        self,
        on_run: Optional[Callable] = None,
        on_discover: Optional[Callable] = None
    ):
        """
        Set callback functions.

        Args:
            on_run: Called when Run button clicked. Receives (module_name, data_item).
            on_discover: Called when Discover button clicked.
        """
        self.on_run_callback = on_run
        self.on_discover_callback = on_discover

    def load_registries(self, registries_dir: Path):
        """
        Load available registries from directory.

        Args:
            registries_dir: Path to registries directory
        """
        self.registries_dir = registries_dir

        if not registries_dir.exists():
            self.vin_combo["values"] = []
            return

        vins = []
        for path in registries_dir.glob("*.json"):
            if path.stem != "README":
                vins.append(path.stem)

        self.vin_combo["values"] = vins

        if vins:
            self.vin_combo.current(0)
            self._on_vin_selected(None)

    def set_registry(self, registry: Dict[str, Any]):
        """
        Set the current registry data.

        Args:
            registry: Registry dictionary with modules info
        """
        self.registry = registry
        self.current_vin = registry.get("vin", "Unknown")

        # Get discovered modules
        self.modules = [
            name for name, info in registry.get("modules", {}).items()
            if info.get("discovered", False)
        ]
        self.modules.sort()

        # Update module list
        self._update_module_list(self.modules)
        self._update_status(f"Loaded: {len(self.modules)} modules")

    def update_progress(self, current: int, total: int, message: str = ""):
        """
        Update progress bar and label.

        Args:
            current: Current step number
            total: Total steps
            message: Optional status message
        """
        if total > 0:
            percent = (current / total) * 100
            self.progress_var.set(percent)
        self.progress_label.config(text=message or f"{current}/{total}")
        self.root.update_idletasks()

    def show_results(self, result: Dict[str, Any]):
        """
        Display workflow results.

        Args:
            result: Result dictionary from workflow
        """
        self.results_text.config(state="normal")
        self.results_text.delete("1.0", tk.END)

        if result.get("success", False):
            # Format successful result
            text = f"Module: {result.get('module', 'N/A')}\n"
            if result.get("data_item"):
                text += f"Data Item: {result['data_item']}\n"
            text += f"Report: {result.get('report_path', 'N/A')}\n"
            text += "\n--- Parameters ---\n\n"

            for param in result.get("parameters", []):
                name = param.get("name", "Unknown")
                value = param.get("value", "N/A")
                unit = param.get("unit", "")
                text += f"{name}: {value} {unit}\n"

            if not result.get("parameters"):
                text += "(No parameters found)\n"
        else:
            # Format error
            text = f"Error: {result.get('error', 'Unknown error')}\n"

        self.results_text.insert("1.0", text)
        self.results_text.config(state="disabled")

    def show_message(self, title: str, message: str, msg_type: str = "info"):
        """
        Show a message dialog.

        Args:
            title: Dialog title
            message: Message text
            msg_type: "info", "warning", or "error"
        """
        if msg_type == "error":
            messagebox.showerror(title, message)
        elif msg_type == "warning":
            messagebox.showwarning(title, message)
        else:
            messagebox.showinfo(title, message)

    def run(self):
        """Start the UI main loop."""
        self.root.mainloop()

    # === Private Methods ===

    def _update_status(self, text: str, color: str = "black"):
        """Update status label."""
        self.status_label.config(text=f"Status: {text}", foreground=color)

    def _update_module_list(self, modules: List[str]):
        """Update module listbox."""
        self.module_listbox.delete(0, tk.END)
        for module in modules:
            self.module_listbox.insert(tk.END, module)

    def _update_data_items(self, items: List[str]):
        """Update data items listbox."""
        self.data_listbox.delete(0, tk.END)
        for item in items:
            self.data_listbox.insert(tk.END, item)

        if items:
            self.data_hint_label.config(text=f"Select a data item ({len(items)} available)")
        else:
            self.data_hint_label.config(text="No data item selection required")

    def _get_selected_module(self) -> Optional[str]:
        """Get currently selected module name."""
        selection = self.module_listbox.curselection()
        if selection:
            return self.module_listbox.get(selection[0])
        return None

    def _get_selected_data_item(self) -> Optional[str]:
        """Get currently selected data item."""
        selection = self.data_listbox.curselection()
        if selection:
            return self.data_listbox.get(selection[0])
        return None

    def _refresh_registries(self):
        """Refresh the registry list."""
        if hasattr(self, "registries_dir"):
            self.load_registries(self.registries_dir)

    # === Event Handlers ===

    def _on_vin_selected(self, event):
        """Handle VIN selection."""
        vin = self.vin_var.get()
        if not vin:
            return

        # Load registry
        registry_path = self.registries_dir / f"{vin}.json"
        if registry_path.exists():
            with open(registry_path, "r") as f:
                registry = json.load(f)
            self.set_registry(registry)
        else:
            self.show_message("Error", f"Registry not found: {vin}", "error")

    def _on_search_changed(self, *args):
        """Handle search text change."""
        search_text = self.search_var.get().lower()

        if not search_text:
            self._update_module_list(self.modules)
        else:
            filtered = [m for m in self.modules if search_text in m.lower()]
            self._update_module_list(filtered)

    def _on_module_selected(self, event):
        """Handle module selection."""
        module_name = self._get_selected_module()
        if not module_name or not self.registry:
            return

        # Get module info
        module_info = self.registry.get("modules", {}).get(module_name, {})

        # Update data items
        if module_info.get("has_data_selection", False):
            self.data_items = module_info.get("data_items", [])
            self._update_data_items(self.data_items)
        else:
            self.data_items = []
            self._update_data_items([])
            self.data_hint_label.config(text="No data item selection required")

        # Enable run button
        self.run_btn.config(state="normal")

    def _on_discover_click(self):
        """Handle Discover button click."""
        if self.on_discover_callback:
            # Disable buttons during discovery
            self.discover_btn.config(state="disabled")
            self.run_btn.config(state="disabled")
            self._update_status("Discovering modules...", "blue")

            # Run discovery in thread
            def run_discovery():
                try:
                    self.on_discover_callback()
                    self.root.after(0, self._on_discovery_complete)
                except Exception as e:
                    self.root.after(0, lambda: self._on_discovery_error(str(e)))

            thread = threading.Thread(target=run_discovery, daemon=True)
            thread.start()
        else:
            self.show_message("Error", "Discovery not configured", "error")

    def _on_discovery_complete(self):
        """Called when discovery completes."""
        self.discover_btn.config(state="normal")
        self._update_status("Discovery complete", "green")
        self._refresh_registries()

    def _on_discovery_error(self, error: str):
        """Called when discovery fails."""
        self.discover_btn.config(state="normal")
        self._update_status("Discovery failed", "red")
        self.show_message("Discovery Error", error, "error")

    def _on_run_click(self):
        """Handle Run button click."""
        module_name = self._get_selected_module()
        if not module_name:
            self.show_message("Error", "Please select a module", "warning")
            return

        # Check if data item is required
        module_info = self.registry.get("modules", {}).get(module_name, {})
        data_item = None

        if module_info.get("has_data_selection", False):
            data_item = self._get_selected_data_item()
            if not data_item:
                self.show_message("Error", "Please select a data item", "warning")
                return

        if self.on_run_callback:
            # Disable buttons during run
            self.run_btn.config(state="disabled")
            self.cancel_btn.config(state="normal")
            self._update_status("Running...", "blue")

            # Run workflow in thread
            def run_workflow():
                try:
                    result = self.on_run_callback(module_name, data_item)
                    self.root.after(0, lambda: self._on_run_complete(result))
                except Exception as e:
                    self.root.after(0, lambda: self._on_run_error(str(e)))

            thread = threading.Thread(target=run_workflow, daemon=True)
            thread.start()
        else:
            self.show_message("Error", "Workflow not configured", "error")

    def _on_run_complete(self, result: Dict[str, Any]):
        """Called when workflow completes."""
        self.run_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")

        if result.get("success", False):
            self._update_status("Complete", "green")
        else:
            self._update_status("Failed", "red")

        self.show_results(result)

    def _on_run_error(self, error: str):
        """Called when workflow fails."""
        self.run_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")
        self._update_status("Error", "red")
        self.show_message("Workflow Error", error, "error")

    def _on_cancel_click(self):
        """Handle Cancel button click."""
        # TODO: Implement workflow cancellation
        self.cancel_btn.config(state="disabled")
        self._update_status("Cancelled", "orange")


def create_ui(driver=None) -> ModuleDataDisplayUI:
    """
    Factory function to create the UI.

    Args:
        driver: Optional ImageDriver instance

    Returns:
        ModuleDataDisplayUI instance
    """
    ui = ModuleDataDisplayUI()

    if driver:
        ui.set_driver(driver)

    return ui


# Quick test
if __name__ == "__main__":
    # Create test UI
    ui = ModuleDataDisplayUI()

    # Load test registry if exists
    registries_dir = Path(__file__).parent.parent / "config" / "registries"
    ui.load_registries(registries_dir)

    # Run
    ui.run()
