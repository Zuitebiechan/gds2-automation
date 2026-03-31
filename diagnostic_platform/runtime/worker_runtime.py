"""Shared worker-scoped runtime container.

This module centralizes process-level state that used to live separately in
`session_api.py`, `diagnostics_api.py`, `navigate_api.py`, and
`diagnostic_platform/sse.py`.

Current MVP assumption:
    1 worker process = 1 GDS2 runtime = 1 active customer session
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from src.agentic.adapters.gds2_adapter import GDS2ActionAdapter
from src.agentic.executor import DeterministicExecutor
from src.agentic.session_orchestrator import SessionOrchestrator

if TYPE_CHECKING:
    from src.diagnosis.ai_engine import AIEngine


@dataclass
class ScopedEventHub:
    """Simple in-memory pub/sub hub keyed by stream scope."""

    subscribers: dict[str, list[queue.Queue]] = field(default_factory=dict)
    lock: Any = field(default_factory=threading.Lock, repr=False)

    def subscribe(self, scope: str, *, maxsize: int = 200) -> queue.Queue:
        client_queue: queue.Queue = queue.Queue(maxsize=maxsize)
        with self.lock:
            self.subscribers.setdefault(scope, []).append(client_queue)
        return client_queue

    def unsubscribe(self, scope: str, client_queue: queue.Queue) -> None:
        with self.lock:
            queues = self.subscribers.get(scope)
            if not queues:
                return
            if client_queue in queues:
                queues.remove(client_queue)
            if not queues:
                self.subscribers.pop(scope, None)

    def broadcast(self, scope: str, message: str) -> int:
        with self.lock:
            queues = list(self.subscribers.get(scope, []))

        dead_clients: list[queue.Queue] = []
        delivered = 0
        for client_queue in queues:
            try:
                client_queue.put_nowait(message)
                delivered += 1
            except queue.Full:
                dead_clients.append(client_queue)

        for client_queue in dead_clients:
            self.unsubscribe(scope, client_queue)

        return delivered

    def client_count(self, scope: str) -> int:
        with self.lock:
            return len(self.subscribers.get(scope, []))


@dataclass
class WorkerSessionBinding:
    """Worker-local execution bindings for the active business session."""

    session_id: str | None = None
    navigation_session_id: str | None = None
    ai_session_id: str | None = None
    live_data_active: bool = False


@dataclass
class WorkerRuntime:
    """Single-process runtime state for one cloud worker."""

    orchestrator: SessionOrchestrator = field(default_factory=SessionOrchestrator)
    backend: Any | None = None
    executor: DeterministicExecutor | None = None
    adapter: GDS2ActionAdapter | None = None
    data_viewer_getter: Callable[[], Any] | None = None
    data_viewer: Any | None = None
    ai_engine: AIEngine | None = None
    diag_collector: Any | None = None
    active_live_data_scope: str | None = None
    business_session_binding: WorkerSessionBinding = field(
        default_factory=WorkerSessionBinding,
        repr=False,
    )
    agent_clients: list[queue.Queue] = field(default_factory=list)
    agent_lock: Any = field(default_factory=threading.Lock, repr=False)
    agent_event_hub: ScopedEventHub = field(default_factory=ScopedEventHub, repr=False)
    navigation_sessions: dict[str, Any] = field(default_factory=dict)
    navigation_sessions_lock: Any = field(default_factory=threading.Lock, repr=False)
    state_lock: Any = field(default_factory=threading.RLock, repr=False)

    def set_orchestrator(self, orchestrator: SessionOrchestrator) -> None:
        with self.state_lock:
            self.orchestrator = orchestrator

    def get_backend(self, factory: Callable[[], Any]) -> Any:
        if self.backend is not None:
            return self.backend

        with self.state_lock:
            if self.backend is None:
                self.backend = factory()

        return self.backend

    def set_data_viewer_getter(self, getter: Callable[[], Any] | None) -> None:
        with self.state_lock:
            self.data_viewer_getter = getter
            self.data_viewer = None
            self.backend = None
            self.executor = None
            self.adapter = None

    def has_data_viewer_getter(self) -> bool:
        return self.data_viewer_getter is not None

    def get_data_viewer(self, backend_factory: Callable[[], Any]) -> Any:
        if self.data_viewer_getter is None:
            return self.get_backend(backend_factory)._get_workflow()

        if self.data_viewer is not None:
            return self.data_viewer

        with self.state_lock:
            if self.data_viewer is None:
                self.data_viewer = self.data_viewer_getter()
                if self.data_viewer is None:
                    raise RuntimeError("Injected data viewer getter returned None")

        return self.data_viewer

    def get_executor(self, workflow_factory: Callable[[], Any]) -> DeterministicExecutor:
        if self.executor is not None:
            return self.executor

        with self.state_lock:
            if self.executor is None:
                workflow = workflow_factory()
                self.adapter = GDS2ActionAdapter(workflow)
                self.executor = DeterministicExecutor()
                self.adapter.register_all(self.executor)

        return self.executor

    def get_adapter(self) -> GDS2ActionAdapter | None:
        return self.adapter

    def reset_executor(self) -> None:
        with self.state_lock:
            self.executor = None
            self.adapter = None

    def get_ai_engine(self, factory: Callable[[], AIEngine]) -> AIEngine:
        if self.ai_engine is not None:
            return self.ai_engine

        with self.state_lock:
            if self.ai_engine is None:
                self.ai_engine = factory()

        return self.ai_engine

    def get_navigation_session(self, session_id: str) -> Any:
        with self.navigation_sessions_lock:
            session = self.navigation_sessions.get(session_id)

        if session is None:
            raise KeyError(f"Navigation session '{session_id}' not found")

        return session

    def set_navigation_session(self, session_id: str, session: Any) -> None:
        with self.navigation_sessions_lock:
            self.navigation_sessions[session_id] = session

    def bind_business_session(self, session_id: str) -> None:
        with self.state_lock:
            binding = self.business_session_binding
            if binding.session_id and binding.session_id != session_id:
                raise RuntimeError(
                    "Worker already bound to another business session "
                    f"(session_id={binding.session_id})"
                )
            if binding.session_id != session_id:
                self.business_session_binding = WorkerSessionBinding(session_id=session_id)

    def get_business_session_binding(self, session_id: str | None = None) -> WorkerSessionBinding:
        with self.state_lock:
            binding = self.business_session_binding
            if session_id is not None and binding.session_id != session_id:
                return WorkerSessionBinding()
            return WorkerSessionBinding(
                session_id=binding.session_id,
                navigation_session_id=binding.navigation_session_id,
                ai_session_id=binding.ai_session_id,
                live_data_active=binding.live_data_active,
            )

    def clear_business_session(self, session_id: str | None = None) -> None:
        with self.state_lock:
            binding = self.business_session_binding
            if session_id is not None and binding.session_id != session_id:
                return
            self.business_session_binding = WorkerSessionBinding()

    def bind_navigation_session(self, session_id: str, navigation_session_id: str) -> None:
        with self.state_lock:
            self.bind_business_session(session_id)
            self.business_session_binding.navigation_session_id = navigation_session_id

    def get_navigation_session_id(self, session_id: str) -> str | None:
        with self.state_lock:
            binding = self.business_session_binding
            if binding.session_id != session_id:
                return None
            return binding.navigation_session_id

    def clear_navigation_session(self, session_id: str) -> None:
        with self.state_lock:
            if self.business_session_binding.session_id == session_id:
                self.business_session_binding.navigation_session_id = None

    def bind_ai_session(self, session_id: str, ai_session_id: str) -> None:
        with self.state_lock:
            self.bind_business_session(session_id)
            self.business_session_binding.ai_session_id = ai_session_id

    def get_ai_session_id(self, session_id: str) -> str | None:
        with self.state_lock:
            binding = self.business_session_binding
            if binding.session_id != session_id:
                return None
            return binding.ai_session_id

    def clear_ai_session(self, session_id: str) -> None:
        with self.state_lock:
            if self.business_session_binding.session_id == session_id:
                self.business_session_binding.ai_session_id = None

    def set_live_data_active(self, session_id: str, active: bool) -> None:
        with self.state_lock:
            self.bind_business_session(session_id)
            self.business_session_binding.live_data_active = active

    def is_live_data_active(self, session_id: str) -> bool:
        with self.state_lock:
            binding = self.business_session_binding
            return binding.session_id == session_id and binding.live_data_active


_WORKER_RUNTIME = WorkerRuntime()


def get_worker_runtime() -> WorkerRuntime:
    """Return the process-level worker runtime singleton."""
    return _WORKER_RUNTIME
