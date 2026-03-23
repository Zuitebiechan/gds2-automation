import math
import struct

from vci_proxy.protocol import HEADER_SIZE, MsgType, ProtocolEncoder

from vci_proxy.benchmark import (
    compare_benchmark_summaries,
    make_proxy_benchmark_event,
    summarize_benchmark_events,
)


def _response_body(encoded: bytes) -> bytes:
    return encoded[HEADER_SIZE:]


def test_make_proxy_benchmark_event_extracts_read_msgs_metrics():
    req_body = struct.pack(">III", 7, 1, 100)
    rsp_body = _response_body(
        ProtocolEncoder.encode_read_msgs_rsp(
            0,
            [
                {"protocol_id": 6, "rx_status": 0, "tx_flags": 0, "timestamp": 11, "data": b"\x01"},
                {"protocol_id": 6, "rx_status": 0, "tx_flags": 0, "timestamp": 12, "data": b"\x02"},
                {"protocol_id": 6, "rx_status": 0, "tx_flags": 0, "timestamp": 13, "data": b"\x03"},
            ],
            sequence=99,
        )
    )

    event = make_proxy_benchmark_event(
        run_label="cloud",
        source="proxy_server",
        started_at_s=100.0,
        duration_ms=42.5,
        msg_type=MsgType.READ_MSGS_REQ,
        req_body=req_body,
        resp_type=MsgType.READ_MSGS_RSP,
        resp_body=rsp_body,
        cache_hit=False,
        status="success",
    )

    assert event["run_label"] == "cloud"
    assert event["source"] == "proxy_server"
    assert event["msg_name"] == "READ_MSGS_REQ"
    assert event["channel_id"] == 7
    assert event["return_code"] == 0
    assert event["message_count"] == 3
    assert event["duration_ms"] == 42.5
    assert event["status"] == "success"
    assert event["cache_hit"] is False


def test_summarize_benchmark_events_reports_latency_and_throughput():
    events = [
        make_proxy_benchmark_event(
            run_label="baseline",
            source="proxy_server",
            started_at_s=0.0,
            duration_ms=20.0,
            msg_type=MsgType.READ_MSGS_REQ,
            req_body=struct.pack(">III", 1, 1, 100),
            resp_type=MsgType.READ_MSGS_RSP,
            resp_body=_response_body(
                ProtocolEncoder.encode_read_msgs_rsp(
                    0,
                    [{"protocol_id": 6, "rx_status": 0, "tx_flags": 0, "timestamp": 1, "data": b"x"}] * 4,
                    sequence=1,
                )
            ),
            cache_hit=False,
            status="success",
        ),
        make_proxy_benchmark_event(
            run_label="baseline",
            source="proxy_server",
            started_at_s=0.5,
            duration_ms=30.0,
            msg_type=MsgType.READ_MSGS_REQ,
            req_body=struct.pack(">III", 1, 1, 100),
            resp_type=MsgType.READ_MSGS_RSP,
            resp_body=_response_body(
                ProtocolEncoder.encode_read_msgs_rsp(
                    0,
                    [{"protocol_id": 6, "rx_status": 0, "tx_flags": 0, "timestamp": 1, "data": b"x"}] * 6,
                    sequence=2,
                )
            ),
            cache_hit=False,
            status="success",
        ),
        make_proxy_benchmark_event(
            run_label="baseline",
            source="proxy_server",
            started_at_s=1.0,
            duration_ms=45.0,
            msg_type=MsgType.WRITE_MSGS_REQ,
            req_body=b"",
            resp_type=MsgType.WRITE_MSGS_RSP,
            resp_body=_response_body(ProtocolEncoder.encode_write_msgs_rsp(0, 1, sequence=3)),
            cache_hit=False,
            status="success",
        ),
    ]

    summary = summarize_benchmark_events(events)

    read_msgs = summary["by_message"]["READ_MSGS_REQ"]
    assert summary["overall"]["event_count"] == 3
    assert math.isclose(summary["overall"]["window_s"], 1.0, rel_tol=0.001)
    assert read_msgs["count"] == 2
    assert read_msgs["message_count_total"] == 10
    assert math.isclose(read_msgs["message_rate_hz"], 10.0, rel_tol=0.001)
    assert math.isclose(read_msgs["request_rate_hz"], 2.0, rel_tol=0.001)
    assert read_msgs["latency_ms"]["p50"] == 25.0
    assert read_msgs["latency_ms"]["max"] == 30.0


def test_compare_benchmark_summaries_highlights_candidate_penalty():
    baseline = summarize_benchmark_events(
        [
            make_proxy_benchmark_event(
                run_label="baseline",
                source="proxy_server",
                started_at_s=0.0,
                duration_ms=18.0,
                msg_type=MsgType.READ_MSGS_REQ,
                req_body=struct.pack(">III", 1, 1, 100),
                resp_type=MsgType.READ_MSGS_RSP,
                resp_body=_response_body(
                    ProtocolEncoder.encode_read_msgs_rsp(
                        0,
                        [{"protocol_id": 6, "rx_status": 0, "tx_flags": 0, "timestamp": 1, "data": b"x"}] * 8,
                        sequence=1,
                    )
                ),
                cache_hit=False,
                status="success",
            )
        ]
    )
    candidate = summarize_benchmark_events(
        [
            make_proxy_benchmark_event(
                run_label="cloud",
                source="proxy_server",
                started_at_s=0.0,
                duration_ms=44.0,
                msg_type=MsgType.READ_MSGS_REQ,
                req_body=struct.pack(">III", 1, 1, 100),
                resp_type=MsgType.READ_MSGS_RSP,
                resp_body=_response_body(
                    ProtocolEncoder.encode_read_msgs_rsp(
                        0,
                        [{"protocol_id": 6, "rx_status": 0, "tx_flags": 0, "timestamp": 1, "data": b"x"}] * 5,
                        sequence=1,
                    )
                ),
                cache_hit=False,
                status="success",
            )
        ]
    )

    comparison = compare_benchmark_summaries(baseline, candidate)

    read_msgs = comparison["by_message"]["READ_MSGS_REQ"]
    assert read_msgs["latency_ms_delta"]["p50"] == 26.0
    assert read_msgs["message_rate_hz_delta"] == -3.0
    assert read_msgs["candidate_label"] == "cloud"
    assert read_msgs["baseline_label"] == "baseline"
