"""
Tkinter diagnostics window for VCI Proxy tray client.

This module provides a standalone window (with its own Tk root) that talks to
cloud diagnostics REST endpoints and displays:
- start/session status
- fault codes (DTCs)
- live data streaming updates from SSE
"""

from __future__ import annotations

import json
import queue
import re
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Any, Optional
from urllib.parse import urlparse

import requests


class DiagnosticsWindow:
    """Vehicle diagnostics UI for cloud API-driven workflows."""

    POLL_INTERVAL_MS = 100

    def __init__(self, api_base_url: str):
        self._api_base = api_base_url.rstrip("/")
        parsed = urlparse(self._api_base)
        self._server_display = parsed.netloc or self._api_base

        self._queue: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()

        self._root = tk.Tk()
        self._root.title("Vehicle Diagnostics")
        self._root.configure(bg="white")
        self._root.protocol("WM_DELETE_WINDOW", self.destroy)

        self._is_destroying = False
        self._sse_running = False
        self._sse_thread: Optional[threading.Thread] = None
        self._sse_response: Optional[requests.Response] = None
        self._stream_active = False

        self._live_param_rows: dict[str, str] = {}

        self._status_message = tk.StringVar(value="Ready")
        self._server_state_text = tk.StringVar(value=f"Server: {self._server_display}")
        self._dtc_count_text = tk.StringVar(value="Found 0 fault code(s)")

        self._selected_module = tk.StringVar(value="")
        self._selected_data_category = tk.StringVar(value="")

        self._build_window_geometry()
        self._build_style()
        self._build_layout()

        self._root.after(self.POLL_INTERVAL_MS, self._poll_queue)

    # ------------------------------------------------------------------
    # Window lifecycle
    # ------------------------------------------------------------------

    def show(self) -> None:
        """Show diagnostics window and enter tkinter loop."""
        self._root.deiconify()
        self._root.lift()
        self._root.mainloop()

    def destroy(self) -> None:
        """Stop background workers and close window."""
        if self._is_destroying:
            return

        self._is_destroying = True
        self._stop_sse_thread()

        try:
            self._root.quit()
        except tk.TclError:
            pass

        try:
            self._root.destroy()
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # UI setup
    # ------------------------------------------------------------------

    def _build_window_geometry(self) -> None:
        width, height = 900, 700
        self._root.update_idletasks()
        screen_w = self._root.winfo_screenwidth()
        screen_h = self._root.winfo_screenheight()
        x = max((screen_w - width) // 2, 0)
        y = max((screen_h - height) // 2, 0)
        self._root.geometry(f"{width}x{height}+{x}+{y}")
        self._root.minsize(860, 640)

    def _build_style(self) -> None:
        style = ttk.Style(self._root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        self._root.option_add("*Font", "Segoe UI 10")

        style.configure("App.TFrame", background="white")
        style.configure("Card.TFrame", background="white")
        style.configure("HeaderTitle.TLabel", background="white", font=("Segoe UI", 20, "bold"), foreground="#111827")
        style.configure("Subtle.TLabel", background="white", foreground="#374151", font=("Segoe UI", 10))
        style.configure("Status.TLabel", background="white", foreground="#111827", font=("Segoe UI", 10, "bold"))
        style.configure("DotOnline.TLabel", background="white", foreground="#16a34a", font=("Segoe UI", 14, "bold"))
        style.configure("DotOffline.TLabel", background="white", foreground="#dc2626", font=("Segoe UI", 14, "bold"))
        style.configure("Big.TButton", font=("Segoe UI", 12, "bold"), padding=(14, 10))
        style.configure("TButton", padding=(10, 6))
        style.configure("TLabelframe", background="white", foreground="#111827")
        style.configure("TLabelframe.Label", background="white", foreground="#111827", font=("Segoe UI", 10, "bold"))

    def _build_layout(self) -> None:
        root_frame = ttk.Frame(self._root, style="App.TFrame", padding=18)
        root_frame.pack(fill=tk.BOTH, expand=True)

        root_frame.rowconfigure(3, weight=1)
        root_frame.rowconfigure(4, weight=1)
        root_frame.columnconfigure(0, weight=1)

        self._build_header(root_frame)
        self._build_start_section(root_frame)
        self._build_dtc_section(root_frame)
        self._build_live_data_section(root_frame)

    def _build_header(self, parent: ttk.Frame) -> None:
        header = ttk.Frame(parent, style="Card.TFrame", padding=(0, 0, 0, 10))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        header.columnconfigure(1, weight=0)

        ttk.Label(header, text="Vehicle Diagnostics", style="HeaderTitle.TLabel").grid(
            row=0,
            column=0,
            sticky="w",
            padx=(0, 10),
        )

        status_box = ttk.Frame(header, style="Card.TFrame")
        status_box.grid(row=0, column=1, sticky="e")

        ttk.Label(status_box, textvariable=self._server_state_text, style="Subtle.TLabel").grid(
            row=0,
            column=0,
            sticky="e",
            padx=(0, 8),
        )

        self._server_dot = ttk.Label(status_box, text="●", style="DotOffline.TLabel")
        self._server_dot.grid(row=0, column=1, sticky="e")

    def _build_start_section(self, parent: ttk.Frame) -> None:
        frame = ttk.Frame(parent, style="Card.TFrame", padding=(0, 0, 0, 12))
        frame.grid(row=1, column=0, sticky="ew")
        frame.columnconfigure(1, weight=1)

        self._start_button = ttk.Button(
            frame,
            text="▶ Start Diagnostics",
            style="Big.TButton",
            command=self._on_start_clicked,
        )
        self._start_button.grid(row=0, column=0, sticky="w", padx=(0, 16))

        self._status_label = ttk.Label(frame, textvariable=self._status_message, style="Status.TLabel")
        self._status_label.grid(row=0, column=1, sticky="w")

    def _build_dtc_section(self, parent: ttk.Frame) -> None:
        dtc_frame = ttk.LabelFrame(parent, text="Fault Codes (DTCs)", padding=12)
        dtc_frame.grid(row=2, column=0, sticky="nsew", pady=(0, 12))
        dtc_frame.columnconfigure(0, weight=1)
        dtc_frame.rowconfigure(1, weight=1)

        button_row = ttk.Frame(dtc_frame, style="Card.TFrame")
        button_row.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        button_row.columnconfigure(2, weight=1)

        self._read_dtc_button = ttk.Button(
            button_row,
            text="Read DTCs",
            command=self._on_read_dtcs_clicked,
            state=tk.DISABLED,
        )
        self._read_dtc_button.grid(row=0, column=0, sticky="w", padx=(0, 10))

        ttk.Label(button_row, textvariable=self._dtc_count_text, style="Subtle.TLabel").grid(
            row=0,
            column=1,
            sticky="w",
        )

        dtc_cols = ("code", "module", "status", "description")
        self._dtc_tree = ttk.Treeview(dtc_frame, columns=dtc_cols, show="headings", height=8)
        self._dtc_tree.grid(row=1, column=0, sticky="nsew")

        self._dtc_tree.heading("code", text="Code")
        self._dtc_tree.heading("module", text="Module")
        self._dtc_tree.heading("status", text="Status")
        self._dtc_tree.heading("description", text="Description")

        self._dtc_tree.column("code", width=110, anchor=tk.W, stretch=False)
        self._dtc_tree.column("module", width=220, anchor=tk.W, stretch=True)
        self._dtc_tree.column("status", width=110, anchor=tk.W, stretch=False)
        self._dtc_tree.column("description", width=420, anchor=tk.W, stretch=True)

        dtc_scroll = ttk.Scrollbar(dtc_frame, orient=tk.VERTICAL, command=self._dtc_tree.yview)
        dtc_scroll.grid(row=1, column=1, sticky="ns")
        self._dtc_tree.configure(yscrollcommand=dtc_scroll.set)

    def _build_live_data_section(self, parent: ttk.Frame) -> None:
        live_frame = ttk.LabelFrame(parent, text="Live Data", padding=12)
        live_frame.grid(row=3, column=0, sticky="nsew")
        live_frame.columnconfigure(1, weight=1)
        live_frame.rowconfigure(4, weight=1)

        # Row 1: module selection
        ttk.Label(live_frame, text="Module:", style="Subtle.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))

        self._module_combo = ttk.Combobox(
            live_frame,
            textvariable=self._selected_module,
            state="readonly",
            width=42,
            values=[],
        )
        self._module_combo.grid(row=0, column=1, sticky="ew", pady=(0, 8), padx=(8, 8))

        self._select_module_button = ttk.Button(
            live_frame,
            text="Select",
            command=self._on_select_module_clicked,
            state=tk.DISABLED,
        )
        self._select_module_button.grid(row=0, column=2, sticky="w", pady=(0, 8))

        # Row 2: data category
        ttk.Label(live_frame, text="Data Category:", style="Subtle.TLabel").grid(row=1, column=0, sticky="w", pady=(0, 8))

        self._data_combo = ttk.Combobox(
            live_frame,
            textvariable=self._selected_data_category,
            state="readonly",
            width=42,
            values=[],
        )
        self._data_combo.grid(row=1, column=1, sticky="ew", pady=(0, 8), padx=(8, 8))

        # Row 3: stream controls
        controls = ttk.Frame(live_frame, style="Card.TFrame")
        controls.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 10))

        self._start_stream_button = ttk.Button(
            controls,
            text="▶ Start Stream",
            command=self._on_start_stream_clicked,
            state=tk.DISABLED,
        )
        self._start_stream_button.grid(row=0, column=0, sticky="w")

        self._stop_stream_button = ttk.Button(
            controls,
            text="⏹ Stop",
            command=self._on_stop_stream_clicked,
            state=tk.DISABLED,
        )
        self._stop_stream_button.grid(row=0, column=1, sticky="w", padx=(10, 0))

        # Row 4+: live table
        live_cols = ("parameter", "value", "unit")
        self._live_tree = ttk.Treeview(live_frame, columns=live_cols, show="headings", height=12)
        self._live_tree.grid(row=4, column=0, columnspan=2, sticky="nsew")

        self._live_tree.heading("parameter", text="Parameter")
        self._live_tree.heading("value", text="Value")
        self._live_tree.heading("unit", text="Unit")

        self._live_tree.column("parameter", width=360, anchor=tk.W, stretch=True)
        self._live_tree.column("value", width=190, anchor=tk.W, stretch=True)
        self._live_tree.column("unit", width=140, anchor=tk.W, stretch=False)

        live_scroll = ttk.Scrollbar(live_frame, orient=tk.VERTICAL, command=self._live_tree.yview)
        live_scroll.grid(row=4, column=2, sticky="ns")
        self._live_tree.configure(yscrollcommand=live_scroll.set)

    # ------------------------------------------------------------------
    # Generic threaded API helpers
    # ------------------------------------------------------------------

    def _api_call(
        self,
        method: str,
        endpoint: str,
        json_data: Optional[dict[str, Any]] = None,
        callback_event: str = "api_result",
    ) -> None:
        """Make API call in background thread and post result to queue."""

        def _worker() -> None:
            url = f"{self._api_base}/{endpoint.lstrip('/')}"
            try:
                if method.upper() == "POST":
                    resp = requests.post(url, json=json_data, timeout=60)
                else:
                    resp = requests.get(url, timeout=60)
                resp.raise_for_status()
                try:
                    data = resp.json()
                except ValueError:
                    data = {"success": False, "error": "Server returned invalid JSON."}

                self._queue.put((callback_event, data))
            except Exception as exc:
                self._queue.put((callback_event, {"success": False, "error": str(exc)}))

        threading.Thread(target=_worker, daemon=True, name=f"diag-api-{callback_event}").start()

    def _poll_queue(self) -> None:
        """Process background thread messages on tkinter thread."""
        if self._is_destroying:
            return

        try:
            while True:
                event, data = self._queue.get_nowait()
                self._handle_event(event, data)
        except queue.Empty:
            pass

        try:
            self._root.after(self.POLL_INTERVAL_MS, self._poll_queue)
        except tk.TclError:
            pass

    def _handle_event(self, event: str, data: dict[str, Any]) -> None:
        """Dispatch queue events from background workers."""
        if event == "start_result":
            self._handle_start_result(data)
        elif event == "dtcs_result":
            self._handle_dtcs_result(data)
        elif event == "module_result":
            self._handle_select_module_result(data)
        elif event == "live_start_result":
            self._handle_live_start_result(data)
        elif event == "live_stop_result":
            self._handle_live_stop_result(data)
        elif event == "sse_snapshot":
            self._handle_sse_snapshot(data)
        elif event == "sse_error":
            self._handle_sse_error(data)

    # ------------------------------------------------------------------
    # Header/status helpers
    # ------------------------------------------------------------------

    def _set_server_connected(self, is_connected: bool) -> None:
        self._server_dot.configure(style="DotOnline.TLabel" if is_connected else "DotOffline.TLabel")

    def _set_status_text(self, message: str) -> None:
        self._status_message.set(message)

    def _error_message(self, payload: dict[str, Any], fallback: str) -> str:
        value = payload.get("error") if isinstance(payload, dict) else None
        return str(value).strip() if value else fallback

    # ------------------------------------------------------------------
    # UI callbacks
    # ------------------------------------------------------------------

    def _on_start_clicked(self) -> None:
        self._start_button.configure(state=tk.DISABLED)
        self._set_status_text("Connecting...")
        self._set_server_connected(False)

        # Reset dependent controls until start succeeds.
        self._read_dtc_button.configure(state=tk.DISABLED)
        self._select_module_button.configure(state=tk.DISABLED)
        self._start_stream_button.configure(state=tk.DISABLED)
        self._stop_stream_button.configure(state=tk.DISABLED)
        self._module_combo.configure(values=[])
        self._data_combo.configure(values=[])
        self._selected_module.set("")
        self._selected_data_category.set("")

        self._api_call("POST", "/api/diagnose/start", callback_event="start_result")

    def _on_read_dtcs_clicked(self) -> None:
        self._read_dtc_button.configure(state=tk.DISABLED)
        self._set_status_text("Reading fault codes...")
        self._api_call("GET", "/api/diagnose/dtcs", callback_event="dtcs_result")

    def _on_select_module_clicked(self) -> None:
        module = self._selected_module.get().strip()
        if not module:
            messagebox.showwarning("Module Required", "Please select a module first.")
            return

        self._select_module_button.configure(state=tk.DISABLED)
        self._start_stream_button.configure(state=tk.DISABLED)
        self._set_status_text("Selecting module...")
        self._api_call(
            "POST",
            "/api/diagnose/select_module",
            json_data={"module": module},
            callback_event="module_result",
        )

    def _on_start_stream_clicked(self) -> None:
        category = self._selected_data_category.get().strip()
        if not category:
            messagebox.showwarning("Data Category Required", "Please select a data category.")
            return

        self._start_stream_button.configure(state=tk.DISABLED)
        self._stop_stream_button.configure(state=tk.DISABLED)
        self._set_status_text("Starting live stream...")

        self._api_call(
            "POST",
            "/api/diagnose/live_data/start",
            json_data={"data_category": category},
            callback_event="live_start_result",
        )

    def _on_stop_stream_clicked(self) -> None:
        self._stop_stream_button.configure(state=tk.DISABLED)
        self._set_status_text("Stopping live stream...")
        self._api_call("POST", "/api/diagnose/live_data/stop", callback_event="live_stop_result")

    # ------------------------------------------------------------------
    # API event handlers
    # ------------------------------------------------------------------

    def _handle_start_result(self, payload: dict[str, Any]) -> None:
        self._start_button.configure(state=tk.NORMAL)

        if payload.get("success"):
            modules = payload.get("modules") or []
            if not isinstance(modules, list):
                modules = []

            vin = payload.get("vin") or "Unknown"

            self._module_combo.configure(values=modules)
            self._selected_module.set(modules[0] if modules else "")

            self._read_dtc_button.configure(state=tk.NORMAL)
            self._select_module_button.configure(state=tk.NORMAL if modules else tk.DISABLED)
            self._set_server_connected(True)
            self._set_status_text(f"Connected — VIN: {vin}")
            return

        self._set_server_connected(False)
        self._set_status_text(f"Connection failed: {self._error_message(payload, 'Unable to start diagnostics.')}")

    def _handle_dtcs_result(self, payload: dict[str, Any]) -> None:
        self._read_dtc_button.configure(state=tk.NORMAL)

        if not payload.get("success"):
            self._set_server_connected(False)
            self._set_status_text(f"Failed to read DTCs: {self._error_message(payload, 'Request failed.')}")
            return

        self._set_server_connected(True)

        for item_id in self._dtc_tree.get_children(""):
            self._dtc_tree.delete(item_id)

        dtcs = payload.get("dtcs") or []
        if not isinstance(dtcs, list):
            dtcs = []

        for dtc in dtcs:
            if not isinstance(dtc, dict):
                continue
            self._dtc_tree.insert(
                "",
                tk.END,
                values=(
                    dtc.get("code", ""),
                    dtc.get("module", ""),
                    dtc.get("status", ""),
                    dtc.get("description", ""),
                ),
            )

        count = payload.get("dtc_count")
        if not isinstance(count, int):
            count = len(dtcs)
        self._dtc_count_text.set(f"Found {count} fault code(s)")
        self._set_status_text("Fault code read completed.")

    def _handle_select_module_result(self, payload: dict[str, Any]) -> None:
        if payload.get("success"):
            categories = payload.get("data_categories") or []
            if not isinstance(categories, list):
                categories = []

            self._data_combo.configure(values=categories)
            self._selected_data_category.set(categories[0] if categories else "")
            self._start_stream_button.configure(state=tk.NORMAL if categories else tk.DISABLED)
            self._stop_stream_button.configure(state=tk.DISABLED)
            self._select_module_button.configure(state=tk.NORMAL)
            self._set_server_connected(True)
            self._set_status_text("Module selected. Choose a data category.")
            return

        self._set_server_connected(False)
        self._select_module_button.configure(state=tk.NORMAL)
        self._set_status_text(f"Module select failed: {self._error_message(payload, 'Request failed.')}")

    def _handle_live_start_result(self, payload: dict[str, Any]) -> None:
        if payload.get("success"):
            self._set_server_connected(True)
            self._stream_active = True
            self._start_stream_button.configure(state=tk.DISABLED)
            self._stop_stream_button.configure(state=tk.NORMAL)
            self._set_status_text(payload.get("message") or "Live stream started.")
            self._start_sse_thread()
            return

        self._stream_active = False
        self._set_server_connected(False)
        self._start_stream_button.configure(state=tk.NORMAL)
        self._stop_stream_button.configure(state=tk.DISABLED)
        self._set_status_text(f"Live stream failed: {self._error_message(payload, 'Request failed.')}")

    def _handle_live_stop_result(self, payload: dict[str, Any]) -> None:
        self._stop_sse_thread()
        self._stream_active = False

        self._start_stream_button.configure(state=tk.NORMAL)
        self._stop_stream_button.configure(state=tk.DISABLED)

        if payload.get("success"):
            self._set_server_connected(True)
            self._set_status_text(payload.get("message") or "Live stream stopped.")
            return

        self._set_server_connected(False)
        self._set_status_text(f"Stop failed: {self._error_message(payload, 'Request failed.')}")

    # ------------------------------------------------------------------
    # SSE streaming
    # ------------------------------------------------------------------

    def _start_sse_thread(self) -> None:
        """Start background thread consuming SSE events."""
        self._stop_sse_thread()
        self._sse_running = True

        def _sse_worker() -> None:
            url = f"{self._api_base}/api/diagnose/live_data/events"
            try:
                with requests.get(url, stream=True, timeout=None) as response:
                    self._sse_response = response
                    response.raise_for_status()

                    for raw_line in response.iter_lines(decode_unicode=True):
                        if not self._sse_running:
                            break
                        if not raw_line:
                            continue

                        line = raw_line.strip()
                        if not line.startswith("data: "):
                            continue

                        chunk = line[6:]
                        try:
                            payload = json.loads(chunk)
                            self._queue.put(("sse_snapshot", payload))
                        except json.JSONDecodeError:
                            continue
            except Exception as exc:
                if self._sse_running:
                    self._queue.put(("sse_error", {"error": str(exc)}))
            finally:
                self._sse_response = None

        self._sse_thread = threading.Thread(target=_sse_worker, daemon=True, name="diag-sse")
        self._sse_thread.start()

    def _stop_sse_thread(self) -> None:
        self._sse_running = False

        if self._sse_response is not None:
            try:
                self._sse_response.close()
            except Exception:
                pass
            self._sse_response = None

        if self._sse_thread and self._sse_thread.is_alive():
            self._sse_thread.join(timeout=1.5)
        self._sse_thread = None

    def _handle_sse_error(self, payload: dict[str, Any]) -> None:
        self._stop_sse_thread()
        self._stream_active = False
        self._set_server_connected(False)
        self._start_stream_button.configure(state=tk.NORMAL)
        self._stop_stream_button.configure(state=tk.DISABLED)
        self._set_status_text(f"Live stream error: {self._error_message(payload, 'Disconnected from server.')}")

    def _handle_sse_snapshot(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return

        event_type = str(payload.get("type", "")).strip().lower()
        if event_type == "connected":
            self._set_server_connected(True)
            self._set_status_text("Live stream connected.")
            return

        if event_type and event_type != "snapshot":
            return

        parameters = self._normalize_parameters(payload.get("parameters"))
        if not parameters:
            # Some payloads may include flattened data under `data`.
            parameters = self._normalize_parameters(payload.get("data"))

        if not parameters:
            return

        self._set_server_connected(True)
        self._set_status_text(f"Streaming live data — {len(parameters)} parameter(s)")
        self._update_live_data_rows(parameters)

    # ------------------------------------------------------------------
    # Live parameter normalization and table update
    # ------------------------------------------------------------------

    def _normalize_parameters(self, raw: Any) -> list[tuple[str, str, str]]:
        """Normalize API/SSE parameter payload into (name, value, unit) rows."""
        rows: list[tuple[str, str, str]] = []

        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    name = str(item.get("name") or item.get("parameter") or "").strip()
                    if not name:
                        continue

                    value = str(item.get("value") or "").strip()
                    unit = str(item.get("unit") or "").strip()

                    # Handle cases where `value` already includes units.
                    if value and not unit:
                        split_value, split_unit = self._split_value_unit(value)
                        value, unit = split_value, split_unit

                    rows.append((name, value, unit))

        elif isinstance(raw, dict):
            for name, combined in raw.items():
                key = str(name).strip()
                if not key:
                    continue

                if isinstance(combined, dict):
                    value = str(combined.get("value") or "").strip()
                    unit = str(combined.get("unit") or "").strip()
                else:
                    value_text = str(combined).strip()
                    value, unit = self._split_value_unit(value_text)

                rows.append((key, value, unit))

        return rows

    def _split_value_unit(self, text: str) -> tuple[str, str]:
        """Best-effort split for strings like '2450 rpm' or '92°C'."""
        value_text = text.strip()
        if not value_text:
            return "", ""

        # Case 1: explicit whitespace separator: "2450 rpm"
        spaced = value_text.rsplit(" ", 1)
        if len(spaced) == 2 and self._looks_numeric(spaced[0]):
            return spaced[0], spaced[1]

        # Case 2: no space: "92°C" or "14.3V"
        match = re.match(r"^([+-]?\d+(?:\.\d+)?)([a-zA-Z°%/]+)$", value_text)
        if match:
            return match.group(1), match.group(2)

        return value_text, ""

    def _looks_numeric(self, text: str) -> bool:
        return bool(re.match(r"^[+-]?\d+(?:\.\d+)?$", text.strip()))

    def _update_live_data_rows(self, rows: list[tuple[str, str, str]]) -> None:
        """Update tree rows in place to avoid UI flicker."""
        for name, value, unit in rows:
            existing_id = self._live_param_rows.get(name)
            if existing_id and self._live_tree.exists(existing_id):
                self._live_tree.item(existing_id, values=(name, value, unit))
            else:
                item_id = self._live_tree.insert("", tk.END, values=(name, value, unit))
                self._live_param_rows[name] = item_id
