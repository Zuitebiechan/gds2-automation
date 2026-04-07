"""Shared worker-scoped runtime container.

This module centralizes process-level state that used to live separately in
`server/api/session.py`, `server/api/diagnostics.py`, `server/api/navigate.py`,
and `diagnostic_platform/sse.py`.

Current MVP assumption:
    1 worker process = 1 active business session = 1 active backend bundle
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from diagnostic_platform.contracts import (
    ActiveBackendBundle,
    BackendActionRuntime,
    BackendDescriptor,
)
from src.gds2_orchestration.session_orchestrator import SessionOrchestrator

if TYPE_CHECKING:
    from src.diagnosis.ai_engine import AIEngine


class OperationCancelledError(RuntimeError):
    """Raised when one in-flight worker operation is cooperatively cancelled."""


class WorkerBusyError(RuntimeError):
    """Raised when one worker-exclusive operation is already in progress."""


@dataclass
class WorkerOperation:
    """One exclusive worker operation bound to one business session."""

    session_id: str
    name: str
    cancel_event: Any = field(default_factory=threading.Event, repr=False)

    def cancel(self) -> None:
        self.cancel_event.set()

    def check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise OperationCancelledError(
                f"Worker operation '{self.name}' cancelled for session {self.session_id}"
            )


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
    active_backend_bundle: ActiveBackendBundle | None = None
    action_runtime: BackendActionRuntime | None = None
    executor: Any | None = None
    adapter: Any | None = None
    data_viewer_getter: Callable[[], Any] | None = None
    data_viewer: Any | None = None
    ai_engine: AIEngine | None = None
    business_session_binding: WorkerSessionBinding = field(
        default_factory=WorkerSessionBinding,
        repr=False,
    )
    agent_clients: list[queue.Queue] = field(default_factory=list)
    agent_lock: Any = field(default_factory=threading.Lock, repr=False)
    agent_event_hub: ScopedEventHub = field(default_factory=ScopedEventHub, repr=False)
    navigation_sessions: dict[str, Any] = field(default_factory=dict)
    navigation_cleanup_timers: dict[str, Any] = field(default_factory=dict, repr=False)
    navigation_sessions_lock: Any = field(default_factory=threading.Lock, repr=False)
    active_operation: WorkerOperation | None = field(default=None, repr=False)
    state_lock: Any = field(default_factory=threading.RLock, repr=False)

    def set_orchestrator(self, orchestrator: SessionOrchestrator) -> None:
        with self.state_lock:
            self.orchestrator = orchestrator

    def ensure_backend_bundle(
        self,
        session_id: str,
        *,
        descriptor: BackendDescriptor,
        backend_factory: Callable[[], Any],
    ) -> ActiveBackendBundle:
        self.bind_business_session(session_id)
        with self.state_lock:
            bundle = self.active_backend_bundle
            if bundle is None or bundle.backend_name != descriptor.backend_name:
                bundle = ActiveBackendBundle(
                    backend_name=descriptor.backend_name,
                    descriptor=descriptor,
                )
                self.active_backend_bundle = bundle
                self.backend = None
                self.action_runtime = None
                self.executor = None
                self.adapter = None
                self.data_viewer = None
            if bundle.backend is None:
                bundle.backend = backend_factory()
            self.backend = bundle.backend
            return bundle

    def get_active_backend_bundle(
        self,
        session_id: str | None = None,
    ) -> ActiveBackendBundle | None:
        with self.state_lock:
            if session_id is not None and self.business_session_binding.session_id != session_id:
                return None
            return self.active_backend_bundle

    def clear_active_backend_bundle(self, session_id: str | None = None) -> None:
        with self.state_lock:
            if session_id is not None and self.business_session_binding.session_id != session_id:
                return
            self.active_backend_bundle = None
            self.backend = None
            self.action_runtime = None
            self.executor = None
            self.adapter = None
            self.data_viewer = None

    def get_backend(self, factory: Callable[[], Any]) -> Any:
        if self.active_backend_bundle is not None and self.active_backend_bundle.backend is not None:
            return self.active_backend_bundle.backend
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
            if self.data_viewer is not None:
                return self.data_viewer

            with self.state_lock:
                if self.data_viewer is None:
                    backend = self.get_backend(backend_factory)
                    guided_runtime_getter = getattr(backend, "get_guided_runtime", None)
                    if not callable(guided_runtime_getter):
                        raise RuntimeError(
                            f"Backend '{getattr(backend, 'name', 'unknown')}' does not expose a guided runtime"
                        )
                    self.data_viewer = guided_runtime_getter()
                    if self.data_viewer is None:
                        raise RuntimeError("Backend guided runtime getter returned None")
            return self.data_viewer

        if self.data_viewer is not None:
            return self.data_viewer

        with self.state_lock:
            if self.data_viewer is None:
                self.data_viewer = self.data_viewer_getter()
                if self.data_viewer is None:
                    raise RuntimeError("Injected data viewer getter returned None")

        return self.data_viewer

    def get_action_runtime(
        self,
        action_runtime_factory: Callable[[], BackendActionRuntime],
    ) -> BackendActionRuntime:
        if self.action_runtime is not None:
            return self.action_runtime

        with self.state_lock:
            if self.action_runtime is None:
                self.action_runtime = action_runtime_factory()
                self.executor = self.action_runtime.executor
                self.adapter = self.action_runtime.adapter
                if self.active_backend_bundle is not None:
                    self.active_backend_bundle.action_executor = self.executor
                    self.active_backend_bundle.backend_private["action_adapter"] = self.adapter

        return self.action_runtime

    def get_executor(self, action_runtime_factory: Callable[[], BackendActionRuntime]) -> Any:
        if self.executor is not None:
            return self.executor

        return self.get_action_runtime(action_runtime_factory).executor

    def get_adapter(self) -> Any | None:
        if self.action_runtime is not None:
            return self.action_runtime.adapter
        return self.adapter

    def reset_executor(self) -> None:
        with self.state_lock:
            self.action_runtime = None
            self.executor = None
            self.adapter = None
            if self.active_backend_bundle is not None:
                self.active_backend_bundle.action_executor = None
                self.active_backend_bundle.backend_private.pop("action_adapter", None)

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

    def remove_navigation_session(self, session_id: str) -> None:
        with self.navigation_sessions_lock:
            self.navigation_sessions.pop(session_id, None)
            self.navigation_cleanup_timers.pop(session_id, None)

    def schedule_navigation_session_cleanup(
        self,
        session_id: str,
        *,
        delay_sec: float = 30.0,
    ) -> None:
        with self.navigation_sessions_lock:
            existing = self.navigation_cleanup_timers.pop(session_id, None)
            if existing is not None:
                existing.cancel()
            timer = threading.Timer(delay_sec, self.remove_navigation_session, args=(session_id,))
            timer.daemon = True
            self.navigation_cleanup_timers[session_id] = timer
        timer.start()

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
                self.clear_active_backend_bundle()

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
            self.clear_active_backend_bundle()

    def start_operation(self, session_id: str, name: str) -> WorkerOperation:
        with self.state_lock:
            active = self.active_operation
            if active is not None:
                raise WorkerBusyError(
                    "Worker busy with active operation "
                    f"'{active.name}' for session {active.session_id}"
                )

            operation = WorkerOperation(session_id=session_id, name=name)
            self.active_operation = operation
            return operation

    def finish_operation(self, operation: WorkerOperation) -> None:
        with self.state_lock:
            if self.active_operation is operation:
                self.active_operation = None

    def cancel_operation(self, session_id: str | None = None) -> bool:
        with self.state_lock:
            operation = self.active_operation
            if operation is None:
                return False
            if session_id is not None and operation.session_id != session_id:
                return False
            operation.cancel()
            return True

    def current_operation_name(self) -> str | None:
        with self.state_lock:
            return self.active_operation.name if self.active_operation is not None else None

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
