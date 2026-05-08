"""Comparison-only store for local sweep shadow results."""

from __future__ import annotations

import hashlib
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque

from .protocol import ProtocolDecoder
from .sweep_protocol import SweepResultRecord


@dataclass
class _ReplayValidationState:
    latest_result_key: str | None = None
    latest_validated: bool = False
    clean_match_streak: int = 0
    last_matched_result_key: str | None = None


class SweepShadowStore:
    """Keep shadow results isolated from normal ReadMsgs serving paths."""

    def __init__(self, *, max_results_per_signature: int = 4):
        self._max_results_per_signature = max(1, int(max_results_per_signature))
        self._results_by_signature: dict[str, Deque[SweepResultRecord]] = defaultdict(deque)
        self._signature_channel: dict[str, int] = {}
        self._replay_validation_by_signature: dict[str, _ReplayValidationState] = {}

    @staticmethod
    def _result_key(result: SweepResultRecord) -> str:
        body_digest = hashlib.sha256(result.read_rsp_body).hexdigest()
        return (
            f"{result.plan_id}:{result.signature_digest}:{result.finished_at_s:.9f}:"
            f"{result.return_code}:{result.error_name or ''}:{body_digest}"
        )

    def record_result(self, result: SweepResultRecord, *, channel_id: int) -> None:
        queue = self._results_by_signature[result.signature_digest]
        queue.append(result)
        while len(queue) > self._max_results_per_signature:
            queue.popleft()
        self._signature_channel[result.signature_digest] = int(channel_id)
        state = self._replay_validation_by_signature.setdefault(
            result.signature_digest,
            _ReplayValidationState(),
        )
        result_key = self._result_key(result)
        if state.latest_result_key != result_key:
            state.latest_result_key = result_key
            state.latest_validated = False

    def latest_for(self, signature_digest: str) -> SweepResultRecord | None:
        queue = self._results_by_signature.get(signature_digest)
        if not queue:
            return None
        return queue[-1]

    def record_comparison(
        self,
        signature_digest: str,
        *,
        result: SweepResultRecord,
        clean_match: bool,
        reset_streak: bool,
    ) -> None:
        state = self._replay_validation_by_signature.setdefault(
            signature_digest,
            _ReplayValidationState(),
        )
        result_key = self._result_key(result)
        if state.latest_result_key != result_key:
            state.latest_result_key = result_key
            state.latest_validated = False
        if clean_match:
            if state.last_matched_result_key != result_key:
                state.clean_match_streak += 1
                state.last_matched_result_key = result_key
            state.latest_validated = True
            return
        if reset_streak:
            state.clean_match_streak = 0
            state.last_matched_result_key = None
        state.latest_validated = False

    def replay_match_streak(self, signature_digest: str) -> int:
        state = self._replay_validation_by_signature.get(signature_digest)
        if state is None:
            return 0
        return max(0, int(state.clean_match_streak))

    def latest_replay_ready(
        self,
        signature_digest: str,
        *,
        max_result_age_ms: int,
        min_clean_matches: int,
    ) -> SweepResultRecord | None:
        result = self.latest_for(signature_digest)
        if result is None:
            return None
        state = self._replay_validation_by_signature.get(signature_digest)
        if state is None:
            return None
        if state.latest_result_key != self._result_key(result):
            return None
        if not state.latest_validated:
            return None
        if state.clean_match_streak < max(1, int(min_clean_matches)):
            return None
        if result.error_name:
            return None
        if int(result.return_code) != 0:
            return None
        if result.age_ms > max(1, int(max_result_age_ms)):
            return None
        try:
            read_return_code, messages = ProtocolDecoder.decode_read_msgs_rsp(
                result.read_rsp_body
            )
        except Exception:
            return None
        if int(read_return_code) != 0:
            return None
        if not messages:
            return None
        return result

    def clear_channel(self, channel_id: int) -> None:
        signatures = [
            signature
            for signature, stored_channel_id in self._signature_channel.items()
            if stored_channel_id == channel_id
        ]
        for signature in signatures:
            self._results_by_signature.pop(signature, None)
            self._signature_channel.pop(signature, None)
            self._replay_validation_by_signature.pop(signature, None)

    def clear(self) -> None:
        self._results_by_signature.clear()
        self._signature_channel.clear()
        self._replay_validation_by_signature.clear()

    def pending_count(self) -> int:
        return sum(len(queue) for queue in self._results_by_signature.values())
