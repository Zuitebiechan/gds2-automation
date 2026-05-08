from __future__ import annotations

import time

from vci_proxy.protocol import HEADER_SIZE, ProtocolEncoder
from vci_proxy.sweep_protocol import SweepResultRecord
from vci_proxy.sweep_shadow_store import SweepShadowStore


def _result(
    *,
    signature_digest: str = "sig-1",
    return_code: int = 0,
    read_rsp_body: bytes,
    started_at_s: float,
    finished_at_s: float,
) -> SweepResultRecord:
    return SweepResultRecord(
        plan_id="plan-1",
        signature_digest=signature_digest,
        return_code=return_code,
        read_rsp_body=read_rsp_body,
        started_at_s=started_at_s,
        finished_at_s=finished_at_s,
    )


def test_latest_replay_ready_requires_two_distinct_clean_matches() -> None:
    store = SweepShadowStore()
    now = time.time()
    read_rsp_body = ProtocolEncoder.encode_read_msgs_rsp(
        0,
        [{"protocol_id": 6, "data": b"\x62\xf4\x0c\x12\x34"}],
    )[HEADER_SIZE:]

    first = _result(
        read_rsp_body=read_rsp_body,
        started_at_s=now - 0.2,
        finished_at_s=now - 0.1,
    )
    store.record_result(first, channel_id=44)
    store.record_comparison("sig-1", result=first, clean_match=True, reset_streak=False)

    assert store.replay_match_streak("sig-1") == 1
    assert (
        store.latest_replay_ready(
            "sig-1",
            max_result_age_ms=10_000_000_000,
            min_clean_matches=2,
        )
        is None
    )

    second = _result(
        read_rsp_body=read_rsp_body,
        started_at_s=now - 0.09,
        finished_at_s=now - 0.05,
    )
    store.record_result(second, channel_id=44)

    assert (
        store.latest_replay_ready(
            "sig-1",
            max_result_age_ms=10_000_000_000,
            min_clean_matches=2,
        )
        is None
    )

    store.record_comparison("sig-1", result=second, clean_match=True, reset_streak=False)

    assert store.replay_match_streak("sig-1") == 2
    assert (
        store.latest_replay_ready(
            "sig-1",
            max_result_age_ms=10_000_000_000,
            min_clean_matches=2,
        )
        == second
    )


def test_latest_replay_ready_rejects_nonzero_return_code_and_empty_results() -> None:
    store = SweepShadowStore()
    now = time.time()
    valid_rsp_body = ProtocolEncoder.encode_read_msgs_rsp(
        0,
        [{"protocol_id": 6, "data": b"\x62\xf4\x0c\x12\x34"}],
    )[HEADER_SIZE:]
    error_rsp_body = ProtocolEncoder.encode_read_msgs_rsp(18, [])[HEADER_SIZE:]
    empty_rsp_body = ProtocolEncoder.encode_read_msgs_rsp(0, [])[HEADER_SIZE:]

    valid = _result(
        read_rsp_body=valid_rsp_body,
        started_at_s=now - 0.2,
        finished_at_s=now - 0.1,
    )
    store.record_result(valid, channel_id=44)
    store.record_comparison("sig-1", result=valid, clean_match=True, reset_streak=False)
    valid_next = _result(
        read_rsp_body=valid_rsp_body,
        started_at_s=now - 0.09,
        finished_at_s=now - 0.05,
    )
    store.record_result(valid_next, channel_id=44)
    store.record_comparison("sig-1", result=valid_next, clean_match=True, reset_streak=False)
    assert store.replay_match_streak("sig-1") == 2

    dirty_return = _result(
        return_code=18,
        read_rsp_body=error_rsp_body,
        started_at_s=now - 0.04,
        finished_at_s=now - 0.03,
    )
    store.record_result(dirty_return, channel_id=44)
    store.record_comparison("sig-1", result=dirty_return, clean_match=False, reset_streak=True)
    assert store.replay_match_streak("sig-1") == 0
    assert (
        store.latest_replay_ready(
            "sig-1",
            max_result_age_ms=10_000_000_000,
            min_clean_matches=2,
        )
        is None
    )

    empty = _result(
        read_rsp_body=empty_rsp_body,
        started_at_s=now - 0.02,
        finished_at_s=now - 0.01,
    )
    store.record_result(empty, channel_id=44)
    store.record_comparison("sig-1", result=empty, clean_match=False, reset_streak=True)
    assert store.replay_match_streak("sig-1") == 0
    assert (
        store.latest_replay_ready(
            "sig-1",
            max_result_age_ms=10_000_000_000,
            min_clean_matches=2,
        )
        is None
    )
