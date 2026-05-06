from __future__ import annotations

from vci_proxy.protocol import HEADER_SIZE, ProtocolDecoder, ProtocolEncoder
from vci_proxy.sweep_signatures import (
    make_signature,
    parse_diagnostic_request_payload,
    payload_digest,
)


def test_parse_read_only_request_shapes_with_optional_can_id_prefix() -> None:
    uds = parse_diagnostic_request_payload(b"\x22\xf4\x0c")
    obd = parse_diagnostic_request_payload(b"\x00\x00\x07\xe0\x01\x0c")

    assert uds is not None
    assert uds.service_id == 0x22
    assert uds.identifier_kind == "uds_did"
    assert uds.identifier == 0xF40C

    assert obd is not None
    assert obd.service_id == 0x01
    assert obd.identifier_kind == "obd_pid"
    assert obd.identifier == 0x0C
    assert obd.logical_ecu_target == 0x7E0


def test_parse_request_shape_rejects_unknown_or_mutating_payloads() -> None:
    assert parse_diagnostic_request_payload(b"\x2e\xf4\x0c\x00") is None
    assert parse_diagnostic_request_payload(b"\x10\x03") is None
    assert parse_diagnostic_request_payload(b"\x22\xf4") is None


def test_signature_is_stable_and_redacted_for_observability() -> None:
    shape = parse_diagnostic_request_payload(b"\x00\x00\x07\xe0\x22\xf4\x0c")
    assert shape is not None
    message = {
        "protocol_id": 6,
        "rx_status": 0,
        "tx_flags": 2,
        "timestamp": 123,
        "data": b"\x00\x00\x07\xe0\x22\xf4\x0c",
    }

    first = make_signature(
        channel_id=44,
        message=message,
        shape=shape,
        connection_epoch="epoch-1",
    )
    second = make_signature(
        channel_id=44,
        message=message,
        shape=shape,
        connection_epoch="epoch-1",
    )
    fields = first.to_observability()

    assert first == second
    assert first.signature_digest == second.signature_digest
    assert fields["sweep_payload_digest"] == payload_digest(message["data"])
    assert "normalized_payload" not in fields
    assert fields["sweep_payload_prefix_hex"] == "000007e022f40c"


def test_equivalent_write_request_body_produces_same_signature_digest() -> None:
    body = ProtocolEncoder.encode_write_msgs_req(
        44,
        [{"protocol_id": 6, "tx_flags": 0, "data": b"\x22\xf4\x0c"}],
        timeout=25,
    )[HEADER_SIZE:]
    channel_id, messages, _timeout = ProtocolDecoder.decode_write_msgs_req(body)
    shape = parse_diagnostic_request_payload(messages[0]["data"])
    assert shape is not None

    first = make_signature(
        channel_id=channel_id,
        message=messages[0],
        shape=shape,
        connection_epoch="epoch-1",
    )
    second = make_signature(
        channel_id=channel_id,
        message=messages[0],
        shape=shape,
        connection_epoch="epoch-1",
    )

    assert first.signature_digest == second.signature_digest
