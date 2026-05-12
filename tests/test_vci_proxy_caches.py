from __future__ import annotations

import struct

from vci_proxy.cache_filter_dedup import FilterDeduplicationCache
from vci_proxy.cache_ioctl import (
    IOCTL_GET_CONFIG,
    IOCTL_READ_VBATT,
    IoctlCache,
)
from vci_proxy.cache_read_msgs import BUFFER_EMPTY, ReadMsgsCache
from vci_proxy.config import (
    FilterDeduplicationConfig,
    IoctlCacheConfig,
    ReadMsgsCacheConfig,
)
from vci_proxy.prefetch_read_msgs import PrefetchReadMsgsBuffer
from vci_proxy.protocol import HEADER_SIZE, Message, MsgType, ProtocolDecoder
from vci_proxy.protocol import ProtocolEncoder


def test_read_msgs_cache_serves_recent_buffer_empty_response(monkeypatch) -> None:
    cache = ReadMsgsCache(ReadMsgsCacheConfig(enabled=True, ttl_ms=150))

    monotonic_values = iter([10.0, 10.1])
    monkeypatch.setattr("vci_proxy.cache_read_msgs.time.monotonic", lambda: next(monotonic_values))

    cache.record_result(33, BUFFER_EMPTY)
    response = cache.try_serve_from_cache(33, num_msgs=4, timeout=25, sequence=9)

    assert response is not None
    magic, length, msg_type, sequence = Message.decode_header(response[:HEADER_SIZE])
    assert magic > 0
    assert length == len(response)
    assert msg_type == MsgType.READ_MSGS_RSP
    assert sequence == 9
    assert ProtocolDecoder.decode_read_msgs_rsp(response[HEADER_SIZE:]) == (BUFFER_EMPTY, [])
    assert cache.stats == (1, 0)


def test_read_msgs_cache_misses_when_disabled_expired_or_invalidated(monkeypatch) -> None:
    disabled = ReadMsgsCache(ReadMsgsCacheConfig(enabled=False, ttl_ms=150))
    assert disabled.try_serve_from_cache(1, 1, 1, 1) is None

    cache = ReadMsgsCache(ReadMsgsCacheConfig(enabled=True, ttl_ms=150))
    monotonic_values = iter([20.0, 20.3, 20.4, 20.4])
    monkeypatch.setattr("vci_proxy.cache_read_msgs.time.monotonic", lambda: next(monotonic_values))

    cache.record_result(44, BUFFER_EMPTY)
    assert cache.try_serve_from_cache(44, 1, 1, 1) is None
    cache.record_result(44, 0)
    assert cache.try_serve_from_cache(44, 1, 1, 2) is None
    cache.invalidate_channel(44)
    cache.clear()
    assert cache.stats == (0, 2)


def test_read_msgs_cache_allows_confirmed_empty_after_same_channel_write(monkeypatch) -> None:
    cache = ReadMsgsCache(
        ReadMsgsCacheConfig(
            enabled=True,
            ttl_ms=500,
            post_write_bypass_ms=150,
            active_ttl_ms=500,
        )
    )
    monotonic_values = iter([10.0, 10.01, 10.02, 10.03, 10.2])
    monkeypatch.setattr("vci_proxy.cache_read_msgs.time.monotonic", lambda: next(monotonic_values))

    cache.record_result(44, BUFFER_EMPTY)
    cache.record_write(44)
    cache.record_result(44, BUFFER_EMPTY)

    response = cache.try_serve_from_cache(44, 1, 1, 1)
    assert response is not None
    assert ProtocolDecoder.decode_read_msgs_rsp(response[HEADER_SIZE:]) == (BUFFER_EMPTY, [])
    assert cache.stats == (1, 0)


def test_read_msgs_cache_post_write_bypass_does_not_affect_unrelated_channels(monkeypatch) -> None:
    cache = ReadMsgsCache(
        ReadMsgsCacheConfig(enabled=True, ttl_ms=150, post_write_bypass_ms=150)
    )
    monotonic_values = iter([20.0, 20.0, 20.01, 20.02])
    monkeypatch.setattr("vci_proxy.cache_read_msgs.time.monotonic", lambda: next(monotonic_values))

    cache.record_result(44, BUFFER_EMPTY)
    cache.record_result(45, BUFFER_EMPTY)
    cache.record_write(44)

    response = cache.try_serve_from_cache(45, 1, 1, 9)

    assert response is not None
    assert ProtocolDecoder.decode_read_msgs_rsp(response[HEADER_SIZE:]) == (BUFFER_EMPTY, [])
    assert cache.stats == (1, 0)


def test_read_msgs_cache_post_write_bypass_zero_keeps_empty_entry_but_still_uses_active_ttl(monkeypatch) -> None:
    cache = ReadMsgsCache(
        ReadMsgsCacheConfig(
            enabled=True,
            ttl_ms=150,
            post_write_bypass_ms=0,
            active_ttl_ms=25,
            active_window_ms=500,
        )
    )
    monotonic_values = iter([30.0, 30.01, 30.02, 30.04])
    monkeypatch.setattr("vci_proxy.cache_read_msgs.time.monotonic", lambda: next(monotonic_values))

    cache.record_result(44, BUFFER_EMPTY)
    cache.record_write(44)

    response = cache.try_serve_from_cache(44, 1, 1, 9)
    assert response is not None
    assert cache.try_serve_from_cache(44, 1, 1, 10) is None
    assert cache.stats == (1, 1)


def test_read_msgs_cache_can_restore_legacy_write_behavior_with_active_window_disabled(monkeypatch) -> None:
    cache = ReadMsgsCache(
        ReadMsgsCacheConfig(
            enabled=True,
            ttl_ms=150,
            post_write_bypass_ms=0,
            active_window_ms=0,
        )
    )
    monotonic_values = iter([30.0, 30.01, 30.04])
    monkeypatch.setattr("vci_proxy.cache_read_msgs.time.monotonic", lambda: next(monotonic_values))

    cache.record_result(44, BUFFER_EMPTY)
    cache.record_write(44)
    response = cache.try_serve_from_cache(44, 1, 1, 9)

    assert response is not None
    assert ProtocolDecoder.decode_read_msgs_rsp(response[HEADER_SIZE:]) == (BUFFER_EMPTY, [])
    assert cache.stats == (1, 0)


def test_read_msgs_cache_uses_short_active_ttl_after_write(monkeypatch) -> None:
    cache = ReadMsgsCache(
        ReadMsgsCacheConfig(
            enabled=True,
            ttl_ms=150,
            post_write_bypass_ms=0,
            active_ttl_ms=25,
            active_window_ms=500,
        )
    )
    monotonic_values = iter([40.0, 40.01, 40.04])
    monkeypatch.setattr("vci_proxy.cache_read_msgs.time.monotonic", lambda: next(monotonic_values))

    cache.record_result(55, BUFFER_EMPTY)
    cache.record_write(55)

    assert cache.try_serve_from_cache(55, 1, 1, 9) is None
    assert cache.stats == (0, 1)


def test_read_msgs_cache_adapts_active_ttl_to_confirmed_empty_cadence(monkeypatch) -> None:
    cache = ReadMsgsCache(
        ReadMsgsCacheConfig(
            enabled=True,
            ttl_ms=150,
            post_write_bypass_ms=0,
            active_ttl_ms=25,
            active_adaptive_ttl_max_ms=70,
            active_adaptive_ttl_margin_ms=8,
            active_window_ms=500,
        )
    )
    monotonic_values = iter([45.0, 45.0, 45.031, 45.062])
    monkeypatch.setattr("vci_proxy.cache_read_msgs.time.monotonic", lambda: next(monotonic_values))

    cache.record_write(55)
    cache.record_result(55, BUFFER_EMPTY)
    cache.record_result(55, BUFFER_EMPTY)

    response = cache.try_serve_from_cache(55, 1, 1, 9)

    assert response is not None
    assert ProtocolDecoder.decode_read_msgs_rsp(response[HEADER_SIZE:]) == (BUFFER_EMPTY, [])
    assert cache.stats == (1, 0)
    state = cache.observability_state(55, 1, now=45.062)
    assert state["empty_cache_recent_empty_gap_ms"] == 31.0
    assert state["empty_cache_effective_ttl_ms"] == 39.0
    assert state["empty_cache_adaptive_ttl_applied"] is True


def test_read_msgs_cache_adaptive_ttl_covers_slow_confirmed_empty_cadence(monkeypatch) -> None:
    cache = ReadMsgsCache(
        ReadMsgsCacheConfig(
            enabled=True,
            ttl_ms=150,
            post_write_bypass_ms=0,
            active_ttl_ms=25,
            active_adaptive_ttl_max_ms=70,
            active_adaptive_ttl_margin_ms=8,
            active_window_ms=500,
        )
    )
    monotonic_values = iter([45.0, 45.0, 45.062, 45.125])
    monkeypatch.setattr("vci_proxy.cache_read_msgs.time.monotonic", lambda: next(monotonic_values))

    cache.record_write(55)
    cache.record_result(55, BUFFER_EMPTY)
    cache.record_result(55, BUFFER_EMPTY)

    response = cache.try_serve_from_cache(55, 1, 1, 9)

    assert response is not None
    assert ProtocolDecoder.decode_read_msgs_rsp(response[HEADER_SIZE:]) == (BUFFER_EMPTY, [])
    state = cache.observability_state(55, 1, now=45.125)
    assert state["empty_cache_recent_empty_gap_ms"] == 62.0
    assert state["empty_cache_effective_ttl_ms"] == 70.0
    assert state["empty_cache_entry_expired"] is False


def test_read_msgs_cache_resets_adaptive_empty_cadence_after_data(monkeypatch) -> None:
    cache = ReadMsgsCache(
        ReadMsgsCacheConfig(
            enabled=True,
            ttl_ms=150,
            post_write_bypass_ms=0,
            active_ttl_ms=25,
            active_adaptive_ttl_max_ms=70,
            active_adaptive_ttl_margin_ms=8,
            active_window_ms=500,
        )
    )
    monotonic_values = iter([46.0, 46.0, 46.031, 46.05, 46.06, 46.091])
    monkeypatch.setattr("vci_proxy.cache_read_msgs.time.monotonic", lambda: next(monotonic_values))

    cache.record_write(55)
    cache.record_result(55, BUFFER_EMPTY)
    cache.record_result(55, BUFFER_EMPTY)
    cache.record_result(55, 0, message_count=1)
    cache.record_result(55, BUFFER_EMPTY)

    assert cache.try_serve_from_cache(55, 1, 1, 9) is None
    assert cache.stats == (0, 1)
    state = cache.observability_state(55, 1, now=46.091)
    assert "empty_cache_recent_empty_gap_ms" not in state
    assert state["empty_cache_effective_ttl_ms"] == 25.0


def test_read_msgs_cache_uses_idle_ttl_when_channel_is_quiet(monkeypatch) -> None:
    cache = ReadMsgsCache(
        ReadMsgsCacheConfig(
            enabled=True,
            ttl_ms=150,
            post_write_bypass_ms=0,
            active_ttl_ms=25,
            active_window_ms=50,
        )
    )
    monotonic_values = iter([50.0, 50.2, 50.3])
    monkeypatch.setattr("vci_proxy.cache_read_msgs.time.monotonic", lambda: next(monotonic_values))

    cache.record_write(66)
    cache.record_result(66, BUFFER_EMPTY)
    response = cache.try_serve_from_cache(66, 1, 1, 9)

    assert response is not None
    assert ProtocolDecoder.decode_read_msgs_rsp(response[HEADER_SIZE:]) == (BUFFER_EMPTY, [])
    assert cache.stats == (1, 0)


def test_read_msgs_cache_recent_data_read_uses_active_ttl_without_caching_data(monkeypatch) -> None:
    cache = ReadMsgsCache(
        ReadMsgsCacheConfig(
            enabled=True,
            ttl_ms=150,
            post_write_bypass_ms=0,
            active_ttl_ms=25,
            active_window_ms=500,
        )
    )
    monotonic_values = iter([60.0, 60.01, 60.05])
    monkeypatch.setattr("vci_proxy.cache_read_msgs.time.monotonic", lambda: next(monotonic_values))

    cache.record_result(77, 0, message_count=2)
    cache.record_result(77, BUFFER_EMPTY)

    assert cache.try_serve_from_cache(77, 1, 1, 9) is None
    assert cache.stats == (0, 1)


def test_read_msgs_cache_only_serves_small_timeout_polls(monkeypatch) -> None:
    cache = ReadMsgsCache(
        ReadMsgsCacheConfig(
            enabled=True,
            ttl_ms=150,
            max_cacheable_timeout_ms=10,
        )
    )
    monotonic_values = iter([70.0, 70.01, 70.02])
    monkeypatch.setattr("vci_proxy.cache_read_msgs.time.monotonic", lambda: next(monotonic_values))

    cache.record_result(88, BUFFER_EMPTY)
    assert cache.try_serve_from_cache(88, 1, 25, 9) is None
    response = cache.try_serve_from_cache(88, 1, 10, 10)

    assert response is not None
    assert ProtocolDecoder.decode_read_msgs_rsp(response[HEADER_SIZE:]) == (BUFFER_EMPTY, [])


def test_prefetch_read_msgs_buffer_serves_data_once_in_fifo_order() -> None:
    buffer = PrefetchReadMsgsBuffer(enabled=True, max_messages=3)
    first = {"protocol_id": 6, "rx_status": 0, "tx_flags": 0, "timestamp": 1, "data": b"\x01"}
    second = {"protocol_id": 6, "rx_status": 0, "tx_flags": 0, "timestamp": 2, "data": b"\x02"}
    read_rsp_body = ProtocolEncoder.encode_read_msgs_rsp(
        0,
        [first, second],
        sequence=0,
    )[HEADER_SIZE:]

    assert buffer.record_read_rsp_body(44, read_rsp_body) == 2

    response = buffer.try_serve(44, num_msgs=1, sequence=10)
    assert response is not None
    _magic, _length, msg_type, sequence = Message.decode_header(response[:HEADER_SIZE])
    assert (msg_type, sequence) == (MsgType.READ_MSGS_RSP, 10)
    assert ProtocolDecoder.decode_read_msgs_rsp(response[HEADER_SIZE:]) == (0, [first])

    response = buffer.try_serve(44, num_msgs=2, sequence=11)
    assert response is not None
    assert ProtocolDecoder.decode_read_msgs_rsp(response[HEADER_SIZE:]) == (0, [second])
    assert buffer.try_serve(44, num_msgs=1, sequence=12) is None


def test_prefetch_read_msgs_buffer_reports_source_and_age(monkeypatch) -> None:
    buffer = PrefetchReadMsgsBuffer(enabled=True, max_messages=3)
    read_rsp_body = ProtocolEncoder.encode_read_msgs_rsp(
        0,
        [{"protocol_id": 6, "data": b"\x62"}],
        sequence=0,
    )[HEADER_SIZE:]

    monotonic_values = iter([100.0, 100.025])
    monkeypatch.setattr(
        "vci_proxy.prefetch_read_msgs.time.monotonic",
        lambda: next(monotonic_values),
    )

    assert buffer.record_read_rsp_body(44, read_rsp_body, source="read_collect") == 1
    drain = buffer.drain(44, 1)

    assert drain.served_count == 1
    assert dict(drain.source_counts) == {"read_collect": 1}
    assert drain.age_min_ms == 25.0
    assert drain.age_avg_ms == 25.0
    assert drain.age_max_ms == 25.0


def test_prefetch_read_msgs_buffer_ignores_empty_and_clears_channel() -> None:
    buffer = PrefetchReadMsgsBuffer(enabled=True, max_messages=3)
    empty_body = ProtocolEncoder.encode_read_msgs_rsp(BUFFER_EMPTY, [], sequence=0)[HEADER_SIZE:]
    data_body = ProtocolEncoder.encode_read_msgs_rsp(
        0,
        [{"protocol_id": 6, "data": b"\x7e"}],
        sequence=0,
    )[HEADER_SIZE:]

    assert buffer.record_read_rsp_body(44, empty_body) == 0
    assert buffer.record_read_rsp_body(44, data_body) == 1
    buffer.clear_channel(44)

    assert buffer.try_serve(44, num_msgs=1, sequence=12) is None


def test_filter_dedup_cache_reuses_successful_start_filter_response_and_cleans_up() -> None:
    cache = FilterDeduplicationCache(FilterDeduplicationConfig(enabled=True))
    body = struct.pack(">II", 12, 7) + b"\x00\x00\x00"
    response_body = struct.pack(">II", 0, 501)

    cache.record_result(body, filter_id=501, return_code=0, response_body=response_body)
    response = cache.try_dedup(body, sequence=77)

    assert response is not None
    _magic, length, msg_type, sequence = Message.decode_header(response[:HEADER_SIZE])
    assert length == len(response)
    assert msg_type == MsgType.START_FILTER_RSP
    assert sequence == 77
    assert ProtocolDecoder.decode_start_filter_rsp(response[HEADER_SIZE:]) == (0, 501)
    assert cache.stats == (1, 0)

    cache.on_stop_filter(501)
    assert cache.try_dedup(body, sequence=78) is None
    assert cache.stats == (1, 1)


def test_filter_dedup_cache_ignores_failed_results_and_channel_invalidation() -> None:
    cache = FilterDeduplicationCache(FilterDeduplicationConfig(enabled=True))
    body_a = struct.pack(">II", 5, 1) + b"\x00\x00\x00"
    body_b = struct.pack(">II", 5, 2) + b"\x00\x00\x00"

    cache.record_result(body_a, filter_id=100, return_code=1, response_body=b"ignored")
    assert cache.try_dedup(body_a, sequence=1) is None

    cache.record_result(body_a, filter_id=101, return_code=0, response_body=struct.pack(">II", 0, 101))
    cache.record_result(body_b, filter_id=102, return_code=0, response_body=struct.pack(">II", 0, 102))
    cache.invalidate_channel(5)

    assert cache.try_dedup(body_a, sequence=2) is None
    assert cache.try_dedup(body_b, sequence=3) is None

    disabled = FilterDeduplicationCache(FilterDeduplicationConfig(enabled=False))
    disabled.record_result(body_a, filter_id=1, return_code=0, response_body=b"")
    assert disabled.try_dedup(body_a, sequence=4) is None


def test_ioctl_cache_returns_cacheable_successes_and_invalidates_entries(monkeypatch) -> None:
    cache = IoctlCache(IoctlCacheConfig(enabled=True, ttl_s=5))
    monotonic_values = iter([30.0, 31.0, 32.0])
    monkeypatch.setattr("vci_proxy.cache_ioctl.time.monotonic", lambda: next(monotonic_values))

    cache.record_result(9, IOCTL_READ_VBATT, 0, b"\x12\x34")
    assert cache.is_cacheable(IOCTL_READ_VBATT) is True
    assert cache.try_get_cached(9, IOCTL_READ_VBATT) == (0, b"\x12\x34")

    cache.invalidate_channel(9)
    assert cache.try_get_cached(9, IOCTL_READ_VBATT) is None
    assert cache.stats == (1, 1)


def test_ioctl_cache_skips_non_cacheable_failures_and_expired_entries(monkeypatch) -> None:
    cache = IoctlCache(IoctlCacheConfig(enabled=True, ttl_s=1))
    monotonic_values = iter([40.0, 42.0])
    monkeypatch.setattr("vci_proxy.cache_ioctl.time.monotonic", lambda: next(monotonic_values))

    cache.record_result(1, 0x99, 0, b"\x00")
    cache.record_result(1, IOCTL_GET_CONFIG, 1, b"\x00")
    cache.record_result(1, IOCTL_GET_CONFIG, 0, b"\xAA")
    assert cache.try_get_cached(1, IOCTL_GET_CONFIG) is None
    assert cache.try_get_cached(1, 0x99) is None

    disabled = IoctlCache(IoctlCacheConfig(enabled=False, ttl_s=5))
    disabled.record_result(1, IOCTL_READ_VBATT, 0, b"\x10")
    assert disabled.try_get_cached(1, IOCTL_READ_VBATT) is None
    disabled.invalidate()
