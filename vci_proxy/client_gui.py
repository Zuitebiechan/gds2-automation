"""
VCI Proxy Client GUI — System tray application for VCI Proxy reverse client.

Provides:
- System tray icon with connection status (green/yellow/red)
- First-run configuration dialog (server address, port, auth token)
- Background asyncio client with auto-reconnect
- Config persistence in %APPDATA%/VCI_Proxy/config.json
"""

import asyncio
import json
import logging
import os
import queue
import socket
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from PIL import Image, ImageDraw

try:
    import pystray
except ImportError:
    print("ERROR: pystray not installed. Run: pip install pystray Pillow")
    sys.exit(1)

from vci_proxy.reverse_client import ReverseProxyClient
from vci_proxy.config import ProxyConfig
from vci_proxy.observability_outbox import ObservabilityOutbox
from diagnostic_platform.observability_artifacts import (
    cleanup_product_observability,
    resolve_product_log_settings,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------

CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "VCI_Proxy"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_CONFIG = {
    "api_scheme": "http",
    "host": "",
    "port": 9000,
    "api_port": 8080,
    "api_token": "",
    "auth_token": "",
    "dll_path": "",
    "tls_enabled": False,
    "tls_ca_file": "",
    "tls_server_name": "",
}


def format_driver_label(
    driver: dict[str, Any],
    *,
    python_arch: str | None = None,
    include_path: bool = False,
) -> str:
    """Build one user-facing label for a discovered J2534 driver."""
    label = str(driver.get("name") or driver.get("dll_path") or "Unknown driver")
    vendor = str(driver.get("vendor") or "").strip()
    if vendor:
        label += f" ({vendor})"

    architecture = str(driver.get("architecture") or "").strip().lower()
    if architecture and architecture != "unknown":
        label += f" [{architecture}]"

    dll_path = str(driver.get("dll_path") or "").strip()
    if include_path and dll_path:
        label += f" - {dll_path}"

    return label


def normalize_config(config: dict | None) -> dict:
    """Merge partial config values with defaults for forward compatibility."""
    return {**DEFAULT_CONFIG, **(config or {})}


def load_config() -> dict:
    """Load config from %APPDATA%/VCI_Proxy/config.json."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            return normalize_config(saved)
        except (json.JSONDecodeError, OSError):
            pass
    return normalize_config(None)


def save_config(cfg: dict) -> None:
    """Save config to %APPDATA%/VCI_Proxy/config.json."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(normalize_config(cfg), f, indent=2)


# ---------------------------------------------------------------------------
# Tray icon image generation
# ---------------------------------------------------------------------------

def _create_icon(color: str, size: int = 64) -> Image.Image:
    """Create a simple filled circle icon for the system tray."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = 4
    draw.ellipse(
        [margin, margin, size - margin, size - margin],
        fill=color,
        outline=(80, 80, 80),
        width=2,
    )
    return img


ICONS = {
    "connected": _create_icon("#22c55e"),      # green
    "connecting": _create_icon("#eab308"),      # yellow
    "disconnected": _create_icon("#ef4444"),    # red
    "error": _create_icon("#ef4444"),           # red
    "idle": _create_icon("#9ca3af"),            # gray
}

STATUS_LABELS = {
    "connected": "Connected",
    "connecting": "Connecting...",
    "disconnected": "Disconnected",
    "error": "Error",
    "idle": "Not started",
}

STARTUP_NOTIFICATION_TITLE = "VCI Proxy"
STARTUP_NOTIFICATION_MESSAGE = (
    "VCI Proxy is running in the system tray. "
    "Right-click the tray icon to open Settings or Diagnostics."
)
_REAL_THREAD = threading.Thread
_UI_THREAD_STOP = object()


# ---------------------------------------------------------------------------
# Configuration Dialog (tkinter)
# ---------------------------------------------------------------------------

class ConfigDialog:
    """Simple tkinter dialog for server connection settings."""

    def __init__(self, config: dict, on_save=None):
        self._config = normalize_config(config)
        self._on_save = on_save
        self._result: Optional[dict] = None

    def show(self) -> Optional[dict]:
        """Show the config dialog. Returns updated config dict, or None if cancelled."""
        root = tk.Tk()
        root.title("VCI Proxy Client — Settings")
        root.resizable(False, False)
        root.attributes("-topmost", True)

        # Center on screen
        w, h = 420, 420
        x = (root.winfo_screenwidth() - w) // 2
        y = (root.winfo_screenheight() - h) // 2
        root.geometry(f"{w}x{h}+{x}+{y}")

        frame = ttk.Frame(root, padding=20)
        frame.pack(fill=tk.BOTH, expand=True)

        # Title
        ttk.Label(frame, text="VCI Proxy Client", font=("Segoe UI", 14, "bold")).grid(
            row=0, column=0, columnspan=3, pady=(0, 15), sticky=tk.W
        )

        # Server host
        ttk.Label(frame, text="Server Address:").grid(row=1, column=0, sticky=tk.W, pady=4)
        host_var = tk.StringVar(value=self._config.get("host", ""))
        host_entry = ttk.Entry(frame, textvariable=host_var, width=30)
        host_entry.grid(row=1, column=1, columnspan=2, sticky=tk.EW, pady=4, padx=(8, 0))

        # API scheme
        ttk.Label(frame, text="API Scheme:").grid(row=2, column=0, sticky=tk.W, pady=4)
        api_scheme_var = tk.StringVar(value=self._config.get("api_scheme", "http"))
        ttk.Combobox(
            frame,
            textvariable=api_scheme_var,
            values=["http", "https"],
            width=10,
            state="readonly",
        ).grid(row=2, column=1, sticky=tk.W, pady=4, padx=(8, 0))

        # Port
        ttk.Label(frame, text="Port:").grid(row=3, column=0, sticky=tk.W, pady=4)
        port_var = tk.StringVar(value=str(self._config.get("port", 9000)))
        ttk.Entry(frame, textvariable=port_var, width=10).grid(
            row=3, column=1, sticky=tk.W, pady=4, padx=(8, 0)
        )

        # API Port
        ttk.Label(frame, text="API Port:").grid(row=4, column=0, sticky=tk.W, pady=4)
        api_port_var = tk.StringVar(value=str(self._config.get("api_port", 8080)))
        ttk.Entry(frame, textvariable=api_port_var, width=10).grid(
            row=4, column=1, sticky=tk.W, pady=4, padx=(8, 0)
        )

        # API token
        ttk.Label(frame, text="API Token:").grid(row=5, column=0, sticky=tk.W, pady=4)
        api_token_var = tk.StringVar(value=self._config.get("api_token", ""))
        ttk.Entry(frame, textvariable=api_token_var, width=30, show="*").grid(
            row=5, column=1, columnspan=2, sticky=tk.EW, pady=4, padx=(8, 0)
        )

        # Auth token
        ttk.Label(frame, text="Auth Token:").grid(row=6, column=0, sticky=tk.W, pady=4)
        token_var = tk.StringVar(value=self._config.get("auth_token", ""))
        ttk.Entry(frame, textvariable=token_var, width=30, show="*").grid(
            row=6, column=1, columnspan=2, sticky=tk.EW, pady=4, padx=(8, 0)
        )

        # J2534 DLL selection (dropdown with auto-discovered drivers + browse)
        ttk.Label(frame, text="J2534 Driver:").grid(row=7, column=0, sticky=tk.W, pady=4)

        # Discover installed J2534 drivers from Windows registry
        from vci_proxy.j2534_driver import discover_j2534_drivers, normalize_dll_path
        discovered_drivers = discover_j2534_drivers()

        # Build combobox values: "Auto-detect" + discovered drivers
        dll_choices = ["Auto-detect (recommended)"]
        dll_path_map: dict[str, str] = {}  # display_name -> dll_path
        for drv in discovered_drivers:
            label = format_driver_label(drv, include_path=True)
            dll_choices.append(label)
            dll_path_map[label] = drv['dll_path']

        # Determine initial selection based on saved config
        saved_dll = self._config.get("dll_path", "")
        initial_value = "Auto-detect (recommended)"
        if saved_dll:
            # Check if saved path matches any discovered driver
            normalized_saved_dll = normalize_dll_path(saved_dll)
            for label, path in dll_path_map.items():
                if normalize_dll_path(path) == normalized_saved_dll:
                    initial_value = label
                    break
            else:
                # Custom path not in registry — show it directly
                initial_value = saved_dll
                dll_choices.append(saved_dll)
                dll_path_map[saved_dll] = saved_dll

        dll_var = tk.StringVar(value=initial_value)
        dll_combo = ttk.Combobox(
            frame, textvariable=dll_var, values=dll_choices,
            width=52, state="readonly",
        )
        dll_combo.grid(row=7, column=1, sticky=tk.EW, pady=4, padx=(8, 0))

        def browse_dll():
            path = filedialog.askopenfilename(
                title="Select J2534 DLL",
                filetypes=[("DLL files", "*.dll"), ("All files", "*.*")],
            )
            if path:
                # Add custom path to choices and select it
                normalized_path = normalize_dll_path(path)
                existing_choice = next(
                    (
                        choice for choice, choice_path in dll_path_map.items()
                        if normalize_dll_path(choice_path) == normalized_path
                    ),
                    None,
                )
                if existing_choice is not None:
                    dll_var.set(existing_choice)
                    return
                if path not in dll_path_map:
                    dll_choices.append(path)
                    dll_path_map[path] = path
                    dll_combo['values'] = dll_choices
                dll_var.set(path)

        ttk.Button(frame, text="...", width=3, command=browse_dll).grid(
            row=7, column=2, pady=4, padx=(4, 0)
        )

        def _resolve_dll_path() -> str:
            """Convert combobox selection to a dll_path string for config."""
            selected = dll_var.get()
            if selected == "Auto-detect (recommended)":
                return ""  # Empty = auto-detect in J2534Driver
            return dll_path_map.get(selected, selected)

        # Driver count hint
        if discovered_drivers:
            hint = (
                f"{len(discovered_drivers)} J2534 driver(s) found on this system. "
                "The client will choose the matching worker automatically."
            )
        else:
            hint = "No J2534 drivers found. Install a VCI driver or browse manually."
        ttk.Label(
            frame, text=hint, foreground="gray", font=("Segoe UI", 8),
        ).grid(row=8, column=0, columnspan=3, sticky=tk.W, pady=(0, 10))

        # Buttons
        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=9, column=0, columnspan=3, sticky=tk.E, pady=(10, 0))

        def on_connect():
            host = host_var.get().strip()
            if not host:
                messagebox.showwarning("Missing Field", "Server address is required.")
                return
            api_scheme = api_scheme_var.get().strip().lower() or "http"
            if api_scheme not in {"http", "https"}:
                messagebox.showwarning("Invalid API Scheme", "API scheme must be http or https.")
                return
            api_token = api_token_var.get().strip()
            auth_token = token_var.get().strip()
            if not auth_token:
                messagebox.showwarning("Missing Field", "Auth token is required.")
                return
            try:
                port = int(port_var.get().strip())
            except ValueError:
                messagebox.showwarning("Invalid Port", "Port must be a number.")
                return
            try:
                api_port = int(api_port_var.get().strip())
            except ValueError:
                messagebox.showwarning("Invalid API Port", "API port must be a number.")
                return

            self._result = {
                "api_scheme": api_scheme,
                "host": host,
                "port": port,
                "api_port": api_port,
                "api_token": api_token,
                "auth_token": auth_token,
                "dll_path": _resolve_dll_path(),
            }
            root.destroy()

        def on_cancel():
            self._result = None
            root.destroy()

        ttk.Button(btn_frame, text="Connect", command=on_connect).pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(btn_frame, text="Cancel", command=on_cancel).pack(side=tk.RIGHT)

        # Column weights
        frame.columnconfigure(1, weight=1)

        # Bind Enter key
        root.bind("<Return>", lambda e: on_connect())
        root.bind("<Escape>", lambda e: on_cancel())
        root.protocol("WM_DELETE_WINDOW", on_cancel)

        def _activate_dialog():
            try:
                root.lift()
                root.focus_force()
                root.grab_set()
                host_entry.focus_set()
                host_entry.icursor(tk.END)
            except tk.TclError:
                pass

        root.after(0, _activate_dialog)
        root.mainloop()
        return self._result


# ---------------------------------------------------------------------------
# System Tray Application
# ---------------------------------------------------------------------------

class VCIProxyTrayApp:
    """System tray application wrapping ReverseProxyClient."""

    def __init__(self):
        self._config = load_config()
        self._product_log_settings = resolve_product_log_settings()
        if self._product_log_settings.enabled:
            cleanup_product_observability(
                appdata=os.environ.get("APPDATA", Path.home()),
                retention_days_raw=self._product_log_settings.retention_days_raw,
                retention_days_session_trace=self._product_log_settings.retention_days_session_trace,
                retention_days_incident=self._product_log_settings.retention_days_incident,
            )
        self._status = "idle"
        self._status_detail = ""
        self._tray: Optional[Any] = None
        self._client: Optional[ReverseProxyClient] = None
        self._client_thread: Optional[threading.Thread] = None
        self._uploader_thread: Optional[threading.Thread] = None
        self._uploader_stop_event = threading.Event()
        self._settings_dialog_thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._active_node_assignment: Optional[dict[str, Any]] = None
        self._ui_thread: Optional[threading.Thread] = None
        self._ui_queue: queue.Queue[object] | None = None
        self._ui_thread_lock = threading.Lock()

    def _ensure_ui_thread(self) -> None:
        """Start one dedicated Tk UI thread on first use."""
        with self._ui_thread_lock:
            if self._ui_thread and self._ui_thread.is_alive() and self._ui_queue is not None:
                return
            self._ui_queue = queue.Queue()
            self._ui_thread = _REAL_THREAD(
                target=self._ui_thread_main,
                daemon=True,
                name="vci-proxy-ui",
            )
            self._ui_thread.start()

    def _ui_thread_main(self) -> None:
        """Run all Tk work on a single thread to avoid Tcl cross-thread crashes."""
        ui_queue = self._ui_queue
        if ui_queue is None:
            return

        while True:
            task = ui_queue.get()
            if task is _UI_THREAD_STOP:
                return

            callback, done, state = task
            try:
                state["result"] = callback()
            except BaseException as exc:  # pragma: no cover - surfaced to caller
                state["error"] = exc
            finally:
                done.set()

    def _run_on_ui_thread(self, callback: Callable[[], Any], timeout: float | None = None) -> Any:
        """Execute one callback on the dedicated UI thread and return its result."""
        if self._ui_thread and threading.current_thread() is self._ui_thread:
            return callback()

        self._ensure_ui_thread()
        if self._ui_queue is None:
            raise RuntimeError("UI thread queue is not available")

        done = threading.Event()
        state: dict[str, Any] = {}
        self._ui_queue.put((callback, done, state))
        if not done.wait(timeout):
            raise TimeoutError("Timed out waiting for UI thread")
        if "error" in state:
            raise state["error"]
        return state.get("result")

    def _stop_ui_thread(self, timeout: float = 1.0) -> None:
        """Best-effort shutdown for the dedicated UI thread."""
        ui_thread = self._ui_thread
        ui_queue = self._ui_queue
        if ui_thread is None or ui_queue is None:
            return
        if threading.current_thread() is ui_thread:
            return

        if ui_thread.is_alive():
            ui_queue.put(_UI_THREAD_STOP)
            ui_thread.join(timeout=timeout)
        if not ui_thread.is_alive():
            self._ui_thread = None
            self._ui_queue = None

    # --- Status management ---

    def _on_status_change(self, status: str, detail: str = ""):
        """Callback from ReverseProxyClient (called from background thread)."""
        previous_status = self._status
        previous_detail = self._status_detail
        self._status = status
        self._status_detail = detail
        if previous_status != status or previous_detail != detail:
            logger.info(
                "[GUI_STATUS] %s -> %s detail=%s",
                previous_status,
                status,
                detail or "-",
            )
        self._update_tray()

    def _update_tray(self):
        """Update tray icon and tooltip based on current status."""
        if self._tray is None:
            return
        icon_img = ICONS.get(self._status, ICONS["idle"])
        label = STATUS_LABELS.get(self._status, self._status)
        tooltip = f"VCI Proxy — {label}"
        if self._status_detail:
            tooltip += f"\n{self._status_detail}"

        self._tray.icon = icon_img
        self._tray.title = tooltip

    def _has_required_config(self) -> bool:
        """Return True when the minimum tunnel settings are configured."""
        cfg = self._effective_runtime_config()
        return bool(cfg.get("host") and cfg.get("auth_token"))

    def _effective_runtime_config(self) -> dict[str, Any]:
        """Return runtime config with any active assigned-node override applied."""
        cfg = normalize_config(self._config)
        assignment = self._active_node_assignment or {}
        parsed = urlparse(str(assignment.get("api_base_url") or "").strip())

        if parsed.scheme:
            cfg["api_scheme"] = parsed.scheme
        if parsed.port is not None:
            cfg["api_port"] = parsed.port
        elif parsed.scheme == "https":
            cfg["api_port"] = 443
        elif parsed.scheme == "http":
            cfg["api_port"] = 80

        tunnel_host = str(assignment.get("tunnel_host") or "").strip()
        if tunnel_host:
            cfg["host"] = tunnel_host
        elif parsed.hostname:
            cfg["host"] = parsed.hostname

        return cfg

    def _effective_api_base_url(self) -> str:
        assignment = self._active_node_assignment or {}
        api_base_url = str(assignment.get("api_base_url") or "").strip()
        if api_base_url:
            return api_base_url.rstrip("/")
        cfg = self._effective_runtime_config()
        return f"{cfg.get('api_scheme') or 'http'}://{cfg.get('host')}:{int(cfg.get('api_port') or 8080)}"

    def _on_node_assignment(self, assignment: dict[str, Any] | None) -> None:
        """Apply one assigned-node override and reconnect the reverse tunnel."""
        self._active_node_assignment = dict(assignment) if assignment else None
        self._restart_client()

    def _client_instance_id(self) -> str:
        return f"{socket.gethostname()}-tray"

    def _upload_observability_once(self) -> dict[str, int]:
        if not self._product_log_settings.upload_enabled:
            return {"queued_count": 0, "uploaded_count": 0}
        cfg = self._effective_runtime_config()
        host = str(cfg.get("host") or "").strip()
        if not host:
            return {"queued_count": 0, "uploaded_count": 0}
        api_base_url = self._effective_api_base_url()
        outbox = ObservabilityOutbox(appdata=os.environ.get("APPDATA", Path.home()))
        staged = outbox.stage_default_artifacts(client_instance_id=self._client_instance_id())
        uploaded = outbox.upload_pending(
            api_base_url=api_base_url,
            api_token=str(cfg.get("api_token") or "").strip(),
            max_artifact_mb=self._product_log_settings.max_artifact_mb,
        )
        return {
            "queued_count": int(staged.get("queued_count") or 0),
            "uploaded_count": int(uploaded.get("uploaded_count") or 0),
        }

    def _uploader_loop(self) -> None:
        while not self._uploader_stop_event.is_set():
            try:
                result = self._upload_observability_once()
                if result["queued_count"] or result["uploaded_count"]:
                    logger.info(
                        "[GUI_OBS] queued=%s uploaded=%s",
                        result["queued_count"],
                        result["uploaded_count"],
                    )
            except Exception:
                logger.exception("[GUI_OBS] upload cycle failed")
            self._uploader_stop_event.wait(15.0)

    def _start_observability_uploader(self) -> None:
        if self._uploader_thread and self._uploader_thread.is_alive():
            return
        self._uploader_stop_event.clear()
        self._uploader_thread = _REAL_THREAD(
            target=self._uploader_loop,
            daemon=True,
            name="vci-proxy-observability-uploader",
        )
        self._uploader_thread.start()

    def _stop_observability_uploader(self) -> None:
        self._uploader_stop_event.set()
        if self._uploader_thread and self._uploader_thread.is_alive():
            self._uploader_thread.join(timeout=5)
            if not self._uploader_thread.is_alive():
                self._uploader_thread = None
        else:
            self._uploader_thread = None

    # --- Client lifecycle ---

    def _start_client(self):
        """Start the reverse proxy client in a background thread."""
        if self._client_thread and self._client_thread.is_alive():
            logger.info("[GUI_CTRL] start skipped because client thread is already alive")
            return
        if not self._has_required_config():
            logger.info("[GUI_CTRL] start skipped because required config is missing")
            self._on_status_change("idle", "Settings required")
            return

        cfg = self._effective_runtime_config()
        logger.info(
            "[GUI_CTRL] starting reverse client pid=%s host=%s port=%s tls=%s api=%s://%s:%s dll_configured=%s",
            os.getpid(),
            cfg.get("host"),
            cfg.get("port"),
            bool(cfg.get("tls_enabled")),
            cfg.get("api_scheme") or "http",
            cfg.get("host"),
            cfg.get("api_port"),
            bool(cfg.get("dll_path")),
        )
        proxy_config = ProxyConfig.from_args(
            auth_token=cfg.get("auth_token") or None,
            tls_enabled=bool(cfg.get("tls_enabled")),
            tls_ca_file=cfg.get("tls_ca_file") or None,
            tls_server_name=cfg.get("tls_server_name") or None,
        )

        self._client = ReverseProxyClient(
            server_host=cfg["host"],
            server_port=cfg["port"],
            dll_path=cfg.get("dll_path") or None,
            config=proxy_config,
            on_status_change=self._on_status_change,
        )

        def run_client():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            client = self._client
            if client is None:
                return
            try:
                self._loop.run_until_complete(client.connect_and_serve())
            except Exception as e:
                logger.exception("Client thread error")
                self._on_status_change("error", str(e))
            finally:
                logger.info("[GUI_CTRL] client thread exiting")
                self._loop.close()
                self._loop = None

        self._client_thread = threading.Thread(target=run_client, daemon=True, name="vci-proxy-client")
        self._client_thread.start()
        self._start_observability_uploader()

    def _stop_client(self):
        """Stop the reverse proxy client."""
        logger.info(
            "[GUI_CTRL] stopping reverse client has_client=%s thread_alive=%s",
            self._client is not None,
            bool(self._client_thread and self._client_thread.is_alive()),
        )
        stop_future = None
        if self._client:
            stop_future = self._client.stop()
        if stop_future is not None:
            try:
                stop_future.result(timeout=5)
            except Exception as exc:
                logger.warning("Timed out waiting for reverse client shutdown: %s", exc)
        if self._client_thread and self._client_thread.is_alive():
            self._client_thread.join(timeout=5)
            if not self._client_thread.is_alive():
                self._client_thread = None
        else:
            self._client_thread = None
        self._stop_observability_uploader()
        self._client = None
        self._loop = None
        self._on_status_change("idle", "")
        logger.info("[GUI_CTRL] reverse client stopped")

    def _restart_client(self):
        """Stop and restart the client (e.g. after config change)."""
        logger.info("[GUI_CTRL] restarting reverse client")
        self._stop_client()
        if self._client_thread:
            self._client_thread.join(timeout=5)
        self._start_client()

    # --- Tray menu actions ---

    def _run_settings_dialog(self) -> None:
        """Open settings dialog outside the tray callback thread."""
        logger.info("[GUI_CTRL] opening settings dialog")
        self._stop_client()
        try:
            result = self._run_on_ui_thread(lambda: ConfigDialog(self._config).show())
            if result:
                logger.info("[GUI_CTRL] settings updated, restarting client")
                self._config = normalize_config({**self._config, **result})
                save_config(self._config)
                self._start_client()
            elif self._has_required_config():
                logger.info("[GUI_CTRL] settings dialog cancelled, restoring previous client")
                # User cancelled but had previous config — restart with old config
                self._start_client()
        finally:
            self._settings_dialog_thread = None

    def _on_settings(self, icon=None, item=None):
        """Open settings dialog."""
        if self._settings_dialog_thread and self._settings_dialog_thread.is_alive():
            logger.info("[GUI_CTRL] settings dialog request ignored because one is already open")
            return
        self._settings_dialog_thread = threading.Thread(
            target=self._run_settings_dialog,
            daemon=True,
            name="vci-proxy-settings",
        )
        self._settings_dialog_thread.start()

    def _on_diagnostics(self, icon=None, item=None):
        """Open the diagnostics window."""
        if not self._config.get("host"):
            self._show_threadsafe_error("Diagnostics", "Server address is not configured. Open Settings first.")
            return

        api_base = self._effective_api_base_url()
        api_token = str(self._config.get("api_token") or "").strip()

        def _open():
            try:
                from vci_proxy.diagnostics_window import DiagnosticsWindow

                def _show_window() -> None:
                    win = DiagnosticsWindow(
                        api_base,
                        api_token=api_token,
                        use_session_bootstrap=True,
                        node_assignment_callback=self._on_node_assignment,
                    )
                    win.show()

                self._run_on_ui_thread(_show_window)
            except Exception as e:
                logger.exception("Failed to open diagnostics window")
                self._show_threadsafe_error(
                    "Diagnostics Error",
                    f"Failed to open diagnostics window.\n\n{e}\n\n"
                    "Please rebuild the client and try again."
                )

        threading.Thread(target=_open, daemon=True, name="diagnostics-window").start()

    def _show_threadsafe_error(self, title: str, message: str) -> None:
        """Show an error dialog from non-UI threads safely."""
        def _show():
            root = tk.Tk()
            root.withdraw()
            try:
                messagebox.showerror(title, message)
            finally:
                root.destroy()

        try:
            self._run_on_ui_thread(_show)
        except Exception:
            # Last resort: keep a log entry even if dialog cannot be shown.
            logger.error("%s: %s", title, message)

    def _on_quit(self, icon=None, item=None):
        """Quit the application."""
        logger.info("[GUI_CTRL] quit requested")
        self._stop_client()
        self._stop_ui_thread()
        if self._tray:
            self._tray.stop()

    def _build_menu(self) -> Any:
        """Build the right-click context menu."""
        return pystray.Menu(
            pystray.MenuItem(
                lambda _: f"Status: {STATUS_LABELS.get(self._status, self._status)}",
                None,
                enabled=False,
            ),
            pystray.MenuItem(
                lambda _: self._status_detail if self._status_detail else "—",
                None,
                enabled=False,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Diagnostics...", self._on_diagnostics),
            pystray.MenuItem("Settings...", self._on_settings),
            pystray.MenuItem("Quit", self._on_quit),
        )

    def _on_tray_setup(self, icon: Any) -> None:
        """Mark the tray icon visible and show one startup notification."""
        try:
            icon.visible = True
        except Exception:
            logger.debug("failed to mark tray icon visible", exc_info=True)

        notify = getattr(icon, "notify", None)
        if not callable(notify):
            return

        try:
            notify(STARTUP_NOTIFICATION_MESSAGE, STARTUP_NOTIFICATION_TITLE)
        except Exception:
            logger.debug("failed to show tray startup notification", exc_info=True)

    # --- Main entry ---

    def run(self):
        """Main entry point — show config dialog if needed, then start tray."""
        try:
            # If required settings are missing, show settings dialog first.
            if not self._has_required_config():
                result = self._run_on_ui_thread(lambda: ConfigDialog(self._config).show())
                if not result:
                    # User cancelled first-run dialog — exit
                    return
                self._config = normalize_config({**self._config, **result})
                save_config(self._config)

            # Create tray icon
            self._tray = pystray.Icon(
                name="VCI Proxy Client",
                icon=ICONS["idle"],
                title="VCI Proxy — Starting...",
                menu=self._build_menu(),
            )
            tray = self._tray

            # Start client before entering tray loop
            self._start_client()

            # pystray.run() blocks — this is the main loop
            if tray is not None:
                tray.run(setup=self._on_tray_setup)
        finally:
            self._stop_ui_thread()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    """Launch the VCI Proxy Client GUI."""
    # Configure logging — output to both console and file
    log_file = CONFIG_DIR / "client.log"
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(str(log_file), encoding='utf-8'),
        ],
        force=True,
    )
    logger.info("[GUI_CTRL] client_gui starting pid=%s log_file=%s", os.getpid(), log_file)

    app = VCIProxyTrayApp()
    app.run()


if __name__ == "__main__":
    main()
