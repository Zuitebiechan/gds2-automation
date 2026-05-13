from __future__ import annotations

import time

from vci_proxy.protocol import HEADER_SIZE, ProtocolEncoder
from vci_proxy.sweep_compare import compare_shadow_to_real
from vci_proxy.sweep_protocol import SweepResultRecord


def _read_rsp_body(data: bytes) -> bytes:
    return ProtocolEncoder.encode_read_msgs_rsp(
        0,
        [{"protocol_id": 6, "rx_status": 0, "tx_flags": 0, "timestamp": 1, "data": data}],
    )[HEADER_SIZE:]


def test_compare_fields_are_redacted_to_shape_and_digest() -> None:
    now = time.time()
    result = SweepResultRecord(
        plan_id="plan-1",
        signature_digest="sig",
        return_code=0,
        read_rsp_body=_read_rsp_body(b"\x62\xf4\x0c\x00\x80"),
        started_at_s=now,
        finished_at_s=now,
    )

    comparison = compare_shadow_to_real(
        signature_digest="sig",
        real_read_rsp_body=_read_rsp_body(b"\x62\xf4\x0c\x00\x80"),
        shadow_result=result,
        max_result_age_ms=1000,
    )

    assert comparison.outcome == "match"
    assert comparison.fields["sweep_real_message_count"] == 1
    assert comparison.fields["sweep_shadow_message_count"] == 1
    assert comparison.fields["sweep_real_message_lengths"] == [5]
    assert comparison.fields["sweep_shadow_message_lengths"] == [5]
    assert comparison.fields["sweep_real_message_prefixes"] == ["62f40c0080"]
    assert comparison.fields["sweep_shadow_message_prefixes"] == ["62f40c0080"]


def test_compare_mismatch_reports_message_shape_details() -> None:
    now = time.time()
    mismatch = SweepResultRecord(
        plan_id="plan-1",
        signature_digest="sig",
        return_code=0,
        read_rsp_body=ProtocolEncoder.encode_read_msgs_rsp(
            0,
            [{"protocol_id": 6, "rx_status": 0, "tx_flags": 0, "timestamp": 1, "data": b"\x62\x00\x31\x12"}],
        )[HEADER_SIZE:],
        started_at_s=now,
        finished_at_s=now,
    )

    comparison = compare_shadow_to_real(
        signature_digest="sig",
        real_read_rsp_body=ProtocolEncoder.encode_read_msgs_rsp(
            0,
            [
                {"protocol_id": 6, "rx_status": 0, "tx_flags": 0, "timestamp": 1, "data": b"\x62\x00\x31\x12"},
                {"protocol_id": 6, "rx_status": 0, "tx_flags": 0, "timestamp": 2, "data": b"\x62\x00\x31\x34"},
            ],
        )[HEADER_SIZE:],
        shadow_result=mismatch,
        max_result_age_ms=1000,
    )

    assert comparison.outcome == "mismatch"
    assert comparison.fields["sweep_real_message_count"] == 2
    assert comparison.fields["sweep_shadow_message_count"] == 1
    assert comparison.fields["sweep_real_message_lengths"] == [4, 4]
    assert comparison.fields["sweep_shadow_message_lengths"] == [4]
    assert comparison.fields["sweep_real_message_prefixes"] == ["62003112", "62003134"]
    assert comparison.fields["sweep_shadow_message_prefixes"] == ["62003112"]
