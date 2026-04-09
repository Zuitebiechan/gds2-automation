"""Platform-owned business-session models shared across runtime layers."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SessionStatus(str, Enum):
    """Lifecycle states for a diagnostics session."""

    PENDING = "pending"
    RUNNING = "running"
    AWAITING_DECISION = "awaiting_decision"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


@dataclass
class DecisionOption:
    """A single selectable option within a decision gate."""

    option_id: str
    label: str
    description: str = ""

    def __post_init__(self) -> None:
        if not self.option_id:
            raise ValueError("option_id cannot be empty")
        if not self.label:
            raise ValueError("label cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "option_id": self.option_id,
            "label": self.label,
            "description": self.description,
        }


@dataclass
class DecisionGate:
    """A pending decision that requires user input."""

    decision_id: str
    prompt: str
    options: list[DecisionOption]
    kind: str = "backend"
    context: dict[str, Any] = field(default_factory=dict)
    timeout_sec: float = 120.0
    fallback_option_id: str | None = None
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.decision_id:
            raise ValueError("decision_id cannot be empty")
        if not self.prompt:
            raise ValueError("prompt cannot be empty")
        if not self.options:
            raise ValueError("options must have at least one entry")
        if not self.kind:
            raise ValueError("kind cannot be empty")
        if self.timeout_sec <= 0:
            raise ValueError("timeout_sec must be > 0")
        if self.fallback_option_id is not None:
            valid = {opt.option_id for opt in self.options}
            if self.fallback_option_id not in valid:
                raise ValueError("fallback_option_id must exist in options")

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "prompt": self.prompt,
            "options": [option.to_dict() for option in self.options],
            "kind": self.kind,
            "context": self.context,
            "timeout_sec": self.timeout_sec,
            "fallback_option_id": self.fallback_option_id,
            "created_at": self.created_at,
        }

    def is_expired(self, now: float | None = None) -> bool:
        ts = time.time() if now is None else now
        return (ts - self.created_at) >= self.timeout_sec


@dataclass
class SessionContext:
    """Input context for starting a session."""

    brand: str
    model: str = ""
    vin: str = ""
    backend_name: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.brand:
            raise ValueError("brand cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "brand": self.brand,
            "model": self.model,
            "vin": self.vin,
            "backend_name": self.backend_name,
            "extra": self.extra,
        }


@dataclass
class Session:
    """In-memory session state."""

    session_id: str
    context: SessionContext
    status: SessionStatus = SessionStatus.PENDING
    backend_name: str | None = None
    capabilities: list[str] = field(default_factory=list)
    pending_decision: DecisionGate | None = None
    network_override: dict[str, Any] | None = None
    resolved_decisions: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    error: str | None = None
    selected_module: str = ""
    selected_data_category: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "context": self.context.to_dict(),
            "status": self.status.value,
            "backend_name": self.backend_name,
            "workflow": self.backend_name,
            "capabilities": self.capabilities,
            "pending_decision": (
                self.pending_decision.to_dict()
                if self.pending_decision is not None
                else None
            ),
            "network_override": self.network_override,
            "resolved_decisions": self.resolved_decisions,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
            "selected_module": self.selected_module,
            "selected_data_category": self.selected_data_category,
        }


__all__ = [
    "DecisionGate",
    "DecisionOption",
    "Session",
    "SessionContext",
    "SessionStatus",
]
