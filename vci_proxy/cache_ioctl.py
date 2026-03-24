"""
Generalized read-only IOCTL response cache.

Replaces the narrow VBATT-only cache with a broader cache that covers
all read-only IOCTLs.  Successful responses (return_code == 0) are cached
per ``(channel_id, ioctl_id)`` with a configurable TTL.

Only IOCTLs whose ID appears in the ``cacheable_ids`` set are eligible.
By default this includes:

- 0x03  READ_VBATT        — battery voltage (changes slowly)
- 0x09  READ_PROG_VOLTAGE — programming voltage
- 0x01  GET_CONFIG         — channel configuration parameters

Write-type IOCTLs (SET_CONFIG, CLEAR_*_BUFFER, FIVE_BAUD_INIT, etc.)
are **never** cached because caching their response would mask failures.
"""

from __future__ import annotations

import time
import logging
from typing import Optional

from .config import IoctlCacheConfig

logger = logging.getLogger(__name__)

# J2534 read-only IOCTL IDs safe to cache
IOCTL_GET_CONFIG = 0x01
IOCTL_READ_VBATT = 0x03
IOCTL_READ_PROG_VOLTAGE = 0x09

DEFAULT_CACHEABLE_IDS: frozenset[int] = frozenset({
    IOCTL_GET_CONFIG,
    IOCTL_READ_VBATT,
    IOCTL_READ_PROG_VOLTAGE,
})


class IoctlCache:
    """Per-(channel, ioctl_id) response cache for read-only IOCTLs."""

    def __init__(self, config: IoctlCacheConfig):
        self._enabled = config.enabled
        self._ttl_s = float(config.ttl_s)
        self._cacheable_ids = DEFAULT_CACHEABLE_IDS
        # (channel_id, ioctl_id) -> (timestamp_mono, return_code, output_data)
        self._entries: dict[tuple[int, int], tuple[float, int, Optional[bytes]]] = {}
        self._hits = 0
        self._misses = 0

    def is_cacheable(self, ioctl_id: int) -> bool:
        """Check if the IOCTL ID is in the cacheable whitelist."""
        return ioctl_id in self._cacheable_ids

    def try_get_cached(
        self, channel_id: int, ioctl_id: int
    ) -> Optional[tuple[int, Optional[bytes]]]:
        """Return (return_code, output_data) from cache, or None on miss."""
        if not self._enabled:
            return None
        if ioctl_id not in self._cacheable_ids:
            return None

        key = (channel_id, ioctl_id)
        entry = self._entries.get(key)
        if entry is None:
            self._misses += 1
            return None

        ts, return_code, output_data = entry
        if time.monotonic() - ts > self._ttl_s:
            self._misses += 1
            return None

        self._hits += 1
        if self._hits % 100 == 0:
            total = self._hits + self._misses
            logger.info(
                f"[IoctlCache] hits={self._hits} misses={self._misses} "
                f"rate={self._hits / total * 100:.1f}%"
            )
        return return_code, output_data

    def record_result(
        self,
        channel_id: int,
        ioctl_id: int,
        return_code: int,
        output_data: Optional[bytes],
    ) -> None:
        """Record a successful IOCTL result.

        Only caches successful reads (return_code == 0) for cacheable IDs.
        """
        if not self._enabled:
            return
        if ioctl_id not in self._cacheable_ids:
            return
        if return_code != 0:
            return
        self._entries[(channel_id, ioctl_id)] = (
            time.monotonic(),
            return_code,
            output_data,
        )

    def invalidate_channel(self, channel_id: int) -> None:
        """Invalidate all cached entries for a channel (on Disconnect)."""
        keys_to_remove = [k for k in self._entries if k[0] == channel_id]
        for k in keys_to_remove:
            del self._entries[k]

    def invalidate(self) -> None:
        """Invalidate the entire cache (on Close/Disconnect)."""
        self._entries.clear()
        logger.debug("[IoctlCache] invalidated")

    @property
    def stats(self) -> tuple[int, int]:
        """Return (hits, misses)."""
        return self._hits, self._misses
