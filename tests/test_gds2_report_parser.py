from __future__ import annotations

from backends.gds2.report_parser import parse_data_display_report


def test_parse_data_display_report_extracts_vehicle_info_and_data_items(tmp_path) -> None:
    report_path = tmp_path / "report.html"
    report_path.write_text(
        """
<table>
<tr><td>Vehicle Identification Number (VIN)</td><td>VIN123</td></tr>
<tr><td>Make</td><td>Chevrolet</td></tr>
<tr><td>Model</td><td>Malibu</td></tr>
<tr><td>Model Year</td><td>2024</td></tr>
<tr><td>Parameter</td><td>Value</td><td>Units</td></tr>
<tr><td>RPM</td><td>850</td><td>rpm</td></tr>
<tr><td>Coolant Temp</td><td>92</td><td>C</td></tr>
</table>
""".strip(),
        encoding="iso-8859-1",
    )

    parsed = parse_data_display_report(report_path)

    assert parsed["vehicle_info"] == {
        "vin": "VIN123",
        "make": "Chevrolet",
        "model": "Malibu",
        "year": "2024",
    }
    assert parsed["data_items"] == [
        {"parameter": "RPM", "value": "850", "units": "rpm"},
        {"parameter": "Coolant Temp", "value": "92", "units": "C"},
    ]


def test_parse_data_display_report_tolerates_cell_attributes_and_nested_text(tmp_path) -> None:
    report_path = tmp_path / "report.html"
    report_path.write_text(
        """
<table>
<tr class="data"><td><span>RPM</span></td><td> 850 </td><td> rpm </td></tr>
<tr><td>Control Module</td><td>ECM</td><td></td></tr>
<tr><td>Commanded &amp; Actual</td><td> yes </td><td> state </td></tr>
</table>
""".strip(),
        encoding="iso-8859-1",
    )

    parsed = parse_data_display_report(report_path)

    assert parsed["data_items"] == [
        {"parameter": "RPM", "value": "850", "units": "rpm"},
        {"parameter": "Commanded & Actual", "value": "yes", "units": "state"},
    ]


def test_parse_data_display_report_returns_empty_result_on_read_error(tmp_path) -> None:
    parsed = parse_data_display_report(tmp_path / "missing.html")

    assert parsed == {"vehicle_info": {}, "data_items": []}
