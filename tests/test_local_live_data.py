import json
import time
from pathlib import Path

import pytest

from vci_proxy.local_live_data import (
    LocalLiveDataMonitor,
    decode_engine_speed_frame,
    get_latest_snapshot_path,
)


def test_decode_engine_speed_obd_mode01_pid_0c_response() -> None:
    decoded = decode_engine_speed_frame(b"\x41\x0c\x1f\x40")

    assert decoded is not None
    assert decoded.value == 2000.0
    assert decoded.source == "proxy_local_obd"
    assert decoded.decoder_id == "obd_mode01_pid_0c"
    assert decoded.raw_value == 0x1F40


def test_decode_engine_speed_can_id_prefixed_uds_did_000c_response() -> None:
    decoded = decode_engine_speed_frame(bytes.fromhex("000007e862000c1234"))

    assert decoded is not None
    assert decoded.value == pytest.approx(1165.0)
    assert decoded.source == "proxy_local_known_uds"
    assert decoded.decoder_id == "uds_did_000c_engine_speed"
    assert decoded.raw_prefix_hex == "000007e862000c1234"


def test_decoder_ignores_engine_speed_marker_inside_gm_a9_payload() -> None:
    decoded = decode_engine_speed_frame(bytes.fromhex("000007e0a98122000c1234"))

    assert decoded is None


def test_latest_snapshot_path_uses_vci_proxy_live_data_dir(tmp_path: Path) -> None:
    assert get_latest_snapshot_path(tmp_path) == (
        tmp_path / "VCI_Proxy" / "live_data" / "latest.json"
    )


def test_monitor_writes_latest_snapshot_for_decoded_engine_speed(tmp_path: Path) -> None:
    monitor = LocalLiveDataMonitor(latest_path=tmp_path / "latest.json")
    now = time.time()

    events = monitor.record_sweep_item_finished(
        write_messages=[{"protocol_id": 6, "data": b"\x22\x00\x0c"}],
        read_messages=[{"protocol_id": 6, "data": b"\x62\x00\x0c\x1f\x40"}],
        return_code=0,
        started_at_s=now - 0.02,
        finished_at_s=now,
        channel_id=44,
        sweep_plan_id="plan-1",
        sweep_item_index=0,
        sweep_signature_digest="sig-000c",
        read_observability={"read_timeout_ms": 15},
    )

    event_types = [event_type for event_type, _fields in events]
    assert event_types == [
        "proxy.local_live_data.sample",
        "proxy.local_live_data.summary",
    ]
    sample = events[0][1]
    assert sample["signal_key"] == "engine_speed"
    assert sample["value"] == 2000.0
    assert sample["source"] == "proxy_local_known_uds"
    assert sample["decoder_id"] == "uds_did_000c_engine_speed"
    assert sample["return_code"] == 0

    payload = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    latest = payload["latest_sample"]
    assert latest["signal_key"] == "engine_speed"
    assert latest["value"] == 2000.0
    assert payload["signals"]["engine_speed"]["decoder_id"] == "uds_did_000c_engine_speed"
    assert payload["summary"]["sample_count"] == 1


def test_monitor_decodes_engine_speed_payload_with_timeout_return_code(
    tmp_path: Path,
) -> None:
    monitor = LocalLiveDataMonitor(latest_path=tmp_path / "latest.json")
    now = time.time()

    events = monitor.record_sweep_item_finished(
        write_messages=[{"protocol_id": 6, "data": b"\x22\x00\x0c"}],
        read_messages=[
            {"protocol_id": 6, "data": bytes.fromhex("000007e0")},
            {"protocol_id": 6, "data": bytes.fromhex("000007e862000c0d96")},
        ],
        return_code=9,
        started_at_s=now - 0.024,
        finished_at_s=now,
        channel_id=1,
        sweep_plan_id="plan-000c",
        sweep_item_index=0,
        sweep_signature_digest="sig-000c",
        read_observability={
            "read_timeout_ms": 15,
            "tail_read_triggered": False,
            "tail_read_attempts": 0,
            "tail_read_data_reads": 0,
        },
    )

    event_types = [event_type for event_type, _fields in events]
    assert event_types == [
        "proxy.local_live_data.sample",
        "proxy.local_live_data.summary",
    ]
    sample = events[0][1]
    assert sample["value"] == pytest.approx(869.5)
    assert sample["source"] == "proxy_local_known_uds"
    assert sample["return_code"] == 9
    assert sample["j2534_return_code_warning"] is True
    assert sample["j2534_return_code_warning_reason"] == "timeout_return_code_with_data"
    assert sample["raw_prefix_hex"] == "000007e862000c0d96"
    assert sample["read_timeout_ms"] == 15
    assert sample["message_index"] == 1

    summary = events[1][1]
    assert summary["sample_count"] == 1
    assert summary["backoff_count"] == 0
    assert summary["timeout_count"] == 0
    assert summary["return_code_warning_count"] == 1

    payload = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    latest = payload["latest_sample"]
    assert latest["value"] == pytest.approx(869.5)
    assert latest["return_code"] == 9
    assert latest["j2534_return_code_warning"] is True


def test_monitor_reports_unsupported_engine_speed_response(tmp_path: Path) -> None:
    monitor = LocalLiveDataMonitor(latest_path=tmp_path / "latest.json")
    now = time.time()

    events = monitor.record_sweep_item_finished(
        write_messages=[{"protocol_id": 6, "data": b"\x22\x00\x0c"}],
        read_messages=[{"protocol_id": 6, "data": b"\x62\x00\x31\x12\x34"}],
        return_code=0,
        started_at_s=now - 0.01,
        finished_at_s=now,
        channel_id=44,
        sweep_plan_id="plan-1",
        sweep_item_index=0,
        sweep_signature_digest="sig-000c",
    )

    assert events[0][0] == "proxy.local_live_data.unsupported"
    assert events[0][1]["reason"] == "no_supported_engine_speed_shape"
    assert events[1][0] == "proxy.local_live_data.summary"
    assert events[1][1]["unsupported_count"] == 1
    assert not (tmp_path / "latest.json").exists()


def test_monitor_reports_backoff_for_failed_engine_speed_read(tmp_path: Path) -> None:
    monitor = LocalLiveDataMonitor(latest_path=tmp_path / "latest.json")
    now = time.time()

    events = monitor.record_sweep_item_finished(
        write_messages=[{"protocol_id": 6, "data": b"\x01\x0c"}],
        read_messages=[],
        return_code=1,
        started_at_s=now - 0.01,
        finished_at_s=now,
        channel_id=44,
        sweep_plan_id="plan-1",
        sweep_item_index=0,
        sweep_signature_digest="sig-obd",
    )

    assert events[0][0] == "proxy.local_live_data.backoff"
    assert events[0][1]["backoff_reason"] == "read_return_code"
    assert events[1][0] == "proxy.local_live_data.summary"
    assert events[1][1]["backoff_count"] == 1
