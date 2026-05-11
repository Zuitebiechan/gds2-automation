"""Consume-once FIFO for locally prefetched ReadMsgs frames."""

from __future__ import annotations

import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from typing import Deque

from .protocol import ProtocolDecoder, ProtocolEncoder

_PREFETCH_RECORDED_MONO_KEY = "_prefetch_recorded_mono"
_PREFETCH_SOURCE_KEY = "_prefetch_source"
_PREFETCH_UNKNOWN_SOURCE = "unknown"


@dataclass(frozen=True)
class PrefetchReadMsgsDrain:
    """Result of a consume-once FIFO drain attempt."""

    channel_id: int
    requested_count: int
    pending_before: int
    messages: tuple[dict, ...]
    pending_after: int
    source_counts: tuple[tuple[str, int], ...] = ()
    age_min_ms: float | None = None
    age_avg_ms: float | None = None
    age_max_ms: float | None = None

    @property
    def served_count(self) -> int:
        return len(self.messages)

    @property
    def underfill_count(self) -> int:
        return max(0, self.requested_count - self.served_count)

    @property
    def is_empty(self) -> bool:
        return self.served_count == 0

    @property
    def is_full(self) -> bool:
        return self.served_count >= self.requested_count > 0

    @property
    def is_partial(self) -> bool:
        return 0 < self.served_count < self.requested_count

    def to_response(self, sequence: int) -> bytes:
        return ProtocolEncoder.encode_read_msgs_rsp(0, list(self.messages), sequence)


class PrefetchReadMsgsBuffer:
    """Per-channel FIFO for ReadMsgs data consumed by local read-ahead."""

    def __init__(self, *, enabled: bool = False, max_messages: int = 16):
        self.enabled = enabled
        self.max_messages = max(0, int(max_messages))
        self._messages_by_channel: dict[int, Deque[dict]] = defaultdict(deque)

    def clear(self) -> None:
        self._messages_by_channel.clear()

    def clear_channel(self, channel_id: int) -> None:
        self._messages_by_channel.pop(channel_id, None)

    def pending_count(self, channel_id: int) -> int:
        return len(self._messages_by_channel.get(channel_id, ()))

    @staticmethod
    def _stored_message(message: dict, *, source: str | None, recorded_mono: float) -> dict:
        stored = {
            "protocol_id": message.get("protocol_id", 0),
            "rx_status": message.get("rx_status", 0),
            "tx_flags": message.get("tx_flags", 0),
            "timestamp": message.get("timestamp", 0),
            "data": bytes(message.get("data", b"")),
        }
        stored[_PREFETCH_SOURCE_KEY] = str(source or _PREFETCH_UNKNOWN_SOURCE)
        stored[_PREFETCH_RECORDED_MONO_KEY] = float(recorded_mono)
        return stored

    def record_read_rsp_body(
        self,
        channel_id: int,
        read_rsp_body: bytes,
        *,
        source: str | None = None,
        now_mono: float | None = None,
    ) -> int:
        if not self.enabled or self.max_messages <= 0:
            return 0

        return_code, messages = ProtocolDecoder.decode_read_msgs_rsp(read_rsp_body)
        if return_code != 0 or not messages:
            return 0

        queue = self._messages_by_channel[channel_id]
        available = self.max_messages - len(queue)
        if available <= 0:
            return 0

        recorded = 0
        recorded_mono = time.monotonic() if now_mono is None else float(now_mono)
        for message in messages[:available]:
            queue.append(
                self._stored_message(
                    message,
                    source=source,
                    recorded_mono=recorded_mono,
                )
            )
            recorded += 1
        return recorded

    def record_read_rsp_bodies(
        self,
        channel_id: int,
        read_rsp_bodies: tuple[bytes, ...],
        *,
        source: str | None = None,
    ) -> int:
        recorded = 0
        now_mono = time.monotonic()
        for read_rsp_body in read_rsp_bodies:
            recorded += self.record_read_rsp_body(
                channel_id,
                read_rsp_body,
                source=source,
                now_mono=now_mono,
            )
        return recorded

    def drain(self, channel_id: int, num_msgs: int) -> PrefetchReadMsgsDrain:
        requested_count = max(0, int(num_msgs))
        if not self.enabled or requested_count <= 0:
            return PrefetchReadMsgsDrain(
                channel_id=channel_id,
                requested_count=requested_count,
                pending_before=0,
                messages=(),
                pending_after=0,
            )

        queue = self._messages_by_channel.get(channel_id)
        pending_before = len(queue or ())
        if not queue:
            return PrefetchReadMsgsDrain(
                channel_id=channel_id,
                requested_count=requested_count,
                pending_before=pending_before,
                messages=(),
                pending_after=0,
            )

        messages: list[dict] = []
        while queue and len(messages) < requested_count:
            messages.append(queue.popleft())
        pending_after = len(queue)
        if not queue:
            self._messages_by_channel.pop(channel_id, None)

        source_counts: Counter[str] = Counter()
        ages_ms: list[float] = []
        now_mono = time.monotonic()
        for message in messages:
            source_counts[
                str(message.get(_PREFETCH_SOURCE_KEY) or _PREFETCH_UNKNOWN_SOURCE)
            ] += 1
            recorded_mono = message.get(_PREFETCH_RECORDED_MONO_KEY)
            if isinstance(recorded_mono, (int, float)):
                ages_ms.append(max(0.0, (now_mono - float(recorded_mono)) * 1000.0))

        return PrefetchReadMsgsDrain(
            channel_id=channel_id,
            requested_count=requested_count,
            pending_before=pending_before,
            messages=tuple(messages),
            pending_after=pending_after,
            source_counts=tuple(sorted(source_counts.items())),
            age_min_ms=round(min(ages_ms), 3) if ages_ms else None,
            age_avg_ms=round(sum(ages_ms) / len(ages_ms), 3) if ages_ms else None,
            age_max_ms=round(max(ages_ms), 3) if ages_ms else None,
        )

    def restore_front(self, channel_id: int, messages: tuple[dict, ...] | list[dict]) -> int:
        if not self.enabled or not messages:
            return 0

        queue = self._messages_by_channel[channel_id]
        restored = 0
        now_mono = time.monotonic()
        for message in reversed(messages):
            recorded_mono = message.get(_PREFETCH_RECORDED_MONO_KEY)
            if not isinstance(recorded_mono, (int, float)):
                recorded_mono = now_mono
            queue.appendleft(
                self._stored_message(
                    message,
                    source=message.get(_PREFETCH_SOURCE_KEY),
                    recorded_mono=float(recorded_mono),
                )
            )
            restored += 1
        return restored

    def try_serve(self, channel_id: int, num_msgs: int, sequence: int) -> bytes | None:
        drain = self.drain(channel_id, num_msgs)
        messages = list(drain.messages)
        if not messages:
            return None
        return ProtocolEncoder.encode_read_msgs_rsp(0, messages, sequence)
