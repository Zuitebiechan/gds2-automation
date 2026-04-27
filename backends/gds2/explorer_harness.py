from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from backends.gds2.observed_state import classify_effective_page
from src.native.device_explorer import DeviceExplorerController
from src.streaming.agent_navigator import AgentNavigator

_LATEST_JSON_ENCODINGS = ("utf-8", "utf-8-sig", "gb18030", "latin-1")


def load_latest_snapshot(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return {}

    snapshot_path = Path(path)
    if not snapshot_path.exists():
        return {}

    raw = snapshot_path.read_bytes()
    for encoding in _LATEST_JSON_ENCODINGS:
        try:
            return json.loads(raw.decode(encoding))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    return {}


def extract_navigation_path(latest_json: dict[str, Any]) -> list[str]:
    def _is_valid_path_value(value: str) -> bool:
        normalized = value.strip()
        if not normalized:
            return False
        if normalized.lower() == "null_tabledata":
            return False
        if normalized.startswith("null_"):
            return False
        return True

    def _looks_like_navigation_path_value(value: str) -> bool:
        normalized = value.strip().lower()
        return any(
            marker in normalized
            for marker in (
                "module diagnostics",
                "vehicle diagnostics",
                "session manager",
                "control functions",
                "data display",
                "diagnostic trouble codes",
                "reset functions",
                "learn functions",
                "configuration/reset functions",
            )
        )

    tables = latest_json.get("tables") or []
    for table in tables:
        columns = [str(column) for column in table.get("columns") or []]
        if columns == [""]:
            values: list[str] = []
            for row in table.get("rows") or []:
                value = str(row.get("", "")).strip()
                if _is_valid_path_value(value):
                    values.append(value)
            if values:
                return values

    page_context = latest_json.get("pageContext") or {}
    module_name = str(page_context.get("moduleName") or "").strip()
    if not module_name:
        return []

    parts = [part.strip() for part in module_name.split(",") if _is_valid_path_value(part)]
    if len(parts) > 3:
        return parts[3:]
    if any(_looks_like_navigation_path_value(part) for part in parts):
        return parts
    return []


class GDS2ExplorerHarness:
    def __init__(
        self,
        *,
        navigator: AgentNavigator,
        latest_json_path: str | Path | None = None,
        artifact_root: str | Path | None = None,
        device_controller_factory: Callable[[], Any] = DeviceExplorerController,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._navigator = navigator
        self._latest_json_path = (
            Path(latest_json_path)
            if latest_json_path is not None
            else Path.home() / "gds2-data" / "latest.json"
        )
        self._artifact_root = Path(artifact_root) if artifact_root is not None else None
        self._device_controller_factory = device_controller_factory
        self._sleep = sleep_fn
        self._snapshots: list[dict[str, Any]] = []
        self._run_dir = self._build_run_dir()
        self._graph_path = self._run_dir / "graph.json" if self._run_dir is not None else None
        self._graph: dict[str, Any] = {
            "version": 1,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "nodes": {},
            "edges": [],
        }

    def run_mainline(
        self,
        *,
        device_name: str,
        module_name: str,
        data_category: str,
        submenu_item: str = "Data Display",
        max_steps: int = 24,
    ) -> dict[str, Any]:
        current_snapshot = self.capture("start")

        for _ in range(max_steps):
            page_id = self._effective_page_id(current_snapshot)
            button_texts = self._snapshot_button_texts(current_snapshot)

            if "OK" in button_texts:
                current_snapshot = self._click_button(
                    "OK",
                    "dismiss_ok_modal",
                    source_snapshot=current_snapshot,
                )
                continue

            if page_id == "loading":
                self._sleep(1.0)
                current_snapshot = self.capture("loading")
                continue

            if page_id == "unknown":
                self._sleep(1.0)
                current_snapshot = self.capture("unknown")
                continue

            if page_id == "main_menu":
                current_snapshot = self._click_button(
                    "Diagnostics",
                    "after_click_diagnostics",
                    source_snapshot=current_snapshot,
                )
                continue

            if page_id == "vehicle_selection":
                if "Enter" in button_texts:
                    current_snapshot = self._click_button(
                        "Enter",
                        "after_click_enter",
                        source_snapshot=current_snapshot,
                    )
                    continue
                if "Select Device" in button_texts:
                    current_snapshot = self._select_device(
                        device_name,
                        source_snapshot=current_snapshot,
                    )
                    continue
                raise RuntimeError("Vehicle Selection reached without Enter or Select Device")

            if page_id == "diagnostics_menu":
                current_snapshot = self._select_list_item(
                    "Module Diagnostics",
                    "after_module_diagnostics",
                    source_snapshot=current_snapshot,
                )
                continue

            if page_id == "module_list":
                current_snapshot = self._select_list_item(
                    module_name,
                    "after_module_selection",
                    source_snapshot=current_snapshot,
                )
                continue

            if page_id == "module_submenu":
                current_snapshot = self._select_list_item(
                    submenu_item,
                    "after_submenu_selection",
                    source_snapshot=current_snapshot,
                )
                continue

            if page_id == "data_list":
                current_snapshot = self._select_list_item(
                    data_category,
                    "after_data_selection",
                    source_snapshot=current_snapshot,
                )
                continue

            if page_id == "data_display":
                final_snapshot = self.capture("data_display")
                return {
                    "final_page": page_id,
                    "artifact_dir": str(self._run_dir) if self._run_dir is not None else "",
                    "snapshots": list(self._snapshots),
                    "graph_path": str(self._graph_path) if self._graph_path is not None else "",
                    "final_node_id": final_snapshot["node_id"],
                }

            raise RuntimeError(f"Unsupported page during exploration: {page_id}")

        raise RuntimeError(f"Mainline exploration exceeded {max_steps} steps")

    def capture(self, label: str) -> dict[str, Any]:
        snapshot = self._collect_snapshot(label)
        self._persist_snapshot(snapshot)
        return snapshot

    def _collect_snapshot(self, label: str) -> dict[str, Any]:
        snapshot = {
            "label": label,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "page": self._navigator.get_page_id() or {},
            "buttons": self._navigator.get_buttons(),
            "list_items": self._navigator.get_list_items(0),
            "latest_json": load_latest_snapshot(self._latest_json_path),
        }
        snapshot["navigation_path"] = extract_navigation_path(snapshot["latest_json"])
        snapshot["observed_actions"] = self._build_observed_actions(snapshot)
        snapshot["raw_page_id"] = self._raw_snapshot_page_id(snapshot)
        snapshot["effective_page_id"] = self._classify_effective_page(snapshot)
        snapshot["node_id"] = self._register_node(snapshot)
        return snapshot

    def _persist_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._snapshots.append(snapshot)
        self._write_snapshot(snapshot)
        self._write_graph()

    def _current_page_id(self) -> str:
        page = self._navigator.get_page_id() or {}
        return str(page.get("page_id") or "unknown")

    def _button_texts(self) -> list[str]:
        texts: list[str] = []
        for button in self._navigator.get_buttons():
            text = str(button.get("text") or "").strip()
            if text:
                texts.append(text)
        return texts

    @staticmethod
    def _raw_snapshot_page_id(snapshot: dict[str, Any]) -> str:
        page = snapshot.get("page") or {}
        return str(page.get("page_id") or "unknown")

    def _classify_effective_page(self, snapshot: dict[str, Any]) -> str:
        page_id = self._raw_snapshot_page_id(snapshot)
        effective_page, evidence = classify_effective_page(
            page_id,
            self._snapshot_button_texts(snapshot),
            [str(item).strip() for item in snapshot.get("list_items") or [] if str(item).strip()],
        )
        snapshot["classification_evidence"] = evidence
        return effective_page

    def _snapshot_page_id(self, snapshot: dict[str, Any]) -> str:
        cached = snapshot.get("effective_page_id")
        if cached:
            return str(cached)
        return self._classify_effective_page(snapshot)

    def _effective_page_id(self, snapshot: dict[str, Any]) -> str:
        return self._snapshot_page_id(snapshot)

    @staticmethod
    def _snapshot_button_texts(snapshot: dict[str, Any]) -> list[str]:
        texts: list[str] = []
        for button in GDS2ExplorerHarness._combined_buttons(snapshot):
            text = str(button.get("text") or "").strip()
            if text:
                texts.append(text)
        return texts

    def _click_button(
        self,
        button_text: str,
        snapshot_label: str,
        *,
        source_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        result = self._navigator.click_button(button_text)
        if not result.get("success"):
            raise RuntimeError(f"Failed to click '{button_text}': {result.get('message')}")
        target_snapshot = self._capture_transition(snapshot_label, source_snapshot=source_snapshot)
        self._record_edge(
            source_snapshot,
            {"kind": "button", "label": button_text},
            target_snapshot,
        )
        return target_snapshot

    def _select_list_item(
        self,
        item_text: str,
        snapshot_label: str,
        *,
        source_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        result = self._navigator.select_list_item_by_text(0, item_text, double_click=True)
        if not result.get("success"):
            raise RuntimeError(f"Failed to select '{item_text}': {result.get('message')}")
        target_snapshot = self._capture_transition(snapshot_label, source_snapshot=source_snapshot)
        self._record_edge(
            source_snapshot,
            {"kind": "list_item", "label": item_text, "list_index": 0},
            target_snapshot,
        )
        return target_snapshot

    def _select_device(
        self,
        device_name: str,
        *,
        source_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        result = self._navigator.click_button("Select Device")
        if not result.get("success"):
            raise RuntimeError(f"Failed to click 'Select Device': {result.get('message')}")

        self._sleep(1.0)
        controller = self._device_controller_factory()
        if not controller.find_dialog(timeout_sec=5.0):
            raise RuntimeError("Device Explorer dialog not found")
        dialog_snapshot = self._capture_device_explorer(controller, label="device_explorer")
        self._record_edge(
            source_snapshot,
            {"kind": "button", "label": "Select Device"},
            dialog_snapshot,
        )
        if not controller.select_device_by_name(device_name):
            devices = []
            if hasattr(controller, "get_device_names"):
                devices = list(controller.get_device_names())
            raise RuntimeError(f"Device '{device_name}' not found. Available: {devices}")
        self._sleep(0.2)
        if not controller.click_continue():
            raise RuntimeError("Failed to click Continue in Device Explorer")
        target_snapshot = self._capture_transition(
            "after_select_device",
            source_snapshot=dialog_snapshot,
            initial_delay=1.5,
        )
        self._record_edge(
            dialog_snapshot,
            {"kind": "device", "label": device_name},
            target_snapshot,
        )
        return target_snapshot

    def _capture_transition(
        self,
        label: str,
        *,
        source_snapshot: dict[str, Any],
        timeout_sec: float = 8.0,
        poll_interval: float = 0.5,
        initial_delay: float = 1.0,
    ) -> dict[str, Any]:
        source_page_id = self._effective_page_id(source_snapshot)
        source_node_id = source_snapshot["node_id"]
        deadline = time.time() + max(timeout_sec, 0.0)
        last_snapshot: dict[str, Any] | None = None
        candidate_snapshot: dict[str, Any] | None = None
        candidate_hits = 0

        if initial_delay > 0:
            self._sleep(initial_delay)

        while time.time() <= deadline:
            last_snapshot = self._collect_snapshot(label)
            effective_page = self._effective_page_id(last_snapshot)
            changed = effective_page != source_page_id or last_snapshot["node_id"] != source_node_id

            if effective_page not in {"loading", "unknown"} and changed:
                if (
                    candidate_snapshot is not None
                    and last_snapshot["node_id"] == candidate_snapshot["node_id"]
                    and effective_page == self._effective_page_id(candidate_snapshot)
                ):
                    candidate_hits += 1
                else:
                    candidate_snapshot = last_snapshot
                    candidate_hits = 1

                if candidate_hits >= 2:
                    self._persist_snapshot(candidate_snapshot)
                    return candidate_snapshot
            else:
                candidate_snapshot = None
                candidate_hits = 0
            if poll_interval > 0:
                self._sleep(poll_interval)

        if candidate_snapshot is not None:
            self._persist_snapshot(candidate_snapshot)
            return candidate_snapshot

        if last_snapshot is None:
            last_snapshot = self._collect_snapshot(label)
        last_snapshot["transition_error"] = (
            f"Transition did not settle from {source_page_id} within {timeout_sec:.1f}s"
        )
        self._persist_snapshot(last_snapshot)
        raise TimeoutError(last_snapshot["transition_error"])

    def _build_run_dir(self) -> Path | None:
        if self._artifact_root is None:
            return None
        run_dir = self._artifact_root / datetime.now().strftime("%Y%m%d-%H%M%S")
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def _write_snapshot(self, snapshot: dict[str, Any]) -> None:
        if self._run_dir is None:
            return
        index = len(self._snapshots)
        safe_label = "".join(
            character if character.isalnum() or character in ("-", "_") else "_"
            for character in snapshot["label"]
        )
        output_path = self._run_dir / f"{index:02d}_{safe_label}.json"
        output_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _normalize_buttons(buttons: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for button in buttons or []:
            text = str(button.get("text") or "").strip()
            if not text:
                continue
            normalized.append(
                {
                    "text": text,
                    "enabled": bool(button.get("enabled", True)),
                }
            )
        deduped: dict[str, dict[str, Any]] = {}
        for button in normalized:
            existing = deduped.get(button["text"])
            if existing is None:
                deduped[button["text"]] = button
            else:
                existing["enabled"] = existing["enabled"] or button["enabled"]
        return sorted(deduped.values(), key=lambda item: item["text"].lower())

    @staticmethod
    def _combined_buttons(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        combined: list[dict[str, Any]] = list(snapshot.get("buttons") or [])
        page = snapshot.get("page") or {}
        for button_text in page.get("buttons") or []:
            text = str(button_text or "").strip()
            if not text:
                continue
            combined.append({"text": text, "enabled": True})
        return GDS2ExplorerHarness._normalize_buttons(combined)

    def _build_observed_actions(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        for button in self._combined_buttons(snapshot):
            actions.append(
                {
                    "kind": "button",
                    "label": button["text"],
                    "enabled": button["enabled"],
                }
            )
        for item in snapshot.get("list_items") or []:
            label = str(item).strip()
            if not label:
                continue
            actions.append(
                {
                    "kind": "list_item",
                    "label": label,
                    "list_index": 0,
                }
            )
        return actions

    def _register_node(self, snapshot: dict[str, Any]) -> str:
        page = snapshot.get("page") or {}
        node_payload = {
            "page_id": self._effective_page_id(snapshot),
            "raw_page_id": self._raw_snapshot_page_id(snapshot),
            "window_title": str(
                page.get("window_title")
                or ((snapshot.get("latest_json") or {}).get("pageContext") or {}).get("windowTitle")
                or ""
            ),
            "buttons": self._combined_buttons(snapshot),
            "list_items": [str(item) for item in snapshot.get("list_items") or []],
            "navigation_path": [str(item) for item in snapshot.get("navigation_path") or []],
        }
        node_id = hashlib.sha1(
            json.dumps(node_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        if node_id not in self._graph["nodes"]:
            self._graph["nodes"][node_id] = {
                "node_id": node_id,
                **node_payload,
                "observed_actions": list(snapshot.get("observed_actions") or []),
                "first_seen_label": snapshot["label"],
            }
        return node_id

    def _record_edge(
        self,
        source_snapshot: dict[str, Any],
        action: dict[str, Any],
        target_snapshot: dict[str, Any],
    ) -> None:
        self._graph["edges"].append(
            {
                "source_id": source_snapshot["node_id"],
                "target_id": target_snapshot["node_id"],
                "source_page_id": self._effective_page_id(source_snapshot),
                "target_page_id": self._effective_page_id(target_snapshot),
                "source_raw_page_id": self._raw_snapshot_page_id(source_snapshot),
                "target_raw_page_id": self._raw_snapshot_page_id(target_snapshot),
                "action": action,
                "timestamp": datetime.now().isoformat(timespec="seconds"),
            }
        )
        self._write_graph()

    def _capture_device_explorer(self, controller: Any, *, label: str) -> dict[str, Any]:
        devices: list[str] = []
        if hasattr(controller, "get_device_names"):
            devices = [str(item) for item in controller.get_device_names()]
        snapshot = {
            "label": label,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "page": {"page_id": "device_explorer", "confidence": "high"},
            "buttons": [
                {"text": "Continue", "enabled": True},
                {"text": "Cancel", "enabled": True},
            ],
            "list_items": devices,
            "latest_json": load_latest_snapshot(self._latest_json_path),
        }
        snapshot["navigation_path"] = extract_navigation_path(snapshot["latest_json"])
        snapshot["observed_actions"] = self._build_observed_actions(snapshot)
        snapshot["raw_page_id"] = self._raw_snapshot_page_id(snapshot)
        snapshot["effective_page_id"] = self._classify_effective_page(snapshot)
        snapshot["node_id"] = self._register_node(snapshot)
        self._persist_snapshot(snapshot)
        return snapshot

    def _write_graph(self) -> None:
        if self._graph_path is None:
            return
        self._graph_path.write_text(
            json.dumps(self._graph, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
