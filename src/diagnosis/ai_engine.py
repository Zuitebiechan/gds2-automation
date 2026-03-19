"""
AI Diagnosis Engine — orchestrates the full diagnostic workflow.

Flow:
1. Start AgentDataCollector with DiagnosticBuffer
2. Collect 30 seconds of live sensor data
3. Extract DTCs from final snapshot
4. Assemble delta-compressed payload
5. Call ZhipuAI LLM (streaming)
6. Stream progress + result via SSE queue

Thread-safe. One active session at a time (enforced by caller).
"""

import json
import logging
import queue
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any, Optional

from ..streaming.agent_data_collector import AgentDataCollector, AgentSnapshot
from ..streaming.diagnostic_buffer import DiagnosticBuffer
from .llm_client import LLMClient

logger = logging.getLogger(__name__)

# In-memory cache for retry (payload_id -> payload)
# Entries expire after 10 minutes
_payload_cache: dict[str, dict[str, Any]] = {}
_cache_lock = threading.Lock()
CACHE_TTL_SECONDS = 600  # 10 minutes


def _sse_event(event_type: str, data: dict[str, Any]) -> str:
    """Format an SSE event string."""
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _cache_payload(payload: dict[str, Any]) -> str:
    """Cache a payload and return its ID."""
    payload_id = str(uuid.uuid4())
    with _cache_lock:
        # Clean expired entries
        now = time.time()
        expired = [
            k for k, v in _payload_cache.items()
            if now - v.get('_cached_at', 0) > CACHE_TTL_SECONDS
        ]
        for k in expired:
            del _payload_cache[k]

        payload['_cached_at'] = now
        _payload_cache[payload_id] = payload

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
        return payload


class AIEngine:
    """
    Orchestrates the AI diagnosis workflow.

    Usage:
        engine = AIEngine(api_key="...")
        session_id = engine.start_session(vehicle_context)
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

    @property
    def is_active(self) -> bool:
        """Check if a diagnosis session is currently running."""
        return self._active_session is not None

    @property
    def active_session_id(self) -> Optional[str]:
        return self._active_session

    def start_session(
        self,
        vehicle_context: dict[str, Any],
        collection_guard: Optional[Callable[[], Optional[dict[str, Any]]]] = None,
    ) -> str:
        """
        Start a new AI diagnosis session.

        Args:
            vehicle_context: {"vin": "...", "module": "...", "data_category": "..."}

        Returns:
            session_id: UUID for this session

        Raises:
            RuntimeError: If a session is already active
        """
        with self._session_lock:
            if self._active_session is not None:
                raise RuntimeError("AI diagnosis session already in progress")

            session_id = str(uuid.uuid4())
            self._active_session = session_id
            self._event_queues[session_id] = queue.Queue(maxsize=500)

        logger.info(
            "AI-DIAG %s started module=%s category=%s",
            session_id,
            vehicle_context.get('module') or '-',
            vehicle_context.get('data_category') or '-',
        )

        # Launch worker thread
        self._worker_thread = threading.Thread(
            target=self._diagnosis_worker,
            args=(session_id, vehicle_context, collection_guard),
            daemon=True,
        )
        self._worker_thread.start()

        return session_id

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

    def _emit(self, session_id: str, event_type: str, data: dict[str, Any]) -> None:
        """Push an SSE event to the session queue."""
        q = self._event_queues.get(session_id)
        if q is None:
            return
        try:
            q.put_nowait(_sse_event(event_type, data))
        except queue.Full:
            logger.warning(f"Event queue full for session {session_id}")

    def _diagnosis_worker(
        self,
        session_id: str,
        vehicle_context: dict[str, Any],
        collection_guard: Optional[Callable[[], Optional[dict[str, Any]]]] = None,
    ) -> None:
        """Main worker: collect data → assemble payload → call LLM → stream result."""
        try:
            # Phase 1: Collect 30 seconds of live data
            self._emit(session_id, 'progress', {
                'phase': 'collecting',
                'elapsed': 0,
                'total': self._collection_seconds,
                'message': 'Starting data collection...',
            })

            collector_guard_error: dict[str, Optional[str]] = {'error': None}
            collector_guard_event: dict[str, Optional[dict[str, Any]]] = {'event': None}

            def _on_collector_error(message: str) -> None:
                collector_guard_error['error'] = message

            def _on_guard_event(event: dict[str, Any]) -> None:
                collector_guard_event['event'] = event

            buffer = DiagnosticBuffer(window_seconds=self._collection_seconds)
            collector = AgentDataCollector(
                on_snapshot=lambda snap, _changes=None: buffer.append_snapshot(snap),
                on_error=_on_collector_error,
                page_guard=collection_guard,
                on_guard_event=_on_guard_event,
                interval_ms=100,
            )

            # Check agent availability
            avail = collector.check_agent_available()
            if not avail.get('available'):
                logger.warning("AI-DIAG %s aborted: Java Agent unavailable", session_id)
                self._emit(session_id, 'error', {
                    'error': 'Java Agent not available. Start GDS2 with the agent.',
                    'retryable': False,
                })
                return

            collector.start()
            logger.info("AI-DIAG %s collecting 0/%ss", session_id, self._collection_seconds)

            start_time = time.time()
            last_collection_log = -1
            try:
                while time.time() - start_time < self._collection_seconds:
                    elapsed = int(time.time() - start_time)
                    if collector_guard_error['error']:
                        self._emit(session_id, 'error', {
                            'error': collector_guard_error['error'],
                            'retryable': False,
                        })
                        return

                    guard_event = collector_guard_event.get('event')
                    if guard_event:
                        new_start_time = self._handle_collection_guard_event(
                            session_id,
                            guard_event,
                            buffer,
                            start_time,
                            elapsed,
                        )
                        if new_start_time != start_time:
                            last_collection_log = -1
                        start_time = new_start_time
                        collector_guard_event['event'] = None

                    if elapsed > 0 and elapsed % 5 == 0 and elapsed != last_collection_log:
                        last_collection_log = elapsed
                        logger.info(
                            "AI-DIAG %s collecting %s/%ss (%s snapshots)",
                            session_id,
                            elapsed,
                            self._collection_seconds,
                            buffer.snapshot_count,
                        )

                    self._emit(session_id, 'progress', {
                        'phase': 'collecting',
                        'elapsed': elapsed,
                        'total': self._collection_seconds,
                        'message': f'Collecting data... {elapsed}/{self._collection_seconds}s',
                    })
                    time.sleep(1)
            finally:
                collector.stop()

            logger.info(
                "AI-DIAG %s collection complete snapshots=%s duration=%.1fs",
                session_id,
                buffer.snapshot_count,
                buffer.duration_seconds,
            )

            # Phase 2: Assemble payload
            self._emit(session_id, 'progress', {
                'phase': 'assembling',
                'message': 'Processing collected data...',
            })

            delta_payload = buffer.get_delta_payload()
            sampling_quality = delta_payload.get('sampling_quality', {})
            quality_summary = delta_payload.get('quality_summary', '')

            self._emit(session_id, 'progress', {
                'phase': 'assembling',
                'message': quality_summary or 'Sampling quality calculated.',
                'sampling_quality': sampling_quality,
            })

            # Cache payload for retry
            cache_data = {
                'delta_payload': delta_payload,
                'vehicle_context': vehicle_context,
            }
            payload_id = _cache_payload(cache_data)

            logger.info(
                "AI-DIAG %s payload ready snapshots=%s dtcs=%s timeline=%s changes=%s quality=%s",
                session_id,
                delta_payload.get('snapshot_count', 0),
                len(delta_payload.get('dtcs', [])),
                len(delta_payload.get('timeline', [])),
                len(delta_payload.get('significant_changes', [])),
                sampling_quality.get('grade', '?'),
            )

            # Phase 3: Call LLM (streaming)
            self._stream_llm(session_id, vehicle_context, delta_payload, payload_id)

        except Exception as e:
            logger.exception(f"AI diagnosis worker failed: {e}")
            self._emit(session_id, 'error', {
                'error': str(e),
                'retryable': False,
            })

        finally:
            self._cleanup_session(session_id)

    def _retry_worker(
        self,
        session_id: str,
        vehicle_context: dict[str, Any],
        cached: dict[str, Any],
    ) -> None:
        """Retry worker: skip collection, go straight to LLM."""
        try:
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
                full_response.append(chunk)
                chunk_count += 1
                self._emit(session_id, 'llm_chunk', {
                    'text': chunk,
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

    def _handle_collection_guard_event(
        self,
        session_id: str,
        guard_event: dict[str, Any],
        buffer: DiagnosticBuffer,
        start_time: float,
        elapsed: int,
    ) -> float:
        """Emit guard status and restart the AI collection window if requested."""
        guard_message = str(guard_event.get('message') or '')
        if guard_message:
            self._emit(session_id, 'progress', {
                'phase': 'collecting',
                'elapsed': elapsed,
                'total': self._collection_seconds,
                'message': guard_message,
            })

        if guard_event.get('restart_collection'):
            logger.info("AI-DIAG %s collection restarted after reconnect recovery", session_id)
            buffer.clear()
            restart_time = time.time()
            self._emit(session_id, 'progress', {
                'phase': 'collecting',
                'elapsed': 0,
                'total': self._collection_seconds,
                'message': 'Recovered connection. Restarting a fresh 30s AI collection window...',
            })
            return restart_time

        return start_time


    def _cleanup_session(self, session_id: str) -> None:
        """Mark session as complete, emit done event, and schedule queue cleanup."""
        self._emit(session_id, 'done', {'session_id': session_id})

        with self._session_lock:
            if self._active_session == session_id:
                self._active_session = None

        # Delay queue cleanup to give SSE consumer time to read remaining events.
        # The queue will be garbage-collected after the timer fires.
        def _deferred_cleanup():
            with self._session_lock:
                self._event_queues.pop(session_id, None)
            logger.debug("AI-DIAG %s queue cleaned up", session_id)

        cleanup_timer = threading.Timer(30.0, _deferred_cleanup)
        cleanup_timer.daemon = True
        cleanup_timer.start()
