"""Constrained branch planner for module/category disambiguation.

This planner is intentionally narrow: it only decides among provided choices
for module, data-category, and sub-category branch points.
"""

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Any, Dict, List, Optional


class DecisionDomain(str, Enum):
    """Supported branch-decision domains in narrow planner scope."""

    MODULE = "module"
    SUB_MODULE = "sub_module"
    DATA_CATEGORY = "data_category"
    SUB_CATEGORY = "sub_category"


@dataclass
class RankedOption:
    """Single ranked candidate from planner scoring."""

    option: str
    score: float
    reasons: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.option:
            raise ValueError("option cannot be empty")
        if self.score < 0.0 or self.score > 1.0:
            raise ValueError("score must be in [0.0, 1.0]")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "option": self.option,
            "score": self.score,
            "reasons": list(self.reasons),
        }


@dataclass
class BranchDecision:
    """Planner output contract for one branch decision."""

    domain: DecisionDomain
    target: str
    selected_option: Optional[str]
    requires_human: bool
    confidence: float
    ranked_options: List[RankedOption] = field(default_factory=list)
    reason: str = ""

    def __post_init__(self) -> None:
        if self.confidence < 0.0 or self.confidence > 1.0:
            raise ValueError("confidence must be in [0.0, 1.0]")
        if self.requires_human and self.selected_option is not None:
            raise ValueError("selected_option must be None when requires_human=True")
        if (not self.requires_human) and not self.selected_option:
            raise ValueError("selected_option is required when requires_human=False")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "domain": self.domain.value,
            "target": self.target,
            "selected_option": self.selected_option,
            "requires_human": self.requires_human,
            "confidence": self.confidence,
            "ranked_options": [item.to_dict() for item in self.ranked_options],
            "reason": self.reason,
        }


class BranchDecisionRequiredError(RuntimeError):
    """Raised when branch selection is ambiguous and requires human input."""

    def __init__(self, decision: BranchDecision, choices: List[str]):
        if not decision.requires_human:
            raise ValueError("BranchDecisionRequiredError requires decision.requires_human=True")
        self.decision = decision
        self.choices = list(choices)
        super().__init__(decision.reason or "Branch decision requires human input")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision.to_dict(),
            "choices": self.choices,
        }


class ConstrainedPlanner:
    """Heuristic constrained planner for branch disambiguation.

    The planner only ranks existing choices and never emits free-form actions.
    """

    def __init__(self, min_confidence: float = 0.58, ambiguity_gap: float = 0.08) -> None:
        if min_confidence <= 0.0 or min_confidence >= 1.0:
            raise ValueError("min_confidence must be in (0, 1)")
        if ambiguity_gap <= 0.0 or ambiguity_gap >= 1.0:
            raise ValueError("ambiguity_gap must be in (0, 1)")
        self.min_confidence = min_confidence
        self.ambiguity_gap = ambiguity_gap

    def decide_module(self, target: str, choices: List[str]) -> BranchDecision:
        return self._decide(DecisionDomain.MODULE, target, choices)

    def decide_sub_module(self, target: str, choices: List[str]) -> BranchDecision:
        return self._decide(DecisionDomain.SUB_MODULE, target, choices)

    def decide_data_category(self, target: str, choices: List[str]) -> BranchDecision:
        return self._decide(DecisionDomain.DATA_CATEGORY, target, choices)

    def decide_sub_category(self, target: str, choices: List[str]) -> BranchDecision:
        return self._decide(DecisionDomain.SUB_CATEGORY, target, choices)

    def _decide(self, domain: DecisionDomain, target: str, choices: List[str]) -> BranchDecision:
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
        reasons: List[str] = []

        if option_norm == target_norm:
            score += 0.62
            reasons.append("exact_normalized_match")

        if target_norm and option_norm:
            if target_norm in option_norm or option_norm in target_norm:
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

        bounded = max(0.0, min(1.0, score))
        return RankedOption(option=option, score=bounded, reasons=reasons)

    @staticmethod
    def _normalize(text: str) -> str:
        lowered = text.lower().strip()
        lowered = re.sub(r"\s+", " ", lowered)
        lowered = re.sub(r"[^\w\s-]", "", lowered)
        return lowered

    @staticmethod
    def _extract_module_code(text: str) -> Optional[str]:
        match = re.search(r"\[([a-z0-9]+)\]", text.lower())
        if match:
            return match.group(1)

        # Also support bare identifiers like "K20" without brackets.
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
