"""
AI Diagnosis Engine analyzes backend-collected diagnostic payloads.

Flow:
1. Receive one standardized DiagnosticPayload
2. Convert it into the shared delta payload shape
3. Call ZhipuAI LLM (streaming)
4. Stream progress + result via SSE queue

Thread-safe. One active session at a time (enforced by caller).
"""

import json
import logging
import queue
import threading
import time
import uuid
from typing import Any, Optional

from diagnostic_platform.contracts import DiagnosticPayload, SamplingQuality
from diagnostic_platform.safe_utils import (
    display_text as _display_text,
    json_dumps_safe as _json_sse_data,
)
from .llm_client import LLMClient

logger = logging.getLogger(__name__)

# In-memory cache for retry (payload_id -> payload)
# Entries expire after 10 minutes
_payload_cache: dict[str, dict[str, Any]] = {}
_cache_lock = threading.Lock()
CACHE_TTL_SECONDS = 600  # 10 minutes
_ESSENTIAL_AI_EVENTS = {"result", "error", "done"}


def _sse_event(event_type: str, data: dict[str, Any]) -> str:
    """Format an SSE event string."""
    normalized_event_type = _display_text(event_type, default="message") or "message"
    return f"event: {normalized_event_type}\ndata: {_json_sse_data(data)}\n\n"


def _cache_payload(payload: dict[str, Any]) -> str:
    """Cache a payload and return its ID."""
    payload_id = str(uuid.uuid4())
    cached_payload = dict(payload)
    with _cache_lock:
        # Clean expired entries
        now = time.time()
        expired = [
            k for k, v in _payload_cache.items()
            if now - v.get('_cached_at', 0) > CACHE_TTL_SECONDS
        ]
        for k in expired:
            del _payload_cache[k]

        cached_payload["_cached_at"] = now
        _payload_cache[payload_id] = cached_payload

    return payload_id


def get_cached_payload(payload_id: str) -> Optional[dict[str, Any]]:
    """Retrieve a cached payload by ID."""
    with _cache_lock:
        payload = _payload_cache.get(payload_id)
        if payload is None:
            return None
        if time.time() - payload.get('_cached_at', 0) > CACHE_TTL_SECONDS:
            del _payload_cache[payload_id]
            return None
        return dict(payload)


def _sampling_quality_to_grade(sampling_quality: SamplingQuality) -> str:
    if sampling_quality == SamplingQuality.EXCELLENT:
        return "A"
    if sampling_quality == SamplingQuality.GOOD:
        return "A"
    if sampling_quality == SamplingQuality.FAIR:
        return "B"
    return "C"


def _sampling_quality_to_status(sampling_quality: SamplingQuality) -> str:
    if sampling_quality in (SamplingQuality.EXCELLENT, SamplingQuality.GOOD):
        return "healthy"
    if sampling_quality == SamplingQuality.FAIR:
        return "degraded"
    return "insufficient"


def _diagnostic_payload_to_delta_payload(payload: DiagnosticPayload) -> dict[str, Any]:
    live_data = sorted(payload.live_data, key=lambda point: point.timestamp)
    timeline: list[dict[str, Any]] = []
    initial_state: list[dict[str, Any]] = []
    last_values: dict[tuple[str, str], tuple[float, float]] = {}

    if live_data:
        start_ts = live_data[0].timestamp
        end_ts = live_data[-1].timestamp
    else:
        start_ts = 0.0
        end_ts = 0.0

    for point in live_data:
        key = (point.parameter, point.unit)
        relative_t = max(0.0, point.timestamp - start_ts)
        if key not in last_values:
            initial_state.append(
                {
                    "name": point.parameter,
                    "value": str(point.value),
                    "unit": point.unit,
                }
            )
            last_values[key] = (point.timestamp, point.value)
            continue

        previous_ts, previous_value = last_values[key]
        if previous_value != point.value:
            timeline.append(
                {
                    "t": f"{relative_t:.1f}s",
                    "param": point.parameter,
                    "from": str(previous_value),
                    "to": str(point.value),
                    "unit": point.unit,
                }
            )
            last_values[key] = (point.timestamp, point.value)

    actual_duration = max(0.0, end_ts - start_ts)
    snapshot_times = sorted({point.timestamp for point in live_data})
    snapshot_count = len(snapshot_times)
    observed_rate = round(snapshot_count / actual_duration, 2) if actual_duration > 0 else float(snapshot_count)
    grade = _sampling_quality_to_grade(payload.sampling_quality)
    sampling_quality = {
        "grade": grade,
        "status": _sampling_quality_to_status(payload.sampling_quality),
        "snapshot_count": snapshot_count,
        "expected_snapshot_count": snapshot_count,
        "completeness_ratio": 1.0 if snapshot_count else 0.0,
        "observed_rate_hz": observed_rate,
        "target_rate_hz": observed_rate,
        "gap_ms": {"avg": 0.0, "p95": 0.0, "max": 0.0},
        "lag_ms": {"avg": 0.0, "p95": 0.0, "max": 0.0},
        "degradation_reasons": [] if grade == "A" else [payload.sampling_quality.value],
    }
    quality_summary = (
        f"[AI-Sampling] grade={grade} status={sampling_quality['status']} "
        f"| samples={snapshot_count}/{snapshot_count}"
    )

    return {
        "window_seconds": int(actual_duration),
        "actual_duration": round(actual_duration, 1),
        "snapshot_count": snapshot_count,
        "initial_state": initial_state,
        "timeline": timeline,
        "dtcs": [
            {
                "code": dtc.code,
                "description": dtc.description,
                "status": dtc.status,
                "dtc_type": dtc.module,
            }
            for dtc in payload.dtcs
        ],
        "significant_changes": [],
        "sampling_quality": sampling_quality,
        "quality_summary": quality_summary,
        "gaps": [],
    }


class AIEngine:
    """
    Orchestrates the AI diagnosis workflow.

    Usage:
        engine = AIEngine(api_key="...")
        session_id = engine.start_session_from_payload(vehicle_context, diagnostic_payload)
        # Subscribe to SSE events via engine.get_event_queue(session_id)
        # Events: progress, llm_chunk, result, error
    """

    def __init__(self, api_key: str, collection_seconds: int = 30):
        self._llm_client = LLMClient(api_key=api_key)
        self._collection_seconds = collection_seconds

        # Active session state
        self._active_session: Optional[str] = None
        self._session_lock = threading.Lock()
        self._event_queues: dict[str, queue.Queue] = {}
        self._worker_thread: Optional[threading.Thread] = None
        self._cancelled_sessions: set[str] = set()

    @property
    def is_active(self) -> bool:
        """Check if a diagnosis session is currently running."""
        return self._active_session is not None

    @property
    def active_session_id(self) -> Optional[str]:
        return self._active_session

    @property
    def collection_seconds(self) -> int:
        return self._collection_seconds

    def get_event_queue(self, session_id: str) -> Optional[queue.Queue]:
        """Get the SSE event queue for a session."""
        return self._event_queues.get(session_id)

    def retry_with_cached(
        self,
        payload_id: str,
        vehicle_context: dict[str, Any],
    ) -> str:
        """
        Retry LLM analysis with a cached payload (skip collection).

        Returns new session_id.
        """
        cached = get_cached_payload(payload_id)
        if cached is None:
            raise RuntimeError(
                f"Cached payload {payload_id} not found or expired"
            )

        with self._session_lock:
            if self._active_session is not None:
                raise RuntimeError("AI diagnosis session already in progress")

            session_id = str(uuid.uuid4())
            self._active_session = session_id
            self._event_queues[session_id] = queue.Queue(maxsize=500)

        logger.info("AI-DIAG %s retry payload=%s", session_id, payload_id)

        self._worker_thread = threading.Thread(
            target=self._retry_worker,
            args=(session_id, vehicle_context, cached),
            daemon=True,
        )
        self._worker_thread.start()

        return session_id

    def start_session_from_payload(
        self,
        vehicle_context: dict[str, Any],
        diagnostic_payload: DiagnosticPayload | dict[str, Any],
    ) -> str:
        with self._session_lock:
            if self._active_session is not None:
                raise RuntimeError("AI diagnosis session already in progress")

            session_id = str(uuid.uuid4())
            self._active_session = session_id
            self._event_queues[session_id] = queue.Queue(maxsize=500)

        delta_payload = (
            _diagnostic_payload_to_delta_payload(diagnostic_payload)
            if isinstance(diagnostic_payload, DiagnosticPayload)
            else dict(diagnostic_payload)
        )

        logger.info(
            "AI-DIAG %s started from payload module=%s category=%s",
            session_id,
            vehicle_context.get('module') or '-',
            vehicle_context.get('data_category') or '-',
        )

        self._worker_thread = threading.Thread(
            target=self._payload_worker,
            args=(session_id, vehicle_context, delta_payload),
            daemon=True,
        )
        self._worker_thread.start()

        return session_id

    def abort_session(self, session_id: str) -> bool:
        """Request cooperative cancellation for an AI session."""
        with self._session_lock:
            if session_id not in self._event_queues and self._active_session != session_id:
                return False
            self._cancelled_sessions.add(session_id)

        self._emit(session_id, 'error', {
            'error': 'Aborted by user',
            'retryable': False,
        })
        logger.info("AI-DIAG %s abort requested", session_id)
        return True

    def _is_cancelled(self, session_id: str) -> bool:
        with self._session_lock:
            return session_id in self._cancelled_sessions

    def _emit(self, session_id: str, event_type: str, data: dict[str, Any]) -> None:
        """Push an SSE event to the session queue."""
        q = self._event_queues.get(session_id)
        if q is None:
            return
        message = _sse_event(event_type, data)
        try:
            q.put_nowait(message)
        except queue.Full:
            if event_type in _ESSENTIAL_AI_EVENTS:
                self._enqueue_essential_event(q, message, session_id, event_type)
                return
            logger.warning("Event queue full for session %s", session_id)

    @staticmethod
    def _enqueue_essential_event(
        event_queue: queue.Queue,
        message: str,
        session_id: str,
        event_type: str,
    ) -> None:
        while True:
            try:
                event_queue.put_nowait(message)
                return
            except queue.Full:
                try:
                    event_queue.get_nowait()
                except queue.Empty:
                    logger.warning(
                        "AI event queue cleanup raced empty for %s in session %s",
                        event_type,
                        session_id,
                    )
                    return

    def _retry_worker(
        self,
        session_id: str,
        vehicle_context: dict[str, Any],
        cached: dict[str, Any],
    ) -> None:
        """Retry worker: skip collection, go straight to LLM."""
        try:
            if self._is_cancelled(session_id):
                logger.info("AI-DIAG %s cancelled before retry start", session_id)
                return
            delta_payload = cached.get('delta_payload', {})
            # Re-cache for potential further retries
            cache_data = {
                'delta_payload': delta_payload,
                'vehicle_context': vehicle_context,
            }
            payload_id = _cache_payload(cache_data)

            self._stream_llm(session_id, vehicle_context, delta_payload, payload_id)

        except Exception as e:
            logger.exception(f"AI diagnosis retry worker failed: {e}")
            self._emit(session_id, 'error', {
                'error': str(e),
                'retryable': False,
            })

        finally:
            self._cleanup_session(session_id)

    def _payload_worker(
        self,
        session_id: str,
        vehicle_context: dict[str, Any],
        delta_payload: dict[str, Any],
    ) -> None:
        try:
            if self._is_cancelled(session_id):
                logger.info("AI-DIAG %s cancelled before payload analysis", session_id)
                return
            payload_id = _cache_payload(
                {
                    'delta_payload': delta_payload,
                    'vehicle_context': vehicle_context,
                }
            )
            self._emit(session_id, 'progress', {
                'phase': 'assembling',
                'message': 'Backend payload collected. Preparing AI analysis...',
                'sampling_quality': delta_payload.get('sampling_quality', {}),
            })
            self._stream_llm(session_id, vehicle_context, delta_payload, payload_id)
        except Exception as e:
            logger.exception(f"AI diagnosis payload worker failed: {e}")
            self._emit(session_id, 'error', {
                'error': str(e),
                'retryable': False,
            })
        finally:
            self._cleanup_session(session_id)

    def _stream_llm(
        self,
        session_id: str,
        vehicle_context: dict[str, Any],
        delta_payload: dict[str, Any],
        payload_id: str,
    ) -> None:
        """Call LLM and stream result tokens via SSE."""
        self._emit(session_id, 'progress', {
            'phase': 'analyzing',
            'message': 'Sending to AI for analysis...',
        })

        full_response = []
        chunk_count = 0
        stream_started = time.monotonic()
        brand = str(vehicle_context.get('brand') or 'GM')
        software = str(vehicle_context.get('software') or 'GDS2')
        logger.info(
            "AI-DIAG %s LLM streaming started brand=%s software=%s",
            session_id,
            brand,
            software,
        )

        try:
            for chunk in self._llm_client.diagnose_stream(
                vehicle_context,
                delta_payload,
                brand=brand,
                software=software,
            ):
                if self._is_cancelled(session_id):
                    logger.info("AI-DIAG %s cancelled during LLM streaming", session_id)
                    return
                chunk_text = chunk if isinstance(chunk, str) else str(chunk)
                full_response.append(chunk_text)
                chunk_count += 1
                self._emit(session_id, 'llm_chunk', {
                    'text': chunk_text,
                })

        except Exception as e:
            logger.exception(f"LLM call failed: {e}")
            self._emit(session_id, 'error', {
                'error': f'AI analysis failed: {e}',
                'cached_payload_id': payload_id,
                'retryable': True,
            })
            return

        # Parse the full response
        response_text = "".join(full_response)
        logger.info(
            "LLM streaming complete session=%s chunks=%s duration=%.1fs",
            session_id,
            chunk_count,
            time.monotonic() - stream_started,
        )
        verdict = LLMClient.parse_verdict(response_text)
        verdict = self._apply_sampling_quality_confidence(verdict, delta_payload)

        self._emit(session_id, 'result', {
            'raw_response': response_text,
            'verdict': verdict,
            'cached_payload_id': payload_id,
            'quality_summary': delta_payload.get('quality_summary', ''),
            'data_summary': {
                'collection_duration': delta_payload.get('actual_duration', 0),
                'snapshot_count': delta_payload.get('snapshot_count', 0),
                'dtc_count': len(delta_payload.get('dtcs', [])),
                'timeline_events': len(delta_payload.get('timeline', [])),
                'significant_changes': len(delta_payload.get('significant_changes', [])),
                'sampling_quality': delta_payload.get('sampling_quality', {}),
                'gaps': delta_payload.get('gaps', []),
            },
        })

        logger.info(
            "AI-DIAG %s verdict=%s confidence=%s",
            session_id,
            verdict.get('verdict', 'parse_failed') if verdict else 'parse_failed',
            verdict.get('confidence', '?') if verdict else '?',
        )

    def _apply_sampling_quality_confidence(
        self,
        verdict: Optional[dict[str, Any]],
        delta_payload: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        """Clamp verdict confidence when data quality is degraded."""
        if not isinstance(verdict, dict):
            return verdict

        sampling_quality = delta_payload.get('sampling_quality', {})
        if not isinstance(sampling_quality, dict):
            return verdict

        grade = str(sampling_quality.get('grade', '')).upper()
        if grade == 'B':
            max_confidence = 70
        elif grade == 'C':
            max_confidence = 40
        else:
            return verdict

        original_confidence = verdict.get('confidence')
        if not isinstance(original_confidence, (int, float)):
            return verdict

        if original_confidence <= max_confidence:
            return verdict

        updated = dict(verdict)
        updated['confidence'] = max_confidence
        updated['confidence_note'] = (
            f"Capped from {original_confidence} due to grade {grade} sampling quality."
        )
        return updated


    def _cleanup_session(self, session_id: str) -> None:
        """Mark session as complete, emit done event, and schedule queue cleanup."""
        self._emit(session_id, 'done', {'session_id': session_id})

        with self._session_lock:
            if self._active_session == session_id:
                self._active_session = None
            self._cancelled_sessions.discard(session_id)

        # Delay queue cleanup to give SSE consumer time to read remaining events.
        # The queue will be garbage-collected after the timer fires.
        def _deferred_cleanup():
            with self._session_lock:
                self._event_queues.pop(session_id, None)
            logger.debug("AI-DIAG %s queue cleaned up", session_id)

        cleanup_timer = threading.Timer(30.0, _deferred_cleanup)
        cleanup_timer.daemon = True
        cleanup_timer.start()
