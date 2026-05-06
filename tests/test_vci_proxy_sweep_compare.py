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
    assert "62f40c0080" not in str(comparison.fields)
