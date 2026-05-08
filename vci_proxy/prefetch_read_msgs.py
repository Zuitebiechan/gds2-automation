"""Consume-once FIFO for locally prefetched ReadMsgs frames."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque

from .protocol import ProtocolDecoder, ProtocolEncoder


@dataclass(frozen=True)
class PrefetchReadMsgsDrain:
    """Result of a consume-once FIFO drain attempt."""

    channel_id: int
    requested_count: int
    pending_before: int
    messages: tuple[dict, ...]
    pending_after: int

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

    def record_read_rsp_body(self, channel_id: int, read_rsp_body: bytes) -> int:
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
        for message in messages[:available]:
            queue.append(
                {
                    "protocol_id": message.get("protocol_id", 0),
                    "rx_status": message.get("rx_status", 0),
                    "tx_flags": message.get("tx_flags", 0),
                    "timestamp": message.get("timestamp", 0),
                    "data": bytes(message.get("data", b"")),
                }
            )
            recorded += 1
        return recorded

    def record_read_rsp_bodies(self, channel_id: int, read_rsp_bodies: tuple[bytes, ...]) -> int:
        recorded = 0
        for read_rsp_body in read_rsp_bodies:
            recorded += self.record_read_rsp_body(channel_id, read_rsp_body)
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

        return PrefetchReadMsgsDrain(
            channel_id=channel_id,
            requested_count=requested_count,
            pending_before=pending_before,
            messages=tuple(messages),
            pending_after=pending_after,
        )

    def restore_front(self, channel_id: int, messages: tuple[dict, ...] | list[dict]) -> int:
        if not self.enabled or not messages:
            return 0

        queue = self._messages_by_channel[channel_id]
        restored = 0
        for message in reversed(messages):
            queue.appendleft(
                {
                    "protocol_id": message.get("protocol_id", 0),
                    "rx_status": message.get("rx_status", 0),
                    "tx_flags": message.get("tx_flags", 0),
                    "timestamp": message.get("timestamp", 0),
                    "data": bytes(message.get("data", b"")),
                }
            )
            restored += 1
        return restored

    def try_serve(self, channel_id: int, num_msgs: int, sequence: int) -> bytes | None:
        drain = self.drain(channel_id, num_msgs)
        messages = list(drain.messages)
        if not messages:
            return None
        return ProtocolEncoder.encode_read_msgs_rsp(0, messages, sequence)
