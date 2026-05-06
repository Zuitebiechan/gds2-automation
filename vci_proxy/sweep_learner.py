"""Stable-loop learner for local sweep observe-only evidence."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .config import LocalSweepConfig
from .protocol import ProtocolDecoder
from .sweep_classifier import SweepReadOnlyClassifier
from .sweep_signatures import SweepObservedRequest, SweepRequestSignature


@dataclass(frozen=True)
class SweepLearnerEvent:
    event_type: str
    fields: dict[str, object]


@dataclass
class SweepCandidateState:
    observed: SweepObservedRequest
    cycles: int = 0
    first_seen_mono: float = field(default_factory=time.monotonic)
    last_seen_mono: float | None = None
    learned: bool = False
    learned_at_mono: float | None = None
    would_have_shadow_hits: int = 0

    @property
    def confidence(self) -> float:
        return min(1.0, self.cycles / max(1, self.min_cycles_hint))

    min_cycles_hint: int = 2

    def cadence_ms(self, now: float) -> float | None:
        if self.last_seen_mono is None:
            return None
        return max(0.0, (now - self.last_seen_mono) * 1000.0)


class SweepPatternLearner:
    """Correlate write requests with later data reads and learn candidates."""

    def __init__(self, config: LocalSweepConfig):
        self._config = config
        self._classifier = SweepReadOnlyClassifier(config)
        self._pending_by_channel: dict[int, SweepObservedRequest] = {}
        self._candidates: dict[str, SweepCandidateState] = {}
        self._read_data_observations = 0
        self._rejections: dict[str, int] = {}

    @property
    def candidate_count(self) -> int:
        return len([state for state in self._candidates.values() if state.learned])

    def observe_write(
        self,
        body: bytes,
        *,
        connection_epoch: str | None,
        read_num_msgs: int = 1,
        read_timeout_ms: int = 0,
    ) -> list[SweepLearnerEvent]:
        if not self._config.enabled:
            return []

        classification = self._classifier.classify_write_request_body(
            body,
            connection_epoch=connection_epoch,
            read_num_msgs=read_num_msgs,
            read_timeout_ms=read_timeout_ms,
        )
        if not classification.accepted or classification.observed is None:
            self._rejections[classification.reason] = (
                self._rejections.get(classification.reason, 0) + 1
            )
            return [
                SweepLearnerEvent(
                    "sweep.pattern.rejected",
                    {
                        "sweep_rejection_reason": classification.reason,
                        "sweep_rejection_count": self._rejections[classification.reason],
                    },
                )
            ]

        observed = classification.observed
        signature = observed.signature
        self._pending_by_channel[signature.channel_id] = observed
        return [
            SweepLearnerEvent(
                "sweep.pattern.observed",
                {
                    **signature.to_observability(),
                    "sweep_observed_reason": classification.reason,
                },
            )
        ]

    def observe_read_response(
        self,
        request_body: bytes,
        response_body: bytes,
        *,
        connection_epoch: str | None,
    ) -> tuple[SweepObservedRequest | None, list[SweepLearnerEvent]]:
        if not self._config.enabled:
            return None, []

        try:
            channel_id, _num_msgs, _timeout = ProtocolDecoder.decode_read_msgs_req(
                request_body
            )
            return_code, messages = ProtocolDecoder.decode_read_msgs_rsp(response_body)
        except Exception:
            return None, []

        observed = self._pending_by_channel.pop(channel_id, None)
        if observed is None:
            return None, []
        if observed.signature.connection_epoch != connection_epoch:
            return observed, [
                SweepLearnerEvent(
                    "sweep.pattern.rejected",
                    {
                        **observed.signature.to_observability(),
                        "sweep_rejection_reason": "connection_epoch_changed",
                    },
                )
            ]
        if return_code != 0 or not messages:
            return observed, [
                SweepLearnerEvent(
                    "sweep.pattern.rejected",
                    {
                        **observed.signature.to_observability(),
                        "sweep_rejection_reason": "read_response_not_data",
                        "sweep_read_return_code": return_code,
                        "sweep_read_message_count": len(messages),
                    },
                )
            ]

        self._read_data_observations += 1
        digest = observed.signature.signature_digest
        now = time.monotonic()
        state = self._candidates.get(digest)
        if state is None:
            state = SweepCandidateState(
                observed=observed,
                min_cycles_hint=self._config.min_cycles,
            )
            self._candidates[digest] = state

        cadence_ms = state.cadence_ms(now)
        state.cycles += 1
        if state.learned:
            state.would_have_shadow_hits += 1
        learned_now = False
        if not state.learned and state.cycles >= self._config.min_cycles:
            state.learned = True
            state.learned_at_mono = now
            learned_now = True
        state.last_seen_mono = now

        common = {
            **observed.signature.to_observability(),
            "sweep_cycles": state.cycles,
            "sweep_min_cycles": self._config.min_cycles,
            "sweep_confidence": round(
                min(1.0, state.cycles / max(1, self._config.min_cycles)),
                3,
            ),
            "sweep_candidate_count": self.candidate_count,
            "sweep_would_have_shadow_hits": state.would_have_shadow_hits,
            "sweep_estimated_would_hit_rate": round(
                state.would_have_shadow_hits / max(1, self._read_data_observations),
                3,
            ),
        }
        events = [
            SweepLearnerEvent(
                "sweep.did.cadence",
                {
                    **common,
                    "sweep_cadence_ms": round(cadence_ms, 3)
                    if cadence_ms is not None
                    else None,
                },
            )
        ]
        if learned_now:
            events.append(SweepLearnerEvent("sweep.pattern.learned", common))
        return observed, events

    def learned_for_channel(
        self,
        channel_id: int,
        *,
        connection_epoch: str | None,
        limit: int | None = None,
    ) -> list[SweepObservedRequest]:
        learned = [
            state.observed
            for state in self._candidates.values()
            if state.learned
            and state.observed.signature.channel_id == channel_id
            and state.observed.signature.connection_epoch == connection_epoch
        ]
        learned.sort(key=lambda item: item.signature.signature_digest)
        if limit is not None:
            return learned[: max(0, int(limit))]
        return learned

    def reset_channel(self, channel_id: int) -> None:
        self._pending_by_channel.pop(channel_id, None)
        self._candidates = {
            digest: state
            for digest, state in self._candidates.items()
            if state.observed.signature.channel_id != channel_id
        }

    def reset_all(self) -> None:
        self._pending_by_channel.clear()
        self._candidates.clear()
