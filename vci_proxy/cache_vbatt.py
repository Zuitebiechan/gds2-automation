"""
READ_VBATT response cache (P2-2).

Battery voltage changes slowly, so repeated READ_VBATT IOCTLs within TTL
can be served from cache instead of hitting the hardware.

IOCTL ID for READ_VBATT is 0x03 (per J2534 spec).
"""

import struct
import time
import logging
from typing import Optional, Tuple

from .config import VbattCacheConfig

logger = logging.getLogger(__name__)

IOCTL_READ_VBATT = 0x03


class VbattCache:
    """Caches READ_VBATT IOCTL responses."""

    def __init__(self, config: VbattCacheConfig):
        self._enabled = config.enabled
        self._ttl_s = float(config.ttl_s)
        # Cached: (timestamp_mono, return_code, output_data_bytes)
        self._cached: Optional[Tuple[float, int, Optional[bytes]]] = None
        self._hits = 0
        self._misses = 0

    @staticmethod
    def is_read_vbatt(ioctl_id: int) -> bool:
        """Check if the IOCTL is READ_VBATT."""
        return ioctl_id == IOCTL_READ_VBATT

    def try_get_cached(self, ioctl_id: int) -> Optional[Tuple[int, Optional[bytes]]]:
        """Return (return_code, output_data) from cache, or None on miss.

        Caller must check is_read_vbatt() first or this always returns None.
        """
        if not self._enabled:
            return None
        if ioctl_id != IOCTL_READ_VBATT:
            return None
        if self._cached is None:
            self._misses += 1
            return None

        ts, return_code, output_data = self._cached
        if time.monotonic() - ts > self._ttl_s:
            self._misses += 1
            return None

        self._hits += 1
        if self._hits % 100 == 0:
            total = self._hits + self._misses
            logger.info(
                f"[VbattCache] hits={self._hits} misses={self._misses} "
                f"rate={self._hits/total*100:.1f}%"
            )
        return return_code, output_data

    def record_result(
        self, ioctl_id: int, return_code: int, output_data: Optional[bytes]
    ) -> None:
        """Record a successful READ_VBATT result.

        Only caches successful reads (return_code == 0).
        """
        if not self._enabled:
            return
        if ioctl_id != IOCTL_READ_VBATT:
            return
        if return_code != 0:
            return
        self._cached = (time.monotonic(), return_code, output_data)
        logger.debug("[VbattCache] cached VBATT response")

    def invalidate(self) -> None:
        """Invalidate the cache (on Close/Disconnect)."""
        self._cached = None
        logger.debug("[VbattCache] invalidated")

    @property
    def stats(self) -> Tuple[int, int]:
        """Return (hits, misses)."""
        return self._hits, self._misses
