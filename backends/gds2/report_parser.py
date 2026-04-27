from __future__ import annotations

import logging
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SKIPPED_DATA_ROWS = {"Parameter", "Control Module", "DTC Type"}
_VEHICLE_INFO_FIELDS = {
    "Vehicle Identification Number (VIN)": "vin",
    "Make": "make",
    "Model": "model",
    "Model Year": "year",
}


class _TableCellParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None
        self._in_cell = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.lower() == "tr":
            self._current_row = []
        elif tag.lower() == "td" and self._current_row is not None:
            self._current_cell = []
            self._in_cell = True

    def handle_data(self, data: str) -> None:
        if self._in_cell and self._current_cell is not None:
            self._current_cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.lower()
        if normalized_tag == "td" and self._current_row is not None and self._current_cell is not None:
            self._current_row.append(_normalize_cell_text("".join(self._current_cell)))
            self._current_cell = None
            self._in_cell = False
        elif normalized_tag == "tr" and self._current_row is not None:
            if self._current_row:
                self.rows.append(self._current_row)
            self._current_row = None
            self._current_cell = None
            self._in_cell = False


def _normalize_cell_text(value: str) -> str:
    return " ".join(value.split())


def _empty_report() -> dict[str, Any]:
    return {"vehicle_info": {}, "data_items": []}


def parse_data_display_report(report_path: str | Path) -> dict[str, Any]:
    """Parse a GDS2 Data Display HTML report into vehicle info and data rows."""
    result = _empty_report()

    try:
        html_content = Path(report_path).read_text(encoding="iso-8859-1")
        parser = _TableCellParser()
        parser.feed(html_content)
        parser.close()
    except Exception:
        logger.exception("Error parsing GDS2 Data Display report: %s", report_path)
        return result

    for row in parser.rows:
        if len(row) >= 2:
            field_name = _VEHICLE_INFO_FIELDS.get(row[0])
            if field_name:
                result["vehicle_info"][field_name] = row[1]

        if len(row) >= 3:
            parameter, value, units = row[:3]
            if parameter and parameter not in _SKIPPED_DATA_ROWS:
                result["data_items"].append(
                    {
                        "parameter": parameter,
                        "value": value,
                        "units": units,
                    }
                )

    return result


__all__ = ["parse_data_display_report"]
