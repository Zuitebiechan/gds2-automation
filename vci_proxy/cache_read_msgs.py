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
        self._idle_ttl_s = config.ttl_ms / 1000.0
        self._active_ttl_s = max(config.active_ttl_ms, 0) / 1000.0
        self._active_window_s = max(config.active_window_ms, 0) / 1000.0
        self._post_write_bypass_s = max(config.post_write_bypass_ms, 0) / 1000.0
        self._max_cacheable_timeout_ms = config.max_cacheable_timeout_ms
        # channel_id -> (timestamp_mono, return_code)
        self._channels: dict[int, Tuple[float, int]] = {}
        self._last_write_at: dict[int, float] = {}
        self._active_until: dict[int, float] = {}
        self._hits = 0
        self._misses = 0

    def _current_ttl_s(self, channel_id: int, now: float) -> float:
        active_until = self._active_until.get(channel_id)
        if active_until is not None and now <= active_until:
            return self._active_ttl_s
        return self._idle_ttl_s

    def mark_channel_active(
        self,
        channel_id: int,
        *,
        now: float | None = None,
        invalidate_empty: bool = False,
    ) -> None:
        """Move a channel into active mode after state-changing traffic."""
        if not self._enabled:
            return
        ts = time.monotonic() if now is None else now
        if invalidate_empty:
            self._channels.pop(channel_id, None)
        if self._active_window_s > 0:
            self._active_until[channel_id] = ts + self._active_window_s

    def try_serve_from_cache(
        self, channel_id: int, num_msgs: int, timeout: int, sequence: int
    ) -> Optional[bytes]:
        """Return a pre-built ReadMsgs_RSP if the cache can answer, else None.

        The response is a fully-encoded protocol message (header + body).
        """
        if not self._enabled:
            return None

        now = time.monotonic()
        if (
            self._max_cacheable_timeout_ms >= 0
            and timeout > self._max_cacheable_timeout_ms
        ):
            self._misses += 1
            return None

        last_write_at = self._last_write_at.get(channel_id)
        if (
            last_write_at is not None
            and self._post_write_bypass_s > 0
            and now - last_write_at <= self._post_write_bypass_s
        ):
            self._misses += 1
            return None

        entry = self._channels.get(channel_id)
        if entry is None:
            self._misses += 1
            return None

        ts, return_code = entry
        if return_code != BUFFER_EMPTY:
            self._misses += 1
            return None

        elapsed = now - ts
        if elapsed > self._current_ttl_s(channel_id, now):
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

    def record_result(
        self,
        channel_id: int,
        return_code: int,
        *,
        message_count: int = 0,
        now: float | None = None,
    ) -> None:
        """Record a real ReadMsgs result for future cache lookups."""
        if not self._enabled:
            return
        ts = time.monotonic() if now is None else now
        if return_code == BUFFER_EMPTY and message_count == 0:
            self._channels[channel_id] = (ts, return_code)
            return
        self._channels.pop(channel_id, None)
        if message_count > 0:
            self.mark_channel_active(channel_id, now=ts, invalidate_empty=False)

    def record_write(self, channel_id: int, *, now: float | None = None) -> None:
        """Apply post-write invalidation/bypass state for a channel."""
        if not self._enabled:
            return
        ts = time.monotonic() if now is None else now
        post_write_bypass_enabled = self._post_write_bypass_s > 0
        self.mark_channel_active(
            channel_id,
            now=ts,
            invalidate_empty=post_write_bypass_enabled,
        )
        if post_write_bypass_enabled:
            self._last_write_at[channel_id] = ts

    def invalidate_channel(self, channel_id: int) -> None:
        """Invalidate cache and write markers for a channel."""
        self._channels.pop(channel_id, None)
        self._last_write_at.pop(channel_id, None)
        self._active_until.pop(channel_id, None)

    def clear(self) -> None:
        """Clear entire cache (on VCI disconnect)."""
        self._channels.clear()
        self._last_write_at.clear()
        self._active_until.clear()
        logger.debug("[ReadMsgsCache] cleared")

    @property
    def stats(self) -> Tuple[int, int]:
        """Return (hits, misses)."""
        return self._hits, self._misses
