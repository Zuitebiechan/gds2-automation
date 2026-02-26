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

        logger.info(f"Starting AI diagnosis session {session_id}")

        # Launch worker thread
        self._worker_thread = threading.Thread(
            target=self._diagnosis_worker,
            args=(session_id, vehicle_context),
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

        logger.info(f"Retrying AI diagnosis session {session_id} with cached payload")

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

            buffer = DiagnosticBuffer(window_seconds=self._collection_seconds)
            collector = AgentDataCollector(
                on_snapshot=lambda snap, _changes=None: buffer.append_snapshot(snap),
                interval_ms=100,
            )

            # Check agent availability
            avail = collector.check_agent_available()
            if not avail.get('available'):
                self._emit(session_id, 'error', {
                    'error': 'Java Agent not available. Start GDS2 with the agent.',
                    'retryable': False,
                })
                return

            collector.start()

            start_time = time.time()
            try:
                while time.time() - start_time < self._collection_seconds:
                    elapsed = int(time.time() - start_time)
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
                f"Collection complete: {buffer.snapshot_count} snapshots, "
                f"{buffer.duration_seconds:.1f}s duration"
            )

            # Phase 2: Assemble payload
            self._emit(session_id, 'progress', {
                'phase': 'assembling',
                'message': 'Processing collected data...',
            })

            delta_payload = buffer.get_delta_payload()

            # Cache payload for retry
            cache_data = {
                'delta_payload': delta_payload,
                'vehicle_context': vehicle_context,
            }
            payload_id = _cache_payload(cache_data)

            logger.info(
                f"Payload assembled: {len(delta_payload.get('timeline', []))} timeline events, "
                f"{len(delta_payload.get('dtcs', []))} DTCs, "
                f"{len(delta_payload.get('significant_changes', []))} significant changes"
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

        try:
            for chunk in self._llm_client.diagnose_stream(
                vehicle_context, delta_payload
            ):
                full_response.append(chunk)
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
        verdict = LLMClient.parse_verdict(response_text)

        self._emit(session_id, 'result', {
            'raw_response': response_text,
            'verdict': verdict,
            'cached_payload_id': payload_id,
            'data_summary': {
                'collection_duration': delta_payload.get('actual_duration', 0),
                'snapshot_count': delta_payload.get('snapshot_count', 0),
                'dtc_count': len(delta_payload.get('dtcs', [])),
                'timeline_events': len(delta_payload.get('timeline', [])),
                'significant_changes': len(delta_payload.get('significant_changes', [])),
            },
        })

        logger.info(
            f"AI diagnosis complete. Verdict: "
            f"{verdict.get('verdict', 'unknown') if verdict else 'parse_failed'}"
        )

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
            logger.debug(f"Cleaned up event queue for session {session_id}")

        cleanup_timer = threading.Timer(30.0, _deferred_cleanup)
        cleanup_timer.daemon = True
        cleanup_timer.start()
