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
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
from typing import Any, Optional

from PIL import Image, ImageDraw

try:
    import pystray
except ImportError:
    print("ERROR: pystray not installed. Run: pip install pystray Pillow")
    sys.exit(1)

from vci_proxy.reverse_client import ReverseProxyClient
from vci_proxy.config import ProxyConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------

CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "VCI_Proxy"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_CONFIG = {
    "host": "",
    "port": 9000,
    "api_port": 8080,
    "auth_token": "",
    "dll_path": "",
}


def load_config() -> dict:
    """Load config from %APPDATA%/VCI_Proxy/config.json."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            # Merge with defaults for forward-compatibility
            return {**DEFAULT_CONFIG, **saved}
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    """Save config to %APPDATA%/VCI_Proxy/config.json."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


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


# ---------------------------------------------------------------------------
# Configuration Dialog (tkinter)
# ---------------------------------------------------------------------------

class ConfigDialog:
    """Simple tkinter dialog for server connection settings."""

    def __init__(self, config: dict, on_save=None):
        self._config = config
        self._on_save = on_save
        self._result: Optional[dict] = None

    def show(self) -> Optional[dict]:
        """Show the config dialog. Returns updated config dict, or None if cancelled."""
        root = tk.Tk()
        root.title("VCI Proxy Client — Settings")
        root.resizable(False, False)
        root.attributes("-topmost", True)

        # Center on screen
        w, h = 420, 340
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

        # Port
        ttk.Label(frame, text="Port:").grid(row=2, column=0, sticky=tk.W, pady=4)
        port_var = tk.StringVar(value=str(self._config.get("port", 9000)))
        ttk.Entry(frame, textvariable=port_var, width=10).grid(
            row=2, column=1, sticky=tk.W, pady=4, padx=(8, 0)
        )

        # API Port
        ttk.Label(frame, text="API Port:").grid(row=3, column=0, sticky=tk.W, pady=4)
        api_port_var = tk.StringVar(value=str(self._config.get("api_port", 8080)))
        ttk.Entry(frame, textvariable=api_port_var, width=10).grid(
            row=3, column=1, sticky=tk.W, pady=4, padx=(8, 0)
        )

        # Auth token
        ttk.Label(frame, text="Auth Token:").grid(row=4, column=0, sticky=tk.W, pady=4)
        token_var = tk.StringVar(value=self._config.get("auth_token", ""))
        ttk.Entry(frame, textvariable=token_var, width=30, show="*").grid(
            row=4, column=1, columnspan=2, sticky=tk.EW, pady=4, padx=(8, 0)
        )

        # J2534 DLL selection (dropdown with auto-discovered drivers + browse)
        ttk.Label(frame, text="J2534 Driver:").grid(row=5, column=0, sticky=tk.W, pady=4)

        # Discover installed J2534 drivers from Windows registry
        from vci_proxy.j2534_driver import discover_j2534_drivers
        discovered_drivers = discover_j2534_drivers()

        # Build combobox values: "Auto-detect" + discovered drivers
        dll_choices = ["Auto-detect (recommended)"]
        dll_path_map: dict[str, str] = {}  # display_name -> dll_path
        for drv in discovered_drivers:
            label = f"{drv['name']}"
            if drv['vendor']:
                label += f" ({drv['vendor']})"
            dll_choices.append(label)
            dll_path_map[label] = drv['dll_path']

        # Determine initial selection based on saved config
        saved_dll = self._config.get("dll_path", "")
        initial_value = "Auto-detect (recommended)"
        if saved_dll:
            # Check if saved path matches any discovered driver
            for label, path in dll_path_map.items():
                if os.path.normcase(path) == os.path.normcase(saved_dll):
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
            width=28, state="readonly",
        )
        dll_combo.grid(row=5, column=1, sticky=tk.EW, pady=4, padx=(8, 0))

        def browse_dll():
            path = filedialog.askopenfilename(
                title="Select J2534 DLL",
                filetypes=[("DLL files", "*.dll"), ("All files", "*.*")],
            )
            if path:
                # Add custom path to choices and select it
                if path not in dll_path_map:
                    dll_choices.append(path)
                    dll_path_map[path] = path
                    dll_combo['values'] = dll_choices
                dll_var.set(path)

        ttk.Button(frame, text="...", width=3, command=browse_dll).grid(
            row=5, column=2, pady=4, padx=(4, 0)
        )

        def _resolve_dll_path() -> str:
            """Convert combobox selection to a dll_path string for config."""
            selected = dll_var.get()
            if selected == "Auto-detect (recommended)":
                return ""  # Empty = auto-detect in J2534Driver
            return dll_path_map.get(selected, selected)

        # Driver count hint
        if discovered_drivers:
            hint = f"{len(discovered_drivers)} J2534 driver(s) found on this system."
        else:
            hint = "No J2534 drivers found. Install a VCI driver or browse manually."
        ttk.Label(
            frame, text=hint, foreground="gray", font=("Segoe UI", 8),
        ).grid(row=6, column=0, columnspan=3, sticky=tk.W, pady=(0, 10))

        # Buttons
        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=7, column=0, columnspan=3, sticky=tk.E, pady=(10, 0))

        def on_connect():
            host = host_var.get().strip()
            if not host:
                messagebox.showwarning("Missing Field", "Server address is required.")
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
                "host": host,
                "port": port,
                "api_port": api_port,
                "auth_token": token_var.get().strip(),
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

        # Focus
        host_entry.focus_set()

        # Bind Enter key
        root.bind("<Return>", lambda e: on_connect())
        root.bind("<Escape>", lambda e: on_cancel())

        root.mainloop()
        return self._result


# ---------------------------------------------------------------------------
# System Tray Application
# ---------------------------------------------------------------------------

class VCIProxyTrayApp:
    """System tray application wrapping ReverseProxyClient."""

    def __init__(self):
        self._config = load_config()
        self._status = "idle"
        self._status_detail = ""
        self._tray: Optional[Any] = None
        self._client: Optional[ReverseProxyClient] = None
        self._client_thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # --- Status management ---

    def _on_status_change(self, status: str, detail: str = ""):
        """Callback from ReverseProxyClient (called from background thread)."""
        self._status = status
        self._status_detail = detail
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

    # --- Client lifecycle ---

    def _start_client(self):
        """Start the reverse proxy client in a background thread."""
        if self._client_thread and self._client_thread.is_alive():
            return

        cfg = self._config
        proxy_config = ProxyConfig.from_args(
            auth_token=cfg.get("auth_token") or None,
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
                logger.error(f"Client thread error: {e}")
                self._on_status_change("error", str(e))
            finally:
                self._loop.close()
                self._loop = None

        self._client_thread = threading.Thread(target=run_client, daemon=True, name="vci-proxy-client")
        self._client_thread.start()

    def _stop_client(self):
        """Stop the reverse proxy client."""
        if self._client:
            self._client.stop()
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._on_status_change("idle", "")

    def _restart_client(self):
        """Stop and restart the client (e.g. after config change)."""
        self._stop_client()
        if self._client_thread:
            self._client_thread.join(timeout=5)
        self._start_client()

    # --- Tray menu actions ---

    def _on_settings(self, icon=None, item=None):
        """Open settings dialog."""
        self._stop_client()
        dialog = ConfigDialog(self._config)
        result = dialog.show()
        if result:
            self._config = result
            save_config(self._config)
            self._start_client()
        elif self._config.get("host"):
            # User cancelled but had previous config — restart with old config
            self._start_client()

    def _on_diagnostics(self, icon=None, item=None):
        """Open the diagnostics window."""
        if not self._config.get("host"):
            self._show_threadsafe_error("Diagnostics", "Server address is not configured. Open Settings first.")
            return

        host = self._config["host"]
        port = self._config.get("api_port", 8080)
        api_base = f"http://{host}:{port}"

        def _open():
            try:
                from vci_proxy.diagnostics_window import DiagnosticsWindow

                win = DiagnosticsWindow(api_base)
                win.show()
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
            _show()
        except Exception:
            # Last resort: keep a log entry even if dialog cannot be shown.
            logger.error("%s: %s", title, message)

    def _on_quit(self, icon=None, item=None):
        """Quit the application."""
        self._stop_client()
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

    # --- Main entry ---

    def run(self):
        """Main entry point — show config dialog if needed, then start tray."""
        # If no host configured, show settings dialog first
        if not self._config.get("host"):
            dialog = ConfigDialog(self._config)
            result = dialog.show()
            if not result:
                # User cancelled first-run dialog — exit
                return
            self._config = result
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
            tray.run()


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
    )

    app = VCIProxyTrayApp()
    app.run()


if __name__ == "__main__":
    main()
