"""
ReadMsgs BUFFER_EMPTY short-circuit cache (P1-1).

When the last ReadMsgs result for a channel was BUFFER_EMPTY (0x10),
subsequent reads within TTL are served directly without a tunnel round-trip.
Only the return code is cached -- actual message data is never cached because
messages are consumed on read.
"""

import struct
import time
import logging
from typing import Optional, Tuple

from .config import ReadMsgsCacheConfig

logger = logging.getLogger(__name__)

BUFFER_EMPTY = 0x10


class ReadMsgsCache:
    """Per-channel BUFFER_EMPTY cache."""

    def __init__(self, config: ReadMsgsCacheConfig):
        self._enabled = config.enabled
        self._ttl_s = config.ttl_ms / 1000.0
        # channel_id -> (timestamp_mono, return_code)
        self._channels: dict[int, Tuple[float, int]] = {}
        self._hits = 0
        self._misses = 0

    def try_serve_from_cache(
        self, channel_id: int, num_msgs: int, timeout: int, sequence: int
    ) -> Optional[bytes]:
        """Return a pre-built ReadMsgs_RSP if the cache can answer, else None.

        The response is a fully-encoded protocol message (header + body).
        """
        if not self._enabled:
            return None

        entry = self._channels.get(channel_id)
        if entry is None:
            self._misses += 1
            return None

        ts, return_code = entry
        if return_code != BUFFER_EMPTY:
            self._misses += 1
            return None

        elapsed = time.monotonic() - ts
        if elapsed > self._ttl_s:
            self._misses += 1
            return None

        self._hits += 1
        if self._hits % 500 == 0:
            total = self._hits + self._misses
            logger.info(
                f"[ReadMsgsCache] hits={self._hits} misses={self._misses} "
                f"rate={self._hits/total*100:.1f}%"
            )

        # Build ReadMsgs_RSP: return_code=BUFFER_EMPTY, num_msgs=0
        from .protocol import MAGIC, HEADER_SIZE, MsgType

        body = struct.pack(">II", BUFFER_EMPTY, 0)
        length = HEADER_SIZE + len(body)
        header = struct.pack(
            ">IIHI", MAGIC, length, MsgType.READ_MSGS_RSP, sequence
        )
        return header + body

    def record_result(self, channel_id: int, return_code: int) -> None:
        """Record a real ReadMsgs result for future cache lookups."""
        if not self._enabled:
            return
        self._channels[channel_id] = (time.monotonic(), return_code)

    def invalidate_channel(self, channel_id: int) -> None:
        """Invalidate cache for a channel (on Disconnect)."""
        self._channels.pop(channel_id, None)

    def clear(self) -> None:
        """Clear entire cache (on VCI disconnect)."""
        self._channels.clear()
        logger.debug("[ReadMsgsCache] cleared")

    @property
    def stats(self) -> Tuple[int, int]:
        """Return (hits, misses)."""
        return self._hits, self._misses
