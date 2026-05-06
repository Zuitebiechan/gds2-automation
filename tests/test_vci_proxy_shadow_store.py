from __future__ import annotations

import time

from vci_proxy.protocol import HEADER_SIZE, ProtocolEncoder
from vci_proxy.sweep_compare import compare_shadow_to_real
from vci_proxy.sweep_protocol import SweepResultRecord
from vci_proxy.sweep_shadow_store import SweepShadowStore


def _read_rsp_body(data: bytes) -> bytes:
    return ProtocolEncoder.encode_read_msgs_rsp(
        0,
        [{"protocol_id": 6, "rx_status": 0, "tx_flags": 0, "timestamp": 1, "data": data}],
    )[HEADER_SIZE:]


def test_shadow_store_keeps_comparison_records_without_serving_api() -> None:
    store = SweepShadowStore(max_results_per_signature=1)
    first = SweepResultRecord(
        plan_id="plan-1",
        signature_digest="sig",
        return_code=0,
        read_rsp_body=_read_rsp_body(b"\x62\x01"),
        started_at_s=time.time(),
        finished_at_s=time.time(),
    )
    second = SweepResultRecord(
        plan_id="plan-1",
        signature_digest="sig",
        return_code=0,
        read_rsp_body=_read_rsp_body(b"\x62\x02"),
        started_at_s=time.time(),
        finished_at_s=time.time(),
    )

    store.record_result(first, channel_id=44)
    store.record_result(second, channel_id=44)

    assert store.latest_for("sig") == second
    assert store.pending_count() == 1
    assert not hasattr(store, "try_serve")
    assert not hasattr(store, "record_read_rsp_body")


def test_shadow_store_clears_by_channel() -> None:
    store = SweepShadowStore()
    result = SweepResultRecord(
        plan_id="plan-1",
        signature_digest="sig",
        return_code=0,
        read_rsp_body=_read_rsp_body(b"\x62\x01"),
        started_at_s=time.time(),
        finished_at_s=time.time(),
    )
    store.record_result(result, channel_id=44)

    store.clear_channel(44)

    assert store.latest_for("sig") is None
    assert store.pending_count() == 0


def test_compare_shadow_results_reports_match_mismatch_stale_missing_and_error() -> None:
    now = time.time()
    matching = SweepResultRecord(
        plan_id="plan-1",
        signature_digest="sig",
        return_code=0,
        read_rsp_body=_read_rsp_body(b"\x62\x01"),
        started_at_s=now,
        finished_at_s=now,
    )
    mismatch = SweepResultRecord(
        plan_id="plan-1",
        signature_digest="sig",
        return_code=0,
        read_rsp_body=_read_rsp_body(b"\x62\x02"),
        started_at_s=now,
        finished_at_s=now,
    )
    stale = SweepResultRecord(
        plan_id="plan-1",
        signature_digest="sig",
        return_code=0,
        read_rsp_body=_read_rsp_body(b"\x62\x01"),
        started_at_s=now - 10,
        finished_at_s=now - 10,
    )
    error = SweepResultRecord(
        plan_id="plan-1",
        signature_digest="sig",
        return_code=1,
        read_rsp_body=b"",
        started_at_s=now,
        finished_at_s=now,
        error_name="driver_error",
    )

    assert compare_shadow_to_real(
        signature_digest="sig",
        real_read_rsp_body=_read_rsp_body(b"\x62\x01"),
        shadow_result=matching,
        max_result_age_ms=1000,
    ).outcome == "match"
    assert compare_shadow_to_real(
        signature_digest="sig",
        real_read_rsp_body=_read_rsp_body(b"\x62\x01"),
        shadow_result=mismatch,
        max_result_age_ms=1000,
    ).outcome == "mismatch"
    assert compare_shadow_to_real(
        signature_digest="sig",
        real_read_rsp_body=_read_rsp_body(b"\x62\x01"),
        shadow_result=stale,
        max_result_age_ms=1,
    ).outcome == "stale"
    assert compare_shadow_to_real(
        signature_digest="sig",
        real_read_rsp_body=_read_rsp_body(b"\x62\x01"),
        shadow_result=None,
        max_result_age_ms=1000,
    ).outcome == "missing"
    assert compare_shadow_to_real(
        signature_digest="sig",
        real_read_rsp_body=_read_rsp_body(b"\x62\x01"),
        shadow_result=error,
        max_result_age_ms=1000,
    ).outcome == "error"
