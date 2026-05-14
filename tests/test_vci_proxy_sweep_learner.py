from __future__ import annotations

from vci_proxy.config import LocalSweepConfig
from vci_proxy.protocol import HEADER_SIZE, MsgType, ProtocolEncoder
from vci_proxy.sweep_learner import SweepPatternLearner


def _write_body(data: bytes = b"\x22\xf4\x0c") -> bytes:
    return ProtocolEncoder.encode_write_msgs_req(
        44,
        [{"protocol_id": 6, "tx_flags": 0, "timestamp": 1, "data": data}],
        timeout=25,
    )[HEADER_SIZE:]


def _read_req_body() -> bytes:
    return ProtocolEncoder.encode_read_msgs_req(44, 1, 0)[HEADER_SIZE:]


def _read_req_body_with(num_msgs: int, timeout: int = 0) -> bytes:
    return ProtocolEncoder.encode_read_msgs_req(44, num_msgs, timeout)[HEADER_SIZE:]


def _read_rsp_body(data: bytes = b"\x62\xf4\x0c\x12\x34") -> bytes:
    return ProtocolEncoder.encode_read_msgs_rsp(
        0,
        [{"protocol_id": 6, "rx_status": 0, "tx_flags": 0, "timestamp": 2, "data": data}],
    )[HEADER_SIZE:]


def _write_rsp_body(return_code: int = 0) -> bytes:
    return ProtocolEncoder.encode_write_msgs_rsp(return_code, 1)[HEADER_SIZE:]


def test_learner_requires_configured_cycles_before_candidate_is_learned() -> None:
    learner = SweepPatternLearner(LocalSweepConfig(enabled=True, min_cycles=2))

    write_events = learner.observe_write(_write_body(), connection_epoch="epoch-1")
    observed, first_read_events = learner.observe_read_response(
        _read_req_body(),
        _read_rsp_body(),
        connection_epoch="epoch-1",
    )

    assert observed is not None
    assert [event.event_type for event in write_events] == ["sweep.pattern.observed"]
    assert [event.event_type for event in first_read_events] == [
        "sweep.did.cadence",
        "sweep.inventory.signature",
        "sweep.inventory.summary",
    ]
    assert learner.candidate_count == 0

    learner.observe_write(_write_body(), connection_epoch="epoch-1")
    _observed, second_read_events = learner.observe_read_response(
        _read_req_body(),
        _read_rsp_body(),
        connection_epoch="epoch-1",
    )

    assert [event.event_type for event in second_read_events] == [
        "sweep.did.cadence",
        "sweep.pattern.learned",
        "sweep.inventory.signature",
        "sweep.inventory.summary",
    ]
    learned_event = [
        event for event in second_read_events if event.event_type == "sweep.pattern.learned"
    ][0]
    assert learned_event.fields["sweep_candidate_count"] == 1
    assert learned_event.fields["sweep_confidence"] == 1.0
    assert learner.candidate_count == 1


def test_learner_carries_real_read_request_shape_into_learned_candidate() -> None:
    learner = SweepPatternLearner(LocalSweepConfig(enabled=True, min_cycles=1))

    learner.observe_write(_write_body(), connection_epoch="epoch-1")
    observed, _events = learner.observe_read_response(
        _read_req_body_with(300, 0),
        _read_rsp_body(),
        connection_epoch="epoch-1",
    )

    assert observed is not None
    learned = learner.learned_for_channel(44, connection_epoch="epoch-1")
    assert len(learned) == 1
    assert learned[0].read_num_msgs == 300
    assert learned[0].read_timeout_ms == 0


def test_learner_estimates_would_have_shadow_hits_after_learning() -> None:
    learner = SweepPatternLearner(LocalSweepConfig(enabled=True, min_cycles=1))
    for _ in range(3):
        learner.observe_write(_write_body(), connection_epoch="epoch-1")
        _observed, events = learner.observe_read_response(
            _read_req_body(),
            _read_rsp_body(),
            connection_epoch="epoch-1",
        )

    cadence = events[0]
    assert cadence.fields["sweep_would_have_shadow_hits"] == 2
    assert cadence.fields["sweep_estimated_would_hit_rate"] > 0


def test_inventory_reports_coverage_and_projected_rtt_savings() -> None:
    learner = SweepPatternLearner(LocalSweepConfig(enabled=True, min_cycles=1))
    write_body = _write_body()

    learner.observe_write(write_body, connection_epoch="epoch-1")
    learner.observe_write_response(
        write_body,
        MsgType.WRITE_MSGS_RSP,
        _write_rsp_body(),
        connection_epoch="epoch-1",
        duration_ms=42.0,
        network_ms=37.0,
    )
    _observed, events = learner.observe_read_response(
        _read_req_body(),
        _read_rsp_body(),
        connection_epoch="epoch-1",
        duration_ms=5.0,
        network_ms=3.0,
    )

    signature_event = [
        event for event in events if event.event_type == "sweep.inventory.signature"
    ][0]
    summary_event = [
        event for event in events if event.event_type == "sweep.inventory.summary"
    ][0]

    assert signature_event.fields["sweep_inventory_learned"] is True
    assert signature_event.fields["sweep_inventory_replay_candidate"] is True
    assert signature_event.fields["sweep_inventory_eligibility_reason"] == (
        "learned_safe_signature"
    )
    assert signature_event.fields["sweep_inventory_write_network_p95_ms"] == 37.0
    assert signature_event.fields["sweep_inventory_read_network_p95_ms"] == 3.0
    assert signature_event.fields["sweep_inventory_pair_network_p95_ms"] == 40.0
    assert summary_event.fields["sweep_inventory_signature_count"] == 1
    assert summary_event.fields["sweep_inventory_replay_candidate_request_count"] == 1
    assert summary_event.fields["sweep_inventory_replay_candidate_coverage_pct"] == 100.0
    assert summary_event.fields["sweep_inventory_projected_write_rtt_savings_ms"] == 37.0
    assert summary_event.fields["sweep_inventory_request_count_by_kind"] == {
        "uds_did": 1
    }


def test_inventory_marks_gm_a9_as_observe_only_by_default() -> None:
    learner = SweepPatternLearner(
        LocalSweepConfig(
            enabled=True,
            mode="shadow_local",
            min_cycles=1,
            shadow_allow_gm_a9_packet=False,
        )
    )
    write_body = _write_body(b"\x00\x00\x07\xe0\xa9\x81\x1a")

    learner.observe_write(write_body, connection_epoch="epoch-1")
    learner.observe_write_response(
        write_body,
        MsgType.WRITE_MSGS_RSP,
        _write_rsp_body(),
        connection_epoch="epoch-1",
        duration_ms=30.0,
        network_ms=25.0,
    )
    _observed, events = learner.observe_read_response(
        _read_req_body(),
        _read_rsp_body(b"\x00\x00\x05\xe8\xa9\x81\x1a\x00"),
        connection_epoch="epoch-1",
        duration_ms=5.0,
        network_ms=2.0,
    )

    signature_event = [
        event for event in events if event.event_type == "sweep.inventory.signature"
    ][0]
    summary_event = [
        event for event in events if event.event_type == "sweep.inventory.summary"
    ][0]

    assert signature_event.fields["sweep_identifier_kind"] == "gm_a9_packet"
    assert signature_event.fields["sweep_inventory_learned"] is True
    assert signature_event.fields["sweep_inventory_shadow_eligible"] is False
    assert signature_event.fields["sweep_inventory_replay_candidate"] is False
    assert signature_event.fields["sweep_inventory_shadow_eligibility_reason"] == (
        "gm_a9_packet_observe_only"
    )
    assert signature_event.fields["sweep_inventory_replay_eligibility_reason"] == (
        "gm_a9_packet_observe_only"
    )
    assert signature_event.fields["sweep_inventory_eligibility_reason"] == (
        "gm_a9_packet_observe_only"
    )
    assert summary_event.fields["sweep_inventory_learned_signature_count"] == 1
    assert summary_event.fields["sweep_inventory_replay_candidate_signature_count"] == 0
    assert summary_event.fields["sweep_inventory_replay_candidate_request_count"] == 0
    assert summary_event.fields["sweep_inventory_replay_candidate_coverage_pct"] == 0.0
    assert summary_event.fields["sweep_inventory_request_count_by_kind"] == {
        "gm_a9_packet": 1
    }
    assert summary_event.fields["sweep_inventory_replay_candidate_request_count_by_kind"] == {}


def test_replay_candidate_for_write_rejects_gm_a9_even_when_learned() -> None:
    learner = SweepPatternLearner(
        LocalSweepConfig(
            enabled=True,
            mode="active_replay",
            min_cycles=1,
        )
    )
    write_body = _write_body(b"\x00\x00\x07\xe0\xa9\x81\x1a")

    learner.observe_write(write_body, connection_epoch="epoch-1")
    learner.observe_write_response(
        write_body,
        MsgType.WRITE_MSGS_RSP,
        _write_rsp_body(),
        connection_epoch="epoch-1",
        duration_ms=30.0,
        network_ms=25.0,
    )
    learner.observe_read_response(
        _read_req_body(),
        _read_rsp_body(b"\x00\x00\x05\xe8\xa9\x81\x1a\x00"),
        connection_epoch="epoch-1",
        duration_ms=5.0,
        network_ms=2.0,
    )

    assert (
        learner.replay_candidate_for_write(
            write_body,
            connection_epoch="epoch-1",
        )
        is None
    )


def test_learner_rejects_non_data_read_response_and_epoch_drift() -> None:
    learner = SweepPatternLearner(LocalSweepConfig(enabled=True, min_cycles=1))
    learner.observe_write(_write_body(), connection_epoch="epoch-1")

    _observed, events = learner.observe_read_response(
        _read_req_body(),
        ProtocolEncoder.encode_read_msgs_rsp(0x10, [])[HEADER_SIZE:],
        connection_epoch="epoch-1",
    )
    assert events[0].fields["sweep_rejection_reason"] == "read_response_not_data"

    learner.observe_write(_write_body(), connection_epoch="epoch-1")
    _observed, events = learner.observe_read_response(
        _read_req_body(),
        _read_rsp_body(),
        connection_epoch="epoch-2",
    )
    assert events[0].fields["sweep_rejection_reason"] == "connection_epoch_changed"


def test_rejected_write_clears_pending_signature_on_same_channel() -> None:
    learner = SweepPatternLearner(LocalSweepConfig(enabled=True, min_cycles=1))

    learner.observe_write(_write_body(), connection_epoch="epoch-1")
    rejection_events = learner.observe_write(
        _write_body(b"\x10\x01"),
        connection_epoch="epoch-1",
    )
    observed, read_events = learner.observe_read_response(
        _read_req_body(),
        _read_rsp_body(),
        connection_epoch="epoch-1",
    )

    assert rejection_events[0].event_type == "sweep.pattern.rejected"
    assert observed is None
    assert read_events == []
    assert learner.candidate_count == 0
