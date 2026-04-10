"""GDS2-specific constrained branch planner."""

from __future__ import annotations

import re
from collections.abc import Sequence

from diagnostic_platform.branch_planning import (
    BranchDecision,
    BranchDecisionRequiredError,
    DecisionDomain,
    RankedOption,
)


class ConstrainedPlanner:
    """Heuristic constrained planner for GDS2 branch disambiguation."""

    def __init__(self, min_confidence: float = 0.58, ambiguity_gap: float = 0.08) -> None:
        if not 0.0 < min_confidence < 1.0:
            raise ValueError("min_confidence must be in (0, 1)")
        if not 0.0 < ambiguity_gap < 1.0:
            raise ValueError("ambiguity_gap must be in (0, 1)")
        self.min_confidence = min_confidence
        self.ambiguity_gap = ambiguity_gap

    def decide_module(self, target: str, choices: Sequence[str]) -> BranchDecision:
        return self._decide(DecisionDomain.MODULE, target, choices)

    def decide_sub_module(self, target: str, choices: Sequence[str]) -> BranchDecision:
        return self._decide(DecisionDomain.SUB_MODULE, target, choices)

    def decide_data_category(self, target: str, choices: Sequence[str]) -> BranchDecision:
        return self._decide(DecisionDomain.DATA_CATEGORY, target, choices)

    def decide_sub_category(self, target: str, choices: Sequence[str]) -> BranchDecision:
        return self._decide(DecisionDomain.SUB_CATEGORY, target, choices)

    def _decide(
        self,
        domain: DecisionDomain,
        target: str,
        choices: Sequence[str],
    ) -> BranchDecision:
        target_text = target.strip()
        if not target_text:
            return BranchDecision(
                domain=domain,
                target=target,
                selected_option=None,
                requires_human=True,
                confidence=0.0,
                ranked_options=[],
                reason="Empty target cannot be auto-resolved",
            )

        cleaned_choices = [choice for choice in choices if choice and choice.strip()]
        if not cleaned_choices:
            return BranchDecision(
                domain=domain,
                target=target_text,
                selected_option=None,
                requires_human=True,
                confidence=0.0,
                ranked_options=[],
                reason="No choices available",
            )

        ranked = sorted(
            [self._score_option(target_text, option) for option in cleaned_choices],
            key=lambda item: item.score,
            reverse=True,
        )

        best = ranked[0]
        second = ranked[1] if len(ranked) > 1 else None

        if best.score < self.min_confidence:
            return BranchDecision(
                domain=domain,
                target=target_text,
                selected_option=None,
                requires_human=True,
                confidence=best.score,
                ranked_options=ranked,
                reason=f"Low confidence ({best.score:.2f})",
            )

        if second is not None:
            gap = best.score - second.score
            second_threshold = max(0.45, self.min_confidence - 0.05)
            if gap < self.ambiguity_gap and second.score >= second_threshold:
                return BranchDecision(
                    domain=domain,
                    target=target_text,
                    selected_option=None,
                    requires_human=True,
                    confidence=best.score,
                    ranked_options=ranked,
                    reason=(
                        f"Ambiguous top candidates: '{best.option}' ({best.score:.2f}) "
                        f"vs '{second.option}' ({second.score:.2f})"
                    ),
                )

        return BranchDecision(
            domain=domain,
            target=target_text,
            selected_option=best.option,
            requires_human=False,
            confidence=best.score,
            ranked_options=ranked,
            reason=f"Selected best match '{best.option}'",
        )

    def _score_option(self, target: str, option: str) -> RankedOption:
        target_norm = self._normalize(target)
        option_norm = self._normalize(option)

        score = 0.0
        reasons: list[str] = []

        if option_norm == target_norm:
            score += 0.62
            reasons.append("exact_normalized_match")

        if target_norm and option_norm and (
            target_norm in option_norm or option_norm in target_norm
        ):
            score += 0.34
            reasons.append("substring_match")

        target_code = self._extract_module_code(target)
        option_code = self._extract_module_code(option)
        if target_code and option_code and target_code == option_code:
            score += 0.26
            reasons.append("module_code_match")

        token_score = self._token_overlap_score(target_norm, option_norm)
        if token_score > 0:
            score += token_score * 0.30
            reasons.append(f"token_overlap={token_score:.2f}")

        if target_norm and option_norm and option_norm.startswith(target_norm):
            score += 0.08
            reasons.append("prefix_match")

        return RankedOption(option=option, score=max(0.0, min(1.0, score)), reasons=reasons)

    @staticmethod
    def _normalize(text: str) -> str:
        lowered = text.lower().strip()
        lowered = re.sub(r"\s+", " ", lowered)
        lowered = re.sub(r"[^\w\s-]", "", lowered)
        return lowered

    @staticmethod
    def _extract_module_code(text: str) -> str | None:
        match = re.search(r"\[([a-z0-9]+)\]", text.lower())
        if match:
            return match.group(1)

        bare = re.search(r"\b([a-z]{1,2}\d{2,3}[a-z]?)\b", text.lower())
        if bare:
            return bare.group(1)
        return None

    @staticmethod
    def _token_overlap_score(a: str, b: str) -> float:
        tokens_a = {token.strip("[]") for token in a.split(" ") if token.strip("[]")}
        tokens_b = {token.strip("[]") for token in b.split(" ") if token.strip("[]")}
        if not tokens_a or not tokens_b:
            return 0.0

        intersection = len(tokens_a.intersection(tokens_b))
        union = len(tokens_a.union(tokens_b))
        if union == 0:
            return 0.0
        return intersection / union


__all__ = [
    "BranchDecision",
    "BranchDecisionRequiredError",
    "ConstrainedPlanner",
    "DecisionDomain",
    "RankedOption",
]
