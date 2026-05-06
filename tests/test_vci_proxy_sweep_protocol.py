from __future__ import annotations

from vci_proxy.protocol import HEADER_SIZE, Message, MsgType
from vci_proxy.sweep_protocol import (
    SWEEP_INTERNAL_REQUEST_RANGE,
    SWEEP_INTERNAL_RESPONSE_RANGE,
    SweepPlanResponse,
    SweepPlanStartRequest,
    SweepPlanStopRequest,
    SweepRequestSpec,
    SweepResultRecord,
    decode_sweep_drain_results_rsp,
    decode_sweep_plan_start_req,
    decode_sweep_plan_start_rsp,
    decode_sweep_plan_stop_req,
    decode_sweep_plan_stop_rsp,
    decode_sweep_status_rsp,
    encode_sweep_drain_results_req,
    encode_sweep_drain_results_rsp,
    encode_sweep_plan_start_req,
    encode_sweep_plan_start_rsp,
    encode_sweep_plan_stop_req,
    encode_sweep_plan_stop_rsp,
    encode_sweep_status_req,
    encode_sweep_status_rsp,
    is_sweep_message_type,
    SweepStatus,
)


def test_sweep_message_ids_are_reserved_internal_ranges() -> None:
    request_values = [
        MsgType.SWEEP_PLAN_START_REQ,
        MsgType.SWEEP_PLAN_STOP_REQ,
        MsgType.SWEEP_STATUS_REQ,
        MsgType.SWEEP_DRAIN_RESULTS_REQ,
    ]
    response_values = [
        MsgType.SWEEP_PLAN_START_RSP,
        MsgType.SWEEP_PLAN_STOP_RSP,
        MsgType.SWEEP_STATUS_RSP,
        MsgType.SWEEP_DRAIN_RESULTS_RSP,
    ]

    assert [int(value) for value in request_values] == list(
        range(SWEEP_INTERNAL_REQUEST_RANGE[0], SWEEP_INTERNAL_REQUEST_RANGE[1] + 1)
    )
    assert [int(value) for value in response_values] == list(
        range(SWEEP_INTERNAL_RESPONSE_RANGE[0], SWEEP_INTERNAL_RESPONSE_RANGE[1] + 1)
    )
    assert all(is_sweep_message_type(value) for value in request_values + response_values)
    assert not is_sweep_message_type(MsgType.WRITE_AND_COLLECT_READS_REQ)


def test_sweep_plan_start_and_stop_round_trip() -> None:
    request = SweepPlanStartRequest(
        plan_id="plan-1",
        connection_epoch="epoch-1",
        channel_id=44,
        max_result_age_ms=1000,
        min_item_interval_ms=5,
        shadow_max_seconds=30,
        requests=(
            SweepRequestSpec(
                signature_digest="abc",
                write_req_body=b"\x00\x00\x00,",
                read_num_msgs=2,
                read_timeout_ms=0,
            ),
        ),
    )
    encoded = encode_sweep_plan_start_req(request, sequence=11)
    _magic, _length, msg_type, sequence = Message.decode_header(encoded[:HEADER_SIZE])

    assert msg_type == MsgType.SWEEP_PLAN_START_REQ
    assert sequence == 11
    assert decode_sweep_plan_start_req(encoded[HEADER_SIZE:]) == request

    response = SweepPlanResponse(True, "plan-1", "ok")
    encoded_response = encode_sweep_plan_start_rsp(response, sequence=11)
    assert decode_sweep_plan_start_rsp(encoded_response[HEADER_SIZE:]) == response

    stop = SweepPlanStopRequest("plan-1", "test_stop")
    encoded_stop = encode_sweep_plan_stop_req(stop, sequence=12)
    assert decode_sweep_plan_stop_req(encoded_stop[HEADER_SIZE:]) == stop
    encoded_stop_rsp = encode_sweep_plan_stop_rsp(response, sequence=12)
    assert decode_sweep_plan_stop_rsp(encoded_stop_rsp[HEADER_SIZE:]) == response


def test_sweep_status_and_immediate_empty_drain_round_trip() -> None:
    status_req = encode_sweep_status_req(sequence=21)
    _magic, length, msg_type, sequence = Message.decode_header(status_req[:HEADER_SIZE])
    assert (length, msg_type, sequence) == (HEADER_SIZE, MsgType.SWEEP_STATUS_REQ, 21)

    status = SweepStatus(active_plan_id="plan-1", state="running", queued_results=0)
    encoded_status = encode_sweep_status_rsp(status, sequence=21)
    assert decode_sweep_status_rsp(encoded_status[HEADER_SIZE:]) == status

    drain_req = encode_sweep_drain_results_req(sequence=22)
    _magic, length, msg_type, sequence = Message.decode_header(drain_req[:HEADER_SIZE])
    assert (length, msg_type, sequence) == (
        HEADER_SIZE,
        MsgType.SWEEP_DRAIN_RESULTS_REQ,
        22,
    )

    encoded_drain = encode_sweep_drain_results_rsp([], sequence=22)
    assert decode_sweep_drain_results_rsp(encoded_drain[HEADER_SIZE:]) == ()


def test_sweep_drain_results_round_trip_with_result_record() -> None:
    result = SweepResultRecord(
        plan_id="plan-1",
        signature_digest="abc",
        return_code=0,
        read_rsp_body=b"\x00\x00\x00\x00\x00\x00\x00\x00",
        started_at_s=1.0,
        finished_at_s=2.0,
    )

    encoded = encode_sweep_drain_results_rsp([result], sequence=31)

    assert decode_sweep_drain_results_rsp(encoded[HEADER_SIZE:]) == (result,)
