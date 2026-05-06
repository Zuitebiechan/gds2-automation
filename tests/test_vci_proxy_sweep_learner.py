from __future__ import annotations

from vci_proxy.config import LocalSweepConfig
from vci_proxy.protocol import HEADER_SIZE, ProtocolEncoder
from vci_proxy.sweep_learner import SweepPatternLearner


def _write_body(data: bytes = b"\x22\xf4\x0c") -> bytes:
    return ProtocolEncoder.encode_write_msgs_req(
        44,
        [{"protocol_id": 6, "tx_flags": 0, "timestamp": 1, "data": data}],
        timeout=25,
    )[HEADER_SIZE:]


def _read_req_body() -> bytes:
    return ProtocolEncoder.encode_read_msgs_req(44, 1, 0)[HEADER_SIZE:]


def _read_rsp_body(data: bytes = b"\x62\xf4\x0c\x12\x34") -> bytes:
    return ProtocolEncoder.encode_read_msgs_rsp(
        0,
        [{"protocol_id": 6, "rx_status": 0, "tx_flags": 0, "timestamp": 2, "data": data}],
    )[HEADER_SIZE:]


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
    assert [event.event_type for event in first_read_events] == ["sweep.did.cadence"]
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
    ]
    learned_event = second_read_events[-1]
    assert learned_event.fields["sweep_candidate_count"] == 1
    assert learned_event.fields["sweep_confidence"] == 1.0
    assert learner.candidate_count == 1


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
