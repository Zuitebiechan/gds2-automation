from __future__ import annotations

from vci_proxy.config import LocalSweepConfig
from vci_proxy.protocol import HEADER_SIZE, ProtocolEncoder
from vci_proxy.sweep_classifier import SweepReadOnlyClassifier


def _write_body(data: bytes, *, messages: int = 1) -> bytes:
    payloads = [
        {"protocol_id": 6, "rx_status": 0, "tx_flags": 0, "timestamp": 1, "data": data}
        for _ in range(messages)
    ]
    return ProtocolEncoder.encode_write_msgs_req(44, payloads, timeout=25)[HEADER_SIZE:]


def test_classifier_accepts_exact_uds_rdbi_and_obd_mode01_shapes() -> None:
    classifier = SweepReadOnlyClassifier(LocalSweepConfig(enabled=True))

    uds = classifier.classify_write_request_body(
        _write_body(b"\x22\xf4\x0c"),
        connection_epoch="epoch-1",
    )
    obd = classifier.classify_write_request_body(
        _write_body(b"\x00\x00\x07\xe0\x01\x0c"),
        connection_epoch="epoch-1",
    )

    assert uds.accepted is True
    assert uds.reason == "allowlisted_read_only"
    assert uds.observed is not None
    assert uds.observed.signature.identifier_kind == "uds_did"
    assert uds.observed.signature.identifier == 0xF40C

    assert obd.accepted is True
    assert obd.observed is not None
    assert obd.observed.signature.identifier_kind == "obd_pid"
    assert obd.observed.signature.identifier == 0x0C


def test_classifier_accepts_strict_gm_a9_packet_request_shape() -> None:
    classifier = SweepReadOnlyClassifier(LocalSweepConfig(enabled=True))

    classification = classifier.classify_write_request_body(
        _write_body(b"\x00\x00\x07\xe0\xa9\x81\x1a"),
        connection_epoch="epoch-1",
    )

    assert classification.accepted is True
    assert classification.reason == "allowlisted_read_only"
    assert classification.observed is not None
    assert classification.observed.signature.identifier_kind == "gm_a9_packet"
    assert classification.observed.signature.identifier == 0x811A
    assert classification.observed.signature.logical_ecu_target == 0x7E0


def test_classifier_rejects_disabled_mutating_unknown_and_multi_message_writes() -> None:
    disabled = SweepReadOnlyClassifier(LocalSweepConfig(enabled=False))
    enabled = SweepReadOnlyClassifier(LocalSweepConfig(enabled=True))

    assert disabled.classify_write_request_body(
        _write_body(b"\x22\xf4\x0c"),
        connection_epoch="epoch-1",
    ).reason == "local_sweep_disabled"
    assert enabled.classify_write_request_body(
        _write_body(b"\x2e\xf4\x0c\x00"),
        connection_epoch="epoch-1",
    ).reason == "not_allowlisted_read_only_shape"
    assert enabled.classify_write_request_body(
        _write_body(b"\x27\x01"),
        connection_epoch="epoch-1",
    ).reason == "not_allowlisted_read_only_shape"
    assert enabled.classify_write_request_body(
        _write_body(b"\x00\x00\x07\xe0\x3e"),
        connection_epoch="epoch-1",
    ).reason == "not_allowlisted_read_only_shape"
    assert enabled.classify_write_request_body(
        _write_body(b"\x22\xf4\x0c", messages=2),
        connection_epoch="epoch-1",
    ).reason == "write_message_count_not_one"


def test_classifier_respects_service_allowlist_flags() -> None:
    classifier = SweepReadOnlyClassifier(
        LocalSweepConfig(
            enabled=True,
            allow_uds_rdbi=False,
            allow_obd_mode01=False,
        )
    )

    assert classifier.classify_write_request_body(
        _write_body(b"\x22\xf4\x0c"),
        connection_epoch="epoch-1",
    ).reason == "uds_rdbi_disabled"
    assert classifier.classify_write_request_body(
        _write_body(b"\x01\x0c"),
        connection_epoch="epoch-1",
    ).reason == "obd_mode01_disabled"

    gm_disabled = SweepReadOnlyClassifier(
        LocalSweepConfig(enabled=True, allow_gm_a9_packet=False)
    )
    assert gm_disabled.classify_write_request_body(
        _write_body(b"\x00\x00\x07\xe0\xa9\x81\x1a"),
        connection_epoch="epoch-1",
    ).reason == "gm_a9_packet_disabled"
