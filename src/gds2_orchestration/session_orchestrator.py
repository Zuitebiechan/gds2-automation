"""In-memory session orchestration with decision flow and SSE events.

Manages session lifecycle: backend routing by brand, decision gates for
uncertain/missing backend selection, and SSE-formatted event strings for
real-time client communication.

NOTE on session management layers:
    This module provides the *business-level* session manager used by
    server/api/session.py (/api/session/*).  It handles brand-to-backend
    routing, DecisionGate with timeout/fallback, and ad-hoc progress
    events from backend executors.

    A separate, simpler session mechanism exists in server/api/navigate.py
    for the *execution-level* deterministic navigation flow used by
    the /api/navigate/* endpoints. The two are intentionally independent.
"""

import json
import logging
import queue
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from diagnostic_platform.backend_registry import get_backend_registry
from diagnostic_platform.contracts import BackendDescriptor

logger = logging.getLogger(__name__)

BACKEND_DECISION_KINDS = {"backend"}

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Module-level singleton registry for brand → backend routing.
# Lazily initialised on first call to avoid circular imports.

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
    NETWORK_QUALITY_CHANGED = "network_quality_changed"
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
    kind: str = "backend"
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
    backend_name: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.brand:
            raise ValueError("brand cannot be empty")

    def to_dict(self) -> Dict[str, Any]:
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
    backend_name: Optional[str] = None
    capabilities: List[str] = field(default_factory=list)
    pending_decision: Optional[DecisionGate] = None
    network_override: Optional[Dict[str, Any]] = None
    resolved_decisions: List[Dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    error: Optional[str] = None
    selected_module: str = ""
    selected_data_category: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "context": self.context.to_dict(),
            "status": self.status.value,
            "backend_name": self.backend_name,
            "workflow": self.backend_name,
            "capabilities": self.capabilities,
            "pending_decision": (
                self.pending_decision.to_dict()
                if self.pending_decision
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


def _get_registry():
    """Return the shared backend registry for brand routing."""
    return get_backend_registry()


def _descriptor_capabilities(descriptor: BackendDescriptor) -> list[str]:
    return descriptor.capability_values()


def _descriptors_for_brand(brand: str) -> list[BackendDescriptor]:
    return [backend.descriptor for backend in _get_registry().find_by_brand(brand)]


def _all_backend_descriptors() -> list[BackendDescriptor]:
    return _get_registry().list_descriptors()


def _backend_option(descriptor: BackendDescriptor) -> DecisionOption:
    supported = ", ".join(descriptor.supported_brands[:3])
    return DecisionOption(
        option_id=f"backend:{descriptor.backend_name}",
        label=descriptor.display_name,
        description=supported or descriptor.ui_mode,
    )


def _build_backend_decision(
    *,
    brand: str,
    descriptors: list[BackendDescriptor],
) -> DecisionGate:
    prompt = (
        f"Multiple backends can handle brand '{brand}'. Please choose a backend."
        if descriptors
        else f"No automatic backend for brand '{brand}'. Please choose a backend."
    )
    options = [_backend_option(descriptor) for descriptor in (descriptors or _all_backend_descriptors())]
    options.append(
        DecisionOption(
            option_id="manual",
            label="Manual",
            description="Skip automated backend binding",
        )
    )
    return DecisionGate(
        decision_id=uuid.uuid4().hex[:12],
        prompt=prompt,
        options=options,
        kind="backend",
        timeout_sec=120.0,
        fallback_option_id="manual",
    )


def route_backend(brand: str) -> Optional[str]:
    """Return the backend name for a brand, or ``None`` if unsupported."""
    return _get_registry().resolve_brand(brand).selected_backend_name


# ---------------------------------------------------------------------------
# Session Orchestrator
# ---------------------------------------------------------------------------


class SessionOrchestrator:
    """In-memory session manager with decision flow.

    Holds sessions, their SSE event queues, and provides the state-machine
    transitions consumed by the API layer.
    """

    def __init__(self, registry_provider: Callable[[], Any] | None = None) -> None:
        self._sessions: Dict[str, Session] = {}
        self._queues: Dict[str, queue.Queue] = {}
        self._registry_provider = registry_provider or get_backend_registry

    def _registry(self):
        return self._registry_provider()

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

    def _find_active_session(self) -> Optional[Session]:
        terminal = {
            SessionStatus.COMPLETED,
            SessionStatus.FAILED,
            SessionStatus.ABORTED,
        }
        for session in self._sessions.values():
            if session.status not in terminal:
                return session
        return None

    # -- public API ----------------------------------------------------------

    def start_session(self, context: SessionContext) -> Session:
        """Create a session, resolve a backend by brand, and emit initial events.

        If the brand maps to a known backend, status becomes ``running``
        and a ``progress`` event is emitted.  Otherwise status becomes
        ``awaiting_decision`` with a ``decision_required`` event asking
        the user to select a backend.
        """
        active_session = self._find_active_session()
        if active_session is not None:
            raise RuntimeError(
                "Another session is already active "
                f"(session_id={active_session.session_id}, status={active_session.status.value})"
            )

        session_id = uuid.uuid4().hex[:16]
        session = Session(session_id=session_id, context=context)
        self._sessions[session_id] = session
        self._queues[session_id] = queue.Queue(maxsize=500)

        preferred_backend_name = (
            context.backend_name
            or str(context.extra.get("backend_name") or "").strip()
        )
        routed_backend_name = (
            preferred_backend_name
            or route_backend(context.brand)
        )
        resolution = self._registry().resolve_brand(
            context.brand,
            preferred_backend_name=routed_backend_name or None,
        )
        descriptors = list(resolution.candidates)

        if resolution.selected_backend_name is not None:
            descriptor = resolution.selected_descriptor or self._registry().get_descriptor(
                resolution.selected_backend_name
            )
            session.backend_name = descriptor.backend_name
            session.capabilities = _descriptor_capabilities(descriptor)
            session.status = SessionStatus.RUNNING
            self._touch(session)
            self._emit(session_id, SessionEventType.PROGRESS, {
                "message": f"Routed to {descriptor.backend_name} backend",
                "workflow": session.backend_name,
                "backend_name": session.backend_name,
                "capabilities": session.capabilities,
                "session_id": session_id,
            })
        else:
            decision = _build_backend_decision(
                brand=context.brand,
                descriptors=descriptors,
            )
            session.pending_decision = decision
            session.status = SessionStatus.AWAITING_DECISION
            self._touch(session)
            self._emit(session_id, SessionEventType.DECISION_REQUIRED, {
                "session_id": session_id,
                "decision": decision.to_dict(),
            })

        return session

    def get_active_session(self) -> Optional[Session]:
        """Return the current non-terminal session, if one exists."""
        return self._find_active_session()

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

        is_backend_gate = gate.kind in BACKEND_DECISION_KINDS
        if is_backend_gate:
            if option_id == "manual":
                session.backend_name = "manual"
                session.capabilities = []
            elif option_id.startswith("backend:"):
                backend_name = option_id.split(":", 1)[1]
                descriptor = self._registry().get_descriptor(backend_name)
                session.backend_name = descriptor.backend_name
                session.capabilities = _descriptor_capabilities(descriptor)
            else:
                session.backend_name = option_id
                try:
                    descriptor = self._registry().get_descriptor(option_id)
                    session.capabilities = _descriptor_capabilities(descriptor)
                except KeyError:
                    session.capabilities = []

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
                f"Backend set to {session.backend_name}"
                if is_backend_gate
                else f"Decision '{option_id}' applied"
            ),
            "workflow": session.backend_name,
            "backend_name": session.backend_name,
            "capabilities": session.capabilities,
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
        session.network_override = None
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
        session.network_override = None
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
        session.network_override = None
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
        """Push a new decision gate mid-session.

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
