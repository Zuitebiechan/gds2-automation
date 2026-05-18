"""Observability-only inventory for Data Display sweep candidates."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from .config import LocalSweepConfig
from .sweep_signatures import SweepRequestSignature


MAX_LATENCY_SAMPLES = 512


def _rounded(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 3)


@dataclass
class LatencySamples:
    """Bounded latency sample set for observability percentiles."""

    count: int = 0
    total_ms: float = 0.0
    max_ms: float | None = None
    samples: list[float] = field(default_factory=list)

    def add(self, value_ms: float | None) -> None:
        if value_ms is None:
            return
        value = max(0.0, float(value_ms))
        self.count += 1
        self.total_ms += value
        self.max_ms = value if self.max_ms is None else max(self.max_ms, value)
        self.samples.append(value)
        if len(self.samples) > MAX_LATENCY_SAMPLES:
            self.samples.pop(0)

    def percentile(self, percentile: float) -> float | None:
        if not self.samples:
            return None
        ordered = sorted(self.samples)
        index = max(
            0,
            min(
                len(ordered) - 1,
                int(math.ceil((percentile / 100.0) * len(ordered))) - 1,
            ),
        )
        return ordered[index]

    def fields(self, prefix: str) -> dict[str, object]:
        average = self.total_ms / self.count if self.count else None
        return {
            f"{prefix}_sample_count": self.count,
            f"{prefix}_total_ms": _rounded(self.total_ms) if self.count else 0.0,
            f"{prefix}_avg_ms": _rounded(average),
            f"{prefix}_p50_ms": _rounded(self.percentile(50.0)),
            f"{prefix}_p95_ms": _rounded(self.percentile(95.0)),
            f"{prefix}_max_ms": _rounded(self.max_ms),
        }


@dataclass
class SweepInventorySignatureState:
    signature: SweepRequestSignature
    first_seen_mono: float = field(default_factory=time.monotonic)
    last_seen_mono: float = field(default_factory=time.monotonic)
    write_observed_count: int = 0
    write_success_count: int = 0
    write_error_count: int = 0
    read_data_count: int = 0
    read_empty_count: int = 0
    read_error_count: int = 0
    read_cache_hit_count: int = 0
    read_forwarded_count: int = 0
    write_network_ms: LatencySamples = field(default_factory=LatencySamples)
    write_duration_ms: LatencySamples = field(default_factory=LatencySamples)
    read_network_ms: LatencySamples = field(default_factory=LatencySamples)
    read_duration_ms: LatencySamples = field(default_factory=LatencySamples)
    pair_network_ms: LatencySamples = field(default_factory=LatencySamples)
    pair_duration_ms: LatencySamples = field(default_factory=LatencySamples)
    _pending_write_network_ms: float | None = None
    _pending_write_duration_ms: float | None = None

    def record_write_observed(self) -> None:
        self.write_observed_count += 1
        self.last_seen_mono = time.monotonic()
        self._pending_write_network_ms = None
        self._pending_write_duration_ms = None

    def record_write_response(
        self,
        *,
        return_code: int | None,
        duration_ms: float | None,
        network_ms: float | None,
    ) -> None:
        if return_code == 0:
            self.write_success_count += 1
        else:
            self.write_error_count += 1
        self.write_duration_ms.add(duration_ms)
        self.write_network_ms.add(network_ms)
        self._pending_write_duration_ms = duration_ms
        self._pending_write_network_ms = network_ms
        self.last_seen_mono = time.monotonic()

    def record_read_response(
        self,
        *,
        return_code: int,
        message_count: int,
        duration_ms: float | None,
        network_ms: float | None,
        cache_hit: bool,
    ) -> None:
        if return_code == 0 and message_count > 0:
            self.read_data_count += 1
        elif message_count == 0:
            self.read_empty_count += 1
        else:
            self.read_error_count += 1
        if cache_hit:
            self.read_cache_hit_count += 1
        else:
            self.read_forwarded_count += 1
        self.read_duration_ms.add(duration_ms)
        self.read_network_ms.add(network_ms)
        if self._pending_write_network_ms is not None or network_ms is not None:
            self.pair_network_ms.add(
                float(self._pending_write_network_ms or 0.0)
                + float(network_ms or 0.0)
            )
        if self._pending_write_duration_ms is not None or duration_ms is not None:
            self.pair_duration_ms.add(
                float(self._pending_write_duration_ms or 0.0)
                + float(duration_ms or 0.0)
            )
        self.last_seen_mono = time.monotonic()

    @property
    def total_response_count(self) -> int:
        return self.read_data_count + self.read_empty_count + self.read_error_count

    def replay_candidate_fields(
        self,
        *,
        config: LocalSweepConfig,
        learned: bool,
    ) -> dict[str, object]:
        if not learned:
            replay_candidate = False
            replay_reason = "awaiting_min_cycles"
            shadow_eligible = False
            shadow_reason = "awaiting_min_cycles"
            signature_verdict = "pending_min_cycles"
            signature_next_step = "keep_observing_until_min_cycles"
        elif self.signature.identifier_kind == "gm_a9_packet":
            replay_candidate = False
            replay_reason = "gm_a9_packet_observe_only"
            shadow_eligible = False
            shadow_reason = "gm_a9_packet_observe_only"
            signature_verdict = "no_go_gm_a9_observe_only"
            signature_next_step = "do_not_shadow_or_replay_gm_a9"
        else:
            replay_candidate = True
            replay_reason = "learned_safe_signature"
            shadow_eligible = True
            shadow_reason = "learned_safe_signature"
            signature_verdict = "go_shadow_local_candidate"
            signature_next_step = "prove_sweep_shadow_match_before_replay"
        return {
            "sweep_inventory_shadow_eligible": shadow_eligible,
            "sweep_inventory_replay_candidate": replay_candidate,
            "sweep_inventory_active_replay_enabled": False,
            "sweep_inventory_eligibility_reason": replay_reason,
            "sweep_inventory_shadow_eligibility_reason": shadow_reason,
            "sweep_inventory_replay_eligibility_reason": replay_reason,
            "sweep_inventory_signature_verdict": signature_verdict,
            "sweep_inventory_signature_next_step": signature_next_step,
        }

    def fields(
        self,
        *,
        config: LocalSweepConfig,
        learned: bool,
        summary_sequence: int,
    ) -> dict[str, object]:
        age_ms = max(0.0, (time.monotonic() - self.first_seen_mono) * 1000.0)
        fields: dict[str, object] = {
            **self.signature.to_observability(),
            "sweep_inventory_sequence": summary_sequence,
            "sweep_inventory_signature_age_ms": round(age_ms, 3),
            "sweep_inventory_learned": learned,
            "sweep_inventory_write_observed_count": self.write_observed_count,
            "sweep_inventory_write_success_count": self.write_success_count,
            "sweep_inventory_write_error_count": self.write_error_count,
            "sweep_inventory_read_data_count": self.read_data_count,
            "sweep_inventory_read_empty_count": self.read_empty_count,
            "sweep_inventory_read_error_count": self.read_error_count,
            "sweep_inventory_read_cache_hit_count": self.read_cache_hit_count,
            "sweep_inventory_read_forwarded_count": self.read_forwarded_count,
            "sweep_inventory_response_count": self.total_response_count,
            "sweep_inventory_projected_write_rtt_savings_ms": _rounded(
                self.write_network_ms.total_ms
            ),
            "sweep_inventory_projected_pair_rtt_savings_ms": _rounded(
                self.pair_network_ms.total_ms
            ),
            **self.write_network_ms.fields("sweep_inventory_write_network"),
            **self.write_duration_ms.fields("sweep_inventory_write_duration"),
            **self.read_network_ms.fields("sweep_inventory_read_network"),
            **self.read_duration_ms.fields("sweep_inventory_read_duration"),
            **self.pair_network_ms.fields("sweep_inventory_pair_network"),
            **self.pair_duration_ms.fields("sweep_inventory_pair_duration"),
        }
        fields.update(self.replay_candidate_fields(config=config, learned=learned))
        return fields


class SweepInventoryTracker:
    """Collect request coverage and RTT-cost evidence without affecting behavior."""

    def __init__(self, config: LocalSweepConfig):
        self._config = config
        self._states: dict[str, SweepInventorySignatureState] = {}
        self._rejections: dict[str, int] = {}
        self._summary_sequence = 0

    def reset_channel(self, channel_id: int) -> None:
        self._states = {
            digest: state
            for digest, state in self._states.items()
            if state.signature.channel_id != channel_id
        }

    def reset_all(self) -> None:
        self._states.clear()
        self._rejections.clear()
        self._summary_sequence = 0

    def record_rejection(self, reason: str) -> None:
        self._rejections[reason] = self._rejections.get(reason, 0) + 1

    def state_for(
        self,
        signature: SweepRequestSignature,
    ) -> SweepInventorySignatureState:
        digest = signature.signature_digest
        state = self._states.get(digest)
        if state is None:
            state = SweepInventorySignatureState(signature=signature)
            self._states[digest] = state
        return state

    def record_write_observed(self, signature: SweepRequestSignature) -> None:
        self.state_for(signature).record_write_observed()

    def record_write_response(
        self,
        signature: SweepRequestSignature,
        *,
        return_code: int | None,
        duration_ms: float | None,
        network_ms: float | None,
    ) -> None:
        self.state_for(signature).record_write_response(
            return_code=return_code,
            duration_ms=duration_ms,
            network_ms=network_ms,
        )

    def record_read_response(
        self,
        signature: SweepRequestSignature,
        *,
        return_code: int,
        message_count: int,
        duration_ms: float | None,
        network_ms: float | None,
        cache_hit: bool,
    ) -> SweepInventorySignatureState:
        state = self.state_for(signature)
        state.record_read_response(
            return_code=return_code,
            message_count=message_count,
            duration_ms=duration_ms,
            network_ms=network_ms,
            cache_hit=cache_hit,
        )
        return state

    @staticmethod
    def _increment(mapping: dict[str, int], key: str, amount: int = 1) -> None:
        mapping[key] = mapping.get(key, 0) + amount

    def _candidate(self, state: SweepInventorySignatureState, *, learned: bool) -> bool:
        return bool(
            state.replay_candidate_fields(config=self._config, learned=learned)[
                "sweep_inventory_replay_candidate"
            ]
        )

    def summary_fields(self, *, learned_digests: set[str]) -> dict[str, object]:
        self._summary_sequence += 1
        signature_count_by_kind: dict[str, int] = {}
        request_count_by_kind: dict[str, int] = {}
        learned_signature_count_by_kind: dict[str, int] = {}
        replay_candidate_request_count_by_kind: dict[str, int] = {}
        replay_candidate_signature_count = 0
        replay_candidate_request_count = 0
        non_gm_replay_candidate_signature_count = 0
        non_gm_replay_candidate_request_count = 0
        learned_signature_count = 0
        learned_request_count = 0
        projected_write_savings_ms = 0.0
        projected_pair_savings_ms = 0.0
        top_candidate: SweepInventorySignatureState | None = None

        accepted_write_count = 0
        data_response_count = 0
        response_count = 0
        for digest, state in self._states.items():
            kind = state.signature.identifier_kind
            learned = digest in learned_digests
            accepted_write_count += state.write_observed_count
            data_response_count += state.read_data_count
            response_count += state.total_response_count
            self._increment(signature_count_by_kind, kind)
            self._increment(request_count_by_kind, kind, state.write_observed_count)
            if learned:
                learned_signature_count += 1
                learned_request_count += state.write_observed_count
                self._increment(learned_signature_count_by_kind, kind)
            if self._candidate(state, learned=learned):
                replay_candidate_signature_count += 1
                replay_candidate_request_count += state.write_observed_count
                projected_write_savings_ms += state.write_network_ms.total_ms
                projected_pair_savings_ms += state.pair_network_ms.total_ms
                self._increment(
                    replay_candidate_request_count_by_kind,
                    kind,
                    state.write_observed_count,
                )
                if kind != "gm_a9_packet":
                    non_gm_replay_candidate_signature_count += 1
                    non_gm_replay_candidate_request_count += state.write_observed_count
                    if top_candidate is None or (
                        state.write_observed_count,
                        state.pair_network_ms.total_ms,
                    ) > (
                        top_candidate.write_observed_count,
                        top_candidate.pair_network_ms.total_ms,
                    ):
                        top_candidate = state

        rejected_write_count = sum(self._rejections.values())
        foreground_write_count = accepted_write_count + rejected_write_count
        coverage_pct = (
            (replay_candidate_request_count / foreground_write_count) * 100.0
            if foreground_write_count
            else 0.0
        )
        learned_coverage_pct = (
            (learned_request_count / foreground_write_count) * 100.0
            if foreground_write_count
            else 0.0
        )
        non_gm_coverage_pct = (
            (non_gm_replay_candidate_request_count / foreground_write_count) * 100.0
            if foreground_write_count
            else 0.0
        )
        if foreground_write_count <= 0:
            inventory_verdict = "pending_no_foreground_writes"
            inventory_next_step = "enter_data_display_and_collect_sweep_inventory"
        elif non_gm_replay_candidate_request_count > 0:
            inventory_verdict = "go_shadow_local"
            inventory_next_step = "run_shadow_local_for_top_non_gm_candidate"
        else:
            inventory_verdict = "no_go_no_non_gm_replay_candidates"
            inventory_next_step = "choose_page_with_repeated_uds_or_obd_read_only_traffic"

        fields: dict[str, object] = {
            "sweep_inventory_sequence": self._summary_sequence,
            "sweep_inventory_signature_count": len(self._states),
            "sweep_inventory_learned_signature_count": learned_signature_count,
            "sweep_inventory_replay_candidate_signature_count": (
                replay_candidate_signature_count
            ),
            "sweep_inventory_non_gm_replay_candidate_signature_count": (
                non_gm_replay_candidate_signature_count
            ),
            "sweep_inventory_foreground_write_count": foreground_write_count,
            "sweep_inventory_accepted_write_count": accepted_write_count,
            "sweep_inventory_rejected_write_count": rejected_write_count,
            "sweep_inventory_data_response_count": data_response_count,
            "sweep_inventory_response_count": response_count,
            "sweep_inventory_learned_request_count": learned_request_count,
            "sweep_inventory_replay_candidate_request_count": (
                replay_candidate_request_count
            ),
            "sweep_inventory_non_gm_replay_candidate_request_count": (
                non_gm_replay_candidate_request_count
            ),
            "sweep_inventory_replay_candidate_coverage_pct": round(coverage_pct, 3),
            "sweep_inventory_non_gm_replay_candidate_coverage_pct": round(
                non_gm_coverage_pct,
                3,
            ),
            "sweep_inventory_learned_coverage_pct": round(learned_coverage_pct, 3),
            "sweep_inventory_projected_write_rtt_savings_ms": round(
                projected_write_savings_ms,
                3,
            ),
            "sweep_inventory_projected_pair_rtt_savings_ms": round(
                projected_pair_savings_ms,
                3,
            ),
            "sweep_inventory_signature_count_by_kind": signature_count_by_kind,
            "sweep_inventory_request_count_by_kind": request_count_by_kind,
            "sweep_inventory_learned_signature_count_by_kind": (
                learned_signature_count_by_kind
            ),
            "sweep_inventory_replay_candidate_request_count_by_kind": (
                replay_candidate_request_count_by_kind
            ),
            "sweep_inventory_rejection_count_by_reason": dict(self._rejections),
            "sweep_inventory_active_replay_enabled": False,
            "sweep_inventory_verdict": inventory_verdict,
            "sweep_inventory_next_step": inventory_next_step,
        }
        if top_candidate is not None:
            fields.update(
                {
                    "sweep_inventory_top_candidate_signature_digest": (
                        top_candidate.signature.signature_digest
                    ),
                    "sweep_inventory_top_candidate_identifier_kind": (
                        top_candidate.signature.identifier_kind
                    ),
                    "sweep_inventory_top_candidate_identifier": (
                        top_candidate.signature.identifier
                    ),
                    "sweep_inventory_top_candidate_payload_prefix_hex": (
                        top_candidate.signature.normalized_payload[:16].hex()
                    ),
                    "sweep_inventory_top_candidate_write_observed_count": (
                        top_candidate.write_observed_count
                    ),
                    "sweep_inventory_top_candidate_read_data_count": (
                        top_candidate.read_data_count
                    ),
                    "sweep_inventory_top_candidate_projected_pair_rtt_savings_ms": round(
                        top_candidate.pair_network_ms.total_ms,
                        3,
                    ),
                }
            )
        return fields
