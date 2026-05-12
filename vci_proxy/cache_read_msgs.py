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
        self._active_adaptive_ttl_max_s = max(
            config.active_adaptive_ttl_max_ms,
            0,
        ) / 1000.0
        self._active_adaptive_ttl_margin_s = max(
            config.active_adaptive_ttl_margin_ms,
            0,
        ) / 1000.0
        self._active_window_s = max(config.active_window_ms, 0) / 1000.0
        self._post_write_bypass_s = max(config.post_write_bypass_ms, 0) / 1000.0
        self._max_cacheable_timeout_ms = config.max_cacheable_timeout_ms
        # channel_id -> (timestamp_mono, return_code)
        self._channels: dict[int, Tuple[float, int]] = {}
        self._last_write_at: dict[int, float] = {}
        self._active_until: dict[int, float] = {}
        self._last_empty_at: dict[int, float] = {}
        self._empty_gap_ema_s: dict[int, float] = {}
        self._empty_gap_samples: dict[int, int] = {}
        self._hits = 0
        self._misses = 0

    def _active_ttl_for_channel_s(self, channel_id: int) -> float:
        ttl_s = self._active_ttl_s
        if self._active_adaptive_ttl_max_s <= ttl_s:
            return ttl_s

        gap_s = self._empty_gap_ema_s.get(channel_id)
        if gap_s is None or self._empty_gap_samples.get(channel_id, 0) <= 0:
            return ttl_s

        adaptive_ttl_s = min(
            self._active_adaptive_ttl_max_s,
            gap_s + self._active_adaptive_ttl_margin_s,
        )
        return max(ttl_s, adaptive_ttl_s)

    def _current_ttl_s(self, channel_id: int, now: float) -> float:
        active_until = self._active_until.get(channel_id)
        if active_until is not None and now <= active_until:
            return self._active_ttl_for_channel_s(channel_id)
        return self._idle_ttl_s

    def _record_empty_gap(self, channel_id: int, ts: float) -> None:
        previous = self._last_empty_at.get(channel_id)
        self._last_empty_at[channel_id] = ts
        if previous is None:
            return

        gap_s = ts - previous
        if gap_s <= 0:
            return

        existing = self._empty_gap_ema_s.get(channel_id)
        if existing is None:
            self._empty_gap_ema_s[channel_id] = gap_s
        else:
            # React fast enough to a stable 30-50ms polling cadence without
            # letting one long quiet interval dominate subsequent freshness.
            self._empty_gap_ema_s[channel_id] = (existing * 0.75) + (gap_s * 0.25)
        self._empty_gap_samples[channel_id] = (
            self._empty_gap_samples.get(channel_id, 0) + 1
        )

    def _reset_empty_gap(self, channel_id: int) -> None:
        self._last_empty_at.pop(channel_id, None)
        self._empty_gap_ema_s.pop(channel_id, None)
        self._empty_gap_samples.pop(channel_id, None)

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
            self._reset_empty_gap(channel_id)
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

    def observability_state(
        self,
        channel_id: int,
        timeout: int,
        *,
        now: float | None = None,
    ) -> dict[str, object]:
        """Describe cache state without changing hit/miss counters."""
        ts = time.monotonic() if now is None else now
        timeout_cacheable = (
            self._max_cacheable_timeout_ms < 0
            or timeout <= self._max_cacheable_timeout_ms
        )
        fields: dict[str, object] = {
            "empty_cache_enabled": self._enabled,
            "empty_cache_max_timeout_ms": self._max_cacheable_timeout_ms,
            "empty_cache_timeout_cacheable": timeout_cacheable,
        }
        if not self._enabled:
            return fields

        active_until = self._active_until.get(channel_id)
        active = active_until is not None and ts <= active_until
        fields["empty_cache_active_window"] = active
        if active and active_until is not None:
            fields["empty_cache_active_remaining_ms"] = round(
                max(0.0, (active_until - ts) * 1000.0),
                3,
            )

        last_write_at = self._last_write_at.get(channel_id)
        post_write_bypass_active = (
            last_write_at is not None
            and self._post_write_bypass_s > 0
            and ts - last_write_at <= self._post_write_bypass_s
        )
        fields["empty_cache_post_write_bypass_active"] = post_write_bypass_active
        if last_write_at is not None:
            fields["empty_cache_last_write_age_ms"] = round(
                max(0.0, (ts - last_write_at) * 1000.0),
                3,
            )
            if post_write_bypass_active:
                remaining_ms = (
                    self._post_write_bypass_s * 1000.0
                    - (ts - last_write_at) * 1000.0
                )
                fields["empty_cache_post_write_bypass_remaining_ms"] = round(
                    max(0.0, remaining_ms),
                    3,
                )

        entry = self._channels.get(channel_id)
        fields["empty_cache_has_entry"] = entry is not None
        if entry is None:
            return fields

        entry_ts, return_code = entry
        ttl_s = self._current_ttl_s(channel_id, ts)
        age_ms = max(0.0, (ts - entry_ts) * 1000.0)
        fields.update(
            {
                "empty_cache_entry_return_code": return_code,
                "empty_cache_entry_age_ms": round(age_ms, 3),
                "empty_cache_effective_ttl_ms": round(ttl_s * 1000.0, 3),
                "empty_cache_entry_expired": age_ms > ttl_s * 1000.0,
            }
        )
        empty_gap_s = self._empty_gap_ema_s.get(channel_id)
        if empty_gap_s is not None:
            fields.update(
                {
                    "empty_cache_recent_empty_gap_ms": round(empty_gap_s * 1000.0, 3),
                    "empty_cache_empty_gap_sample_count": self._empty_gap_samples.get(
                        channel_id,
                        0,
                    ),
                    "empty_cache_active_adaptive_ttl_max_ms": round(
                        self._active_adaptive_ttl_max_s * 1000.0,
                        3,
                    ),
                    "empty_cache_active_adaptive_ttl_margin_ms": round(
                        self._active_adaptive_ttl_margin_s * 1000.0,
                        3,
                    ),
                    "empty_cache_adaptive_ttl_applied": (
                        active and ttl_s > self._active_ttl_s
                    ),
                }
            )
        return fields

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
        # Once a real post-write read has completed, any older write marker is no
        # longer protecting against stale pre-write empties.
        self._last_write_at.pop(channel_id, None)
        if return_code == BUFFER_EMPTY and message_count == 0:
            self._channels[channel_id] = (ts, return_code)
            self._record_empty_gap(channel_id, ts)
            return
        self._channels.pop(channel_id, None)
        self._reset_empty_gap(channel_id)
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
        self._reset_empty_gap(channel_id)

    def clear(self) -> None:
        """Clear entire cache (on VCI disconnect)."""
        self._channels.clear()
        self._last_write_at.clear()
        self._active_until.clear()
        self._last_empty_at.clear()
        self._empty_gap_ema_s.clear()
        self._empty_gap_samples.clear()
        logger.debug("[ReadMsgsCache] cleared")

    @property
    def stats(self) -> Tuple[int, int]:
        """Return (hits, misses)."""
        return self._hits, self._misses
