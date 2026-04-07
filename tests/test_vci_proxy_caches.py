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
from vci_proxy.protocol import HEADER_SIZE, Message, MsgType, ProtocolDecoder


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
    monotonic_values = iter([20.0, 20.3, 20.4])
    monkeypatch.setattr("vci_proxy.cache_read_msgs.time.monotonic", lambda: next(monotonic_values))

    cache.record_result(44, BUFFER_EMPTY)
    assert cache.try_serve_from_cache(44, 1, 1, 1) is None
    cache.record_result(44, 0)
    assert cache.try_serve_from_cache(44, 1, 1, 2) is None
    cache.invalidate_channel(44)
    cache.clear()
    assert cache.stats == (0, 2)


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
