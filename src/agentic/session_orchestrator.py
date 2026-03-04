"""In-memory session orchestration with decision flow and SSE events.

Manages session lifecycle: routing by brand, decision gates for
uncertain/missing workflows, and SSE-formatted event strings for
real-time client communication.
"""

import json
import logging
import queue
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Brands routed to the gds2 workflow adapter
GM_BRANDS = frozenset({
    "chevrolet", "buick", "gmc", "cadillac",
    "holden", "opel", "vauxhall", "baojun", "wuling",
})

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SessionStatus(str, Enum):
    """Lifecycle states for a diagnostics session."""

    PENDING = "pending"
    RUNNING = "running"
    AWAITING_DECISION = "awaiting_decision"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


class SessionEventType(str, Enum):
    """Event types emitted over SSE."""

    PROGRESS = "progress"
    DECISION_REQUIRED = "decision_required"
    DECISION_RESOLVED = "decision_resolved"
    DECISION_TIMEOUT = "decision_timeout"
    ERROR = "error"
    DONE = "done"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


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

    def to_dict(self) -> Dict[str, Any]:
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
    options: List[DecisionOption]
    kind: str = "workflow"
    context: Dict[str, Any] = field(default_factory=dict)
    timeout_sec: float = 120.0
    fallback_option_id: Optional[str] = None
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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "prompt": self.prompt,
            "options": [o.to_dict() for o in self.options],
            "kind": self.kind,
            "context": self.context,
            "timeout_sec": self.timeout_sec,
            "fallback_option_id": self.fallback_option_id,
            "created_at": self.created_at,
        }

    def is_expired(self, now: Optional[float] = None) -> bool:
        ts = time.time() if now is None else now
        return (ts - self.created_at) >= self.timeout_sec


@dataclass
class SessionContext:
    """Input context for starting a session."""

    brand: str
    model: str = ""
    vin: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.brand:
            raise ValueError("brand cannot be empty")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "brand": self.brand,
            "model": self.model,
            "vin": self.vin,
            "extra": self.extra,
        }


@dataclass
class Session:
    """In-memory session state."""

    session_id: str
    context: SessionContext
    status: SessionStatus = SessionStatus.PENDING
    workflow: Optional[str] = None
    pending_decision: Optional[DecisionGate] = None
    resolved_decisions: List[Dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "context": self.context.to_dict(),
            "status": self.status.value,
            "workflow": self.workflow,
            "pending_decision": (
                self.pending_decision.to_dict()
                if self.pending_decision
                else None
            ),
            "resolved_decisions": self.resolved_decisions,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------


def sse_event(event_type: str, data: Dict[str, Any]) -> str:
    """Build an SSE-formatted event string.

    Format: ``event: <type>\\ndata: <json>\\n\\n``
    """
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------------------
# Brand routing
# ---------------------------------------------------------------------------


def route_workflow(brand: str) -> Optional[str]:
    """Return the workflow adapter name for *brand*, or ``None``."""
    if brand.lower().strip() in GM_BRANDS:
        return "gds2"
    return None


# ---------------------------------------------------------------------------
# Session Orchestrator
# ---------------------------------------------------------------------------


class SessionOrchestrator:
    """In-memory session manager with decision flow.

    Holds sessions, their SSE event queues, and provides the state-machine
    transitions consumed by the API layer.
    """

    def __init__(self) -> None:
        self._sessions: Dict[str, Session] = {}
        self._queues: Dict[str, queue.Queue] = {}

    # -- helpers -------------------------------------------------------------

    def _get_session(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"Session {session_id} not found")
        return session

    def _emit(self, session_id: str, event_type: SessionEventType,
              data: Dict[str, Any]) -> None:
        q = self._queues.get(session_id)
        if q is None:
            return
        msg = sse_event(event_type.value, data)
        try:
            q.put_nowait(msg)
        except queue.Full:
            logger.warning("Event queue full for session %s, dropping event", session_id)

    def _touch(self, session: Session) -> None:
        session.updated_at = time.time()

    # -- public API ----------------------------------------------------------

    def start_session(self, context: SessionContext) -> Session:
        """Create a session, route by brand, and emit initial events.

        If the brand maps to a known workflow, status becomes ``running``
        and a ``progress`` event is emitted.  Otherwise status becomes
        ``awaiting_decision`` with a ``decision_required`` event asking
        the user to select a workflow.
        """
        session_id = uuid.uuid4().hex[:16]
        session = Session(session_id=session_id, context=context)
        self._sessions[session_id] = session
        self._queues[session_id] = queue.Queue(maxsize=500)

        workflow = route_workflow(context.brand)

        if workflow is not None:
            session.workflow = workflow
            session.status = SessionStatus.RUNNING
            self._touch(session)
            self._emit(session_id, SessionEventType.PROGRESS, {
                "message": f"Routed to {workflow} workflow",
                "workflow": workflow,
                "session_id": session_id,
            })
        else:
            # Unknown brand — ask user to pick
            decision = DecisionGate(
                decision_id=uuid.uuid4().hex[:12],
                prompt=f"No automatic workflow for brand '{context.brand}'. Please select a workflow.",
                options=[
                    DecisionOption(option_id="gds2", label="GDS2",
                                   description="General Motors GDS2 diagnostic tool"),
                    DecisionOption(option_id="manual", label="Manual",
                                   description="Skip automated workflow"),
                ],
                timeout_sec=120.0,
                fallback_option_id="manual",
            )
            session.pending_decision = decision
            session.status = SessionStatus.AWAITING_DECISION
            self._touch(session)
            self._emit(session_id, SessionEventType.DECISION_REQUIRED, {
                "session_id": session_id,
                "decision": decision.to_dict(),
            })

        return session

    def get_session(self, session_id: str) -> Session:
        """Return the session or raise ``KeyError``."""
        return self._get_session(session_id)

    def get_event_queue(self, session_id: str) -> Optional[queue.Queue]:
        """Return the SSE event queue for *session_id*, or ``None``."""
        return self._queues.get(session_id)

    def submit_decision(self, session_id: str, decision_id: str,
                        option_id: str, source: str = "user") -> Session:
        """Submit a user decision for a pending gate.

        Validates that the session exists, is awaiting a decision,
        the decision_id matches, and the option_id is valid.

        On success transitions the session back to ``running`` and
        emits ``decision_resolved`` + ``progress``.
        """
        session = self._get_session(session_id)

        if session.status != SessionStatus.AWAITING_DECISION:
            raise ValueError(
                f"Session {session_id} is not awaiting a decision "
                f"(status={session.status.value})"
            )

        gate = session.pending_decision
        if gate is None or gate.decision_id != decision_id:
            raise ValueError(
                f"Decision ID mismatch: expected "
                f"{gate.decision_id if gate else 'None'}, got {decision_id}"
            )

        valid_ids = {o.option_id for o in gate.options}
        if option_id not in valid_ids:
            raise ValueError(
                f"Invalid option_id '{option_id}'. "
                f"Valid options: {sorted(valid_ids)}"
            )

        # Resolve
        resolution = {
            "decision_id": decision_id,
            "option_id": option_id,
            "resolved_at": time.time(),
            "source": source,
        }
        session.resolved_decisions.append(resolution)

        # Apply decision only for workflow-routing gates.
        if gate.kind == "workflow":
            if option_id == "gds2":
                session.workflow = "gds2"
            elif option_id == "manual":
                session.workflow = "manual"
            else:
                session.workflow = option_id

        session.pending_decision = None
        session.status = SessionStatus.RUNNING
        self._touch(session)

        self._emit(session_id, SessionEventType.DECISION_RESOLVED, {
            "session_id": session_id,
            "decision_id": decision_id,
            "option_id": option_id,
            "source": source,
        })
        self._emit(session_id, SessionEventType.PROGRESS, {
            "session_id": session_id,
            "message": (
                f"Workflow set to {session.workflow}"
                if gate.kind == "workflow"
                else f"Decision '{option_id}' applied"
            ),
            "workflow": session.workflow,
        })

        return session


    def check_decision_timeout(self, session_id: str) -> bool:
        """Auto-resolve expired pending decisions with deterministic fallback."""
        session = self._get_session(session_id)
        if session.status != SessionStatus.AWAITING_DECISION:
            return False

        gate = session.pending_decision
        if gate is None:
            return False

        if not gate.is_expired():
            return False

        fallback = gate.fallback_option_id or gate.options[0].option_id
        self._emit(session_id, SessionEventType.DECISION_TIMEOUT, {
            "session_id": session_id,
            "decision_id": gate.decision_id,
            "fallback_option": fallback,
            "message": "Decision timed out. Applying fallback option.",
        })
        self.submit_decision(session_id, gate.decision_id, fallback, source="timeout")
        return True

    def abort_session(self, session_id: str, reason: str = "") -> Session:
        """Abort a session regardless of current status.

        Emits a ``done`` event with ``aborted=True`` so SSE consumers
        know to close the stream.
        """
        session = self._get_session(session_id)
        terminal = {SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.ABORTED}
        if session.status in terminal:
            raise ValueError(
                f"Session {session_id} already in terminal state "
                f"({session.status.value})"
            )

        session.status = SessionStatus.ABORTED
        session.error = reason or "Aborted by user"
        session.pending_decision = None
        self._touch(session)

        self._emit(session_id, SessionEventType.DONE, {
            "session_id": session_id,
            "aborted": True,
            "reason": session.error,
        })

        return session

    def complete_session(self, session_id: str,
                         result: Optional[Dict[str, Any]] = None) -> Session:
        """Mark session as completed and emit ``done``."""
        session = self._get_session(session_id)
        session.status = SessionStatus.COMPLETED
        self._touch(session)
        self._emit(session_id, SessionEventType.DONE, {
            "session_id": session_id,
            "result": result or {},
        })
        return session

    def fail_session(self, session_id: str, error: str) -> Session:
        """Mark session as failed and emit ``error`` + ``done``."""
        session = self._get_session(session_id)
        session.status = SessionStatus.FAILED
        session.error = error
        self._touch(session)
        self._emit(session_id, SessionEventType.ERROR, {
            "session_id": session_id,
            "error": error,
        })
        self._emit(session_id, SessionEventType.DONE, {
            "session_id": session_id,
            "error": error,
        })
        return session

    def emit_progress(self, session_id: str, message: str,
                      extra: Optional[Dict[str, Any]] = None) -> None:
        """Emit an ad-hoc progress event (e.g. from executor callbacks)."""
        _ = self._get_session(session_id)  # validate existence
        data: Dict[str, Any] = {"session_id": session_id, "message": message}
        if extra:
            data.update(extra)
        self._emit(session_id, SessionEventType.PROGRESS, data)

    def raise_decision(self, session_id: str, gate: DecisionGate) -> Session:
        """Push a new decision gate mid-workflow.

        Transitions session to ``awaiting_decision``.
        """
        session = self._get_session(session_id)
        if session.status != SessionStatus.RUNNING:
            raise ValueError(
                f"Cannot raise decision in status {session.status.value}"
            )
        session.pending_decision = gate
        session.status = SessionStatus.AWAITING_DECISION
        self._touch(session)
        self._emit(session_id, SessionEventType.DECISION_REQUIRED, {
            "session_id": session_id,
            "decision": gate.to_dict(),
        })
        return session
