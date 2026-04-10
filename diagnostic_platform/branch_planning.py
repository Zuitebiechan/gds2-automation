"""Platform-owned branch-planning result models and exceptions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


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
    reasons: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.option:
            raise ValueError("option cannot be empty")
        if self.score < 0.0 or self.score > 1.0:
            raise ValueError("score must be in [0.0, 1.0]")

    def to_dict(self) -> dict[str, Any]:
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
    selected_option: str | None
    requires_human: bool
    confidence: float
    ranked_options: list[RankedOption] = field(default_factory=list)
    reason: str = ""

    def __post_init__(self) -> None:
        if self.confidence < 0.0 or self.confidence > 1.0:
            raise ValueError("confidence must be in [0.0, 1.0]")
        if self.requires_human and self.selected_option is not None:
            raise ValueError("selected_option must be None when requires_human=True")
        if (not self.requires_human) and not self.selected_option:
            raise ValueError("selected_option is required when requires_human=False")

    def to_dict(self) -> dict[str, Any]:
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

    def __init__(self, decision: BranchDecision, choices: list[str]):
        if not decision.requires_human:
            raise ValueError("BranchDecisionRequiredError requires decision.requires_human=True")
        self.decision = decision
        self.choices = list(choices)
        super().__init__(decision.reason or "Branch decision requires human input")

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.to_dict(),
            "choices": self.choices,
        }


__all__ = [
    "BranchDecision",
    "BranchDecisionRequiredError",
    "DecisionDomain",
    "RankedOption",
]
