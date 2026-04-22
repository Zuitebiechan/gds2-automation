"""Platform-owned business-session orchestrator and SSE helpers."""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import uuid
from enum import Enum
from typing import Any, Protocol

from diagnostic_platform.backend_registry import get_backend_registry
from diagnostic_platform.contracts import BackendDescriptor
from diagnostic_platform.safe_utils import (
    display_text as _display_text,
    json_dumps_safe as _json_sse_data,
    mapping_or_empty as _mapping_or_empty,
)
from diagnostic_platform.session_models import (
    DecisionGate,
    DecisionOption,
    Session,
    SessionContext,
    SessionStatus,
)
from diagnostic_platform.session_observability import emit_session_runtime_event

logger = logging.getLogger(__name__)

BACKEND_DECISION_KINDS = {"backend"}
_TERMINAL_SESSION_RETENTION_SEC = 30.0


class SessionEventType(str, Enum):
    """Event types emitted over SSE."""

    PROGRESS = "progress"
    DECISION_REQUIRED = "decision_required"
    DECISION_RESOLVED = "decision_resolved"
    DECISION_TIMEOUT = "decision_timeout"
    NETWORK_QUALITY_CHANGED = "network_quality_changed"
    ERROR = "error"
    DONE = "done"


_TERMINAL_EVENT_TYPES = {
    SessionEventType.ERROR,
    SessionEventType.DONE,
}


def sse_event(event_type: str, data: dict[str, Any]) -> str:
    """Build an SSE-formatted event string."""
    return f"event: {event_type}\ndata: {_json_sse_data(data)}\n\n"


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


def route_backend(brand: str) -> str | None:
    """Return the backend name for a brand, or ``None`` if unsupported."""
    return _get_registry().resolve_brand(brand).selected_backend_name


class BusinessSessionOrchestrator(Protocol):
    """Protocol for the worker-scoped business-session manager."""

    def start_session(self, context: SessionContext) -> Session: ...

    def get_session(self, session_id: str) -> Session: ...

    def get_active_session(self) -> Session | None: ...

    def abort_session(self, session_id: str, reason: str) -> Session: ...

    def complete_session(self, session_id: str, result: dict[str, Any] | None = None) -> Session: ...

    def raise_decision(self, session_id: str, gate: DecisionGate) -> Session: ...

    def submit_decision(self, session_id: str, decision_id: str, option_id: str) -> Session: ...

    def emit_progress(
        self,
        session_id: str,
        message: str,
        extra: dict[str, Any] | None = None,
    ) -> None: ...

    def get_event_queue(self, session_id: str) -> Any: ...

    def check_decision_timeout(self, session_id: str) -> bool: ...


class SessionOrchestrator:
    """In-memory session manager with decision flow."""

    def __init__(
        self,
        registry_provider: Any | None = None,
        *,
        terminal_retention_sec: float = _TERMINAL_SESSION_RETENTION_SEC,
    ) -> None:
        self._sessions: dict[str, Session] = {}
        self._queues: dict[str, queue.Queue] = {}
        self._registry_provider = registry_provider or get_backend_registry
        self._terminal_retention_sec = max(0.0, float(terminal_retention_sec))
        self._cleanup_timers: dict[str, threading.Timer] = {}
        self._lock = threading.RLock()

    def _registry(self):
        return self._registry_provider()

    def _get_session(self, session_id: str) -> Session:
        with self._lock:
            return self._get_session_unlocked(session_id)

    def _get_session_unlocked(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"Session {session_id} not found")
        return session

    def _emit(self, session_id: str, event_type: SessionEventType, data: dict[str, Any]) -> None:
        with self._lock:
            event_queue = self._queues.get(session_id)
        if event_queue is None:
            return
        message = sse_event(event_type.value, data)
        try:
            event_queue.put_nowait(message)
        except queue.Full:
            if event_type in _TERMINAL_EVENT_TYPES:
                self._enqueue_terminal_event(event_queue, message, session_id)
                return
            logger.warning("Event queue full for session %s, dropping event", session_id)

    @staticmethod
    def _enqueue_terminal_event(event_queue: queue.Queue, message: str, session_id: str) -> None:
        while True:
            try:
                event_queue.put_nowait(message)
                return
            except queue.Full:
                try:
                    event_queue.get_nowait()
                except queue.Empty:
                    logger.warning(
                        "Event queue cleanup raced empty for terminal event in session %s",
                        session_id,
                    )
                    return

    def _touch(self, session: Session) -> None:
        session.updated_at = time.time()

    def _find_active_session(self) -> Session | None:
        with self._lock:
            return self._find_active_session_unlocked()

    def _find_active_session_unlocked(self) -> Session | None:
        terminal = {
            SessionStatus.COMPLETED,
            SessionStatus.FAILED,
            SessionStatus.ABORTED,
        }
        for session in self._sessions.values():
            if session.status not in terminal:
                return session
        return None

    def _cleanup_session_state(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)
            self._queues.pop(session_id, None)
            self._cleanup_timers.pop(session_id, None)

    def _schedule_terminal_cleanup(self, session_id: str) -> None:
        with self._lock:
            existing = self._cleanup_timers.pop(session_id, None)
            if existing is not None:
                existing.cancel()
            timer = threading.Timer(
                self._terminal_retention_sec,
                self._cleanup_session_state,
                args=(session_id,),
            )
            timer.daemon = True
            self._cleanup_timers[session_id] = timer
        timer.start()

    def start_session(self, context: SessionContext) -> Session:
        """Create a session, resolve a backend by brand, and emit initial events."""
        with self._lock:
            active_session = self._find_active_session_unlocked()
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
            routed_backend_name = preferred_backend_name or route_backend(context.brand)
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
                self._emit(
                    session_id,
                    SessionEventType.PROGRESS,
                    {
                        "message": f"Routed to {descriptor.backend_name} backend",
                        "workflow": session.backend_name,
                        "backend_name": session.backend_name,
                        "capabilities": session.capabilities,
                        "session_id": session_id,
                    },
                )
            else:
                decision = _build_backend_decision(
                    brand=context.brand,
                    descriptors=descriptors,
                )
                session.pending_decision = decision
                session.status = SessionStatus.AWAITING_DECISION
                self._touch(session)
                self._emit(
                    session_id,
                    SessionEventType.DECISION_REQUIRED,
                    {
                        "session_id": session_id,
                        "decision": decision.to_dict(),
                    },
                )

        return session

    def get_active_session(self) -> Session | None:
        return self._find_active_session()

    def get_session(self, session_id: str) -> Session:
        return self._get_session(session_id)

    def get_event_queue(self, session_id: str) -> queue.Queue | None:
        with self._lock:
            return self._queues.get(session_id)

    def submit_decision(
        self,
        session_id: str,
        decision_id: str,
        option_id: str,
        source: str = "user",
    ) -> Session:
        with self._lock:
            session = self._get_session_unlocked(session_id)

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

            valid_ids = {option.option_id for option in gate.options}
            if option_id not in valid_ids:
                raise ValueError(
                    f"Invalid option_id '{option_id}'. "
                    f"Valid options: {sorted(valid_ids)}"
                )

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

            self._emit(
                session_id,
                SessionEventType.DECISION_RESOLVED,
                {
                    "session_id": session_id,
                    "decision_id": decision_id,
                    "option_id": option_id,
                    "source": source,
                },
            )
            self._emit(
                session_id,
                SessionEventType.PROGRESS,
                {
                    "session_id": session_id,
                    "message": (
                        f"Backend set to {session.backend_name}"
                        if is_backend_gate
                        else f"Decision '{option_id}' applied"
                    ),
                    "workflow": session.backend_name,
                    "backend_name": session.backend_name,
                    "capabilities": session.capabilities,
                },
            )

        return session

    def check_decision_timeout(self, session_id: str) -> bool:
        with self._lock:
            session = self._get_session_unlocked(session_id)
            if session.status != SessionStatus.AWAITING_DECISION:
                return False

            gate = session.pending_decision
            if gate is None or not gate.is_expired():
                return False

            fallback = gate.fallback_option_id or gate.options[0].option_id
            self._emit(
                session_id,
                SessionEventType.DECISION_TIMEOUT,
                {
                    "session_id": session_id,
                    "decision_id": gate.decision_id,
                    "fallback_option": fallback,
                    "message": "Decision timed out. Applying fallback option.",
                },
            )
            self.submit_decision(session_id, gate.decision_id, fallback, source="timeout")
            return True

    def abort_session(self, session_id: str, reason: str = "") -> Session:
        with self._lock:
            session = self._get_session_unlocked(session_id)
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

            self._emit(
                session_id,
                SessionEventType.DONE,
                {
                    "session_id": session_id,
                    "aborted": True,
                    "reason": session.error,
                },
            )
        self._schedule_terminal_cleanup(session_id)
        emit_session_runtime_event(
            "session.lifecycle.aborted",
            session=session,
            operation_kind="session.abort",
            status="error",
            failure_code="aborted",
            failure_domain="session_runtime",
            reason=session.error,
        )
        return session

    def complete_session(self, session_id: str, result: dict[str, Any] | None = None) -> Session:
        with self._lock:
            session = self._get_session_unlocked(session_id)
            session.status = SessionStatus.COMPLETED
            session.network_override = None
            self._touch(session)
            self._emit(
                session_id,
                SessionEventType.DONE,
                {
                    "session_id": session_id,
                    "result": result or {},
                },
            )
        self._schedule_terminal_cleanup(session_id)
        emit_session_runtime_event(
            "session.lifecycle.completed",
            session=session,
            operation_kind="session.complete",
            reason="session_completed",
            result=result or {},
        )
        return session

    def fail_session(self, session_id: str, error: str) -> Session:
        with self._lock:
            session = self._get_session_unlocked(session_id)
            session.status = SessionStatus.FAILED
            session.error = error
            session.network_override = None
            self._touch(session)
            self._emit(
                session_id,
                SessionEventType.ERROR,
                {
                    "session_id": session_id,
                    "error": error,
                },
            )
            self._emit(
                session_id,
                SessionEventType.DONE,
                {
                    "session_id": session_id,
                    "error": error,
                },
            )
        self._schedule_terminal_cleanup(session_id)
        emit_session_runtime_event(
            "session.lifecycle.failed",
            session=session,
            operation_kind="session.fail",
            status="error",
            failure_code="failed",
            failure_domain="session_runtime",
            reason=error,
        )
        return session

    def emit_progress(
        self,
        session_id: str,
        message: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            _ = self._get_session_unlocked(session_id)
            data: dict[str, Any] = {
                "session_id": session_id,
                "message": _display_text(message),
            }
            data.update(_mapping_or_empty(extra))
            self._emit(session_id, SessionEventType.PROGRESS, data)

    def raise_decision(self, session_id: str, gate: DecisionGate) -> Session:
        with self._lock:
            session = self._get_session_unlocked(session_id)
            if session.status != SessionStatus.RUNNING:
                raise ValueError(
                    f"Cannot raise decision in status {session.status.value}"
                )
            session.pending_decision = gate
            session.status = SessionStatus.AWAITING_DECISION
            self._touch(session)
            self._emit(
                session_id,
                SessionEventType.DECISION_REQUIRED,
                {
                    "session_id": session_id,
                    "decision": gate.to_dict(),
                },
            )
        return session


def create_session_orchestrator() -> BusinessSessionOrchestrator:
    """Create the current business-session orchestrator implementation."""
    return SessionOrchestrator()


__all__ = [
    "BACKEND_DECISION_KINDS",
    "BusinessSessionOrchestrator",
    "SessionEventType",
    "SessionOrchestrator",
    "create_session_orchestrator",
    "route_backend",
    "sse_event",
]
