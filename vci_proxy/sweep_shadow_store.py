"""Comparison-only store for local sweep shadow results."""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Deque

from .sweep_protocol import SweepResultRecord


class SweepShadowStore:
    """Keep shadow results isolated from normal ReadMsgs serving paths."""

    def __init__(self, *, max_results_per_signature: int = 4):
        self._max_results_per_signature = max(1, int(max_results_per_signature))
        self._results_by_signature: dict[str, Deque[SweepResultRecord]] = defaultdict(deque)
        self._signature_channel: dict[str, int] = {}

    def record_result(self, result: SweepResultRecord, *, channel_id: int) -> None:
        queue = self._results_by_signature[result.signature_digest]
        queue.append(result)
        while len(queue) > self._max_results_per_signature:
            queue.popleft()
        self._signature_channel[result.signature_digest] = int(channel_id)

    def latest_for(self, signature_digest: str) -> SweepResultRecord | None:
        queue = self._results_by_signature.get(signature_digest)
        if not queue:
            return None
        return queue[-1]

    def clear_channel(self, channel_id: int) -> None:
        signatures = [
            signature
            for signature, stored_channel_id in self._signature_channel.items()
            if stored_channel_id == channel_id
        ]
        for signature in signatures:
            self._results_by_signature.pop(signature, None)
            self._signature_channel.pop(signature, None)

    def clear(self) -> None:
        self._results_by_signature.clear()
        self._signature_channel.clear()

    def pending_count(self) -> int:
        return sum(len(queue) for queue in self._results_by_signature.values())
