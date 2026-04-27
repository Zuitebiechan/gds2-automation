from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.navigation.snapshot import ControllerSnapshot


@dataclass(frozen=True)
class GDS2ObservedState:
    raw: ControllerSnapshot
    effective_page_id: str
    observed_actions: tuple[dict[str, Any], ...]
    classification_evidence: dict[str, Any] = field(default_factory=dict)
    ambiguity_metadata: dict[str, Any] = field(default_factory=dict)
    derived_action_data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_controller_snapshot(cls, raw: ControllerSnapshot) -> "GDS2ObservedState":
        effective_page_id, evidence = classify_effective_page(
            raw.raw_page_id,
            list(raw.buttons),
            list(raw.list_items),
        )
        observed_actions = tuple(
            [{"kind": "button", "label": button} for button in raw.buttons]
            + [
                {"kind": "list_item", "label": item, "list_index": 0}
                for item in raw.list_items
            ]
        )
        return cls(
            raw=raw,
            effective_page_id=effective_page_id,
            observed_actions=observed_actions,
            classification_evidence=evidence,
            derived_action_data={
                "button_labels": list(raw.buttons),
                "list_item_labels": list(raw.list_items),
            },
        )

    def to_snapshot_dict(self) -> dict[str, Any]:
        return {
            "page": {"page_id": self.raw.raw_page_id},
            "raw_page_id": self.raw.raw_page_id,
            "effective_page_id": self.effective_page_id,
            "observed_actions": [dict(action) for action in self.observed_actions],
            "list_items": list(self.raw.list_items),
            "navigation_path": list(self.raw.navigation_path),
            "classification_evidence": dict(self.classification_evidence),
            "ambiguity_metadata": dict(self.ambiguity_metadata),
            "derived_action_data": dict(self.derived_action_data),
        }


def classify_effective_page(
    page: str,
    buttons: list[str],
    list_items: list[str],
) -> tuple[str, dict[str, Any]]:
    page = str(page or "").strip()
    button_texts = set(buttons)
    evidence: dict[str, Any] = {
        "raw_page_id": page,
        "button_labels": list(buttons),
        "list_item_labels": list(list_items),
        "rule": "raw_page",
    }

    if page in {"loading", "unknown", "vehicle_selection"} and any("Module Diagnostics" in item for item in list_items):
        evidence["rule"] = "diagnostics_menu_list_items"
        return "diagnostics_menu", evidence
    if page in {"loading", "unknown", "vehicle_selection", "diagnostics_menu"} and any("[" in item and "]" in item for item in list_items):
        evidence["rule"] = "module_list_bracketed_items"
        return "module_list", evidence
    if page in {"loading", "unknown", "vehicle_selection"} and "Back" in button_texts:
        module_submenu_markers = {
            "Data Display",
            "Diagnostic Trouble Codes (DTC)",
            "Control Functions",
            "Configuration/Reset Functions",
        }
        if len(module_submenu_markers & set(list_items)) >= 2:
            evidence["rule"] = "module_submenu_markers"
            return "module_submenu", evidence
        if list_items:
            evidence["rule"] = "data_list_with_back"
            return "data_list", evidence

    if page != "loading":
        return page, evidence

    vehicle_markers = {
        "Select Device",
        "Disconnect",
        "Clear Vehicle Selection",
        "Read VIN",
        "Copy VIN",
    }
    deep_page_markers = {"Home", "Vehicle Menu"}
    if button_texts & vehicle_markers:
        evidence["rule"] = "loading_vehicle_markers"
        return "vehicle_selection", evidence
    if button_texts & deep_page_markers and not list_items:
        evidence["rule"] = "loading_deep_page_markers"
        return "loading", evidence
    return page, evidence
