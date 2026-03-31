"""Business-session execution state helpers.

This keeps worker-local execution bindings out of the Flask route layer while
preserving the current single-worker MVP behavior.
"""

from __future__ import annotations

import time
from typing import Any

from .worker_runtime import WorkerRuntime


def session_binding_payload(runtime: WorkerRuntime, session_id: str) -> dict[str, Any]:
    """Build the execution-state overlay for one business session."""
    binding = runtime.get_business_session_binding(session_id)
    return {
        "active_navigation_session_id": binding.navigation_session_id,
        "active_ai_session_id": binding.ai_session_id,
        "live_data_active": binding.live_data_active,
    }


def bind_business_session(runtime: WorkerRuntime, session: Any) -> None:
    runtime.bind_business_session(session.session_id)
    session.updated_at = time.time()


def clear_business_session(runtime: WorkerRuntime, session_id: str) -> None:
    runtime.clear_business_session(session_id)


def set_session_selection(
    session: Any,
    *,
    module: str | None = None,
    data_category: str | None = None,
) -> None:
    if module is not None:
        session.selected_module = module
    if data_category is not None:
        session.selected_data_category = data_category
    session.updated_at = time.time()


def bind_navigation_session(runtime: WorkerRuntime, session: Any, navigation_session_id: str) -> None:
    runtime.bind_navigation_session(session.session_id, navigation_session_id)
    session.updated_at = time.time()


def navigation_session_id(runtime: WorkerRuntime, session_id: str) -> str | None:
    return runtime.get_navigation_session_id(session_id)


def clear_navigation_binding(runtime: WorkerRuntime, session: Any) -> None:
    runtime.clear_navigation_session(session.session_id)
    session.updated_at = time.time()


def bind_ai_session(runtime: WorkerRuntime, session: Any, ai_session_id: str) -> None:
    runtime.bind_ai_session(session.session_id, ai_session_id)
    session.updated_at = time.time()


def ai_session_id(runtime: WorkerRuntime, session_id: str) -> str | None:
    return runtime.get_ai_session_id(session_id)


def clear_ai_binding(runtime: WorkerRuntime, session: Any) -> None:
    runtime.clear_ai_session(session.session_id)
    session.updated_at = time.time()


def set_live_data_active(runtime: WorkerRuntime, session: Any, active: bool) -> None:
    runtime.set_live_data_active(session.session_id, active)
    session.updated_at = time.time()


def live_data_active(runtime: WorkerRuntime, session_id: str) -> bool:
    return runtime.is_live_data_active(session_id)
