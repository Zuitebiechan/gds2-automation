"""Consume-once FIFO for locally prefetched ReadMsgs frames."""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Deque

from .protocol import ProtocolDecoder, ProtocolEncoder


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

    def try_serve(self, channel_id: int, num_msgs: int, sequence: int) -> bytes | None:
        if not self.enabled or num_msgs <= 0:
            return None

        queue = self._messages_by_channel.get(channel_id)
        if not queue:
            return None

        messages: list[dict] = []
        while queue and len(messages) < num_msgs:
            messages.append(queue.popleft())
        if not queue:
            self._messages_by_channel.pop(channel_id, None)

        if not messages:
            return None
        return ProtocolEncoder.encode_read_msgs_rsp(0, messages, sequence)
