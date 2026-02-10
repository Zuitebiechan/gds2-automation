"""
StartFilter deduplication cache (P2-1).

Identical StartFilter requests (same channel + filter_type + mask + pattern +
flow_control) are deduplicated: the first call goes through the tunnel and the
cached filter_id is returned for subsequent identical requests.

Cache keys are SHA-256 hashes of the full StartFilter request body.
"""

import hashlib
import logging
from typing import Optional, Tuple

from .config import FilterDeduplicationConfig

logger = logging.getLogger(__name__)


class FilterDeduplicationCache:
    """Deduplicates identical StartFilter requests."""

    def __init__(self, config: FilterDeduplicationConfig):
        self._enabled = config.enabled
        # filter_key (sha256 hex) -> (filter_id, response_body bytes)
        self._filters: dict[str, Tuple[int, bytes]] = {}
        # channel_id -> set of filter_keys
        self._channel_filters: dict[int, set[str]] = {}
        # filter_id -> filter_key (for StopFilter cleanup)
        self._id_to_key: dict[int, str] = {}
        self._hits = 0
        self._misses = 0

    @staticmethod
    def _compute_key(body: bytes) -> str:
        """SHA-256 of the full StartFilter request body."""
        return hashlib.sha256(body).hexdigest()

    def try_dedup(self, body: bytes, sequence: int) -> Optional[bytes]:
        """Return a cached StartFilter_RSP if an identical filter exists.

        Returns fully-encoded protocol message or None.
        """
        if not self._enabled:
            return None

        key = self._compute_key(body)
        entry = self._filters.get(key)
        if entry is None:
            self._misses += 1
            return None

        filter_id, cached_resp_body = entry
        self._hits += 1
        logger.info(f"[FilterDedup] HIT filter_id={filter_id} (key={key[:12]}...)")

        import struct
        from .protocol import MAGIC, HEADER_SIZE, MsgType

        length = HEADER_SIZE + len(cached_resp_body)
        header = struct.pack(
            ">IIHI", MAGIC, length, MsgType.START_FILTER_RSP, sequence
        )
        return header + cached_resp_body

    def record_result(
        self, body: bytes, filter_id: int, return_code: int, response_body: bytes
    ) -> None:
        """Record a successful StartFilter result for future deduplication.

        Only caches successful results (return_code == 0).
        """
        if not self._enabled:
            return
        if return_code != 0:
            return

        import struct

        # Extract channel_id from the request body (first 4 bytes)
        channel_id = struct.unpack(">I", body[:4])[0]

        key = self._compute_key(body)
        self._filters[key] = (filter_id, response_body)
        self._id_to_key[filter_id] = key
        self._channel_filters.setdefault(channel_id, set()).add(key)
        logger.debug(
            f"[FilterDedup] cached filter_id={filter_id} ch={channel_id} "
            f"(key={key[:12]}...)"
        )

    def on_stop_filter(self, filter_id: int) -> None:
        """Remove a filter from the cache when StopFilter is called."""
        key = self._id_to_key.pop(filter_id, None)
        if key is None:
            return
        self._filters.pop(key, None)
        # Remove from channel sets
        for channel_keys in self._channel_filters.values():
            channel_keys.discard(key)
        logger.debug(f"[FilterDedup] removed filter_id={filter_id}")

    def invalidate_channel(self, channel_id: int) -> None:
        """Remove all cached filters for a channel (on Disconnect)."""
        keys = self._channel_filters.pop(channel_id, set())
        for key in keys:
            entry = self._filters.pop(key, None)
            if entry is not None:
                self._id_to_key.pop(entry[0], None)
        if keys:
            logger.debug(
                f"[FilterDedup] invalidated {len(keys)} filters for ch={channel_id}"
            )

    def clear(self) -> None:
        """Clear entire cache (on VCI disconnect)."""
        self._filters.clear()
        self._channel_filters.clear()
        self._id_to_key.clear()
        logger.debug("[FilterDedup] cleared")

    @property
    def stats(self) -> Tuple[int, int]:
        """Return (hits, misses)."""
        return self._hits, self._misses
