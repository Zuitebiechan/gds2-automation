import math
import struct

from vci_proxy.protocol import HEADER_SIZE, MsgType, ProtocolEncoder

from vci_proxy.benchmark import (
    attach_timing_trailer,
    compare_benchmark_summaries,
    generate_benchmark_report,
    make_proxy_benchmark_event,
    strip_timing_trailer,
    summarize_benchmark_events,
    TIMING_MAGIC,
    TIMING_TRAILER_SIZE,
)


def _response_body(encoded: bytes) -> bytes:
    return encoded[HEADER_SIZE:]


# --- helpers for building test data ---

_MSG = {"protocol_id": 6, "rx_status": 0, "tx_flags": 0, "timestamp": 1, "data": b"x"}


def _read_msgs_event(
    started: float,
    duration: float,
    msg_count: int,
    *,
    hw_ms: float | None = None,
    cache_hit: bool = False,
    label: str = "test",
) -> dict:
    return make_proxy_benchmark_event(
        run_label=label,
        source="proxy_server",
        started_at_s=started,
        duration_ms=duration,
        msg_type=MsgType.READ_MSGS_REQ,
        req_body=struct.pack(">III", 1, 1, 100),
        resp_type=MsgType.READ_MSGS_RSP,
        resp_body=_response_body(
            ProtocolEncoder.encode_read_msgs_rsp(
                0, [_MSG] * msg_count, sequence=1
            )
        ),
        cache_hit=cache_hit,
        status="success",
        hw_ms=hw_ms,
    )


# ===================================================================
# Original tests (preserved, slightly reformatted)
# ===================================================================


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


# ===================================================================
# Timing trailer tests
# ===================================================================


def test_attach_timing_trailer_updates_length_and_appends_bytes():
    encoded = ProtocolEncoder.encode_read_msgs_rsp(0, [], sequence=1)
    original_len = len(encoded)
    hw_ms = 12.345

    result = attach_timing_trailer(encoded, hw_ms)

    assert len(result) == original_len + TIMING_TRAILER_SIZE
    # Length field in header should be updated
    new_length = struct.unpack(">I", result[4:8])[0]
    old_length = struct.unpack(">I", encoded[4:8])[0]
    assert new_length == old_length + TIMING_TRAILER_SIZE
    # Trailer has magic + double
    marker = struct.unpack(">I", result[-12:-8])[0]
    assert marker == TIMING_MAGIC
    hw = struct.unpack(">d", result[-8:])[0]
    assert math.isclose(hw, hw_ms, rel_tol=1e-9)


def test_strip_timing_trailer_extracts_hw_ms():
    encoded = ProtocolEncoder.encode_read_msgs_rsp(0, [], sequence=1)
    body_original = encoded[HEADER_SIZE:]
    hw_ms = 7.89

    with_trailer = attach_timing_trailer(encoded, hw_ms)
    body_with_trailer = with_trailer[HEADER_SIZE:]

    clean_body, extracted_hw = strip_timing_trailer(body_with_trailer)

    assert clean_body == body_original
    assert extracted_hw is not None
    assert math.isclose(extracted_hw, hw_ms, rel_tol=1e-9)


def test_strip_timing_trailer_returns_none_when_no_trailer():
    body = b"\x00\x00\x00\x00\x00\x00\x00\x08"  # random bytes, no magic
    clean, hw = strip_timing_trailer(body)
    assert clean == body
    assert hw is None


def test_strip_timing_trailer_returns_none_for_short_body():
    body = b"\x01\x02\x03"
    clean, hw = strip_timing_trailer(body)
    assert clean == body
    assert hw is None


# ===================================================================
# hw_ms / network_ms field tests
# ===================================================================


def test_make_event_with_hw_ms_computes_network_ms():
    event = _read_msgs_event(0.0, 50.0, 3, hw_ms=30.0)

    assert event["hw_ms"] == 30.0
    assert event["network_ms"] == 20.0
    assert event["duration_ms"] == 50.0


def test_make_event_without_hw_ms_leaves_fields_none():
    event = _read_msgs_event(0.0, 50.0, 3)

    assert event["hw_ms"] is None
    assert event["network_ms"] is None


def test_make_event_hw_ms_larger_than_duration_clamps_network_to_zero():
    # Can happen due to clock skew between server monotonic and client monotonic
    event = _read_msgs_event(0.0, 10.0, 1, hw_ms=15.0)

    assert event["hw_ms"] == 15.0
    assert event["network_ms"] == 0.0  # clamped, not negative


# ===================================================================
# READ_MSGS bucketing tests
# ===================================================================


def test_summarize_creates_read_msgs_empty_and_data_buckets():
    events = [
        # Two empty reads
        _read_msgs_event(0.0, 5.0, 0),
        _read_msgs_event(0.5, 6.0, 0),
        # One data read
        _read_msgs_event(1.0, 40.0, 3),
    ]

    summary = summarize_benchmark_events(events)

    assert "READ_MSGS_REQ" in summary["by_message"]
    assert "READ_MSGS_REQ(empty)" in summary["by_message"]
    assert "READ_MSGS_REQ(data)" in summary["by_message"]

    empty = summary["by_message"]["READ_MSGS_REQ(empty)"]
    data = summary["by_message"]["READ_MSGS_REQ(data)"]

    assert empty["count"] == 2
    assert data["count"] == 1
    assert data["message_count_total"] == 3
    assert empty["message_count_total"] == 0
    assert data["latency_ms"]["avg"] == 40.0
    assert math.isclose(empty["latency_ms"]["avg"], 5.5, rel_tol=0.01)


def test_summarize_no_empty_bucket_when_all_have_data():
    events = [
        _read_msgs_event(0.0, 20.0, 2),
        _read_msgs_event(1.0, 30.0, 5),
    ]

    summary = summarize_benchmark_events(events)

    assert "READ_MSGS_REQ(data)" in summary["by_message"]
    assert "READ_MSGS_REQ(empty)" not in summary["by_message"]


def test_summarize_includes_hw_and_network_blocks_when_present():
    events = [
        _read_msgs_event(0.0, 50.0, 2, hw_ms=30.0),
        _read_msgs_event(1.0, 60.0, 3, hw_ms=35.0),
    ]

    summary = summarize_benchmark_events(events)
    read = summary["by_message"]["READ_MSGS_REQ"]

    assert "hw_ms" in read
    assert "network_ms" in read
    assert math.isclose(read["hw_ms"]["avg"], 32.5, rel_tol=0.01)
    assert math.isclose(read["network_ms"]["avg"], 22.5, rel_tol=0.01)


# ===================================================================
# Markdown report generation tests
# ===================================================================


def test_generate_benchmark_report_contains_methodology():
    summary = summarize_benchmark_events([_read_msgs_event(0.0, 10.0, 0)])
    report = generate_benchmark_report(summary)

    assert "# VCI Proxy Benchmark Report" in report
    assert "## Measurement Methodology" in report
    assert "duration_ms" in report
    assert "hw_ms" in report
    assert "network_ms" in report
    assert "TME0" in report


def test_generate_benchmark_report_contains_overall_stats():
    events = [
        _read_msgs_event(0.0, 10.0, 0, cache_hit=True),
        _read_msgs_event(0.5, 20.0, 2),
    ]
    summary = summarize_benchmark_events(events)
    report = generate_benchmark_report(summary)

    assert "## Overall Statistics" in report
    assert "Total events" in report
    assert "Cache hits" in report


def test_generate_benchmark_report_shows_read_msgs_bucketing():
    events = [
        _read_msgs_event(0.0, 5.0, 0),
        _read_msgs_event(0.5, 40.0, 3),
    ]
    summary = summarize_benchmark_events(events)
    report = generate_benchmark_report(summary)

    assert "## READ_MSGS Bucketing" in report
    assert "READ_MSGS_REQ(empty)" in report
    assert "READ_MSGS_REQ(data)" in report


def test_generate_benchmark_report_shows_network_vs_hardware():
    events = [
        _read_msgs_event(0.0, 50.0, 2, hw_ms=30.0),
        _read_msgs_event(1.0, 60.0, 3, hw_ms=35.0),
    ]
    summary = summarize_benchmark_events(events)
    report = generate_benchmark_report(summary)

    assert "## Network vs Hardware Latency" in report
    assert "Avg hw" in report
    assert "Avg network" in report


def test_generate_benchmark_report_shows_cache_effectiveness():
    events = [
        _read_msgs_event(0.0, 0.0, 0, cache_hit=True),
        _read_msgs_event(0.5, 0.0, 0, cache_hit=True),
        _read_msgs_event(1.0, 20.0, 2),
    ]
    summary = summarize_benchmark_events(events)
    report = generate_benchmark_report(summary)

    assert "## Cache Effectiveness" in report
    assert "Hit rate" in report


def test_generate_benchmark_report_custom_title():
    summary = summarize_benchmark_events([_read_msgs_event(0.0, 10.0, 0)])
    report = generate_benchmark_report(summary, title="My Custom Title")

    assert "# My Custom Title" in report
