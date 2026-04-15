from vci_proxy.protocol import HEADER_SIZE, Message, MsgType, ProtocolEncoder

import json
from datetime import datetime, timezone

from vci_proxy.tunnel_quality import (
    TunnelQualityTracker,
    read_tunnel_quality_snapshot,
    write_tunnel_quality_snapshot,
)


def test_read_snapshot_defaults_to_block_when_missing(tmp_path):
    snapshot = read_tunnel_quality_snapshot(tmp_path / "missing.json")

    assert snapshot["grade"] == "block"
    assert snapshot["status"] == "blocked"
    assert snapshot["reason"] == "snapshot_missing"
    assert snapshot["connected"] is False


def test_tracker_requires_two_windows_before_recovering_to_good():
    tracker = TunnelQualityTracker(window_size=5, freshness_seconds=10, hysteresis_windows=2)
    tracker.mark_connected("epoch-1", measured_at=0.0)

    for index in range(5):
        tracker.record_probe(60.0, measured_at=float(index + 1))

    first_snapshot = tracker.snapshot(now=5.1)
    assert first_snapshot["grade"] == "block"
    assert first_snapshot["reason"] == "awaiting_hysteresis"

    tracker.record_probe(58.0, measured_at=6.0)
    recovered_snapshot = tracker.snapshot(now=6.1)

    assert recovered_snapshot["grade"] == "good"
    assert recovered_snapshot["status"] == "healthy"
    assert recovered_snapshot["sample_count"] == 5
    assert recovered_snapshot["network_ms"]["p95"] == 60.0


def test_write_snapshot_round_trips_payload(tmp_path):
    path = tmp_path / "ProgramData" / "VCI_Proxy" / "tunnel_quality.json"
    snapshot_time = datetime(2026, 3, 27, 0, 0, 0, tzinfo=timezone.utc)
    expected = {
        "connection_epoch": "epoch-1",
        "connected": True,
        "fresh": True,
        "updated_at": snapshot_time.isoformat().replace("+00:00", "Z"),
        "source": "probe",
        "sample_count": 5,
        "network_ms": {"last": 70.0, "p50": 72.0, "p95": 81.0},
        "grade": "warn",
        "status": "degraded",
        "reason": "p95 above good threshold",
        "probe_failures": 0,
    }

    write_tunnel_quality_snapshot(expected, path)
    actual = read_tunnel_quality_snapshot(path, now=snapshot_time.timestamp() + 1.0)

    assert actual == expected


def test_read_snapshot_recomputes_freshness_from_updated_at(tmp_path):
    path = tmp_path / "ProgramData" / "VCI_Proxy" / "tunnel_quality.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_time = datetime(2026, 3, 27, 0, 0, 0, tzinfo=timezone.utc)
    path.write_text(
        json.dumps(
            {
                "connection_epoch": "epoch-9",
                "connected": True,
                "fresh": True,
                "updated_at": snapshot_time.isoformat().replace("+00:00", "Z"),
                "source": "probe",
                "sample_count": 5,
                "network_ms": {"last": 20.0, "p50": 22.0, "p95": 30.0},
                "grade": "good",
                "status": "healthy",
                "reason": "p95 within good threshold",
                "probe_failures": 0,
            }
        ),
        encoding="utf-8",
    )

    actual = read_tunnel_quality_snapshot(path, now=snapshot_time.timestamp() + 60.0)

    assert actual["connected"] is True
    assert actual["fresh"] is False
    assert actual["grade"] == "block"
    assert actual["status"] == "blocked"
    assert actual["reason"] == "snapshot_stale"


def test_ping_request_encoder_uses_ping_message_type():
    encoded = ProtocolEncoder.encode_ping_req(sequence=7)
    magic, length, msg_type, sequence = Message.decode_header(encoded[:HEADER_SIZE])

    assert magic > 0
    assert length == HEADER_SIZE
    assert msg_type == MsgType.PING_REQ
    assert sequence == 7
    assert encoded[HEADER_SIZE:] == b""
