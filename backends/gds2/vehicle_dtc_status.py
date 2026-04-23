from __future__ import annotations

import re
from typing import Any

from src.streaming.agent_data_collector import AgentSnapshot

_SUMMARY_TABLE_COLUMNS = {
    "Status",
    "Control Module Name",
    "Control Module Status",
    "DTC Count",
    "DLC Pin",
}
_WAITING_FOR_DATA_MARKER = "waiting for data"
_LOADING_MESSAGE = (
    "Vehicle DTC Information is still loading. "
    "Wait until the DTC table finishes loading and retry."
)
_READY_MESSAGE = "Vehicle DTC Information is ready."
_VEHICLE_DTC_INFORMATION_ALIASES = {
    "vehicle dtc information",
    "vehicle dtcs",
    "vehicle_dtc.information",
}


def _normalize_text(value: Any) -> str:
    return str(value or "").strip().casefold()


def _coerce_int(value: Any) -> int:
    try:
        return int(str(value or "").strip())
    except (TypeError, ValueError):
        match = re.search(r"\d+", str(value or ""))
        return int(match.group(0)) if match else 0


def is_vehicle_dtc_information_label(value: Any) -> bool:
    return _normalize_text(value) in _VEHICLE_DTC_INFORMATION_ALIASES


def _match_vehicle_dtc_summary_table(snapshot: AgentSnapshot | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None

    for table in snapshot.raw_tables or []:
        columns = {str(column).strip() for column in table.get("columns") or [] if str(column).strip()}
        if _SUMMARY_TABLE_COLUMNS.issubset(columns):
            return dict(table)
    return None


def build_vehicle_dtc_summary_rows(snapshot: AgentSnapshot | None) -> list[dict[str, str]]:
    table = _match_vehicle_dtc_summary_table(snapshot)
    if table is None:
        return []

    rows_out: list[dict[str, str]] = []
    for row in table.get("rows") or []:
        module_name = str((row or {}).get("Control Module Name") or "").strip()
        module_status = str((row or {}).get("Control Module Status") or "").strip()
        dtc_count = str((row or {}).get("DTC Count") or "").strip()
        dlc_pin = str((row or {}).get("DLC Pin") or "").strip()
        if not any((module_name, module_status, dtc_count, dlc_pin)):
            continue
        rows_out.append(
            {
                "code": dtc_count,
                "control_module": module_name,
                "status": module_status,
                "description": f"DLC Pin: {dlc_pin}" if dlc_pin else "",
            }
        )
    return rows_out


def evaluate_vehicle_dtc_status(snapshot: AgentSnapshot | None) -> dict[str, Any]:
    table = _match_vehicle_dtc_summary_table(snapshot)
    if table is None:
        return {
            "applicable": False,
            "ready": False,
            "reason": "not_vehicle_dtc_information",
            "message": "",
            "row_count": 0,
            "waiting_for_data_count": 0,
        }

    rows = list(table.get("rows") or [])
    row_count = int(table.get("rowCount") or len(rows) or 0)
    waiting_for_data_count = sum(
        1
        for row in rows
        if _WAITING_FOR_DATA_MARKER
        in _normalize_text((row or {}).get("Control Module Status"))
    )
    total_dtc_count = sum(_coerce_int((row or {}).get("DTC Count")) for row in rows)
    ready = row_count > 0 and waiting_for_data_count == 0
    return {
        "applicable": True,
        "ready": ready,
        "reason": "ready" if ready else ("waiting_for_data" if waiting_for_data_count else "table_empty"),
        "message": _READY_MESSAGE if ready else _LOADING_MESSAGE,
        "row_count": row_count,
        "waiting_for_data_count": waiting_for_data_count,
        "total_dtc_count": total_dtc_count,
    }


def vehicle_dtc_not_ready_message(status: dict[str, Any] | None = None) -> str:
    if isinstance(status, dict):
        message = str(status.get("message") or "").strip()
        if message:
            return message
    return _LOADING_MESSAGE


__all__ = [
    "build_vehicle_dtc_summary_rows",
    "evaluate_vehicle_dtc_status",
    "is_vehicle_dtc_information_label",
    "vehicle_dtc_not_ready_message",
]
