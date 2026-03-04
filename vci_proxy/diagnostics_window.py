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

        self._ai_sse_running = False
        self._ai_sse_thread: Optional[threading.Thread] = None
        self._ai_sse_response: Optional[requests.Response] = None
        self._vin = ""
        self._cached_payload_id = ""

        # Session flow state
        self._session_id: Optional[str] = None
        self._session_sse_running = False
        self._session_sse_thread: Optional[threading.Thread] = None
        self._session_sse_response: Optional[requests.Response] = None
        self._session_decision_window: Optional[tk.Toplevel] = None
        self._session_category_confirmed = False

        self._live_param_rows: dict[str, str] = {}

        self._status_message = tk.StringVar(value="Ready")
        self._server_state_text = tk.StringVar(value=f"Server: {self._server_display}")
        self._dtc_count_text = tk.StringVar(value="Found 0 fault code(s)")

        self._selected_module = tk.StringVar(value="")
        self._selected_data_category = tk.StringVar(value="")
        self._session_brand = tk.StringVar(value="")
        self._session_status_var = tk.StringVar(value="")
        self._session_hint_var = tk.StringVar(
            value="Hint: Start Session 后按 Module -> Select -> Data Category -> Select。"
        )

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
        self._stop_ai_sse_thread()
        self._stop_session_sse_thread()
        self._close_decision_modal()

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
        width, height = 900, 850
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

        # Use ttk default font via style to avoid Tcl parsing issues with
        # family names containing spaces (e.g. "Segoe UI").
        style.configure(".", font=("Segoe UI", 10))

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

        root_frame.rowconfigure(4, weight=1)
        root_frame.rowconfigure(5, weight=1)
        root_frame.columnconfigure(0, weight=1)

        self._build_header(root_frame)
        self._build_start_section(root_frame)
        self._build_session_section(root_frame)
        self._build_dtc_section(root_frame)
        self._build_live_data_section(root_frame)
        self._build_ai_result_section(root_frame)

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

    def _build_session_section(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Session Diagnostics (New)", padding=(12, 6))
        frame.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Brand:", style="Subtle.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 6),
        )

        brand_entry = ttk.Entry(frame, textvariable=self._session_brand, width=18)
        brand_entry.grid(row=0, column=1, sticky="w", padx=(0, 12))

        self._session_start_button = ttk.Button(
            frame,
            text="\u25b6 Start Session",
            command=self._on_session_start_clicked,
        )
        self._session_start_button.grid(row=0, column=2, sticky="w", padx=(0, 12))

        self._session_abort_button = ttk.Button(
            frame,
            text="\u23f9 Abort",
            command=self._on_session_abort_clicked,
            state=tk.DISABLED,
        )
        self._session_abort_button.grid(row=0, column=3, sticky="w", padx=(0, 12))

        ttk.Label(frame, textvariable=self._session_status_var, style="Subtle.TLabel").grid(
            row=0, column=4, sticky="w",
        )

        ttk.Label(frame, textvariable=self._session_hint_var, style="Subtle.TLabel").grid(
            row=1, column=0, columnspan=5, sticky="w", pady=(6, 0),
        )

    def _build_dtc_section(self, parent: ttk.Frame) -> None:
        dtc_frame = ttk.LabelFrame(parent, text="Fault Codes (DTCs)", padding=12)
        dtc_frame.grid(row=3, column=0, sticky="nsew", pady=(0, 12))
        dtc_frame.columnconfigure(0, weight=1)
        dtc_frame.rowconfigure(1, weight=1)

        button_row = ttk.Frame(dtc_frame, style="Card.TFrame")
        button_row.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        button_row.columnconfigure(1, weight=1)

        ttk.Label(button_row, textvariable=self._dtc_count_text, style="Subtle.TLabel").grid(
            row=0,
            column=0,
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
        live_frame.grid(row=4, column=0, sticky="nsew")
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
        self._data_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_data_category_selected())

        self._select_data_category_button = ttk.Button(
            live_frame,
            text="Select",
            command=self._on_select_data_category_clicked,
            state=tk.DISABLED,
        )
        self._select_data_category_button.grid(row=1, column=2, sticky="w", pady=(0, 8))

        # Row 3: Primary controls
        primary_controls = ttk.Frame(live_frame, style="Card.TFrame")
        primary_controls.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 5))

        self._ai_diagnose_button = ttk.Button(
            primary_controls,
            text="✨ AI Diagnose",
            style="Big.TButton",
            command=self._on_ai_diagnose_clicked,
            state=tk.DISABLED,
        )
        self._ai_diagnose_button.grid(row=0, column=0, sticky="w")

        # Row 4: Secondary controls (Advanced)
        secondary_controls = ttk.Frame(live_frame, style="Card.TFrame")
        secondary_controls.grid(row=3, column=0, columnspan=3, sticky="w", pady=(0, 10))
        
        ttk.Label(secondary_controls, text="Advanced:", style="Subtle.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))

        self._read_dtc_button = ttk.Button(
            secondary_controls,
            text="Read DTCs",
            command=self._on_read_dtcs_clicked,
            state=tk.DISABLED,
        )
        self._read_dtc_button.grid(row=0, column=1, sticky="w", padx=(0, 8))

        self._start_stream_button = ttk.Button(
            secondary_controls,
            text="▶ Start Stream",
            command=self._on_start_stream_clicked,
            state=tk.DISABLED,
        )
        self._start_stream_button.grid(row=0, column=2, sticky="w", padx=(0, 8))

        self._stop_stream_button = ttk.Button(
            secondary_controls,
            text="⏹ Stop",
            command=self._on_stop_stream_clicked,
            state=tk.DISABLED,
        )
        self._stop_stream_button.grid(row=0, column=3, sticky="w")

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

    def _build_ai_result_section(self, parent: ttk.Frame) -> None:
        ai_frame = ttk.LabelFrame(parent, text="AI Diagnosis Result", padding=12)
        ai_frame.grid(row=5, column=0, sticky="nsew", pady=(12, 0))
        ai_frame.columnconfigure(0, weight=1)
        ai_frame.rowconfigure(1, weight=1)

        header_row = ttk.Frame(ai_frame, style="Card.TFrame")
        header_row.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        header_row.columnconfigure(1, weight=1)

        self._ai_status_text = tk.StringVar(value="Ready")
        ttk.Label(header_row, textvariable=self._ai_status_text, style="Status.TLabel").grid(row=0, column=0, sticky="w")

        self._ai_retry_button = ttk.Button(
            header_row,
            text="Retry Analysis",
            command=self._on_ai_retry_clicked,
        )
        self._ai_retry_button.grid(row=0, column=2, sticky="e")
        self._ai_retry_button.grid_remove() # Initially hidden

        self._ai_result_text = tk.Text(ai_frame, height=8, wrap=tk.WORD, font=("Segoe UI", 10), bg="#f9fafb", fg="#111827", state=tk.DISABLED)
        self._ai_result_text.grid(row=1, column=0, sticky="nsew")

        ai_scroll = ttk.Scrollbar(ai_frame, orient=tk.VERTICAL, command=self._ai_result_text.yview)
        ai_scroll.grid(row=1, column=1, sticky="ns")
        self._ai_result_text.configure(yscrollcommand=ai_scroll.set)
    # ------------------------------------------------------------------
    # Generic threaded API helpers
    # ------------------------------------------------------------------

    def _api_call(
        self,
        method: str,
        endpoint: str,
        json_data: Optional[dict[str, Any]] = None,
        query_params: Optional[dict[str, Any]] = None,
        callback_event: str = "api_result",
    ) -> None:
        """Make API call in background thread and post result to queue."""

        def _worker() -> None:
            url = f"{self._api_base}/{endpoint.lstrip('/')}"
            try:
                if method.upper() == "POST":
                    resp = requests.post(url, json=json_data, params=query_params, timeout=60)
                else:
                    resp = requests.get(url, params=query_params, timeout=60)

                try:
                    data = resp.json()
                except ValueError:
                    data = {
                        "success": False,
                        "error": f"Server returned non-JSON response (HTTP {resp.status_code}).",
                    }

                if not resp.ok and "success" not in data:
                    data = {
                        "success": False,
                        "error": data.get("error") or f"HTTP {resp.status_code} {resp.reason}",
                    }

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
        elif event == "session_select_module_result":
            self._handle_session_select_module_result(data)
        elif event == "session_select_data_category_result":
            self._handle_session_select_data_category_result(data)
        elif event == "live_start_result":
            self._handle_live_start_result(data)
        elif event == "live_stop_result":
            self._handle_live_stop_result(data)
        elif event == "sse_snapshot":
            self._handle_sse_snapshot(data)
        elif event == "sse_error":
            self._handle_sse_error(data)
        elif event == "ai_start_result":
            self._handle_ai_start_result(data)
        elif event == "ai_progress":
            self._handle_ai_progress(data)
        elif event == "ai_llm_chunk":
            self._handle_ai_llm_chunk(data)
        elif event == "ai_result":
            self._handle_ai_result(data)
        elif event == "ai_error":
            self._handle_ai_error(data)
        elif event == "ai_done":
            self._handle_ai_done(data)
        elif event == "session_start_result":
            self._handle_session_start_result(data)
        elif event == "session_start_exec_result":
            self._handle_session_start_exec_result(data)
        elif event == "session_connect_device_result":
            self._handle_session_connect_device_result(data)
        elif event == "session_connected":
            self._session_status_var.set("Connected. Waiting for events...")
        elif event == "session_progress":
            self._handle_session_progress(data)
        elif event == "session_decision_required":
            self._handle_session_decision_required(data)
        elif event == "session_decision_resolved":
            self._handle_session_decision_resolved(data)
        elif event == "session_decision_timeout":
            self._handle_session_decision_timeout(data)
        elif event == "session_error":
            self._handle_session_error(data)
        elif event == "session_done":
            self._handle_session_done(data)
        elif event == "session_decision_submit_result":
            self._handle_session_decision_submit_result(data)
        elif event == "session_abort_result":
            self._handle_session_abort_result(data)
    # ------------------------------------------------------------------
    # Header/status helpers
    # ------------------------------------------------------------------

    def _set_server_connected(self, is_connected: bool) -> None:
        self._server_dot.configure(style="DotOnline.TLabel" if is_connected else "DotOffline.TLabel")

    def _set_status_text(self, message: str) -> None:
        self._status_message.set(message)

    def _set_session_hint(self, message: str) -> None:
        self._session_hint_var.set(message)

    def _refresh_action_buttons(self) -> None:
        """Refresh module/category/diagnostic action buttons from current state."""
        has_module = bool(self._selected_module.get().strip())
        has_category = bool(self._selected_data_category.get().strip())
        session_mode = bool(self._session_id)

        can_select_module = has_module and not self._stream_active
        self._select_module_button.configure(
            state=tk.NORMAL if can_select_module else tk.DISABLED
        )

        can_select_category = has_category and session_mode and not self._stream_active
        self._select_data_category_button.configure(
            state=tk.NORMAL if can_select_category else tk.DISABLED
        )

        can_run_actions = has_category and not self._stream_active
        if session_mode and not self._session_category_confirmed:
            can_run_actions = False

        self._read_dtc_button.configure(
            state=tk.NORMAL if can_run_actions else tk.DISABLED
        )
        self._start_stream_button.configure(
            state=tk.NORMAL if can_run_actions else tk.DISABLED
        )
        self._ai_diagnose_button.configure(
            state=tk.NORMAL if can_run_actions else tk.DISABLED
        )

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
        self._select_data_category_button.configure(state=tk.DISABLED)
        self._start_stream_button.configure(state=tk.DISABLED)
        self._stop_stream_button.configure(state=tk.DISABLED)
        self._module_combo.configure(values=[])
        self._data_combo.configure(values=[])
        self._selected_module.set("")
        self._selected_data_category.set("")
        self._session_category_confirmed = False
        self._refresh_action_buttons()
        self._set_session_hint("Hint: 先 Start Diagnostics 或 Start Session，再进行选择。")

        # Session mode (new agentic path): keep GUI as simple as old one-click start.
        if self._session_id:
            self._set_session_hint("Session 模式：正在通过新 Agentic 路径启动诊断...")
            self._api_call(
                "POST",
                "/api/session/execute",
                json_data={
                    "session_id": self._session_id,
                    "action": "start_diagnostics",
                },
                callback_event="session_start_exec_result",
            )
            return

        self._api_call("POST", "/api/diagnose/start", callback_event="start_result")

    def _on_ai_diagnose_clicked(self) -> None:
        module = self._selected_module.get().strip()
        category = self._selected_data_category.get().strip()

        if not module:
            messagebox.showwarning("Module Required", "Please select a module first.")
            return

        if not category:
            messagebox.showwarning("Data Category Required", "Please select a data category.")
            return

        if self._session_id and not self._session_category_confirmed:
            messagebox.showwarning(
                "Category Not Confirmed",
                "In Session mode, please click Select next to Data Category before AI Diagnose.",
            )
            self._set_session_hint("请先提交 Data Category（点右侧 Select）再执行 AI Diagnose。")
            return

        self._ai_diagnose_button.configure(state=tk.DISABLED)
        self._start_stream_button.configure(state=tk.DISABLED)
        self._read_dtc_button.configure(state=tk.DISABLED)
        self._ai_retry_button.grid_remove()
        
        self._ai_status_text.set("Starting AI Diagnosis...")
        self._set_ai_result_text("")

        self._api_call(
            "POST",
            "/api/diagnose/ai_diagnose",
            json_data={"module": module, "data_category": category, "vin": self._vin},
            callback_event="ai_start_result",
        )

    def _on_ai_retry_clicked(self) -> None:
        if not self._cached_payload_id:
            return
            
        self._ai_diagnose_button.configure(state=tk.DISABLED)
        self._start_stream_button.configure(state=tk.DISABLED)
        self._read_dtc_button.configure(state=tk.DISABLED)
        self._ai_retry_button.grid_remove()
        
        self._ai_status_text.set("Retrying AI Diagnosis...")
        self._set_ai_result_text("")

        self._api_call(
            "POST",
            "/api/diagnose/ai_diagnose/retry",
            json_data={"cached_payload_id": self._cached_payload_id},
            callback_event="ai_start_result",
        )
    def _on_read_dtcs_clicked(self) -> None:
        category = self._selected_data_category.get().strip()

        if not category:
            messagebox.showwarning("Data Category Required", "Please select a data category first.")
            return

        self._read_dtc_button.configure(state=tk.DISABLED)
        self._set_status_text("Reading fault codes...")
        self._api_call(
            "GET",
            "/api/diagnose/dtcs",
            query_params={"data_category": category},
            callback_event="dtcs_result",
        )

    def _on_select_module_clicked(self) -> None:
        module = self._selected_module.get().strip()
        if not module:
            messagebox.showwarning("Module Required", "Please select a module first.")
            return

        self._select_module_button.configure(state=tk.DISABLED)
        self._start_stream_button.configure(state=tk.DISABLED)
        self._read_dtc_button.configure(state=tk.DISABLED)
        self._data_combo.configure(values=[])
        self._selected_data_category.set("")
        self._set_status_text("Selecting module...")
        if self._session_id:
            self._set_session_hint("正在提交 Module 选择到 Session...")
            self._api_call(
                "POST",
                "/api/session/select_module",
                json_data={"session_id": self._session_id, "module": module},
                callback_event="session_select_module_result",
            )
        else:
            self._api_call(
                "POST",
                "/api/diagnose/select_module",
                json_data={"module": module},
                callback_event="module_result",
            )

    def _on_select_data_category_clicked(self) -> None:
        category = self._selected_data_category.get().strip()
        if not category:
            messagebox.showwarning("Data Category Required", "Please select a data category first.")
            return

        if self._session_id and not self._session_category_confirmed:
            messagebox.showwarning(
                "Category Not Confirmed",
                "In Session mode, please click Select next to Data Category first.",
            )
            self._set_session_hint("请先提交 Data Category（点右侧 Select）再执行 Read DTCs。")
            return

        if not self._session_id:
            self._set_status_text("Session mode not active. Use Start Session first.")
            self._set_session_hint("先点击 Start Session，再进行 Category 提交。")
            return

        self._select_data_category_button.configure(state=tk.DISABLED)
        self._start_stream_button.configure(state=tk.DISABLED)
        self._read_dtc_button.configure(state=tk.DISABLED)
        self._ai_diagnose_button.configure(state=tk.DISABLED)
        self._set_status_text("Selecting data category in session...")
        self._set_session_hint("正在提交 Data Category 选择到 Session...")
        self._api_call(
            "POST",
            "/api/session/select_data_category",
            json_data={"session_id": self._session_id, "data_category": category},
            callback_event="session_select_data_category_result",
        )

    def _on_start_stream_clicked(self) -> None:
        module = self._selected_module.get().strip()
        category = self._selected_data_category.get().strip()

        if not module:
            messagebox.showwarning("Module Required", "Please select a module first.")
            return

        if not category:
            messagebox.showwarning("Data Category Required", "Please select a data category.")
            return

        if self._session_id and not self._session_category_confirmed:
            messagebox.showwarning(
                "Category Not Confirmed",
                "In Session mode, please click Select next to Data Category before Start Stream.",
            )
            self._set_session_hint("请先提交 Data Category（点右侧 Select）再执行 Start Stream。")
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

    def _on_data_category_selected(self) -> None:
        """Enable actions only after user selects a data category."""
        has_category = bool(self._selected_data_category.get().strip())
        session_mode = bool(self._session_id)
        if session_mode:
            # In session mode, category must be confirmed via /api/session/select_data_category.
            self._session_category_confirmed = False
        self._refresh_action_buttons()
        if session_mode and has_category:
            self._set_session_hint("已选择 Data Category，请点击右侧 Select 提交到 Session。")
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
            self._data_combo.configure(values=[])
            self._selected_data_category.set("")
            self._session_category_confirmed = False

            self._stop_stream_button.configure(state=tk.DISABLED)
            self._refresh_action_buttons()
            self._set_server_connected(True)
            self._set_status_text(f"Connected — VIN: {vin}. Select module and data category.")
            self._set_session_hint("Hint: 选择 Module 后点 Select，再选择 Data Category。")
            self._vin = vin
            return

        self._set_server_connected(False)
        self._refresh_action_buttons()
        self._set_status_text(f"Connection failed: {self._error_message(payload, 'Unable to start diagnostics.')}")

    def _handle_session_start_exec_result(self, payload: dict[str, Any]) -> None:
        """Handle session-mode start diagnostics via /api/session/execute."""
        if not payload.get("success"):
            error_text = self._error_message(payload, "Unable to start diagnostics in session mode.")

            # Common runtime case: already at vehicle_selection; continue by connect_device(default).
            if self._session_id and "start_diagnostics is not allowed on page vehicle_selection" in error_text:
                self._set_status_text("Detected Vehicle Selection. Continuing connect flow...")
                self._set_session_hint("检测到已在 Vehicle Selection，正在自动继续连接流程。")
                self._api_call(
                    "POST",
                    "/api/session/execute",
                    json_data={
                        "session_id": self._session_id,
                        "action": "connect_device",
                        "args": {"device_name": "default"},
                    },
                    callback_event="session_connect_device_result",
                )
                return

            self._start_button.configure(state=tk.NORMAL)
            self._set_server_connected(False)
            self._refresh_action_buttons()
            self._set_status_text(f"Session start failed: {error_text}")
            self._set_session_hint("Session 启动失败，请确认 GDS2 页面后重试。")
            return

        result = payload.get("result") or {}
        modules = result.get("modules") or []
        devices = result.get("devices") or []

        if isinstance(modules, list) and modules:
            vin = result.get("vin") or self._vin or "Unknown"

            self._module_combo.configure(values=modules)
            self._selected_module.set(modules[0])
            self._data_combo.configure(values=[])
            self._selected_data_category.set("")
            self._session_category_confirmed = False

            self._stop_stream_button.configure(state=tk.DISABLED)
            self._refresh_action_buttons()
            self._set_server_connected(True)
            self._set_status_text(f"Session ready — VIN: {vin}. Select module and data category.")
            self._set_session_hint("Session 已就绪：选择 Module -> Select，再选择 Category -> Select。")
            self._start_button.configure(state=tk.NORMAL)
            if vin != "Unknown":
                self._vin = vin
            return

        if isinstance(devices, list) and devices:
            preferred = "VCI Proxy (Remote)"
            selected_device = preferred if preferred in devices else devices[0]
            self._set_status_text(f"Found {len(devices)} device(s). Auto-connecting {selected_device}...")
            self._set_session_hint("Session 模式：正在自动连接设备。")
            self._api_call(
                "POST",
                "/api/session/execute",
                json_data={
                    "session_id": self._session_id,
                    "action": "connect_device",
                    "args": {"device_name": selected_device},
                },
                callback_event="session_connect_device_result",
            )
            return

        self._start_button.configure(state=tk.NORMAL)
        self._set_server_connected(False)
        self._refresh_action_buttons()
        self._set_status_text("Session start returned no modules or devices.")
        self._set_session_hint("后端返回缺少模块/设备信息，请重试或检查日志。")

    def _handle_session_connect_device_result(self, payload: dict[str, Any]) -> None:
        """Handle connect_device result in session mode and populate module list."""
        self._start_button.configure(state=tk.NORMAL)

        if not payload.get("success"):
            self._set_server_connected(False)
            self._refresh_action_buttons()
            self._set_status_text(
                f"Session connect failed: {self._error_message(payload, 'Unable to connect device.')}"
            )
            self._set_session_hint("设备连接失败，请检查 VCI Proxy 与 GDS2 状态后重试。")
            return

        result = payload.get("result") or {}
        modules = result.get("modules") or []
        vin = result.get("vin") or self._vin or "Unknown"

        if not isinstance(modules, list) or not modules:
            self._set_server_connected(False)
            self._refresh_action_buttons()
            self._set_status_text("Session connected but no module list returned.")
            self._set_session_hint("连接成功但模块列表为空，请重试 Start Diagnostics。")
            return

        self._module_combo.configure(values=modules)
        self._selected_module.set(modules[0])
        self._data_combo.configure(values=[])
        self._selected_data_category.set("")
        self._session_category_confirmed = False

        self._stop_stream_button.configure(state=tk.DISABLED)
        self._refresh_action_buttons()
        self._set_server_connected(True)
        self._set_status_text(f"Connected — VIN: {vin}. Select module and data category.")
        self._set_session_hint("模块列表已加载：请选择 Module 并点击 Select。")
        if vin != "Unknown":
            self._vin = vin

    def _handle_dtcs_result(self, payload: dict[str, Any]) -> None:
        can_read = bool(self._selected_data_category.get().strip()) and not self._stream_active
        self._read_dtc_button.configure(state=tk.NORMAL if can_read else tk.DISABLED)

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
            self._selected_data_category.set("")
            self._session_category_confirmed = False
            self._stop_stream_button.configure(state=tk.DISABLED)
            self._refresh_action_buttons()
            self._set_server_connected(True)
            self._set_status_text("Module selected. Choose a data category.")
            self._set_session_hint("Hint: 选择 Data Category 后可直接开始 DTC/Stream/AI。")
            return

        self._set_server_connected(False)
        self._refresh_action_buttons()
        self._set_status_text(f"Module select failed: {self._error_message(payload, 'Request failed.')}")

    def _handle_session_select_module_result(self, payload: dict[str, Any]) -> None:
        self._refresh_action_buttons()

        if not payload.get("success"):
            self._set_server_connected(False)
            self._set_status_text(
                f"Session module select failed: {self._error_message(payload, 'Request failed.')}"
            )
            self._set_session_hint("Module 选择失败，请重试或检查 Session 状态。")
            return

        if payload.get("decision_required"):
            decision = payload.get("decision")
            if decision:
                self._show_decision_modal(decision)
            self._session_status_var.set("Module selection requires your decision.")
            self._set_status_text("Session awaiting module decision...")
            self._set_session_hint("Module 存在多个候选，请在弹窗中选择。")
            return

        result = payload.get("result") or {}
        categories = result.get("data_categories") or []
        if not isinstance(categories, list):
            categories = []

        self._data_combo.configure(values=categories)
        self._selected_data_category.set("")
        self._session_category_confirmed = False
        self._select_data_category_button.configure(state=tk.DISABLED)
        self._start_stream_button.configure(state=tk.DISABLED)
        self._read_dtc_button.configure(state=tk.DISABLED)
        self._ai_diagnose_button.configure(state=tk.DISABLED)
        self._set_server_connected(True)
        self._set_status_text("Session module selected. Choose a data category.")
        self._set_session_hint("下一步：选择 Data Category 后点击右侧 Select。")
        self._refresh_action_buttons()

    def _handle_session_select_data_category_result(self, payload: dict[str, Any]) -> None:
        self._refresh_action_buttons()

        if not payload.get("success"):
            self._set_server_connected(False)
            self._set_status_text(
                f"Session category select failed: {self._error_message(payload, 'Request failed.')}"
            )
            self._set_session_hint("Data Category 选择失败，请重试。")
            return

        if payload.get("decision_required"):
            decision = payload.get("decision")
            if decision:
                self._show_decision_modal(decision)
            self._session_status_var.set("Data category selection requires your decision.")
            self._set_status_text("Session awaiting category decision...")
            self._set_session_hint("Category 存在多个候选，请在弹窗中选择。")
            return

        self._set_server_connected(True)
        self._set_status_text("Session data category selected. Ready for DTC/Stream/AI Diagnose.")
        self._session_category_confirmed = True
        has_category = bool(self._selected_data_category.get().strip())
        self._read_dtc_button.configure(state=tk.NORMAL if has_category else tk.DISABLED)
        self._start_stream_button.configure(state=tk.NORMAL if has_category else tk.DISABLED)
        self._ai_diagnose_button.configure(state=tk.NORMAL if has_category else tk.DISABLED)
        self._set_session_hint("已确认 Category：现在可执行 Read DTCs / Start Stream / AI Diagnose。")
        self._refresh_action_buttons()

    def _handle_live_start_result(self, payload: dict[str, Any]) -> None:
        if payload.get("success"):
            self._set_server_connected(True)
            self._stream_active = True
            self._refresh_action_buttons()
            self._stop_stream_button.configure(state=tk.NORMAL)
            self._set_status_text(payload.get("message") or "Live stream started.")
            self._start_sse_thread()
            return

        self._stream_active = False
        self._set_server_connected(False)
        self._refresh_action_buttons()
        self._stop_stream_button.configure(state=tk.DISABLED)
        self._set_status_text(f"Live stream failed: {self._error_message(payload, 'Request failed.')}")

    def _handle_live_stop_result(self, payload: dict[str, Any]) -> None:
        self._stop_sse_thread()
        self._stream_active = False

        self._refresh_action_buttons()
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
        self._ai_diagnose_button.configure(state=tk.NORMAL)
        self._read_dtc_button.configure(state=tk.NORMAL)
        self._stop_stream_button.configure(state=tk.DISABLED)
        self._set_status_text(f"Live stream error: {self._error_message(payload, 'Disconnected from server.')}")

    # ------------------------------------------------------------------
    # AI SSE streaming and handlers
    # ------------------------------------------------------------------

    def _set_ai_result_text(self, text: str) -> None:
        self._ai_result_text.configure(state=tk.NORMAL)
        self._ai_result_text.delete("1.0", tk.END)
        if text:
            self._ai_result_text.insert(tk.END, text)
        self._ai_result_text.configure(state=tk.DISABLED)
        self._ai_result_text.see(tk.END)

    def _append_ai_result_text(self, text: str) -> None:
        self._ai_result_text.configure(state=tk.NORMAL)
        self._ai_result_text.insert(tk.END, text)
        self._ai_result_text.configure(state=tk.DISABLED)
        self._ai_result_text.see(tk.END)

    def _handle_ai_start_result(self, payload: dict[str, Any]) -> None:
        if payload.get("success"):
            session_id = payload.get("session_id")
            if session_id:
                self._ai_status_text.set("AI Diagnosis started. Waiting for events...")
                self._start_ai_sse_thread(session_id)
            else:
                self._ai_status_text.set("Error: No session_id returned.")
                self._ai_diagnose_button.configure(state=tk.NORMAL)
                self._start_stream_button.configure(state=tk.NORMAL)
                self._read_dtc_button.configure(state=tk.NORMAL)
        else:
            self._ai_status_text.set(f"Failed to start AI Diagnosis: {self._error_message(payload, 'Request failed.')}")
            self._ai_diagnose_button.configure(state=tk.NORMAL)
            self._start_stream_button.configure(state=tk.NORMAL)
            self._read_dtc_button.configure(state=tk.NORMAL)

    def _handle_ai_progress(self, payload: dict[str, Any]) -> None:
        message = payload.get("message", "Processing...")
        self._ai_status_text.set(message)

    def _handle_ai_llm_chunk(self, payload: dict[str, Any]) -> None:
        chunk = payload.get("text", "")
        if chunk:
            self._append_ai_result_text(chunk)

    def _handle_ai_result(self, payload: dict[str, Any]) -> None:
        self._cached_payload_id = payload.get("cached_payload_id", "")

        verdict_data = payload.get("verdict")
        raw_response = payload.get("raw_response", "")

        # If LLM chunks were already streamed, the text widget has content.
        # If not (e.g. non-stream fallback), show raw_response as base text.
        current_text = self._ai_result_text.get("1.0", tk.END).strip()
        if not current_text and raw_response:
            self._set_ai_result_text(raw_response)

        # Build structured verdict summary
        if isinstance(verdict_data, dict) and verdict_data:
            verdict = verdict_data.get("verdict", "Unknown")
            confidence = verdict_data.get("confidence", "Unknown")
            findings = verdict_data.get("findings", [])
            recommended_action = verdict_data.get("recommended_action", "None")
            ai_summary = verdict_data.get("summary", "")

            summary = "\n\n--- FINAL VERDICT ---\n"
            summary += f"Verdict: {verdict}\n"
            summary += f"Confidence: {confidence}\n"
            if ai_summary:
                summary += f"Summary: {ai_summary}\n"
            summary += "Findings:\n"
            for finding in findings:
                if isinstance(finding, dict):
                    dtc = finding.get('dtc', 'N/A')
                    severity = finding.get('severity', 'N/A')
                    analysis = finding.get('analysis', '')
                    summary += f"  [{severity.upper()}] {dtc}: {analysis}\n"
                else:
                    summary += f"  - {finding}\n"
            summary += f"Recommended Action: {recommended_action}\n"
            self._append_ai_result_text(summary)
        else:
            # parse_verdict failed — raw_response is already displayed
            self._append_ai_result_text("\n\n[Note: Could not parse structured verdict from AI response.]\n")

        self._ai_status_text.set("AI Diagnosis Complete.")

    def _handle_ai_error(self, payload: dict[str, Any]) -> None:
        error_msg = payload.get("error", "Unknown error")
        self._ai_status_text.set(f"Error: {error_msg}")
        self._append_ai_result_text(f"\n\n[Error: {error_msg}]")
        
        self._cached_payload_id = payload.get("cached_payload_id", "")
        is_retryable = payload.get("retryable", False)
        
        if is_retryable and self._cached_payload_id:
            self._ai_retry_button.grid()

    def _handle_ai_done(self, payload: dict[str, Any]) -> None:
        self._stop_ai_sse_thread()
        self._ai_diagnose_button.configure(state=tk.NORMAL)
        self._start_stream_button.configure(state=tk.NORMAL)
        self._read_dtc_button.configure(state=tk.NORMAL)

    def _start_ai_sse_thread(self, session_id: str) -> None:
        self._stop_ai_sse_thread()
        self._ai_sse_running = True

        def _ai_sse_worker() -> None:
            url = f"{self._api_base}/api/diagnose/ai_diagnose/events?session_id={session_id}"
            try:
                with requests.get(url, stream=True, timeout=(10, 300)) as response:
                    self._ai_sse_response = response
                    response.raise_for_status()

                    current_event = None
                    for raw_line in response.iter_lines(decode_unicode=True):
                        if not self._ai_sse_running:
                            break
                        if not raw_line:
                            continue

                        line = raw_line.strip()
                        if line.startswith("event: "):
                            current_event = line[7:]
                        elif line.startswith("data: "):
                            chunk = line[6:]
                            try:
                                payload = json.loads(chunk)
                                if current_event:
                                    self._queue.put((f"ai_{current_event}", payload))
                            except json.JSONDecodeError:
                                continue
            except Exception as exc:
                if self._ai_sse_running:
                    self._queue.put(("ai_error", {"error": str(exc)}))
            finally:
                self._ai_sse_response = None
                if self._ai_sse_running:
                    self._queue.put(("ai_done", {}))

        self._ai_sse_thread = threading.Thread(target=_ai_sse_worker, daemon=True, name="diag-ai-sse")
        self._ai_sse_thread.start()

    def _stop_ai_sse_thread(self) -> None:
        self._ai_sse_running = False

        if self._ai_sse_response is not None:
            try:
                self._ai_sse_response.close()
            except Exception:
                pass
            self._ai_sse_response = None

        if self._ai_sse_thread and self._ai_sse_thread.is_alive():
            self._ai_sse_thread.join(timeout=1.5)
        self._ai_sse_thread = None
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
        extraction_count = payload.get("extraction_count")
        change_count = payload.get("param_changes")
        if isinstance(change_count, list):
            changed = len(change_count)
        else:
            changed = 0

        if isinstance(extraction_count, int):
            self._set_status_text(
                f"Streaming #{extraction_count} — {len(parameters)} parameter(s), {changed} changed"
            )
        else:
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

    # ------------------------------------------------------------------
    # Session flow: UI callbacks
    # ------------------------------------------------------------------

    def _on_session_start_clicked(self) -> None:
        brand = self._session_brand.get().strip()
        if not brand:
            messagebox.showwarning("Brand Required", "Please enter a vehicle brand.")
            return

        self._session_start_button.configure(state=tk.DISABLED)
        self._session_status_var.set("Starting session...")
        self._api_call(
            "POST",
            "/api/session/start",
            json_data={"brand": brand},
            callback_event="session_start_result",
        )

    def _on_session_abort_clicked(self) -> None:
        if not self._session_id:
            return
        self._session_abort_button.configure(state=tk.DISABLED)
        self._session_status_var.set("Aborting...")
        self._api_call(
            "POST",
            "/api/session/abort",
            json_data={"session_id": self._session_id},
            callback_event="session_abort_result",
        )

    # ------------------------------------------------------------------
    # Session flow: event handlers
    # ------------------------------------------------------------------

    def _handle_session_start_result(self, payload: dict[str, Any]) -> None:
        if not payload.get("success"):
            self._session_start_button.configure(state=tk.NORMAL)
            self._session_status_var.set(
                f"Failed: {self._error_message(payload, 'Could not start session.')}"
            )
            self._set_session_hint("Hint: 请输入品牌后重试 Start Session。")
            return

        self._session_id = payload.get("session_id", "")
        status = payload.get("status", "")
        workflow = payload.get("workflow")
        sid_preview = (self._session_id or "")[:8]

        self._session_start_button.configure(state=tk.DISABLED)
        self._session_abort_button.configure(state=tk.NORMAL)
        self._session_category_confirmed = False
        self._refresh_action_buttons()
        self._session_status_var.set(
            f"Session {sid_preview}... status={status}"
            + (f" workflow={workflow}" if workflow else "")
        )
        self._set_session_hint("Session 已启动：1) 选 Module 并点 Select；2) 选 Category 并点 Select。")

        # If the start response already includes a decision, show it
        decision = payload.get("decision")
        if decision:
            self._show_decision_modal(decision)

        # Start SSE listener
        if self._session_id:
            self._start_session_sse_thread(self._session_id)

    def _handle_session_progress(self, payload: dict[str, Any]) -> None:
        message = payload.get("message", "Processing...")
        workflow = payload.get("workflow")
        text = message
        if workflow:
            text += f" [{workflow}]"
        self._session_status_var.set(text)

    def _handle_session_decision_required(self, payload: dict[str, Any]) -> None:
        decision = payload.get("decision")
        if decision:
            self._show_decision_modal(decision)
            self._set_session_hint("出现歧义，请在弹窗中选择一个候选项。")
        else:
            self._session_status_var.set("Decision required but no details received.")

    def _handle_session_decision_resolved(self, payload: dict[str, Any]) -> None:
        self._close_decision_modal()
        option_id = payload.get("option_id", "?")
        self._session_status_var.set(f"Decision resolved: {option_id}")
        self._set_session_hint("决策已提交，系统正在继续执行。")

    def _handle_session_decision_timeout(self, payload: dict[str, Any]) -> None:
        self._close_decision_modal()
        message = payload.get("message") or "Decision timed out. Applying fallback option."
        fallback = payload.get("fallback_option")
        if fallback:
            message = f"{message} [{fallback}]"
        self._session_status_var.set(message)
        self._set_session_hint("未及时选择，系统已按兜底选项继续。")

    def _handle_session_error(self, payload: dict[str, Any]) -> None:
        error = payload.get("error", "Unknown error")
        self._session_status_var.set(f"Session error: {error}")
        self._set_session_hint("Session 发生错误，请检查网络或重新 Start Session。")

    def _handle_session_done(self, payload: dict[str, Any]) -> None:
        self._stop_session_sse_thread()
        self._close_decision_modal()
        aborted = payload.get("aborted", False)
        if aborted:
            reason = payload.get("reason", "")
            self._session_status_var.set(f"Session aborted. {reason}".strip())
        else:
            self._session_status_var.set("Session completed.")
        self._session_start_button.configure(state=tk.NORMAL)
        self._session_abort_button.configure(state=tk.DISABLED)
        self._select_data_category_button.configure(state=tk.DISABLED)
        self._session_category_confirmed = False
        self._session_id = None
        self._refresh_action_buttons()
        self._set_session_hint("Session 已结束。可重新 Start Session。")

    def _handle_session_decision_submit_result(self, payload: dict[str, Any]) -> None:
        if payload.get("success"):
            if payload.get("decision_required"):
                decision = payload.get("decision")
                if decision:
                    self._show_decision_modal(decision)
                self._session_status_var.set("More decisions required...")
                self._set_session_hint("仍有歧义，请继续在弹窗中选择。")
                return

            if payload.get("resumed"):
                resume_action = payload.get("resume_action", "")
                result = payload.get("result") or {}

                if resume_action == "select_module":
                    categories = result.get("data_categories") or []
                    if not isinstance(categories, list):
                        categories = []
                    self._data_combo.configure(values=categories)
                    self._selected_data_category.set("")
                    self._session_category_confirmed = False
                    self._select_data_category_button.configure(state=tk.DISABLED)
                    self._set_status_text("Decision applied. Module resolved; choose data category.")
                    self._set_session_hint("Module 已确定。请选择 Data Category 并点击 Select。")
                elif resume_action == "select_data_category":
                    self._session_category_confirmed = True
                    has_category = bool(self._selected_data_category.get().strip())
                    self._read_dtc_button.configure(state=tk.NORMAL if has_category else tk.DISABLED)
                    self._start_stream_button.configure(state=tk.NORMAL if has_category else tk.DISABLED)
                    self._ai_diagnose_button.configure(state=tk.NORMAL if has_category else tk.DISABLED)
                    self._set_status_text("Decision applied. Data category resolved.")
                    self._set_session_hint("Category 已确定。现在可执行诊断动作。")

                self._session_status_var.set("Decision applied. Continuing...")
                self._refresh_action_buttons()
                return

            self._session_status_var.set("Decision submitted. Continuing...")
            self._set_session_hint("决策已提交，等待后续进度事件。")
        else:
            self._session_status_var.set(
                f"Decision failed: {self._error_message(payload, 'Request failed.')}"
            )
            self._set_session_hint("决策提交失败，请重试。")
            # Re-enable submit button if modal is still open
            if (self._session_decision_window is not None
                    and self._session_decision_window.winfo_exists()):
                for child in self._session_decision_window.winfo_children():
                    if isinstance(child, ttk.Button):
                        child.configure(state=tk.NORMAL)

    def _handle_session_abort_result(self, payload: dict[str, Any]) -> None:
        if payload.get("success"):
            self._session_status_var.set("Abort request sent.")
            self._set_session_hint("正在结束 Session...")
        else:
            self._session_abort_button.configure(state=tk.NORMAL)
            self._session_status_var.set(
                f"Abort failed: {self._error_message(payload, 'Request failed.')}"
            )
            self._set_session_hint("Abort 失败，请重试。")

    # ------------------------------------------------------------------
    # Session flow: decision modal
    # ------------------------------------------------------------------

    def _show_decision_modal(self, decision: dict[str, Any]) -> None:
        self._close_decision_modal()

        prompt = decision.get("prompt", "Please make a selection:")
        decision_id = decision.get("decision_id", "")
        options = decision.get("options", [])

        win = tk.Toplevel(self._root)
        win.title("Decision Required")
        win.configure(bg="white")
        win.resizable(False, False)
        win.transient(self._root)
        win.grab_set()

        # Center relative to main window
        win.update_idletasks()
        x = self._root.winfo_x() + (self._root.winfo_width() - 400) // 2
        y = self._root.winfo_y() + (self._root.winfo_height() - 250) // 2
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")

        self._session_decision_window = win

        frame = ttk.Frame(win, style="App.TFrame", padding=18)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text=prompt, wraplength=380, style="Subtle.TLabel").pack(
            anchor="w", pady=(0, 12),
        )

        selected_option = tk.StringVar(value="")

        for opt in options:
            opt_id = opt.get("option_id", "")
            label = opt.get("label", opt_id)
            desc = opt.get("description", "")
            display = f"{label} \u2014 {desc}" if desc else label
            rb = ttk.Radiobutton(frame, text=display, variable=selected_option, value=opt_id)
            rb.pack(anchor="w", pady=2)

        if options:
            selected_option.set(options[0].get("option_id", ""))

        btn_frame = ttk.Frame(frame, style="Card.TFrame")
        btn_frame.pack(anchor="e", pady=(12, 0))

        def _submit() -> None:
            choice = selected_option.get()
            if not choice:
                messagebox.showwarning(
                    "Selection Required", "Please select an option.", parent=win,
                )
                return
            submit_btn.configure(state=tk.DISABLED)
            self._session_submit_decision(decision_id, choice)

        submit_btn = ttk.Button(btn_frame, text="Submit", command=_submit)
        submit_btn.pack(side=tk.RIGHT, padx=(8, 0))

        ttk.Button(
            btn_frame, text="Cancel", command=self._close_decision_modal,
        ).pack(side=tk.RIGHT)

        self._session_status_var.set("Awaiting your decision...")
        self._set_session_hint("请选择最符合当前车辆的候选项并提交。")

    def _close_decision_modal(self) -> None:
        if self._session_decision_window is not None:
            try:
                self._session_decision_window.destroy()
            except tk.TclError:
                pass
            self._session_decision_window = None

    def _session_submit_decision(self, decision_id: str, option_id: str) -> None:
        if not self._session_id:
            return
        self._api_call(
            "POST",
            "/api/session/decision",
            json_data={
                "session_id": self._session_id,
                "decision_id": decision_id,
                "option_id": option_id,
            },
            callback_event="session_decision_submit_result",
        )

    # ------------------------------------------------------------------
    # Session flow: SSE thread
    # ------------------------------------------------------------------

    def _start_session_sse_thread(self, session_id: str) -> None:
        """Start background thread consuming session SSE events."""
        self._stop_session_sse_thread()
        self._session_sse_running = True

        def _session_sse_worker() -> None:
            url = f"{self._api_base}/api/session/events?session_id={session_id}"
            try:
                with requests.get(url, stream=True, timeout=(10, None)) as response:
                    self._session_sse_response = response
                    response.raise_for_status()

                    current_event = None
                    for raw_line in response.iter_lines(decode_unicode=True):
                        if not self._session_sse_running:
                            break
                        if not raw_line:
                            continue

                        line = raw_line.strip()
                        if line.startswith(":"):
                            continue  # keepalive
                        if line.startswith("event: "):
                            current_event = line[7:]
                        elif line.startswith("data: "):
                            chunk = line[6:]
                            try:
                                payload = json.loads(chunk)
                                if current_event:
                                    self._queue.put(
                                        (f"session_{current_event}", payload)
                                    )
                            except json.JSONDecodeError:
                                continue
            except Exception as exc:
                if self._session_sse_running:
                    self._queue.put(("session_error", {"error": str(exc)}))
            finally:
                self._session_sse_response = None

        self._session_sse_thread = threading.Thread(
            target=_session_sse_worker, daemon=True, name="diag-session-sse",
        )
        self._session_sse_thread.start()

    def _stop_session_sse_thread(self) -> None:
        self._session_sse_running = False

        if self._session_sse_response is not None:
            try:
                self._session_sse_response.close()
            except Exception:
                pass
            self._session_sse_response = None

        if self._session_sse_thread and self._session_sse_thread.is_alive():
            self._session_sse_thread.join(timeout=1.5)
        self._session_sse_thread = None
