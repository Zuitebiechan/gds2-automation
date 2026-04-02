from vci_proxy.protocol import HEADER_SIZE, Message, MsgType, ProtocolEncoder

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
    expected = {
        "connection_epoch": "epoch-1",
        "connected": True,
        "fresh": True,
        "updated_at": "2026-03-27T00:00:00Z",
        "source": "probe",
        "sample_count": 5,
        "network_ms": {"last": 70.0, "p50": 72.0, "p95": 81.0},
        "grade": "warn",
        "status": "degraded",
        "reason": "p95 above good threshold",
        "probe_failures": 0,
    }

    write_tunnel_quality_snapshot(expected, path)
    actual = read_tunnel_quality_snapshot(path)

    assert actual == expected


def test_ping_request_encoder_uses_ping_message_type():
    encoded = ProtocolEncoder.encode_ping_req(sequence=7)
    magic, length, msg_type, sequence = Message.decode_header(encoded[:HEADER_SIZE])

    assert magic > 0
    assert length == HEADER_SIZE
    assert msg_type == MsgType.PING_REQ
    assert sequence == 7
    assert encoded[HEADER_SIZE:] == b""
