from __future__ import annotations

from vci_proxy.cache_vbatt import IOCTL_READ_VBATT, VbattCache
from vci_proxy.config import VbattCacheConfig


def test_vbatt_cache_serves_recent_successful_read(monkeypatch) -> None:
    cache = VbattCache(VbattCacheConfig(enabled=True, ttl_s=5))
    monotonic_values = iter([10.0, 11.0])
    monkeypatch.setattr("vci_proxy.cache_vbatt.time.monotonic", lambda: next(monotonic_values))

    cache.record_result(IOCTL_READ_VBATT, 0, b"\x12\x34")

    assert cache.try_get_cached(IOCTL_READ_VBATT) == (0, b"\x12\x34")
    assert cache.stats == (1, 0)


def test_vbatt_cache_misses_when_disabled_wrong_ioctl_or_expired(monkeypatch) -> None:
    disabled = VbattCache(VbattCacheConfig(enabled=False, ttl_s=5))
    disabled.record_result(IOCTL_READ_VBATT, 0, b"\x10")
    assert disabled.try_get_cached(IOCTL_READ_VBATT) is None

    cache = VbattCache(VbattCacheConfig(enabled=True, ttl_s=1))
    monotonic_values = iter([20.0, 22.0])
    monkeypatch.setattr("vci_proxy.cache_vbatt.time.monotonic", lambda: next(monotonic_values))

    cache.record_result(IOCTL_READ_VBATT, 0, b"\xAA")

    assert cache.try_get_cached(0x99) is None
    assert cache.try_get_cached(IOCTL_READ_VBATT) is None
    assert cache.stats == (0, 1)


def test_vbatt_cache_only_records_successful_vbatt_reads_and_can_invalidate() -> None:
    cache = VbattCache(VbattCacheConfig(enabled=True, ttl_s=5))

    cache.record_result(0x99, 0, b"\x00")
    cache.record_result(IOCTL_READ_VBATT, 1, b"\x00")
    assert cache.try_get_cached(IOCTL_READ_VBATT) is None

    cache.record_result(IOCTL_READ_VBATT, 0, b"\x20")
    cache.invalidate()

    assert cache.try_get_cached(IOCTL_READ_VBATT) is None
    assert cache.stats == (0, 2)
    assert VbattCache.is_read_vbatt(IOCTL_READ_VBATT) is True
    assert VbattCache.is_read_vbatt(0x09) is False
