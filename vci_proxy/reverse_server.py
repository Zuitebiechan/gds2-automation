"""
VCI Proxy 反向连接服务器（运行在阿里云）

接受本地 VCI Proxy 的反向连接，并提供本地代理服务。

架构:
  本地 VCI Proxy ──连接──▶ 此服务器 (端口 9000)
                              ↑
                         本地测试程序连接 localhost:9001
"""

import asyncio
import socket
import struct
import time
import logging
import argparse
import ssl
import signal
import os
import hashlib
from dataclasses import dataclass
from typing import Optional
from pathlib import Path

from diagnostic_platform.observability import (
    LogContext,
    emit_event,
    get_product_log_writer,
    install_observability_log_handler,
    read_active_session_snapshot,
)

from .config import ProxyConfig
from .cache_read_msgs import BUFFER_EMPTY, ReadMsgsCache
from .cache_filter_dedup import FilterDeduplicationCache
from .cache_ioctl import IoctlCache
from .auth import MAX_DRIFT_S, verify_signature
from .benchmark import (
    JsonlBenchmarkWriter,
    decode_benchmark_response,
    make_proxy_benchmark_event,
    strip_timing_trailer,
)
from .prefetch_read_msgs import PrefetchReadMsgsBuffer, PrefetchReadMsgsDrain
from .protocol import (
    MAGIC,
    HEADER_SIZE,
    Message,
    MSG_NAMES,
    MsgType,
    ProtocolDecoder,
    ProtocolEncoder,
    strip_read_msgs_prefetch_bundle,
)
from .sweep_compare import compare_shadow_to_real
from .sweep_learner import SweepLearnerEvent, SweepPatternLearner
from .sweep_protocol import (
    SweepPlanStartRequest,
    SweepPlanStopRequest,
    SweepRequestSpec,
    SweepResultRecord,
    decode_sweep_drain_results_rsp,
    decode_sweep_plan_start_rsp,
    encode_sweep_drain_results_req,
    encode_sweep_plan_start_req,
    encode_sweep_plan_stop_req,
    encode_sweep_status_req,
)
from .sweep_shadow_store import SweepShadowStore
from .sweep_signatures import SweepObservedRequest
from .tls_utils import harden_tls_context
from .tunnel_quality import (
    TunnelQualityTracker,
    write_tunnel_quality_snapshot,
)

_bootstrap_logs: list[tuple[str, str]] = []

try:
    from dotenv import load_dotenv

    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
        _bootstrap_logs.append(("debug", f"Loaded environment variables from {env_path}"))
    else:
        _bootstrap_logs.append(("debug", f".env file not found at {env_path}"))
except ImportError:
    _bootstrap_logs.append(("debug", "python-dotenv not installed"))


def _resolve_reverse_server_log_path(
    file_name: str = "vci_proxy.log",
    *,
    environ: dict[str, str] | None = None,
) -> Path | None:
    env = os.environ if environ is None else environ
    configured_dir = str(env.get("LOG_DIR", "") or "").strip()
    if not configured_dir:
        return None
    log_dir = Path(configured_dir)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _bootstrap_logs.append(
            ("warning", f"Failed to create LOG_DIR {log_dir}: {exc}; console logging only")
        )
        return None
    return log_dir / file_name


_logging_handlers: list[logging.Handler] = [logging.StreamHandler()]
_reverse_server_log_path = _resolve_reverse_server_log_path()
if _reverse_server_log_path is not None:
    _logging_handlers.insert(
        0,
        logging.FileHandler(_reverse_server_log_path, encoding="utf-8"),
    )

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
    handlers=_logging_handlers,
)
logger = logging.getLogger(__name__)

for level, message in _bootstrap_logs:
    getattr(logger, level)(message)

MAX_FRAME_BODY_BYTES = 1_000_000
DEFAULT_FRAME_BODY_READ_TIMEOUT_S = 10.0
LIVE_DATA_GAP_WARN_MS = 1000.0
LIVE_DATA_GAP_STALL_MS = 3000.0
PAYLOAD_SAMPLE_LIMIT = 3
PAYLOAD_PREFIX_BYTES = 16
READ_COLLECT_BASE_WINDOW_CAP_MS = 40
READ_COLLECT_BASE_MAX_READS_CAP = 3
READ_COLLECT_DEEP_MAX_READS_CAP = 6
READ_COLLECT_DEEP_RECENT_PREFETCH_MS = 120.0
READ_COLLECT_DEEP_CONFIRMED_EMPTY_AFTER_PREFETCH_MS = 80.0
OVERSIZED_PARTIAL_MERGE_COUNT_CAP = 300


def _disable_windows_quick_edit() -> None:
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-10)
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)) == 0:
            return
        new_mode = (mode.value | 0x0080) & ~0x0040
        kernel32.SetConsoleMode(handle, new_mode)
    except Exception:
        pass


def _validated_body_length(length: int) -> int:
    if length < HEADER_SIZE:
        raise ValueError(f"Invalid frame length: {length} < {HEADER_SIZE}")

    body_len = length - HEADER_SIZE
    if body_len > MAX_FRAME_BODY_BYTES:
        raise ValueError(
            f"Frame body too large: {body_len} > {MAX_FRAME_BODY_BYTES}"
        )
    return body_len


async def _read_frame_body(
    reader: asyncio.StreamReader,
    length: int,
    *,
    timeout: float = DEFAULT_FRAME_BODY_READ_TIMEOUT_S,
) -> bytes:
    body_len = _validated_body_length(length)
    if body_len <= 0:
        return b""

    try:
        return await asyncio.wait_for(reader.readexactly(body_len), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise TimeoutError(f"Timed out reading frame body ({body_len} bytes)") from exc
    except asyncio.IncompleteReadError as exc:
        raise ConnectionError(
            f"Incomplete frame body ({len(exc.partial)} of {body_len} bytes)"
        ) from exc


@dataclass(frozen=True)
class _ActiveReplayPending:
    observed: SweepObservedRequest
    shadow_result: SweepResultRecord


@dataclass(frozen=True)
class _PrefetchRecordObservation:
    message_count: int
    source: str | None
    proxy_seq: int | None
    observed_mono: float


@dataclass(frozen=True)
class _PrefetchDrainObservation:
    reason: str
    requested_count: int
    served_count: int
    pending_before: int
    pending_after: int
    observed_mono: float


@dataclass(frozen=True)
class _ReadCollectTransactionBudget:
    collect_window_ms: int
    max_reads: int
    read_timeout_ms: int
    max_messages: int
    reason: str

    @property
    def deepened(self) -> bool:
        return self.reason != "standard_read_tail"


@dataclass(frozen=True)
class _ReadResultObservation:
    result: str
    return_code: int
    message_count: int
    dll_seq: int | None
    proxy_seq: int | None
    observed_mono: float


class ReverseProxyServer:
    """反向代理服务器 - 接受 VCI Proxy 的连接"""

    def __init__(self, listen_port: int = 9000, proxy_port: int = 9001,
                 config: Optional[ProxyConfig] = None,
                 benchmark_writer: Optional[JsonlBenchmarkWriter] = None,
                 benchmark_label: str = "proxy_run"):
        self.listen_port = listen_port  # VCI Proxy 连接的端口
        self.proxy_port = proxy_port    # 本地程序连接的端口
        self.config = config or ProxyConfig()
        self.benchmark_writer = benchmark_writer
        self.benchmark_label = benchmark_label
        self.vci_reader: Optional[asyncio.StreamReader] = None
        self.vci_writer: Optional[asyncio.StreamWriter] = None
        self.vci_connected = asyncio.Event()
        self.vci_lock = asyncio.Lock()  # 保护 VCI 写操作
        self.request_queue = asyncio.Queue()
        self.response_futures: dict[int, asyncio.Future] = {}
        self.sequence = 0
        self._shutting_down = False
        self._vci_server: asyncio.AbstractServer | None = None
        self._proxy_server: asyncio.AbstractServer | None = None
        self._probe_task: asyncio.Task | None = None
        self._connection_counter = 0
        self._connection_epoch: str | None = None
        self._tunnel_quality = TunnelQualityTracker()
        self._last_quality_signature: tuple | None = None
        self._last_snapshot_write_error: str | None = None
        self._seen_auth_signatures: dict[tuple[int, bytes], int] = {}
        self._observability_writer = get_product_log_writer("reverse_server")
        self._process_started_emitted = False
        self._process_shutdown_started_emitted = False
        self._process_shutdown_finished_emitted = False
        self._vci_read_collect_supported = False
        self._vci_write_collect_supported = False
        self._vci_sweep_shadow_supported = False
        self._read_ahead_transaction_guard_until_mono = 0.0
        self._read_ahead_transaction_guard_reason: str | None = None
        self._read_ahead_transaction_guard_network_ms: float | None = None
        self._prefetch_bundle_source_by_proxy_seq: dict[int, str] = {}
        self._prefetch_empty_confirmations_by_proxy_seq: dict[int, tuple[int, int]] = {}

        # P1-1: ReadMsgs BUFFER_EMPTY cache
        self._read_cache = ReadMsgsCache(self.config.read_msgs_cache)
        # P2-1: Filter deduplication cache
        self._filter_cache = FilterDeduplicationCache(self.config.filter_dedup)
        # Generalized read-only IOCTL cache (replaces VBATT-only cache)
        self._ioctl_cache = IoctlCache(self.config.ioctl_cache)
        # Consume-once store for local-side ReadMsgs read-ahead data.
        self._prefetch_read_msgs = PrefetchReadMsgsBuffer(
            enabled=self.config.read_ahead.enabled,
            max_messages=self.config.read_ahead.max_messages,
        )
        self._prefetch_read_locks: dict[int, asyncio.Lock] = {}
        # channel_id -> (dll sequence, monotonic timestamp)
        self._last_write_by_channel: dict[int, tuple[int | None, float]] = {}
        # channel_id -> (message name, dll sequence, monotonic timestamp)
        self._last_live_request_by_channel: dict[int, tuple[str, int | None, float]] = {}
        # channel_id -> (aggregate payload digest, monotonic timestamp)
        self._last_read_payload_by_channel: dict[int, tuple[str, float]] = {}
        self._last_prefetch_record_by_channel: dict[int, _PrefetchRecordObservation] = {}
        self._last_prefetch_drain_by_channel: dict[int, _PrefetchDrainObservation] = {}
        self._confirmed_empty_deepened_drain_mono_by_channel: dict[int, float] = {}
        self._last_read_result_by_channel: dict[int, _ReadResultObservation] = {}
        self._sweep_learner = SweepPatternLearner(self.config.local_sweep)
        self._sweep_shadow_store = SweepShadowStore()
        self._sweep_active_plan: SweepPlanStartRequest | None = None
        self._sweep_active_plan_started_mono: float | None = None
        self._sweep_plan_start_task: asyncio.Task | None = None
        self._sweep_pending_plan_channel: int | None = None
        self._sweep_poll_task: asyncio.Task | None = None
        self._sweep_mismatch_count = 0
        self._sweep_error_count = 0
        self._sweep_plan_skip_logged: set[tuple[str | None, int, str]] = set()
        self._active_replay_pending_by_channel: dict[int, _ActiveReplayPending] = {}

    def _current_observability_context(
        self,
        *,
        connection_epoch: str | None = None,
        operation_kind: str = "reverse_tunnel",
    ) -> LogContext:
        if self._shutting_down:
            snapshot: dict[str, object] = {}
        else:
            snapshot = read_active_session_snapshot() or {}
        return LogContext(
            session_id=str(snapshot.get("session_id") or "") or None,
            connection_epoch=connection_epoch or self._connection_epoch,
            operation_kind=str(snapshot.get("operation_kind") or operation_kind),
            page=str(snapshot.get("current_page") or "") or None,
            module=str(snapshot.get("selected_module") or "") or None,
            data_category=str(snapshot.get("selected_data_category") or "") or None,
        )

    def _emit_tunnel_event(
        self,
        event_type: str,
        *,
        connection_epoch: str | None = None,
        operation_kind: str = "reverse_tunnel",
        status: str = "ok",
        failure_code: str | None = None,
        failure_domain: str = "unknown",
        reason: str | None = None,
        impact_scope: str = "reverse_tunnel",
        **extra: object,
    ) -> None:
        try:
            emit_event(
                self._observability_writer,
                component="reverse_server",
                event_type=event_type,
                context=self._current_observability_context(
                    connection_epoch=connection_epoch,
                    operation_kind=operation_kind,
                ),
                status=status,
                failure_code=failure_code,
                failure_domain=failure_domain,
                reason=reason,
                impact_scope=impact_scope,
                **extra,
            )
        except Exception:
            if self._shutting_down:
                logger.debug("Skipping tunnel observability during shutdown", exc_info=True)
                return
            logger.warning("Failed to emit tunnel observability event: %s", event_type, exc_info=True)

    def _emit_proxy_request_event(
        self,
        event_type: str,
        *,
        dll_seq: int,
        msg_name: str,
        proxy_seq: int | None = None,
        status: str = "ok",
        failure_code: str | None = None,
        failure_domain: str = "unknown",
        reason: str | None = None,
        duration_ms: float | None = None,
        hw_ms: float | None = None,
        network_ms: float | None = None,
        cache_hit: bool | None = None,
        **extra: object,
    ) -> None:
        try:
            base_context = self._current_observability_context(
                connection_epoch=self._connection_epoch,
                operation_kind=f"j2534:{msg_name}",
            )
            emit_event(
                self._observability_writer,
                component="reverse_server",
                event_type=event_type,
                context=LogContext(
                    session_id=base_context.session_id,
                    connection_epoch=base_context.connection_epoch,
                    dll_seq=dll_seq,
                    proxy_seq=proxy_seq,
                    worker_request_id=base_context.worker_request_id,
                    operation_kind=base_context.operation_kind,
                    page=base_context.page,
                    module=base_context.module,
                    data_category=base_context.data_category,
                    request_id=base_context.request_id,
                ),
                status=status,
                failure_code=failure_code,
                failure_domain=failure_domain,
                reason=reason,
                duration_ms=duration_ms,
                hw_ms=hw_ms,
                network_ms=network_ms,
                impact_scope="proxy_request",
                msg_name=msg_name,
                cache_hit=cache_hit,
                **extra,
            )
        except Exception:
            if self._shutting_down:
                logger.debug("Skipping proxy observability during shutdown", exc_info=True)
                return
            logger.warning("Failed to emit proxy observability event: %s", event_type, exc_info=True)

    def _emit_sweep_learner_events(
        self,
        events: list[SweepLearnerEvent],
        *,
        dll_seq: int,
        msg_name: str,
    ) -> None:
        for event in events:
            self._emit_proxy_request_event(
                event.event_type,
                dll_seq=dll_seq,
                msg_name=msg_name,
                reason=event.event_type,
                **event.fields,
            )

    def _observe_sweep_write(
        self,
        msg_type: int,
        body: bytes,
        *,
        dll_seq: int,
        msg_name: str,
    ) -> None:
        if msg_type != MsgType.WRITE_MSGS_REQ or not self.config.local_sweep.enabled:
            return
        events = self._sweep_learner.observe_write(
            body,
            connection_epoch=self._connection_epoch,
            read_num_msgs=1,
            read_timeout_ms=self.config.local_sweep.read_timeout_ms,
        )
        self._emit_sweep_learner_events(events, dll_seq=dll_seq, msg_name=msg_name)

    def _observe_sweep_read_response(
        self,
        msg_type: int,
        body: bytes,
        resp_type: int,
        resp_body: bytes,
        *,
        dll_seq: int,
        msg_name: str,
        duration_ms: float | None = None,
        network_ms: float | None = None,
        cache_hit: bool = False,
    ) -> None:
        if msg_type != MsgType.READ_MSGS_REQ or resp_type != MsgType.READ_MSGS_RSP:
            return
        observed, events = self._sweep_learner.observe_read_response(
            body,
            resp_body,
            connection_epoch=self._connection_epoch,
            duration_ms=duration_ms,
            network_ms=network_ms,
            cache_hit=cache_hit,
        )
        self._emit_sweep_learner_events(events, dll_seq=dll_seq, msg_name=msg_name)
        if observed is not None:
            self._compare_shadow_result(observed, resp_body, dll_seq=dll_seq, msg_name=msg_name)
            self._maybe_start_shadow_plan(observed)

    def _observe_sweep_write_response(
        self,
        msg_type: int,
        body: bytes,
        resp_type: int,
        resp_body: bytes,
        *,
        dll_seq: int,
        msg_name: str,
        duration_ms: float | None = None,
        network_ms: float | None = None,
    ) -> None:
        if msg_type != MsgType.WRITE_MSGS_REQ or not self.config.local_sweep.enabled:
            return
        events = self._sweep_learner.observe_write_response(
            body,
            resp_type,
            resp_body,
            connection_epoch=self._connection_epoch,
            duration_ms=duration_ms,
            network_ms=network_ms,
        )
        self._emit_sweep_learner_events(events, dll_seq=dll_seq, msg_name=msg_name)

    def _active_replay_result_ready(
        self,
        observed: SweepObservedRequest,
    ) -> SweepResultRecord | None:
        if not self.config.local_sweep.active_replay:
            return None
        signature_digest = observed.signature.signature_digest
        return self._sweep_shadow_store.latest_replay_ready(
            signature_digest,
            max_result_age_ms=self.config.local_sweep.max_result_age_ms,
            min_clean_matches=self._active_replay_min_clean_matches(),
        )

    def _active_replay_min_clean_matches(self) -> int:
        # Replay must prove the latest shadow generation is comparison-clean,
        # not merely "present", before it can replace a foreground request.
        return max(2, int(self.config.local_sweep.min_cycles))

    @staticmethod
    def _observe_inventory_only_request(observed: SweepObservedRequest) -> bool:
        return observed.signature.identifier_kind == "gm_a9_packet"

    def _candidate_for_active_replay(
        self,
        body: bytes,
    ) -> SweepObservedRequest | None:
        if not self.config.local_sweep.active_replay:
            return None
        observed = self._sweep_learner.replay_candidate_for_write(
            body,
            connection_epoch=self._connection_epoch,
            read_num_msgs=1,
            read_timeout_ms=self.config.local_sweep.read_timeout_ms,
        )
        if observed is None:
            return None
        if self._observe_inventory_only_request(observed):
            return None
        return observed

    def _try_prepare_active_replay_write(
        self,
        body: bytes,
    ) -> _ActiveReplayPending | None:
        observed = self._candidate_for_active_replay(body)
        if observed is None:
            return None
        shadow_result = self._active_replay_result_ready(observed)
        if shadow_result is None:
            return None
        return _ActiveReplayPending(observed=observed, shadow_result=shadow_result)

    def _try_consume_active_replay_read(
        self,
        body: bytes,
    ) -> _ActiveReplayPending | None:
        if not self.config.local_sweep.active_replay:
            return None
        try:
            channel_id, _num_msgs, _timeout = ProtocolDecoder.decode_read_msgs_req(body)
        except Exception:
            return None
        pending = self._active_replay_pending_by_channel.get(channel_id)
        if pending is None:
            return None
        shadow_result = self._active_replay_result_ready(pending.observed)
        if shadow_result is None:
            self._active_replay_pending_by_channel.pop(channel_id, None)
            return None
        self._active_replay_pending_by_channel.pop(channel_id, None)
        return _ActiveReplayPending(
            observed=pending.observed,
            shadow_result=shadow_result,
        )

    def _shadow_plan_request_allowed(self, observed: SweepObservedRequest) -> bool:
        if self._observe_inventory_only_request(observed):
            return False
        signature = observed.signature
        if signature.identifier_kind != "uds_did":
            return True
        include_dids = {
            int(value) for value in self.config.local_sweep.include_uds_dids
        }
        exclude_dids = {
            int(value) for value in self.config.local_sweep.exclude_uds_dids
        }
        if include_dids and int(signature.identifier) not in include_dids:
            return False
        if int(signature.identifier) in exclude_dids:
            return False
        return True

    def _learned_for_shadow_plan(
        self,
        channel_id: int,
    ) -> tuple[list[SweepObservedRequest], int, int]:
        if self._connection_epoch is None:
            return [], 0, 0
        learned = self._sweep_learner.learned_for_channel(
            channel_id,
            connection_epoch=self._connection_epoch,
        )
        allowed: list[SweepObservedRequest] = []
        skipped_gm_a9 = 0
        for item in learned:
            if self._shadow_plan_request_allowed(item):
                allowed.append(item)
            elif item.signature.identifier_kind == "gm_a9_packet":
                skipped_gm_a9 += 1
        return (
            allowed[: self.config.local_sweep.max_items],
            max(0, len(learned) - len(allowed)),
            skipped_gm_a9,
        )

    def _emit_shadow_plan_skipped_once(
        self,
        *,
        channel_id: int,
        reason: str,
        skipped_count: int,
        skipped_gm_a9_count: int,
        observed: SweepObservedRequest | None = None,
    ) -> None:
        key = (self._connection_epoch, channel_id, reason)
        if key in self._sweep_plan_skip_logged:
            return
        self._sweep_plan_skip_logged.add(key)
        fields: dict[str, object] = {}
        if observed is not None:
            fields.update(observed.signature.to_observability())
        self._emit_tunnel_event(
            "sweep.plan.skipped",
            reason=reason,
            channel_id=channel_id,
            sweep_skipped_item_count=skipped_count,
            sweep_skipped_gm_a9_count=skipped_gm_a9_count,
            sweep_shadow_allow_gm_a9_packet=(
                self.config.local_sweep.shadow_allow_gm_a9_packet
            ),
            sweep_include_uds_dids=list(self.config.local_sweep.include_uds_dids),
            sweep_exclude_uds_dids=list(self.config.local_sweep.exclude_uds_dids),
            **fields,
        )

    def _build_sweep_plan_for_channel(self, channel_id: int) -> SweepPlanStartRequest | None:
        if self._connection_epoch is None:
            return None
        learned, _skipped_count, _skipped_gm_a9_count = self._learned_for_shadow_plan(
            channel_id
        )
        if not learned:
            return None
        plan_id = f"{self._connection_epoch}-ch{channel_id}-{int(time.time() * 1000)}"
        return SweepPlanStartRequest(
            plan_id=plan_id,
            connection_epoch=self._connection_epoch,
            channel_id=channel_id,
            max_result_age_ms=self.config.local_sweep.max_result_age_ms,
            min_item_interval_ms=self.config.local_sweep.min_item_interval_ms,
            shadow_max_seconds=self.config.local_sweep.shadow_max_seconds,
            requests=tuple(
                SweepRequestSpec(
                    signature_digest=item.signature.signature_digest,
                    write_req_body=item.write_req_body,
                    read_num_msgs=item.read_num_msgs,
                    read_timeout_ms=max(
                        int(item.read_timeout_ms),
                        int(self.config.local_sweep.read_timeout_ms),
                    ),
                )
                for item in learned
            ),
        )

    def _sweep_plan_start_pending(self) -> bool:
        task = self._sweep_plan_start_task
        return task is not None and not task.done()

    def _start_shadow_plan(self, plan: SweepPlanStartRequest, *, reason: str) -> None:
        self._sweep_active_plan = plan
        self._sweep_active_plan_started_mono = time.monotonic()
        self._sweep_pending_plan_channel = None
        self._sweep_mismatch_count = 0
        self._sweep_error_count = 0
        _learned, skipped_count, skipped_gm_a9_count = self._learned_for_shadow_plan(
            plan.channel_id
        )
        self._emit_tunnel_event(
            "sweep.plan.started",
            reason=reason,
            sweep_plan_id=plan.plan_id,
            channel_id=plan.channel_id,
            sweep_item_count=len(plan.requests),
            sweep_plan_read_num_msgs=[
                int(request.read_num_msgs) for request in plan.requests
            ],
            sweep_plan_read_timeout_ms=[
                int(request.read_timeout_ms) for request in plan.requests
            ],
            sweep_skipped_item_count=skipped_count,
            sweep_skipped_gm_a9_count=skipped_gm_a9_count,
            sweep_shadow_allow_gm_a9_packet=(
                self.config.local_sweep.shadow_allow_gm_a9_packet
            ),
            sweep_min_item_interval_ms=plan.min_item_interval_ms,
            sweep_plan_delay_ms=self.config.local_sweep.plan_delay_ms,
            sweep_include_uds_dids=list(self.config.local_sweep.include_uds_dids),
            sweep_exclude_uds_dids=list(self.config.local_sweep.exclude_uds_dids),
        )
        self._schedule_sweep_task(self._send_sweep_plan_start(plan))

    def _maybe_start_shadow_plan(self, observed: SweepObservedRequest) -> None:
        if not self.config.local_sweep.shadow_transport_enabled:
            return
        if not self._vci_sweep_shadow_supported:
            return
        if self._sweep_active_plan is not None:
            return
        if self._sweep_plan_start_pending():
            return
        channel_id = observed.signature.channel_id
        plan = self._build_sweep_plan_for_channel(channel_id)
        if plan is None:
            _learned, skipped_count, skipped_gm_a9_count = self._learned_for_shadow_plan(
                channel_id
            )
            if skipped_gm_a9_count:
                self._emit_shadow_plan_skipped_once(
                    channel_id=channel_id,
                    reason="gm_a9_packet_observe_only",
                    skipped_count=skipped_count,
                    skipped_gm_a9_count=skipped_gm_a9_count,
                    observed=observed,
                )
            return
        delay_ms = max(0, int(self.config.local_sweep.plan_delay_ms))
        if delay_ms <= 0:
            self._start_shadow_plan(plan, reason="shadow_plan_requested")
            return
        self._sweep_pending_plan_channel = channel_id
        _learned, skipped_count, skipped_gm_a9_count = self._learned_for_shadow_plan(
            channel_id
        )
        self._emit_tunnel_event(
            "sweep.plan.deferred",
            reason="shadow_plan_delay_window",
            channel_id=channel_id,
            sweep_item_count=len(plan.requests),
            sweep_plan_read_num_msgs=[
                int(request.read_num_msgs) for request in plan.requests
            ],
            sweep_plan_read_timeout_ms=[
                int(request.read_timeout_ms) for request in plan.requests
            ],
            sweep_skipped_item_count=skipped_count,
            sweep_skipped_gm_a9_count=skipped_gm_a9_count,
            sweep_shadow_allow_gm_a9_packet=(
                self.config.local_sweep.shadow_allow_gm_a9_packet
            ),
            sweep_min_item_interval_ms=plan.min_item_interval_ms,
            sweep_plan_delay_ms=delay_ms,
            sweep_include_uds_dids=list(self.config.local_sweep.include_uds_dids),
            sweep_exclude_uds_dids=list(self.config.local_sweep.exclude_uds_dids),
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._sweep_plan_start_task = loop.create_task(
            self._delayed_start_shadow_plan(channel_id, delay_ms)
        )

    async def _delayed_start_shadow_plan(self, channel_id: int, delay_ms: int) -> None:
        try:
            await asyncio.sleep(delay_ms / 1000.0)
            if self._sweep_active_plan is not None:
                return
            if not (self.config.local_sweep.shadow_transport_enabled and self._vci_sweep_shadow_supported):
                return
            if self._sweep_pending_plan_channel != channel_id:
                return
            plan = self._build_sweep_plan_for_channel(channel_id)
            if plan is None:
                self._emit_tunnel_event(
                    "sweep.plan.cancelled",
                    reason="no_learned_items_after_delay",
                    channel_id=channel_id,
                    sweep_plan_delay_ms=delay_ms,
                )
                return
            self._start_shadow_plan(plan, reason="shadow_plan_delay_elapsed")
        except asyncio.CancelledError:
            raise
        finally:
            self._sweep_pending_plan_channel = None

    def _expire_sweep_plan_if_needed(self) -> None:
        plan = self._sweep_active_plan
        started = self._sweep_active_plan_started_mono
        if plan is None or started is None:
            return
        if time.monotonic() - started > plan.shadow_max_seconds:
            self._cancel_sweep_plan("max_seconds_expired", channel_id=plan.channel_id)

    def _schedule_sweep_task(self, coro) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            return
        loop.create_task(coro)

    async def _send_sweep_frame(self, encoded: bytes, *, timeout_s: float = 1.0):
        if self.vci_writer is None:
            raise ConnectionError("VCI tunnel unavailable for sweep control")
        _magic, _length, _msg_type, sequence = Message.decode_header(encoded[:HEADER_SIZE])
        future = asyncio.get_running_loop().create_future()
        self.response_futures[sequence] = future
        try:
            async with self.vci_lock:
                if self.vci_writer is None:
                    raise ConnectionError("VCI tunnel unavailable for sweep control")
                self.vci_writer.write(encoded)
                await self.vci_writer.drain()
            return await asyncio.wait_for(future, timeout=timeout_s)
        except Exception:
            self.response_futures.pop(sequence, None)
            raise

    async def _send_sweep_plan_start(self, plan: SweepPlanStartRequest) -> None:
        sequence = self._next_sequence()
        try:
            resp_type, resp_body, _hw_ms = await self._send_sweep_frame(
                encode_sweep_plan_start_req(plan, sequence=sequence),
            )
            if resp_type != MsgType.SWEEP_PLAN_START_RSP:
                raise RuntimeError(f"unexpected sweep start response: {resp_type:#x}")
            response = decode_sweep_plan_start_rsp(resp_body)
            if not response.success:
                raise RuntimeError(response.reason)
            self._schedule_sweep_poll()
        except Exception as exc:
            self._emit_tunnel_event(
                "sweep.plan.cancelled",
                status="error",
                failure_code="sweep_plan_start_failed",
                failure_domain="cloud_proxy_tunnel",
                reason=str(exc),
                sweep_plan_id=plan.plan_id,
                channel_id=plan.channel_id,
            )
            if self._sweep_active_plan is plan:
                self._sweep_active_plan = None
                self._sweep_active_plan_started_mono = None

    async def _send_sweep_plan_stop(self, plan_id: str, reason: str) -> None:
        sequence = self._next_sequence()
        try:
            await self._send_sweep_frame(
                encode_sweep_plan_stop_req(
                    SweepPlanStopRequest(plan_id=plan_id, reason=reason),
                    sequence=sequence,
                ),
            )
        except Exception:
            logger.debug("Sweep plan stop control failed", exc_info=True)

    def _schedule_sweep_poll(self) -> None:
        if self._sweep_active_plan is None:
            return
        if self._sweep_poll_task is not None and not self._sweep_poll_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._sweep_poll_task = loop.create_task(self._poll_sweep_once())

    async def _poll_sweep_once(self) -> None:
        plan = self._sweep_active_plan
        if plan is None:
            return
        self._expire_sweep_plan_if_needed()
        if self._sweep_active_plan is None:
            return
        try:
            status_seq = self._next_sequence()
            await self._send_sweep_frame(
                encode_sweep_status_req(sequence=status_seq),
                timeout_s=0.5,
            )
            drain_seq = self._next_sequence()
            resp_type, resp_body, _hw_ms = await self._send_sweep_frame(
                encode_sweep_drain_results_req(sequence=drain_seq),
                timeout_s=0.5,
            )
            if resp_type != MsgType.SWEEP_DRAIN_RESULTS_RSP:
                return
            results = decode_sweep_drain_results_rsp(resp_body)
            for result in results:
                self._sweep_shadow_store.record_result(
                    result,
                    channel_id=plan.channel_id,
                )
            self._emit_tunnel_event(
                "sweep.batch.drained",
                reason="drain_completed",
                sweep_plan_id=plan.plan_id,
                channel_id=plan.channel_id,
                sweep_result_count=len(results),
            )
        except Exception:
            logger.debug("Sweep status/drain poll failed", exc_info=True)

    def _shadow_comparison_state_fields(
        self,
        signature_digest: str,
        channel_id: int,
    ) -> dict[str, object]:
        active_plan = self._sweep_active_plan
        signature_in_active_plan = False
        if active_plan is not None:
            signature_in_active_plan = any(
                request.signature_digest == signature_digest
                for request in active_plan.requests
            )
        return {
            "sweep_plan_active": active_plan is not None,
            "sweep_plan_pending": self._sweep_plan_start_pending(),
            "sweep_active_plan_id": active_plan.plan_id if active_plan is not None else None,
            "sweep_pending_plan_channel": self._sweep_pending_plan_channel,
            "sweep_store_pending_count": self._sweep_shadow_store.pending_count(),
            "sweep_signature_in_active_plan": signature_in_active_plan,
            "channel_id": channel_id,
        }

    @staticmethod
    def _shadow_not_ready_reason(state_fields: dict[str, object]) -> str | None:
        if bool(state_fields.get("sweep_plan_pending")):
            return "plan_pending"
        if not bool(state_fields.get("sweep_plan_active")):
            return "no_active_plan"
        if (
            bool(state_fields.get("sweep_signature_in_active_plan"))
            and int(state_fields.get("sweep_store_pending_count") or 0) <= 0
        ):
            return "active_plan_no_drained_results"
        return None

    def _compare_shadow_result(
        self,
        observed: SweepObservedRequest,
        real_read_rsp_body: bytes,
        *,
        dll_seq: int,
        msg_name: str,
    ) -> None:
        if not self.config.local_sweep.shadow_transport_enabled:
            return
        signature_digest = observed.signature.signature_digest
        shadow_result = self._sweep_shadow_store.latest_for(signature_digest)
        result = compare_shadow_to_real(
            signature_digest=signature_digest,
            real_read_rsp_body=real_read_rsp_body,
            shadow_result=shadow_result,
            max_result_age_ms=self.config.local_sweep.max_result_age_ms,
        )
        state_fields = self._shadow_comparison_state_fields(
            signature_digest,
            observed.signature.channel_id,
        )
        if result.outcome == "missing":
            not_ready_reason = self._shadow_not_ready_reason(state_fields)
            if not_ready_reason is not None:
                self._emit_proxy_request_event(
                    "sweep.shadow.not_ready",
                    dll_seq=dll_seq,
                    msg_name=msg_name,
                    reason=f"shadow_{not_ready_reason}",
                    **result.fields,
                    **state_fields,
                    sweep_shadow_not_ready_reason=not_ready_reason,
            )
                return
            missing_reason = (
                "signature_not_in_active_plan"
                if not bool(state_fields.get("sweep_signature_in_active_plan"))
                else "signature_not_drained"
            )
            result_fields = {
                **result.fields,
                "sweep_shadow_missing_reason": missing_reason,
            }
        else:
            result_fields = result.fields
        if shadow_result is not None:
            real_return_code = result.fields.get("sweep_real_return_code")
            real_message_count = result.fields.get("sweep_real_message_count")
            shadow_return_code = result.fields.get("sweep_shadow_return_code")
            shadow_message_count = result.fields.get("sweep_shadow_message_count")
            shadow_structurally_clean = (
                shadow_return_code is not None
                and int(shadow_return_code) == 0
                and shadow_message_count is not None
                and int(shadow_message_count) > 0
            )
            real_structurally_clean = (
                real_return_code is not None
                and int(real_return_code) == 0
                and real_message_count is not None
                and int(real_message_count) > 0
            )
            clean_match = (
                result.outcome == "match"
                and real_structurally_clean
                and shadow_structurally_clean
            )
            self._sweep_shadow_store.record_comparison(
                signature_digest,
                result=shadow_result,
                clean_match=clean_match,
                reset_streak=(
                    result.outcome in {"mismatch", "error"}
                    or not shadow_structurally_clean
                ),
            )
            result_fields = {
                **result_fields,
                "sweep_shadow_clean_match_streak": self._sweep_shadow_store.replay_match_streak(
                    signature_digest
                ),
                "sweep_replay_min_clean_matches": self._active_replay_min_clean_matches(),
            }
        self._emit_proxy_request_event(
            f"sweep.shadow.{result.outcome}",
            dll_seq=dll_seq,
            msg_name=msg_name,
            reason=f"shadow_{result.outcome}",
            **result_fields,
            **state_fields,
        )
        if result.outcome == "mismatch":
            self._sweep_mismatch_count += 1
            if self._sweep_mismatch_count >= self.config.local_sweep.mismatch_threshold:
                self._cancel_sweep_plan(
                    "mismatch_threshold_reached",
                    channel_id=observed.signature.channel_id,
                )
        elif result.outcome == "error":
            self._sweep_error_count += 1
            if self._sweep_error_count >= self.config.local_sweep.error_threshold:
                self._cancel_sweep_plan(
                    "error_threshold_reached",
                    channel_id=observed.signature.channel_id,
                )

    def _cancel_pending_sweep_plan_start(
        self,
        reason: str,
        *,
        channel_id: int | None = None,
    ) -> None:
        task = self._sweep_plan_start_task
        if task is None or task.done():
            return
        pending_channel = self._sweep_pending_plan_channel
        if channel_id is not None and pending_channel not in {None, channel_id}:
            return
        task.cancel()
        self._sweep_pending_plan_channel = None
        self._emit_tunnel_event(
            "sweep.plan.cancelled",
            reason=f"pending_start_{reason}",
            channel_id=pending_channel if pending_channel is not None else channel_id,
        )

    def _cancel_sweep_plan(self, reason: str, *, channel_id: int | None = None) -> None:
        plan = self._sweep_active_plan
        self._cancel_pending_sweep_plan_start(reason, channel_id=channel_id)
        if channel_id is not None:
            self._sweep_shadow_store.clear_channel(channel_id)
            self._sweep_learner.reset_channel(channel_id)
            self._active_replay_pending_by_channel.pop(channel_id, None)
        else:
            self._sweep_shadow_store.clear()
            self._active_replay_pending_by_channel.clear()
        if plan is None:
            return
        if channel_id is not None and plan.channel_id != channel_id:
            return
        self._sweep_active_plan = None
        self._sweep_active_plan_started_mono = None
        self._emit_tunnel_event(
            "sweep.plan.cancelled",
            reason=reason,
            sweep_plan_id=plan.plan_id,
            channel_id=plan.channel_id,
        )
        self._schedule_sweep_task(self._send_sweep_plan_stop(plan.plan_id, reason))

    @staticmethod
    def _message_payload_bytes(messages: list[dict]) -> int:
        return sum(len(message.get("data", b"")) for message in messages)

    @staticmethod
    def _payload_digest(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @classmethod
    def _messages_payload_digest(cls, messages: list[dict]) -> str | None:
        if not messages:
            return None
        digest = hashlib.sha256()
        for message in messages:
            data = bytes(message.get("data", b"") or b"")
            digest.update(len(data).to_bytes(4, "big", signed=False))
            digest.update(data)
        return digest.hexdigest()

    @staticmethod
    def _engine_speed_candidate(data: bytes) -> dict[str, object]:
        candidates = (
            (b"\x41\x0c", 2, "obd_pid_0c"),
            (b"\x62\xf4\x0c", 3, "uds_did_f40c"),
        )
        for marker, value_offset, source in candidates:
            start = data.find(marker)
            if start < 0:
                continue
            value_start = start + value_offset
            if value_start + 2 > len(data):
                continue
            raw_value = int.from_bytes(data[value_start:value_start + 2], "big")
            return {
                "engine_speed_candidate_rpm": round(raw_value / 4.0, 3),
                "engine_speed_candidate_source": source,
                "engine_speed_candidate_offset": start,
            }
        return {}

    @staticmethod
    def _can_id_prefixed_payload_fields(data: bytes) -> dict[str, object]:
        if len(data) < 5:
            return {}
        can_id = int.from_bytes(data[:4], "big", signed=False)
        if not (0x500 <= can_id <= 0x7FF):
            return {}
        payload = data[4:]
        fields: dict[str, object] = {
            "can_id": can_id,
            "can_id_hex": f"{can_id:08x}",
            "can_payload_length": len(payload),
            "can_payload_prefix_hex": payload[:PAYLOAD_PREFIX_BYTES].hex(),
        }
        if len(payload) >= 3 and payload[0] == 0xA9:
            fields["gm_request_service_id"] = payload[0]
            fields["gm_request_subfunction"] = payload[1]
            fields["gm_request_packet_id"] = payload[2]
            fields["gm_request_packet_id_hex"] = f"{payload[2]:02x}"
        elif 0x500 <= can_id <= 0x5FF and payload:
            fields["gm_data_packet_id"] = payload[0]
            fields["gm_data_packet_id_hex"] = f"{payload[0]:02x}"
        return fields

    @classmethod
    def _message_payload_summary(
        cls,
        prefix: str,
        messages: list[dict],
    ) -> dict[str, object]:
        fields: dict[str, object] = {}
        digest = cls._messages_payload_digest(messages)
        if digest is not None:
            fields[f"{prefix}_payload_digest"] = digest

        samples: list[dict[str, object]] = []
        for index, message in enumerate(messages[:PAYLOAD_SAMPLE_LIMIT]):
            data = bytes(message.get("data", b"") or b"")
            sample: dict[str, object] = {
                "index": index,
                "protocol_id": int(message.get("protocol_id", 0) or 0),
                "rx_status": int(message.get("rx_status", 0) or 0),
                "tx_flags": int(message.get("tx_flags", 0) or 0),
                "j2534_timestamp": int(message.get("timestamp", 0) or 0),
                "data_length": len(data),
                "data_digest": cls._payload_digest(data),
                "data_prefix_hex": data[:PAYLOAD_PREFIX_BYTES].hex(),
            }
            sample.update(cls._can_id_prefixed_payload_fields(data))
            candidate = cls._engine_speed_candidate(data)
            if candidate:
                sample.update(candidate)
                fields.setdefault(
                    f"{prefix}_engine_speed_candidate_rpm",
                    candidate["engine_speed_candidate_rpm"],
                )
                fields.setdefault(
                    f"{prefix}_engine_speed_candidate_source",
                    candidate["engine_speed_candidate_source"],
                )
            samples.append(sample)

        if samples:
            fields[f"{prefix}_payload_sample_count"] = len(samples)
            fields[f"{prefix}_payload_samples"] = samples
        return fields

    def _request_observability_fields(self, msg_type: int, body: bytes) -> dict[str, object]:
        fields: dict[str, object] = {}
        try:
            if msg_type == MsgType.READ_MSGS_REQ:
                channel_id, num_msgs, timeout = ProtocolDecoder.decode_read_msgs_req(body)
                fields.update(
                    {
                        "channel_id": channel_id,
                        "num_msgs": num_msgs,
                        "timeout": timeout,
                    }
                )
                marker = self._last_write_by_channel.get(channel_id)
                if marker is not None:
                    last_write_seq, last_write_ts = marker
                    fields["last_write_seq"] = last_write_seq
                    fields["post_write_age_ms"] = round(
                        max(0.0, (time.monotonic() - last_write_ts) * 1000.0), 3
                    )
            elif msg_type == MsgType.WRITE_MSGS_REQ:
                channel_id, messages, timeout = ProtocolDecoder.decode_write_msgs_req(body)
                fields.update(
                    {
                        "channel_id": channel_id,
                        "write_message_count": len(messages),
                        "timeout": timeout,
                        "write_payload_bytes": self._message_payload_bytes(messages),
                        **self._message_payload_summary("write", messages),
                    }
                )
        except Exception:
            fields["decode_error"] = True
        return fields

    def _augment_live_cadence_fields(
        self,
        msg_type: int,
        sequence: int | None,
        fields: dict[str, object],
    ) -> None:
        if msg_type not in (MsgType.READ_MSGS_REQ, MsgType.WRITE_MSGS_REQ):
            return
        channel_id = fields.get("channel_id")
        if not isinstance(channel_id, int):
            return

        msg_name = MSG_NAMES.get(msg_type, f"0x{int(msg_type):04x}")
        now = time.monotonic()
        fields["live_request_kind"] = "read" if msg_type == MsgType.READ_MSGS_REQ else "write"
        previous = self._last_live_request_by_channel.get(channel_id)
        if previous is None:
            fields["live_inter_request_gap_bucket"] = "first_on_channel"
        else:
            previous_msg_name, previous_sequence, previous_ts = previous
            gap_ms = max(0.0, (now - previous_ts) * 1000.0)
            fields["previous_live_msg_name"] = previous_msg_name
            fields["previous_live_dll_seq"] = previous_sequence
            fields["live_inter_request_gap_ms"] = round(gap_ms, 3)
            if gap_ms >= LIVE_DATA_GAP_STALL_MS:
                fields["live_inter_request_gap_bucket"] = "ge_3000ms"
            elif gap_ms >= LIVE_DATA_GAP_WARN_MS:
                fields["live_inter_request_gap_bucket"] = "ge_1000ms"
            else:
                fields["live_inter_request_gap_bucket"] = "lt_1000ms"
        self._last_live_request_by_channel[channel_id] = (msg_name, sequence, now)

    def _emit_live_cadence_gap_if_needed(
        self,
        *,
        dll_seq: int,
        msg_name: str,
        request_fields: dict[str, object],
    ) -> None:
        gap_ms = request_fields.get("live_inter_request_gap_ms")
        if not isinstance(gap_ms, (int, float)) or gap_ms < LIVE_DATA_GAP_WARN_MS:
            return
        threshold_ms = (
            LIVE_DATA_GAP_STALL_MS
            if gap_ms >= LIVE_DATA_GAP_STALL_MS
            else LIVE_DATA_GAP_WARN_MS
        )
        reason = (
            "inter_request_gap_ge_3000ms"
            if threshold_ms == LIVE_DATA_GAP_STALL_MS
            else "inter_request_gap_ge_1000ms"
        )
        self._emit_proxy_request_event(
            "proxy.j2534.cadence_gap",
            dll_seq=dll_seq,
            msg_name=msg_name,
            reason=reason,
            live_gap_threshold_ms=threshold_ms,
            **request_fields,
        )

    def _response_observability_fields(self, resp_type: int | None, resp_body: bytes) -> dict[str, object]:
        if resp_type != MsgType.READ_MSGS_RSP:
            return {}
        try:
            return_code, messages = ProtocolDecoder.decode_read_msgs_rsp(resp_body)
            message_count = len(messages)
            return {
                "return_code": return_code,
                "message_count": message_count,
                "payload_bytes": self._message_payload_bytes(messages),
                "read_result": "data" if message_count else "empty",
                **self._message_payload_summary("read", messages),
            }
        except Exception:
            return {"response_decode_error": True}

    def _augment_read_payload_delta_fields(
        self,
        request_fields: dict[str, object],
        response_fields: dict[str, object],
    ) -> None:
        if response_fields.get("read_result") != "data":
            return
        channel_id = request_fields.get("channel_id")
        payload_digest = response_fields.get("read_payload_digest")
        if not isinstance(channel_id, int) or not isinstance(payload_digest, str):
            return

        now = time.monotonic()
        previous = self._last_read_payload_by_channel.get(channel_id)
        if previous is None:
            response_fields["read_payload_changed"] = True
            response_fields["read_payload_change_kind"] = "first_data_on_channel"
        else:
            previous_digest, previous_ts = previous
            gap_ms = max(0.0, (now - previous_ts) * 1000.0)
            response_fields["previous_read_payload_digest"] = previous_digest
            response_fields["read_payload_observation_gap_ms"] = round(gap_ms, 3)
            response_fields["read_payload_changed"] = previous_digest != payload_digest
            response_fields["read_payload_change_kind"] = (
                "changed" if previous_digest != payload_digest else "unchanged"
            )
        self._last_read_payload_by_channel[channel_id] = (payload_digest, now)

    def _cache_miss_reason(self, msg_type: int, request_fields: dict[str, object]) -> str:
        if msg_type != MsgType.READ_MSGS_REQ:
            return "cache_miss"
        age_ms = request_fields.get("post_write_age_ms")
        bypass_ms = self.config.read_msgs_cache.post_write_bypass_ms
        if isinstance(age_ms, (int, float)) and bypass_ms > 0 and age_ms <= bypass_ms:
            return "post_write_bypass"
        if (
            self.config.read_ahead.enabled
            and request_fields.get("prefetch_fifo_pending_before") == 0
        ):
            return "prefetch_miss"
        return "cache_miss"

    def _emit_process_lifecycle_event(
        self,
        event_type: str,
        *,
        status: str = "ok",
        failure_code: str | None = None,
        reason: str | None = None,
        **extra: object,
    ) -> None:
        self._emit_tunnel_event(
            event_type,
            operation_kind="reverse_server_process",
            impact_scope="reverse_server_process",
            status=status,
            failure_code=failure_code,
            failure_domain="cloud_proxy_tunnel" if status == "error" else "unknown",
            reason=reason,
            pid=os.getpid(),
            listen_port=self.listen_port,
            proxy_port=self.proxy_port,
            **extra,
        )

    def _build_tls_server_context(self) -> ssl.SSLContext | None:
        """Build optional TLS listener context for inbound reverse clients."""
        if not self.config.tls.enabled:
            return None

        certfile = self.config.tls.certfile
        keyfile = self.config.tls.keyfile
        if not certfile or not keyfile:
            raise ValueError("TLS certfile and keyfile are required")

        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(certfile=certfile, keyfile=keyfile)
        harden_tls_context(context)

        if self.config.tls.require_client_cert:
            if not self.config.tls.ca_file:
                raise ValueError("TLS CA file is required when client certificates are enforced")
            context.load_verify_locations(cafile=self.config.tls.ca_file)
            context.verify_mode = ssl.CERT_REQUIRED

        return context

    def _auth_success_message(self, *, connection_epoch: str | None = None) -> str:
        capabilities = ["ok"]
        if self.config.read_ahead.enabled:
            capabilities.append("read_ahead=1")
            if self.config.read_ahead.transaction_enabled:
                capabilities.append("read_collect=1")
                capabilities.append("write_collect=1")
        if self.config.local_sweep.shadow_transport_enabled:
            capabilities.append("sweep_shadow=1")
        if connection_epoch:
            capabilities.append(f"connection_epoch={connection_epoch}")
        return ";".join(capabilities)

    @staticmethod
    def _capability_enabled(message: str, capability: str) -> bool:
        expected = f"{capability}=1"
        return any(
            token.strip().lower() == expected
            for token in str(message or "").replace(",", ";").split(";")
        )

    async def start(self):
        """启动服务器"""
        # 启动 VCI 监听服务
        tls_context = self._build_tls_server_context()
        self._vci_server = await asyncio.start_server(
            self._handle_vci_connection, '0.0.0.0', self.listen_port, ssl=tls_context
        )
        logger.info(f"等待 VCI Proxy 连接到端口 {self.listen_port}...")

        # 启动代理服务
        self._proxy_server = await asyncio.start_server(
            self._handle_proxy_connection, '127.0.0.1', self.proxy_port
        )
        logger.info(f"代理服务监听端口 {self.proxy_port}")

        if self.config.auth.enabled:
            logger.info("PSK authentication enabled")
        if self.config.tls.enabled:
            logger.info("Reverse tunnel TLS enabled")
        if self.config.read_msgs_cache.enabled:
            logger.info(
                f"ReadMsgs cache enabled (idle TTL={self.config.read_msgs_cache.ttl_ms}ms, "
                f"active TTL={self.config.read_msgs_cache.active_ttl_ms}ms, "
                f"adaptive active max={self.config.read_msgs_cache.active_adaptive_ttl_max_ms}ms, "
                f"adaptive margin={self.config.read_msgs_cache.active_adaptive_ttl_margin_ms}ms, "
                f"active window={self.config.read_msgs_cache.active_window_ms}ms, "
                f"post-write bypass={self.config.read_msgs_cache.post_write_bypass_ms}ms, "
                f"max timeout={self.config.read_msgs_cache.max_cacheable_timeout_ms}ms)"
            )
        if self.config.filter_dedup.enabled:
            logger.info("Filter deduplication enabled")
        if self.config.vbatt_cache.enabled:
            logger.info(
                f"VBATT cache enabled (TTL={self.config.vbatt_cache.ttl_s}s)"
            )
        if self.config.ioctl_cache.enabled:
            logger.info(
                f"IOCTL cache enabled (TTL={self.config.ioctl_cache.ttl_s}s, "
                f"covers GET_CONFIG/READ_VBATT/READ_PROG_VOLTAGE)"
            )
        if self.config.read_ahead.enabled:
            logger.info(
                "Read-ahead enabled (window=%sms, max_reads=%s, write_collect_max_reads=%s, timeout=%sms, max_messages=%s, max_empty_reads=%s, max_consecutive_empty_reads=%s, min_drain=%sms, transaction=%s, transaction_guard=%sms/%sms)",
                self.config.read_ahead.window_ms,
                self.config.read_ahead.max_reads,
                self.config.read_ahead.write_collect_max_reads,
                self.config.read_ahead.read_timeout_ms,
                self.config.read_ahead.max_messages,
                self.config.read_ahead.max_empty_reads,
                self.config.read_ahead.max_consecutive_empty_reads,
                self.config.read_ahead.min_drain_ms,
                "enabled" if self.config.read_ahead.transaction_enabled else "disabled",
                self.config.read_ahead.transaction_max_network_ms,
                self.config.read_ahead.transaction_cooldown_ms,
            )
        if self.config.local_sweep.enabled:
            logger.info(
                "Local sweep enabled (mode=%s, min_cycles=%s, max_items=%s, allow_gm_a9_packet=%s, shadow_allow_gm_a9_packet=%s, min_item_interval_ms=%s, shadow_max_seconds=%s, plan_delay_ms=%s, include_uds_dids=%s, exclude_uds_dids=%s)",
                self.config.local_sweep.mode,
                self.config.local_sweep.min_cycles,
                self.config.local_sweep.max_items,
                self.config.local_sweep.allow_gm_a9_packet,
                self.config.local_sweep.shadow_allow_gm_a9_packet,
                self.config.local_sweep.min_item_interval_ms,
                self.config.local_sweep.shadow_max_seconds,
                self.config.local_sweep.plan_delay_ms,
                list(self.config.local_sweep.include_uds_dids),
                list(self.config.local_sweep.exclude_uds_dids),
            )
        if self.benchmark_writer is not None:
            logger.info("Proxy benchmark logging enabled: %s", self.benchmark_writer.path)
        logger.info("Waiting for local VCI Proxy connection on port %s", self.listen_port)
        logger.info("After connection, J2534 is reachable through localhost:%s", self.proxy_port)
        if not self._process_started_emitted:
            self._process_started_emitted = True
            self._emit_process_lifecycle_event(
                "process.lifecycle.started",
                reason="server_started",
                auth_enabled=self.config.auth.enabled,
                tls_enabled=self.config.tls.enabled,
                read_ahead_enabled=self.config.read_ahead.enabled,
                read_ahead_transaction_enabled=self.config.read_ahead.transaction_enabled,
                read_cache_enabled=self.config.read_msgs_cache.enabled,
                read_cache_ttl_ms=self.config.read_msgs_cache.ttl_ms,
                read_cache_active_ttl_ms=self.config.read_msgs_cache.active_ttl_ms,
                read_cache_active_adaptive_ttl_max_ms=(
                    self.config.read_msgs_cache.active_adaptive_ttl_max_ms
                ),
                read_cache_active_adaptive_ttl_margin_ms=(
                    self.config.read_msgs_cache.active_adaptive_ttl_margin_ms
                ),
                read_cache_active_window_ms=(
                    self.config.read_msgs_cache.active_window_ms
                ),
                read_cache_post_write_bypass_ms=(
                    self.config.read_msgs_cache.post_write_bypass_ms
                ),
                read_cache_max_timeout_ms=(
                    self.config.read_msgs_cache.max_cacheable_timeout_ms
                ),
                read_ahead_write_collect_max_reads=(
                    self.config.read_ahead.write_collect_max_reads
                ),
                read_ahead_max_empty_reads=self.config.read_ahead.max_empty_reads,
                read_ahead_max_consecutive_empty_reads=(
                    self.config.read_ahead.max_consecutive_empty_reads
                ),
                read_ahead_min_drain_ms=self.config.read_ahead.min_drain_ms,
                read_ahead_transaction_max_network_ms=(
                    self.config.read_ahead.transaction_max_network_ms
                ),
                read_ahead_transaction_cooldown_ms=(
                    self.config.read_ahead.transaction_cooldown_ms
                ),
                local_sweep_enabled=self.config.local_sweep.enabled,
                local_sweep_mode=self.config.local_sweep.mode,
                local_sweep_allow_gm_a9_packet=self.config.local_sweep.allow_gm_a9_packet,
                local_sweep_shadow_allow_gm_a9_packet=(
                    self.config.local_sweep.shadow_allow_gm_a9_packet
                ),
                local_sweep_min_item_interval_ms=self.config.local_sweep.min_item_interval_ms,
                local_sweep_read_timeout_ms=self.config.local_sweep.read_timeout_ms,
                local_sweep_plan_delay_ms=self.config.local_sweep.plan_delay_ms,
                local_sweep_include_uds_dids=list(self.config.local_sweep.include_uds_dids),
                local_sweep_exclude_uds_dids=list(self.config.local_sweep.exclude_uds_dids),
            )

        try:
            await asyncio.gather(
                self._vci_server.serve_forever(),
                self._proxy_server.serve_forever()
            )
        except asyncio.CancelledError:
            self._shutting_down = True
            logger.info("ReverseProxyServer shutdown requested")
        finally:
            await self._shutdown_servers()

    async def _shutdown_servers(self) -> None:
        if self._shutting_down:
            if self._vci_server is None and self._proxy_server is None:
                return
        if not self._process_shutdown_started_emitted:
            self._process_shutdown_started_emitted = True
            self._emit_process_lifecycle_event(
                "process.lifecycle.shutdown_started",
                reason="shutdown_requested",
            )
        self._shutting_down = True
        self._cancel_pending_futures()
        await self._cancel_probe_task_async()
        self.vci_connected.clear()

        writer = self.vci_writer
        self.vci_reader = None
        self.vci_writer = None
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass
            wait_closed = getattr(writer, "wait_closed", None)
            if callable(wait_closed):
                try:
                    await wait_closed()
                except Exception:
                    pass

        for server in (self._vci_server, self._proxy_server):
            if server is None:
                continue
            server.close()
            try:
                await server.wait_closed()
            except Exception:
                pass
        self._vci_server = None
        self._proxy_server = None
        if not self._process_shutdown_finished_emitted:
            self._process_shutdown_finished_emitted = True
            self._emit_process_lifecycle_event(
                "process.lifecycle.shutdown_finished",
                reason="shutdown_finished",
            )

    def _cancel_pending_futures(self):
        """取消所有挂起的 Future（VCI 断开时调用）"""
        pending = list(self.response_futures.items())
        self.response_futures.clear()
        self._prefetch_bundle_source_by_proxy_seq.clear()
        self._prefetch_empty_confirmations_by_proxy_seq.clear()
        self._read_cache.clear()
        self._filter_cache.clear()
        self._ioctl_cache.invalidate()
        self._prefetch_read_msgs.clear()
        self._sweep_shadow_store.clear()
        self._sweep_learner.reset_all()
        self._cancel_pending_sweep_plan_start("connection_cancel_pending_futures")
        self._sweep_active_plan = None
        self._sweep_active_plan_started_mono = None
        for seq, future in pending:
            if not future.done():
                future.set_exception(ConnectionError("VCI Proxy 已断开"))
                logger.debug(f"取消挂起的 Future: seq={seq}")
        if pending:
            logger.info(f"已取消 {len(pending)} 个挂起的请求")

    async def _authenticate_vci(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        connection_epoch_hint: str | None = None,
    ) -> bool:
        """Authenticate the VCI connection.

        Reads the first message from the VCI client. If auth is enabled,
        expects AUTH_REQ with valid HMAC. If auth is disabled, accepts
        either AUTH_REQ or legacy HEARTBEAT with two-phase handshake.

        The two-phase handshake for legacy heartbeat registration works as:
          1. Client sends HEARTBEAT
          2. Server replies HEARTBEAT_ACK
          3. Client must send a second HEARTBEAT within 5s
        This prevents port scanners (which send random bytes) from
        accidentally registering as a VCI client.

        Returns True if authenticated, False otherwise.
        """
        peer = writer.get_extra_info("peername")
        self._vci_read_collect_supported = False
        self._vci_write_collect_supported = False
        self._vci_sweep_shadow_supported = False
        try:
            header = await asyncio.wait_for(
                reader.readexactly(HEADER_SIZE),
                timeout=self.config.auth.auth_timeout_s,
            )
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            logger.warning("VCI client did not send registration message in time: %s", peer)
            return False
        except (ConnectionResetError, BrokenPipeError, OSError) as exc:
            logger.warning("VCI client disconnected during auth: %s error=%s", peer, exc)
            return False

        magic, length, msg_type, sequence = struct.unpack('>IIHI', header)
        if magic != MAGIC:
            logger.warning(f"Invalid magic during auth: {magic:#x}")
            return False

        try:
            body = await _read_frame_body(
                reader,
                length,
                timeout=float(self.config.auth.auth_timeout_s),
            )
        except (ValueError, TimeoutError, ConnectionError) as exc:
            logger.warning("Invalid auth frame from VCI client: %s", exc)
            return False
        except (ConnectionResetError, BrokenPipeError, OSError) as exc:
            logger.warning("VCI client disconnected while sending auth frame: %s error=%s", peer, exc)
            return False

        if msg_type == MsgType.AUTH_REQ:
            if self.config.auth.enabled and not self.config.auth.token:
                logger.error("Auth enabled but no server token is configured")
                rsp = ProtocolEncoder.encode_auth_rsp(False, "server auth token not configured", sequence)
                try:
                    writer.write(rsp)
                    await writer.drain()
                except (ConnectionResetError, BrokenPipeError, OSError) as exc:
                    logger.warning(
                        "VCI client disconnected before auth response could be sent: %s error=%s",
                        peer,
                        exc,
                    )
                    return False
                return False
            if not self.config.auth.enabled:
                # Auth not required, but client sent AUTH_REQ -- accept it
                logger.info("Auth not required, accepting AUTH_REQ")
                capabilities = ProtocolDecoder.decode_auth_req_capabilities(body)
                self._vci_read_collect_supported = self._capability_enabled(
                    capabilities,
                    "read_collect",
                )
                self._vci_write_collect_supported = self._capability_enabled(
                    capabilities,
                    "write_collect",
                )
                self._vci_sweep_shadow_supported = self._capability_enabled(
                    capabilities,
                    "sweep_shadow",
                )
                rsp = ProtocolEncoder.encode_auth_rsp(
                    True,
                    self._auth_success_message(
                        connection_epoch=connection_epoch_hint,
                    ),
                    sequence,
                )
                try:
                    writer.write(rsp)
                    await writer.drain()
                except (ConnectionResetError, BrokenPipeError, OSError) as exc:
                    logger.warning(
                        "VCI client disconnected before auth response could be sent: %s error=%s",
                        peer,
                        exc,
                    )
                    return False
                return True

            timestamp, signature = ProtocolDecoder.decode_auth_req(body)
            capabilities = ProtocolDecoder.decode_auth_req_capabilities(body)
            success, reason = verify_signature(
                self.config.auth.token, timestamp, signature
            )
            if success and self._is_replayed_auth(signature, timestamp):
                success = False
                reason = "replay detected"
            if success:
                reason = self._auth_success_message(
                    connection_epoch=connection_epoch_hint,
                )
                self._vci_read_collect_supported = self._capability_enabled(
                    capabilities,
                    "read_collect",
                )
                self._vci_write_collect_supported = self._capability_enabled(
                    capabilities,
                    "write_collect",
                )
                self._vci_sweep_shadow_supported = self._capability_enabled(
                    capabilities,
                    "sweep_shadow",
                )
            rsp = ProtocolEncoder.encode_auth_rsp(success, reason, sequence)
            try:
                writer.write(rsp)
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError) as exc:
                logger.warning(
                    "VCI client disconnected before auth response could be sent: %s error=%s",
                    peer,
                    exc,
                )
                return False

            if success:
                logger.info("VCI client authenticated successfully")
            else:
                logger.warning(f"VCI client authentication failed: {reason}")
            return success

        if msg_type == MsgType.HEARTBEAT:
            self._vci_read_collect_supported = False
            self._vci_write_collect_supported = False
            self._vci_sweep_shadow_supported = False
            if self.config.auth.enabled:
                logger.warning(
                    "Auth required but VCI client sent HEARTBEAT (legacy client)"
                )
                return False

            # Phase 1: reply ACK to the first heartbeat
            ack_header = struct.pack(
                '>IIHI', MAGIC, HEADER_SIZE, MsgType.HEARTBEAT_ACK, sequence
            )
            try:
                writer.write(ack_header)
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError) as exc:
                logger.warning(
                    "VCI client disconnected before handshake ACK could be sent: %s error=%s",
                    peer,
                    exc,
                )
                return False

            # Phase 2: require a second heartbeat within 5 seconds.
            # Real VCI clients will respond; port scanners won't.
            try:
                header2 = await asyncio.wait_for(
                    reader.readexactly(HEADER_SIZE), timeout=5.0
                )
            except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                logger.warning(
                    "VCI client did not complete two-phase handshake (no 2nd heartbeat)"
                )
                return False
            except (ConnectionResetError, BrokenPipeError, OSError) as exc:
                logger.warning(
                    "VCI client disconnected during handshake phase 2: %s error=%s",
                    peer,
                    exc,
                )
                return False

            magic2, length2, msg_type2, seq2 = struct.unpack('>IIHI', header2)
            if magic2 != MAGIC:
                logger.warning(f"Invalid magic in handshake phase 2: {magic2:#x}")
                return False

            try:
                await _read_frame_body(reader, length2, timeout=5.0)
            except (ValueError, TimeoutError, ConnectionError) as exc:
                logger.warning("Invalid handshake frame in phase 2: %s", exc)
                return False
            except (ConnectionResetError, BrokenPipeError, OSError) as exc:
                logger.warning(
                    "VCI client disconnected while finishing handshake phase 2: %s error=%s",
                    peer,
                    exc,
                )
                return False

            if msg_type2 not in (MsgType.HEARTBEAT, MsgType.HEARTBEAT_ACK):
                logger.warning(
                    f"Unexpected message in handshake phase 2: {msg_type2:#x}"
                )
                return False

            # Phase 2 ACK
            ack2 = struct.pack(
                '>IIHI', MAGIC, HEADER_SIZE, MsgType.HEARTBEAT_ACK, seq2
            )
            try:
                writer.write(ack2)
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError) as exc:
                logger.warning(
                    "VCI client disconnected before handshake completion ACK could be sent: %s error=%s",
                    peer,
                    exc,
                )
                return False

            logger.info("VCI client registered via two-phase heartbeat handshake")
            return True

        logger.warning(f"Unexpected first message type during auth: {msg_type:#x}")
        return False

    def _is_replayed_auth(self, signature: bytes, timestamp: int) -> bool:
        now = int(time.time())
        stale_before = now - MAX_DRIFT_S
        self._seen_auth_signatures = {
            key: seen_at
            for key, seen_at in self._seen_auth_signatures.items()
            if seen_at >= stale_before
        }
        cache_key = (timestamp, signature)
        if cache_key in self._seen_auth_signatures:
            return True
        self._seen_auth_signatures[cache_key] = now
        return False

    def _should_accept_new_vci_connection(
        self,
        new_addr: tuple[str, int] | None,
    ) -> tuple[bool, str, tuple[str, int] | None]:
        existing_writer = self.vci_writer
        if existing_writer is None:
            return True, "no_existing_tunnel", None

        existing_addr = existing_writer.get_extra_info("peername")
        is_closing = getattr(existing_writer, "is_closing", None)
        if callable(is_closing) and is_closing():
            return True, "existing_writer_closing", existing_addr
        if not self.vci_connected.is_set():
            return True, "existing_tunnel_inactive", existing_addr

        snapshot = self._tunnel_quality.snapshot()
        try:
            probe_failures = int(snapshot.get("probe_failures") or 0)
        except (TypeError, ValueError):
            probe_failures = 0
        has_probe_failures = probe_failures > 0 or snapshot.get("reason") == "probe_failures"
        if snapshot.get("connected") and snapshot.get("fresh") and not has_probe_failures:
            return False, "existing_tunnel_healthy", existing_addr
        if has_probe_failures:
            return True, "existing_tunnel_probe_failed", existing_addr
        return True, "existing_tunnel_stale", existing_addr

    async def _handle_vci_connection(self, reader: asyncio.StreamReader,
                                     writer: asyncio.StreamWriter):
        """处理 VCI Proxy 的连接"""
        addr = writer.get_extra_info('peername')
        disconnect_reason = "handler_exit"
        local_epoch: str | None = None
        logger.info("VCI tunnel connected: %s", addr)

        # Disable Nagle algorithm for lower latency
        sock = writer.get_extra_info('socket')
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        next_connection_counter = self._connection_counter + 1
        provisional_epoch = (
            f"epoch-{int(time.time() * 1000)}-{next_connection_counter:03d}"
        )

        # Authenticate before accepting the connection
        if not await self._authenticate_vci(
            reader,
            writer,
            connection_epoch_hint=provisional_epoch,
        ):
            logger.warning("VCI tunnel authentication failed: %s", addr)
            self._emit_tunnel_event(
                "tunnel.auth.failed",
                status="error",
                failure_code="auth_failed",
                failure_domain="cloud_proxy_tunnel",
                reason="authentication_failed",
                remote_addr=str(addr),
            )
            writer.close()
            return

        accept_new, decision_reason, existing_addr = self._should_accept_new_vci_connection(addr)
        if not accept_new:
            logger.warning(
                "Rejecting additional VCI tunnel: existing_epoch=%s existing_addr=%s new_addr=%s reason=%s",
                self._connection_epoch,
                existing_addr,
                addr,
                decision_reason,
            )
            self._emit_tunnel_event(
                "tunnel.lifecycle.rejected",
                connection_epoch=self._connection_epoch,
                status="error",
                failure_code="tunnel_rejected",
                failure_domain="cloud_proxy_tunnel",
                reason=decision_reason,
                remote_addr=str(addr),
                existing_addr=str(existing_addr),
            )
            writer.close()
            return

        # 关闭已有的 VCI 连接
        if self.vci_writer is not None:
            logger.warning(
                "Replacing inactive VCI tunnel: old_epoch=%s old_addr=%s new_addr=%s reason=%s",
                self._connection_epoch,
                existing_addr,
                addr,
                decision_reason,
            )
            self._emit_tunnel_event(
                "tunnel.lifecycle.replaced",
                connection_epoch=self._connection_epoch,
                status="error",
                failure_code="tunnel_replaced",
                failure_domain="cloud_proxy_tunnel",
                reason=decision_reason,
                remote_addr=str(addr),
                existing_addr=str(existing_addr),
            )
            old_writer = self.vci_writer
            self.vci_connected.clear()
            await self._cancel_probe_task_async()
            self._cancel_pending_futures()
            self.vci_reader = None
            self.vci_writer = None
            try:
                old_writer.close()
            except Exception:
                pass

        self.vci_reader = reader
        self.vci_writer = writer
        logger.info("VCI Proxy connected: %s", addr)
        self._connection_counter = next_connection_counter
        local_epoch = provisional_epoch
        self._connection_epoch = local_epoch
        self._tunnel_quality.mark_connected(local_epoch)
        self._write_tunnel_quality_snapshot()
        self._emit_tunnel_event(
            "tunnel.lifecycle.connected",
            connection_epoch=local_epoch,
            reason="tunnel_connected",
            remote_addr=str(addr),
        )
        self.vci_connected.set()
        self._probe_task = asyncio.create_task(self._probe_loop(local_epoch))

        try:
            while True:
                # 读取消息
                header = await reader.readexactly(HEADER_SIZE)
                magic, length, msg_type, sequence = struct.unpack('>IIHI', header)

                if magic != MAGIC:
                    disconnect_reason = f"invalid_magic:{magic:#x}"
                    logger.warning(f"无效的 Magic: {magic:#x}")
                    break

                try:
                    body = await _read_frame_body(reader, length)
                except (ValueError, TimeoutError, ConnectionError) as exc:
                    disconnect_reason = f"frame_rejected:{exc}"
                    logger.warning("VCI frame rejected: %s", exc)
                    break

                # 处理心跳消息 - 发送 ACK
                if msg_type == MsgType.HEARTBEAT:
                    logger.debug(f"收到心跳, seq={sequence}")
                    ack_header = struct.pack('>IIHI', MAGIC, HEADER_SIZE, MsgType.HEARTBEAT_ACK, sequence)
                    async with self.vci_lock:
                        writer.write(ack_header)
                        await writer.drain()
                    continue

                # 心跳 ACK - 忽略
                if msg_type == MsgType.HEARTBEAT_ACK:
                    logger.debug(f"收到心跳ACK, seq={sequence}")
                    continue

                # 查找对应的 Future（响应消息）
                if sequence in self.response_futures:
                    future = self.response_futures.pop(sequence)
                    prefetch_source = self._prefetch_bundle_source_by_proxy_seq.pop(
                        sequence,
                        None,
                    )
                    clean_body, hw_ms = strip_timing_trailer(body)
                    if msg_type in (MsgType.READ_MSGS_RSP, MsgType.WRITE_MSGS_RSP):
                        try:
                            clean_body, prefetch_bundle = strip_read_msgs_prefetch_bundle(
                                clean_body
                            )
                        except ValueError:
                            logger.warning(
                                "Discarding malformed ReadMsgs prefetch bundle seq=%s",
                                sequence,
                                exc_info=True,
                            )
                            if msg_type == MsgType.READ_MSGS_RSP:
                                try:
                                    return_code, messages = ProtocolDecoder.decode_read_msgs_rsp(
                                        clean_body
                                    )
                                    clean_body = ProtocolEncoder.encode_read_msgs_rsp(
                                        return_code,
                                        messages,
                                        sequence,
                                    )[HEADER_SIZE:]
                                except Exception:
                                    clean_body = clean_body[:8]
                            else:
                                clean_body = clean_body[:8]
                        else:
                            if (
                                prefetch_bundle is not None
                                and self.config.read_ahead.enabled
                            ):
                                empty_confirmations = 0
                                for read_rsp_body in prefetch_bundle.read_rsp_bodies:
                                    try:
                                        return_code, messages = (
                                            ProtocolDecoder.decode_read_msgs_rsp(
                                                read_rsp_body
                                            )
                                        )
                                    except Exception:
                                        continue
                                    if return_code == BUFFER_EMPTY and not messages:
                                        empty_confirmations += 1
                                if empty_confirmations:
                                    self._prefetch_empty_confirmations_by_proxy_seq[
                                        sequence
                                    ] = (
                                        prefetch_bundle.channel_id,
                                        empty_confirmations,
                                    )
                                recorded = self._prefetch_read_msgs.record_read_rsp_bodies(
                                    prefetch_bundle.channel_id,
                                    prefetch_bundle.read_rsp_bodies,
                                    source=prefetch_source,
                                )
                                if recorded:
                                    self._record_prefetch_record_observation(
                                        prefetch_bundle.channel_id,
                                        message_count=recorded,
                                        source=prefetch_source,
                                        proxy_seq=sequence,
                                    )
                                    logger.debug(
                                        "Recorded %s prefetched ReadMsgs message(s) for channel_id=%s",
                                        recorded,
                                        prefetch_bundle.channel_id,
                                    )
                    future.set_result((msg_type, clean_body, hw_ms))
                else:
                    logger.warning(f"收到未知消息: type={msg_type:#x}, seq={sequence}")

        except asyncio.CancelledError:
            self._shutting_down = True
            disconnect_reason = "shutdown_cancelled"
        except asyncio.IncompleteReadError as exc:
            disconnect_reason = f"eof expected={exc.expected} partial={len(exc.partial)}"
        except (ConnectionResetError, BrokenPipeError, OSError) as exc:
            disconnect_reason = f"connection_lost:{type(exc).__name__}:{exc}"
            logger.warning("VCI tunnel lost connection: addr=%s error=%s", addr, exc)
        except Exception as e:
            disconnect_reason = f"exception:{type(e).__name__}:{e}"
            logger.exception(f"VCI 连接错误: {e}")
        finally:
            owns_current_tunnel = self.vci_writer is writer
            if owns_current_tunnel:
                self._vci_write_collect_supported = False
                self._vci_read_collect_supported = False
                self._vci_sweep_shadow_supported = False
                self._cancel_sweep_plan("connection_epoch_changed")
                self.vci_connected.clear()
                self._cancel_pending_futures()
                await self._cancel_probe_task_async()
                self._tunnel_quality.mark_disconnected(local_epoch)
                self._write_tunnel_quality_snapshot()
                self.vci_reader = None
                self.vci_writer = None
            else:
                logger.info(
                    "VCI tunnel handler exited after ownership changed: addr=%s epoch=%s current_epoch=%s",
                    addr,
                    local_epoch,
                    self._connection_epoch,
                )
            shutdown_disconnect = self._shutting_down and disconnect_reason in {
                "handler_exit",
                "shutdown_cancelled",
                "cancelled",
            }
            if shutdown_disconnect:
                logger.info(
                    "VCI tunnel handler stopping during server shutdown: addr=%s epoch=%s reason=%s",
                    addr,
                    local_epoch,
                    disconnect_reason,
                )
            else:
                logger.log(
                    logging.ERROR if disconnect_reason.startswith("exception:") else logging.WARNING,
                    "VCI tunnel disconnected: addr=%s epoch=%s reason=%s",
                    addr,
                    local_epoch,
                    disconnect_reason,
                )
                self._emit_tunnel_event(
                    "tunnel.lifecycle.disconnected",
                    connection_epoch=local_epoch,
                    status="error" if disconnect_reason != "handler_exit" else "ok",
                    failure_code=disconnect_reason.split(":", 1)[0] if disconnect_reason else None,
                    failure_domain="cloud_proxy_tunnel",
                    reason=disconnect_reason,
                    remote_addr=str(addr),
                )
            writer.close()

    @staticmethod
    def _frame_response(resp_type: int, resp_body: bytes, sequence: int) -> bytes:
        return Message(resp_type, sequence, resp_body).encode()

    @staticmethod
    def _prefetch_drain_fields(drain: PrefetchReadMsgsDrain) -> dict[str, object]:
        fields: dict[str, object] = {
            "prefetch_fifo_pending_before": drain.pending_before,
            "prefetch_fifo_pending_after": drain.pending_after,
            "prefetch_requested_count": drain.requested_count,
            "prefetch_served_count": drain.served_count,
            "prefetch_underfill_count": drain.underfill_count,
        }
        if drain.served_count > 0:
            fields["prefetch_source_counts"] = dict(drain.source_counts)
            if drain.age_min_ms is not None:
                fields.update(
                    {
                        "prefetch_age_min_ms": drain.age_min_ms,
                        "prefetch_age_avg_ms": drain.age_avg_ms,
                        "prefetch_age_max_ms": drain.age_max_ms,
                    }
                )
        return fields

    @staticmethod
    def _observation_age_ms(observed_mono: float, now: float) -> float:
        return round(max(0.0, (now - observed_mono) * 1000.0), 3)

    def _record_prefetch_record_observation(
        self,
        channel_id: int,
        *,
        message_count: int,
        source: str | None,
        proxy_seq: int | None,
    ) -> None:
        self._last_prefetch_record_by_channel[channel_id] = _PrefetchRecordObservation(
            message_count=max(0, int(message_count)),
            source=source,
            proxy_seq=proxy_seq,
            observed_mono=time.monotonic(),
        )

    def _record_prefetch_drain_observation(
        self,
        drain: PrefetchReadMsgsDrain,
        *,
        reason: str,
    ) -> None:
        self._last_prefetch_drain_by_channel[drain.channel_id] = _PrefetchDrainObservation(
            reason=reason,
            requested_count=drain.requested_count,
            served_count=drain.served_count,
            pending_before=drain.pending_before,
            pending_after=drain.pending_after,
            observed_mono=time.monotonic(),
        )
        self._confirmed_empty_deepened_drain_mono_by_channel.pop(drain.channel_id, None)

    def _record_read_result_observation(
        self,
        channel_id: int,
        *,
        return_code: int,
        message_count: int,
        dll_seq: int | None,
        proxy_seq: int | None,
    ) -> None:
        result = "data" if message_count else "empty"
        if return_code not in {0, BUFFER_EMPTY} and message_count == 0:
            result = "error"
        self._last_read_result_by_channel[channel_id] = _ReadResultObservation(
            result=result,
            return_code=int(return_code),
            message_count=max(0, int(message_count)),
            dll_seq=dll_seq,
            proxy_seq=proxy_seq,
            observed_mono=time.monotonic(),
        )

    def _apply_prefetch_empty_confirmations(self, proxy_seq: int) -> dict[str, object]:
        pending = self._prefetch_empty_confirmations_by_proxy_seq.pop(proxy_seq, None)
        if pending is None:
            return {}
        channel_id, count = pending
        fields: dict[str, object] = {
            "prefetch_empty_confirmation_count": count,
        }
        if count <= 0:
            return fields
        self._read_cache.record_result(
            channel_id,
            BUFFER_EMPTY,
            message_count=0,
        )
        self._record_read_result_observation(
            channel_id,
            return_code=BUFFER_EMPTY,
            message_count=0,
            dll_seq=None,
            proxy_seq=proxy_seq,
        )
        fields["prefetch_empty_cache_recorded"] = True
        return fields

    def _read_collect_blocked_reason(self, msg_type: int, body: bytes) -> str | None:
        if msg_type != MsgType.READ_MSGS_REQ:
            return "not_read_msgs_req"
        read_ahead = self.config.read_ahead
        if not read_ahead.enabled:
            return "read_ahead_disabled"
        if not read_ahead.transaction_enabled:
            return "read_ahead_transaction_disabled"
        if not self._vci_read_collect_supported:
            return "client_read_collect_not_supported"
        if read_ahead.window_ms <= 0:
            return "read_ahead_window_disabled"
        if read_ahead.max_reads <= 0:
            return "read_ahead_max_reads_disabled"
        if read_ahead.max_messages <= 0:
            return "read_ahead_max_messages_disabled"
        try:
            _channel_id, num_msgs, timeout = ProtocolDecoder.decode_read_msgs_req(body)
        except Exception:
            return "decode_error"
        if timeout > 0:
            return "blocking_read"
        if num_msgs <= 0:
            return "zero_requested_messages"
        return None

    def _prefetch_miss_detail(
        self,
        channel_id: int,
        drain: PrefetchReadMsgsDrain,
        *,
        read_collect_blocked_reason: str | None,
    ) -> str:
        if not self.config.read_ahead.enabled or not self._prefetch_read_msgs.enabled:
            return "fifo_disabled"
        if self.config.read_ahead.max_messages <= 0:
            return "fifo_capacity_zero"
        if drain.pending_before > 0:
            return "fifo_underfilled"
        last_read = self._last_read_result_by_channel.get(channel_id)
        if last_read is not None and last_read.result == "empty":
            return "fifo_empty_after_confirmed_empty"
        last_drain = self._last_prefetch_drain_by_channel.get(channel_id)
        if (
            last_drain is not None
            and last_drain.served_count > 0
            and last_drain.pending_after == 0
        ):
            return "fifo_empty_after_prefetch_exhausted"
        if self._last_prefetch_record_by_channel.get(channel_id) is not None:
            return "fifo_empty_after_prefetch_recorded"
        if read_collect_blocked_reason is not None:
            return "fifo_empty_read_collect_unavailable"
        return "fifo_empty_no_prior_prefetch"

    def _prefetch_state_fields(
        self,
        channel_id: int,
        *,
        msg_type: int,
        body: bytes,
        timeout_ms: int,
        drain: PrefetchReadMsgsDrain,
    ) -> dict[str, object]:
        now = time.monotonic()
        read_collect_blocked = self._read_collect_blocked_reason(msg_type, body)
        fields: dict[str, object] = {
            "prefetch_fifo_enabled": self._prefetch_read_msgs.enabled,
            "prefetch_fifo_capacity": self._prefetch_read_msgs.max_messages,
            "read_collect_supported": self._vci_read_collect_supported,
            "write_collect_supported": self._vci_write_collect_supported,
            "read_collect_eligible": read_collect_blocked is None,
        }
        if read_collect_blocked is not None:
            fields["read_collect_blocked_reason"] = read_collect_blocked

        fields.update(self._read_cache.observability_state(channel_id, timeout_ms, now=now))

        last_record = self._last_prefetch_record_by_channel.get(channel_id)
        if last_record is not None:
            fields.update(
                {
                    "prefetch_last_record_age_ms": self._observation_age_ms(
                        last_record.observed_mono,
                        now,
                    ),
                    "prefetch_last_record_count": last_record.message_count,
                    "prefetch_last_record_source": last_record.source,
                    "prefetch_last_record_proxy_seq": last_record.proxy_seq,
                }
            )

        last_drain = self._last_prefetch_drain_by_channel.get(channel_id)
        if last_drain is not None:
            fields.update(
                {
                    "prefetch_last_drain_age_ms": self._observation_age_ms(
                        last_drain.observed_mono,
                        now,
                    ),
                    "prefetch_last_drain_reason": last_drain.reason,
                    "prefetch_last_drain_requested_count": last_drain.requested_count,
                    "prefetch_last_drain_served_count": last_drain.served_count,
                    "prefetch_last_drain_pending_before": last_drain.pending_before,
                    "prefetch_last_drain_pending_after": last_drain.pending_after,
                }
            )

        last_read = self._last_read_result_by_channel.get(channel_id)
        if last_read is not None:
            fields.update(
                {
                    "prefetch_last_real_read_age_ms": self._observation_age_ms(
                        last_read.observed_mono,
                        now,
                    ),
                    "prefetch_last_real_read_result": last_read.result,
                    "prefetch_last_real_read_return_code": last_read.return_code,
                    "prefetch_last_real_read_message_count": last_read.message_count,
                    "prefetch_last_real_read_dll_seq": last_read.dll_seq,
                    "prefetch_last_real_read_proxy_seq": last_read.proxy_seq,
                }
            )

        if drain.is_empty:
            fields["prefetch_miss_detail"] = self._prefetch_miss_detail(
                channel_id,
                drain,
                read_collect_blocked_reason=read_collect_blocked,
            )
        return fields

    def _should_serve_partial_prefetch_without_tunnel(
        self,
        drain: PrefetchReadMsgsDrain,
        *,
        timeout_ms: int,
    ) -> bool:
        return (
            drain.is_partial
            and timeout_ms <= 0
            and drain.requested_count > max(0, int(self.config.read_ahead.max_messages))
        )

    def _should_merge_partial_prefetch_with_tunnel(
        self,
        msg_type: int,
        body: bytes,
        drain: PrefetchReadMsgsDrain,
        *,
        timeout_ms: int,
    ) -> bool:
        if not drain.is_partial or timeout_ms > 0:
            return False
        if self._read_collect_blocked_reason(msg_type, body) is None:
            return True
        return not self._should_serve_partial_prefetch_without_tunnel(
            drain,
            timeout_ms=timeout_ms,
        )

    def _prefetch_read_lock(self, channel_id: int) -> asyncio.Lock:
        lock = self._prefetch_read_locks.get(channel_id)
        if lock is None:
            lock = asyncio.Lock()
            self._prefetch_read_locks[channel_id] = lock
        return lock

    def _finalize_prefetch_underfill_response(
        self,
        drain: PrefetchReadMsgsDrain,
        *,
        resp_type: int,
        resp_body: bytes,
        sequence: int,
    ) -> tuple[int, bytes, str, dict[str, object], bool]:
        """Merge a partial FIFO drain with the reduced tunnel response.

        Returns (final_type, final_body, reason, fields, should_restore_fifo).
        """
        fields: dict[str, object] = {}
        if resp_type != MsgType.READ_MSGS_RSP:
            fields["prefetch_merge_unexpected_resp_type"] = int(resp_type)
            return (
                resp_type,
                resp_body,
                "prefetch_underfill_tunnel_unexpected_response",
                fields,
                True,
            )

        try:
            return_code, tunnel_messages = ProtocolDecoder.decode_read_msgs_rsp(resp_body)
        except Exception:
            fields["prefetch_merge_decode_error"] = True
            return (
                resp_type,
                resp_body,
                "prefetch_underfill_tunnel_decode_error",
                fields,
                True,
            )

        fields.update(
            {
                "prefetch_merge_tunnel_return_code": return_code,
                "prefetch_merge_tunnel_message_count": len(tunnel_messages),
            }
        )

        if return_code == 0:
            limited_tunnel_messages = tunnel_messages[:drain.underfill_count]
            fields["prefetch_merge_tunnel_limited_message_count"] = len(
                limited_tunnel_messages
            )
            merged_messages = list(drain.messages) + limited_tunnel_messages
            fields["prefetch_merge_final_message_count"] = len(merged_messages)
            final_body = ProtocolEncoder.encode_read_msgs_rsp(
                0,
                merged_messages,
                sequence,
            )[HEADER_SIZE:]
            return (
                MsgType.READ_MSGS_RSP,
                final_body,
                "prefetch_merge_tunnel_data",
                fields,
                False,
            )

        if return_code == BUFFER_EMPTY:
            merged_messages = list(drain.messages)
            fields["prefetch_merge_final_message_count"] = len(merged_messages)
            final_body = ProtocolEncoder.encode_read_msgs_rsp(
                0,
                merged_messages,
                sequence,
            )[HEADER_SIZE:]
            return (
                MsgType.READ_MSGS_RSP,
                final_body,
                "prefetch_merge_tunnel_empty",
                fields,
                False,
            )

        return (
            resp_type,
            resp_body,
            "prefetch_underfill_tunnel_error",
            fields,
            True,
        )

    async def _serve_prefetch_underfill(
        self,
        *,
        writer: asyncio.StreamWriter,
        body: bytes,
        sequence: int,
        msg_name: str,
        started_at_s: float,
        request_fields: dict[str, object],
        drain: PrefetchReadMsgsDrain,
    ) -> bool:
        remaining = drain.underfill_count
        merge_fields = {
            **self._prefetch_drain_fields(drain),
            "prefetch_merge_requested_count": remaining,
        }
        event_fields = {**request_fields, **merge_fields}
        self._emit_proxy_request_event(
            "proxy.request.cache_decision",
            dll_seq=sequence,
            msg_name=msg_name,
            cache_hit=False,
            reason="prefetch_underfill_forwarded",
            **event_fields,
        )

        new_seq = self._next_sequence()
        fwd_start = time.monotonic()
        future = asyncio.get_running_loop().create_future()
        self.response_futures[new_seq] = future
        tunnel_request = ProtocolEncoder.encode_read_msgs_req(
            drain.channel_id,
            num_msgs=remaining,
            timeout=int(request_fields.get("timeout", 0)),
            sequence=new_seq,
        )
        fwd_msg_type = MsgType.READ_MSGS_REQ
        fwd_reason = "prefetch_underfill_forwarded"
        transaction_fields: dict[str, object] = {}
        should_collect_underfill_tail = (
            drain.requested_count > max(0, int(self.config.read_ahead.max_messages))
            and self._read_collect_transaction_base_allowed(MsgType.READ_MSGS_REQ, body)
        )
        if should_collect_underfill_tail:
            tunnel_request, fwd_msg_type, _fwd_body, transaction_reason = (
                self._build_tunnel_request(
                    MsgType.READ_MSGS_REQ,
                    ProtocolEncoder.encode_read_msgs_req(
                        drain.channel_id,
                        num_msgs=min(
                            remaining,
                            OVERSIZED_PARTIAL_MERGE_COUNT_CAP,
                        ),
                        timeout=int(request_fields.get("timeout", 0)),
                        sequence=sequence,
                    )[HEADER_SIZE:],
                    new_seq,
                    transaction_fields=transaction_fields,
                )
            )
            if transaction_reason is not None:
                fwd_reason = f"prefetch_underfill_{transaction_reason}"
            if fwd_msg_type == MsgType.READ_AND_COLLECT_READS_REQ:
                self._prefetch_bundle_source_by_proxy_seq[new_seq] = "read_collect"
                event_fields.update(transaction_fields)

        try:
            async with self.vci_lock:
                if self.vci_writer is None:
                    raise ConnectionError("VCI tunnel disconnected")
                self.vci_writer.write(tunnel_request)
                await self.vci_writer.drain()
            self._emit_proxy_request_event(
                "proxy.request.forwarded_to_tunnel",
                dll_seq=sequence,
                proxy_seq=new_seq,
                msg_name=msg_name,
                reason=fwd_reason,
                forwarded_msg_type=int(fwd_msg_type),
                forwarded_msg_name=MSG_NAMES.get(fwd_msg_type),
                reduced_num_msgs=remaining,
                original_num_msgs=drain.requested_count,
                **event_fields,
                **self._read_ahead_transaction_guard_fields(),
            )
        except Exception as exc:
            logger.error("Failed to forward reduced ReadMsgs request: %s", exc)
            self.response_futures.pop(new_seq, None)
            self._prefetch_bundle_source_by_proxy_seq.pop(new_seq, None)
            self._prefetch_empty_confirmations_by_proxy_seq.pop(new_seq, None)
            restored = self._prefetch_read_msgs.restore_front(
                drain.channel_id,
                drain.messages,
            )
            self._emit_proxy_request_event(
                "proxy.request.failed",
                dll_seq=sequence,
                proxy_seq=new_seq,
                msg_name=msg_name,
                status="error",
                failure_code="forward_failed",
                failure_domain="cloud_proxy_tunnel",
                reason=str(exc),
                prefetch_restored_count=restored,
                **event_fields,
            )
            return False

        try:
            resp_type, resp_body, hw_ms = await asyncio.wait_for(future, timeout=30.0)
        except (asyncio.TimeoutError, ConnectionError) as exc:
            self.response_futures.pop(new_seq, None)
            self._prefetch_bundle_source_by_proxy_seq.pop(new_seq, None)
            self._prefetch_empty_confirmations_by_proxy_seq.pop(new_seq, None)
            fwd_ms = (time.monotonic() - fwd_start) * 1000
            restored = self._prefetch_read_msgs.restore_front(
                drain.channel_id,
                drain.messages,
            )
            reason = "VCI_DISCONNECTED" if isinstance(exc, ConnectionError) else "TIMEOUT"
            self._cancel_sweep_plan("gds2_communication_error")
            self._record_benchmark_event(
                started_at_s=started_at_s,
                duration_ms=fwd_ms,
                msg_type=MsgType.READ_MSGS_REQ,
                req_body=body,
                resp_type=None,
                resp_body=b"",
                cache_hit=False,
                status=reason.lower(),
            )
            self._emit_proxy_request_event(
                "proxy.request.timeout" if isinstance(exc, asyncio.TimeoutError) else "proxy.request.failed",
                dll_seq=sequence,
                proxy_seq=new_seq,
                msg_name=msg_name,
                status="error",
                failure_code="timeout" if isinstance(exc, asyncio.TimeoutError) else "vci_disconnected",
                failure_domain="cloud_proxy_tunnel",
                reason="wait_response_timeout" if isinstance(exc, asyncio.TimeoutError) else "wait_response_connection_lost",
                duration_ms=fwd_ms,
                prefetch_restored_count=restored,
                **event_fields,
            )
            logger.error("[PROXY] %s seq=%s %s after %.0fms", msg_name, sequence, reason, fwd_ms)
            return False

        fwd_ms = (time.monotonic() - fwd_start) * 1000
        network_ms = max(0.0, fwd_ms - float(hw_ms or 0.0))
        self._arm_read_ahead_transaction_guard(
            network_ms=network_ms,
            duration_ms=fwd_ms,
            dll_seq=sequence,
            proxy_seq=new_seq,
            msg_name=msg_name,
            request_fields=event_fields,
        )
        final_type, final_body, reason, final_fields, should_restore = (
            self._finalize_prefetch_underfill_response(
                drain,
                resp_type=resp_type,
                resp_body=resp_body,
                sequence=sequence,
            )
        )
        if should_restore:
            final_fields["prefetch_restored_count"] = self._prefetch_read_msgs.restore_front(
                drain.channel_id,
                drain.messages,
            )

        self._observe_sweep_write_response(
            MsgType.READ_MSGS_REQ,
            body,
            final_type,
            final_body,
            dll_seq=sequence,
            msg_name=msg_name,
            duration_ms=fwd_ms,
            network_ms=network_ms,
        )
        if final_type == MsgType.READ_MSGS_RSP:
            self._record_in_caches(
                MsgType.READ_MSGS_REQ,
                body,
                final_type,
                final_body,
                ioctl_id=None,
                dll_seq=sequence,
                proxy_seq=new_seq,
            )
        final_fields.update(self._apply_prefetch_empty_confirmations(new_seq))
        response_fields = self._response_observability_fields(final_type, final_body)
        self._augment_read_payload_delta_fields(request_fields, response_fields)
        self._observe_sweep_read_response(
            MsgType.READ_MSGS_REQ,
            body,
            final_type,
            final_body,
            dll_seq=sequence,
            msg_name=msg_name,
            duration_ms=fwd_ms,
            network_ms=network_ms,
            cache_hit=False,
        )
        self._record_benchmark_event(
            started_at_s=started_at_s,
            duration_ms=fwd_ms,
            msg_type=MsgType.READ_MSGS_REQ,
            req_body=body,
            resp_type=final_type,
            resp_body=final_body,
            cache_hit=False,
            status="success",
            hw_ms=hw_ms,
        )
        self._emit_proxy_request_event(
            "proxy.request.response_received",
            dll_seq=sequence,
            proxy_seq=new_seq,
            msg_name=msg_name,
            duration_ms=fwd_ms,
            hw_ms=hw_ms,
            network_ms=network_ms,
            reason=reason,
            **event_fields,
            **final_fields,
            **response_fields,
        )

        writer.write(self._frame_response(final_type, final_body, sequence))
        await writer.drain()
        self._emit_proxy_request_event(
            "proxy.request.replied_to_dll",
            dll_seq=sequence,
            proxy_seq=new_seq,
            msg_name=msg_name,
            duration_ms=fwd_ms,
            hw_ms=hw_ms,
            network_ms=network_ms,
            reason=reason,
            **event_fields,
            **final_fields,
            **response_fields,
        )
        self._schedule_sweep_poll()

        if fwd_ms > 1000:
            logger.warning(
                "Slow proxy request: %s seq=%s duration=%.1fms",
                msg_name,
                sequence,
                fwd_ms,
            )
        return True

    def _try_serve_cached(self, msg_type: int, body: bytes,
                          sequence: int) -> tuple[Optional[bytes], Optional[int], str | None]:
        """Try to serve the request from cache.

        Returns (cached_response, ioctl_id, cache_reason).
        cached_response is None if cache miss.
        ioctl_id is set when msg_type is IOCTL_REQ (needed for recording later).
        """
        ioctl_id = None

        if msg_type == MsgType.READ_MSGS_REQ:
            channel_id, num_msgs, timeout = ProtocolDecoder.decode_read_msgs_req(body)
            cached = self._read_cache.try_serve_from_cache(
                channel_id, num_msgs, timeout, sequence
            )
            if cached is not None:
                return cached, ioctl_id, "empty_cache_hit"

        elif msg_type == MsgType.START_FILTER_REQ:
            cached = self._filter_cache.try_dedup(body, sequence)
            if cached is not None:
                return cached, ioctl_id, "cache_hit"

        elif msg_type == MsgType.IOCTL_REQ:
            channel_id, ioctl_id, _input = ProtocolDecoder.decode_ioctl_req(body)
            cached = self._ioctl_cache.try_get_cached(channel_id, ioctl_id)
            if cached is not None:
                ret, output_data = cached
                resp = ProtocolEncoder.encode_ioctl_rsp(ret, output_data, sequence)
                return resp, ioctl_id, "cache_hit"

        return None, ioctl_id, None

    def _invalidate_caches(self, msg_type: int, body: bytes, sequence: int | None = None):
        """Invalidate caches based on the request type."""
        if msg_type == MsgType.DISCONNECT_REQ:
            channel_id = ProtocolDecoder.decode_disconnect_req(body)
            self._cancel_sweep_plan("disconnect_req", channel_id=channel_id)
            self._active_replay_pending_by_channel.pop(channel_id, None)
            self._read_cache.invalidate_channel(channel_id)
            self._filter_cache.invalidate_channel(channel_id)
            self._ioctl_cache.invalidate_channel(channel_id)
            self._prefetch_read_msgs.clear_channel(channel_id)
            self._last_write_by_channel.pop(channel_id, None)
            self._last_live_request_by_channel.pop(channel_id, None)
            self._last_read_payload_by_channel.pop(channel_id, None)
            self._last_prefetch_record_by_channel.pop(channel_id, None)
            self._last_prefetch_drain_by_channel.pop(channel_id, None)
            self._last_read_result_by_channel.pop(channel_id, None)
        elif msg_type == MsgType.CLOSE_REQ:
            self._cancel_sweep_plan("close_req")
            self._active_replay_pending_by_channel.clear()
            self._read_cache.clear()
            self._filter_cache.clear()
            self._ioctl_cache.invalidate()
            self._prefetch_read_msgs.clear()
            self._last_write_by_channel.clear()
            self._last_live_request_by_channel.clear()
            self._last_read_payload_by_channel.clear()
            self._last_prefetch_record_by_channel.clear()
            self._last_prefetch_drain_by_channel.clear()
            self._last_read_result_by_channel.clear()
        elif msg_type == MsgType.WRITE_MSGS_REQ:
            channel_id, _messages, _timeout = ProtocolDecoder.decode_write_msgs_req(body)
            now = time.monotonic()
            self._read_cache.record_write(channel_id, now=now)
            self._last_write_by_channel[channel_id] = (sequence, now)
        elif msg_type == MsgType.START_FILTER_REQ:
            channel_id, _filter_type, _mask, _pattern, _flow = (
                ProtocolDecoder.decode_start_filter_req(body)
            )
            self._cancel_sweep_plan("start_filter_req", channel_id=channel_id)
            self._active_replay_pending_by_channel.pop(channel_id, None)
            self._read_cache.mark_channel_active(channel_id, invalidate_empty=True)
            self._prefetch_read_msgs.clear_channel(channel_id)
            self._last_read_payload_by_channel.pop(channel_id, None)
            self._last_prefetch_record_by_channel.pop(channel_id, None)
            self._last_prefetch_drain_by_channel.pop(channel_id, None)
            self._last_read_result_by_channel.pop(channel_id, None)
        elif msg_type == MsgType.STOP_FILTER_REQ:
            channel_id, filter_id = ProtocolDecoder.decode_stop_filter_req(body)
            self._cancel_sweep_plan("stop_filter_req", channel_id=channel_id)
            self._active_replay_pending_by_channel.pop(channel_id, None)
            self._read_cache.mark_channel_active(channel_id, invalidate_empty=True)
            self._prefetch_read_msgs.clear_channel(channel_id)
            self._last_read_payload_by_channel.pop(channel_id, None)
            self._last_prefetch_record_by_channel.pop(channel_id, None)
            self._last_prefetch_drain_by_channel.pop(channel_id, None)
            self._last_read_result_by_channel.pop(channel_id, None)
            self._filter_cache.on_stop_filter(filter_id)
        elif msg_type == MsgType.IOCTL_REQ:
            channel_id, ioctl_id, _input = ProtocolDecoder.decode_ioctl_req(body)
            if not self._ioctl_cache.is_cacheable(ioctl_id):
                self._cancel_sweep_plan("mutating_or_non_cacheable_ioctl", channel_id=channel_id)
                self._active_replay_pending_by_channel.pop(channel_id, None)
                self._read_cache.mark_channel_active(channel_id, invalidate_empty=True)
                self._prefetch_read_msgs.clear_channel(channel_id)
                self._last_read_payload_by_channel.pop(channel_id, None)
                self._last_prefetch_record_by_channel.pop(channel_id, None)
                self._last_prefetch_drain_by_channel.pop(channel_id, None)
                self._last_read_result_by_channel.pop(channel_id, None)

    def _clear_prefetch_after_failed_write(self, msg_type: int, body: bytes) -> None:
        if msg_type != MsgType.WRITE_MSGS_REQ:
            return
        try:
            channel_id, _messages, _timeout = ProtocolDecoder.decode_write_msgs_req(body)
        except Exception:
            return
        self._prefetch_read_msgs.clear_channel(channel_id)
        self._last_prefetch_record_by_channel.pop(channel_id, None)
        self._last_prefetch_drain_by_channel.pop(channel_id, None)

    def _write_collect_transaction_base_allowed(self, msg_type: int, body: bytes) -> bool:
        if msg_type != MsgType.WRITE_MSGS_REQ:
            return False
        read_ahead = self.config.read_ahead
        if not (
            read_ahead.enabled
            and read_ahead.transaction_enabled
            and self._vci_write_collect_supported
            and read_ahead.window_ms > 0
            and read_ahead.write_collect_max_reads > 0
            and read_ahead.max_messages > 0
        ):
            return False
        try:
            ProtocolDecoder.decode_write_msgs_req(body)
        except Exception:
            return False
        return True

    def _read_collect_transaction_base_allowed(self, msg_type: int, body: bytes) -> bool:
        if msg_type != MsgType.READ_MSGS_REQ:
            return False
        read_ahead = self.config.read_ahead
        if not (
            read_ahead.enabled
            and read_ahead.transaction_enabled
            and self._vci_read_collect_supported
            and read_ahead.window_ms > 0
            and read_ahead.max_reads > 0
            and read_ahead.max_messages > 0
        ):
            return False
        try:
            _channel_id, num_msgs, timeout = ProtocolDecoder.decode_read_msgs_req(body)
        except Exception:
            return False
        return timeout <= 0 and num_msgs > 0

    def _read_ahead_transaction_guard_active(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return self._read_ahead_transaction_guard_until_mono > now

    def _read_ahead_transaction_guard_fields(
        self,
        now: float | None = None,
    ) -> dict[str, object]:
        now = time.monotonic() if now is None else now
        active = self._read_ahead_transaction_guard_active(now)
        fields: dict[str, object] = {
            "read_ahead_transaction_guard_active": active,
            "read_ahead_transaction_max_network_ms": (
                self.config.read_ahead.transaction_max_network_ms
            ),
            "read_ahead_transaction_cooldown_ms": (
                self.config.read_ahead.transaction_cooldown_ms
            ),
        }
        if active:
            fields.update(
                {
                    "read_ahead_transaction_guard_reason": (
                        self._read_ahead_transaction_guard_reason
                    ),
                    "read_ahead_transaction_guard_network_ms": (
                        self._read_ahead_transaction_guard_network_ms
                    ),
                    "read_ahead_transaction_guard_remaining_ms": round(
                        max(
                            0.0,
                            (
                                self._read_ahead_transaction_guard_until_mono - now
                            )
                            * 1000.0,
                        ),
                        3,
                    ),
                }
            )
        return fields

    def _arm_read_ahead_transaction_guard(
        self,
        *,
        network_ms: float | None,
        duration_ms: float | None,
        dll_seq: int,
        proxy_seq: int,
        msg_name: str,
        request_fields: dict[str, object],
    ) -> None:
        read_ahead = self.config.read_ahead
        threshold_ms = int(read_ahead.transaction_max_network_ms)
        cooldown_ms = int(read_ahead.transaction_cooldown_ms)
        if (
            not read_ahead.enabled
            or not read_ahead.transaction_enabled
            or threshold_ms <= 0
            or cooldown_ms <= 0
            or network_ms is None
            or network_ms < threshold_ms
        ):
            return

        now = time.monotonic()
        was_active = self._read_ahead_transaction_guard_active(now)
        self._read_ahead_transaction_guard_until_mono = max(
            self._read_ahead_transaction_guard_until_mono,
            now + cooldown_ms / 1000.0,
        )
        self._read_ahead_transaction_guard_reason = "slow_tunnel_response"
        self._read_ahead_transaction_guard_network_ms = round(float(network_ms), 3)
        if was_active:
            return

        self._emit_proxy_request_event(
            "read_ahead.transaction.guard_armed",
            dll_seq=dll_seq,
            proxy_seq=proxy_seq,
            msg_name=msg_name,
            reason="slow_tunnel_response",
            duration_ms=duration_ms,
            network_ms=network_ms,
            read_ahead_transaction_guard_trigger_network_ms=round(float(network_ms), 3),
            **request_fields,
            **self._read_ahead_transaction_guard_fields(now),
        )

    def _should_use_write_collect_transaction(self, msg_type: int, body: bytes) -> bool:
        return self._write_collect_transaction_base_allowed(
            msg_type,
            body,
        ) and not self._read_ahead_transaction_guard_active()

    def _should_use_read_collect_transaction(self, msg_type: int, body: bytes) -> bool:
        return self._read_collect_transaction_base_allowed(
            msg_type,
            body,
        )

    def _read_collect_transaction_budget(
        self,
        channel_id: int,
        *,
        now: float | None = None,
    ) -> _ReadCollectTransactionBudget:
        read_ahead = self.config.read_ahead
        collect_window_ms = min(
            max(0, int(read_ahead.window_ms)),
            READ_COLLECT_BASE_WINDOW_CAP_MS,
        )
        max_reads = min(
            max(0, int(read_ahead.max_reads)),
            READ_COLLECT_BASE_MAX_READS_CAP,
        )
        max_messages = min(max(0, int(read_ahead.max_messages)), 16)
        budget = _ReadCollectTransactionBudget(
            collect_window_ms=collect_window_ms,
            max_reads=max_reads,
            read_timeout_ms=0,
            max_messages=max_messages,
            reason="standard_read_tail",
        )
        if max_reads <= 0 or max_messages <= 0:
            return budget
        if self._read_ahead_transaction_guard_active(now=now):
            return budget

        ts = time.monotonic() if now is None else now
        last_read = self._last_read_result_by_channel.get(channel_id)
        last_empty_mono = (
            last_read.observed_mono
            if last_read is not None and last_read.result == "empty"
            else None
        )

        deep_reason: str | None = None
        deepened_drain_mono: float | None = None
        last_drain = self._last_prefetch_drain_by_channel.get(channel_id)
        if last_empty_mono is not None and last_drain is not None:
            empty_age_ms = self._observation_age_ms(last_empty_mono, ts)
            drain_age_ms = self._observation_age_ms(last_drain.observed_mono, ts)
            already_deepened_drain_mono = (
                self._confirmed_empty_deepened_drain_mono_by_channel.get(channel_id)
            )
            if (
                last_drain.served_count > 0
                and last_drain.pending_after == 0
                and last_drain.observed_mono <= last_empty_mono
                and already_deepened_drain_mono != last_drain.observed_mono
                and empty_age_ms <= READ_COLLECT_DEEP_CONFIRMED_EMPTY_AFTER_PREFETCH_MS
                and drain_age_ms <= READ_COLLECT_DEEP_RECENT_PREFETCH_MS
            ):
                deep_reason = "after_confirmed_empty_following_prefetch_drain"
                deepened_drain_mono = last_drain.observed_mono

        if (
            deep_reason is None
            and last_drain is not None
            and last_drain.served_count > 0
            and last_drain.pending_after == 0
            and (
                last_empty_mono is None
                or last_drain.observed_mono >= last_empty_mono
            )
            and self._observation_age_ms(last_drain.observed_mono, ts)
            <= READ_COLLECT_DEEP_RECENT_PREFETCH_MS
        ):
            deep_reason = f"after_{last_drain.reason}"

        if deep_reason is None:
            last_record = self._last_prefetch_record_by_channel.get(channel_id)
            if (
                last_record is not None
                and last_record.message_count > 0
                and (
                    last_empty_mono is None
                    or last_record.observed_mono >= last_empty_mono
                )
                and self._observation_age_ms(last_record.observed_mono, ts)
                <= READ_COLLECT_DEEP_RECENT_PREFETCH_MS
            ):
                deep_reason = "after_prefetch_record"

        if deep_reason is None:
            return budget

        deep_max_reads = min(
            max(
                max_reads,
                int(read_ahead.write_collect_max_reads),
            ),
            READ_COLLECT_DEEP_MAX_READS_CAP,
        )
        if deep_max_reads <= max_reads:
            return budget

        if deepened_drain_mono is not None:
            self._confirmed_empty_deepened_drain_mono_by_channel[channel_id] = (
                deepened_drain_mono
            )

        return _ReadCollectTransactionBudget(
            collect_window_ms=min(
                max(collect_window_ms, max(0, int(read_ahead.min_drain_ms))),
                max(0, int(read_ahead.window_ms)),
            ),
            max_reads=deep_max_reads,
            read_timeout_ms=0,
            max_messages=max_messages,
            reason=deep_reason,
        )

    def _build_tunnel_request(
        self,
        msg_type: int,
        body: bytes,
        sequence: int,
        transaction_fields: dict[str, object] | None = None,
    ) -> tuple[bytes, int, bytes, str | None]:
        if self._should_use_read_collect_transaction(msg_type, body):
            channel_id, _num_msgs, _timeout = ProtocolDecoder.decode_read_msgs_req(body)
            budget = self._read_collect_transaction_budget(channel_id)
            if transaction_fields is not None:
                transaction_fields.update(
                    {
                        "read_collect_budget_reason": budget.reason,
                        "read_collect_budget_deepened": budget.deepened,
                        "read_collect_collect_window_ms": budget.collect_window_ms,
                        "read_collect_max_reads": budget.max_reads,
                        "read_collect_read_timeout_ms": budget.read_timeout_ms,
                        "read_collect_max_messages": budget.max_messages,
                    }
                )
            encoded = ProtocolEncoder.encode_read_and_collect_reads_req(
                body,
                collect_window_ms=budget.collect_window_ms,
                max_reads=budget.max_reads,
                read_timeout_ms=budget.read_timeout_ms,
                max_messages=budget.max_messages,
                sequence=sequence,
            )
            return (
                encoded,
                MsgType.READ_AND_COLLECT_READS_REQ,
                encoded[HEADER_SIZE:],
                "read_collect_transaction",
            )

        if self._write_collect_transaction_base_allowed(msg_type, body):
            read_ahead = self.config.read_ahead
            if self._read_ahead_transaction_guard_active():
                encoded = ProtocolEncoder.encode_write_and_collect_reads_req(
                    body,
                    collect_window_ms=0,
                    max_reads=0,
                    read_timeout_ms=0,
                    max_messages=0,
                    sequence=sequence,
                )
                return (
                    encoded,
                    MsgType.WRITE_AND_COLLECT_READS_REQ,
                    encoded[HEADER_SIZE:],
                    "write_collect_guarded_no_collect",
                )
            encoded = ProtocolEncoder.encode_write_and_collect_reads_req(
                body,
                collect_window_ms=read_ahead.window_ms,
                max_reads=read_ahead.write_collect_max_reads,
                read_timeout_ms=read_ahead.read_timeout_ms,
                max_messages=read_ahead.max_messages,
                sequence=sequence,
            )
            return (
                encoded,
                MsgType.WRITE_AND_COLLECT_READS_REQ,
                encoded[HEADER_SIZE:],
                "write_collect_transaction",
            )

        header = struct.pack('>IIHI', MAGIC, HEADER_SIZE + len(body), msg_type, sequence)
        return header + body, msg_type, body, None

    def _record_in_caches(
        self,
        msg_type: int,
        body: bytes,
        resp_type: int,
        resp_body: bytes,
        ioctl_id: Optional[int],
        *,
        dll_seq: int | None = None,
        proxy_seq: int | None = None,
    ):
        """Record response in caches for future lookups."""
        if msg_type == MsgType.READ_MSGS_REQ:
            channel_id = struct.unpack('>I', body[:4])[0]
            return_code, messages = ProtocolDecoder.decode_read_msgs_rsp(resp_body)
            self._record_read_result_observation(
                channel_id,
                return_code=return_code,
                message_count=len(messages),
                dll_seq=dll_seq,
                proxy_seq=proxy_seq,
            )
            self._read_cache.record_result(
                channel_id,
                return_code,
                message_count=len(messages),
            )

        elif msg_type == MsgType.START_FILTER_REQ and resp_type == MsgType.START_FILTER_RSP:
            return_code, filter_id = ProtocolDecoder.decode_start_filter_rsp(resp_body)
            self._filter_cache.record_result(body, filter_id, return_code, resp_body)

        elif msg_type == MsgType.WRITE_MSGS_REQ:
            if resp_type != MsgType.WRITE_MSGS_RSP:
                self._clear_prefetch_after_failed_write(msg_type, body)
                try:
                    channel_id, _messages, _timeout = ProtocolDecoder.decode_write_msgs_req(body)
                    self._cancel_sweep_plan("failed_write_msgs_rsp_type", channel_id=channel_id)
                except Exception:
                    self._cancel_sweep_plan("failed_write_msgs_rsp_type")
                return
            return_code, _num_written = ProtocolDecoder.decode_write_msgs_rsp(resp_body)
            if return_code != 0:
                self._clear_prefetch_after_failed_write(msg_type, body)
                try:
                    channel_id, _messages, _timeout = ProtocolDecoder.decode_write_msgs_req(body)
                    self._cancel_sweep_plan("failed_write_msgs_req", channel_id=channel_id)
                except Exception:
                    self._cancel_sweep_plan("failed_write_msgs_req")

        elif msg_type == MsgType.IOCTL_REQ and ioctl_id is not None and resp_type == MsgType.IOCTL_RSP:
            channel_id = struct.unpack('>I', body[:4])[0]
            ret, output_data = ProtocolDecoder.decode_ioctl_rsp(resp_body)
            self._ioctl_cache.record_result(channel_id, ioctl_id, ret, output_data)

    def _record_benchmark_event(
        self,
        *,
        started_at_s: float,
        duration_ms: float,
        msg_type: int,
        req_body: bytes,
        resp_type: int | None,
        resp_body: bytes,
        cache_hit: bool,
        status: str,
        hw_ms: float | None = None,
    ) -> None:
        if self.benchmark_writer is None:
            return

        event = make_proxy_benchmark_event(
            run_label=self.benchmark_label,
            source="proxy_server",
            started_at_s=started_at_s,
            duration_ms=duration_ms,
            msg_type=msg_type,
            req_body=req_body,
            resp_type=resp_type,
            resp_body=resp_body,
            cache_hit=cache_hit,
            status=status,
            hw_ms=hw_ms,
        )
        self.benchmark_writer.write_event(event)

    def _next_sequence(self) -> int:
        self.sequence = (self.sequence + 1) & 0xFFFFFFFF
        return self.sequence

    def _write_tunnel_quality_snapshot(self) -> None:
        snapshot = self._tunnel_quality.snapshot()
        try:
            write_tunnel_quality_snapshot(snapshot)
            self._last_snapshot_write_error = None
        except PermissionError as exc:
            self._log_tunnel_quality_snapshot_error(exc)
        except OSError as exc:
            self._log_tunnel_quality_snapshot_error(exc)
        signature = self._quality_signature(snapshot)
        if signature == self._last_quality_signature:
            return
        self._last_quality_signature = signature
        self._emit_tunnel_event(
            "tunnel.quality.changed",
            connection_epoch=snapshot.get("connection_epoch"),
            status="error" if not snapshot.get("connected") else "ok",
            failure_code="quality_blocked" if snapshot.get("status") == "blocked" else None,
            failure_domain="cloud_proxy_tunnel" if snapshot.get("status") == "blocked" else "unknown",
            reason=str(snapshot.get("reason") or ""),
            network_grade=snapshot.get("grade"),
            tunnel_status=snapshot.get("status"),
            tunnel_connected=bool(snapshot.get("connected")),
            tunnel_fresh=bool(snapshot.get("fresh")),
            probe_failures=int(snapshot.get("probe_failures") or 0),
        )
        if not self._should_log_tunnel_quality(snapshot):
            return

        level = logging.INFO
        if (
            not snapshot.get("connected")
            or snapshot.get("probe_failures")
            or snapshot.get("reason") in {"snapshot_stale", "tunnel_disconnected"}
        ):
            level = logging.WARNING
        logger.log(
            level,
            "[TUNNEL_QUALITY] status=%s connected=%s reason=%s",
            snapshot.get("status"),
            snapshot.get("connected"),
            snapshot.get("reason"),
        )

    @staticmethod
    def _should_log_tunnel_quality(snapshot: dict) -> bool:
        if not snapshot.get("connected"):
            return True
        if snapshot.get("probe_failures"):
            return True
        return snapshot.get("status") != "healthy"

    def _log_tunnel_quality_snapshot_error(self, exc: Exception) -> None:
        message = str(exc)
        if self._last_snapshot_write_error == message:
            return
        self._last_snapshot_write_error = message
        logger.warning("Failed to persist tunnel quality snapshot: %s", exc)

    @staticmethod
    def _quality_signature(snapshot: dict) -> tuple:
        return (
            snapshot.get("connection_epoch"),
            snapshot.get("grade"),
            snapshot.get("status"),
            snapshot.get("connected"),
            snapshot.get("fresh"),
            snapshot.get("reason"),
            snapshot.get("probe_failures"),
        )

    @staticmethod
    def _format_ms(value: float | None) -> str:
        if value is None:
            return "n/a"
        return f"{float(value):.1f}ms"

    def _cancel_probe_task(self) -> asyncio.Task | None:
        task = self._probe_task
        self._probe_task = None
        if task is not None:
            task.cancel()
        return task

    async def _cancel_probe_task_async(self) -> None:
        task = self._cancel_probe_task()
        if task is None:
            return
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("probe task cleanup failed", exc_info=True)

    async def _probe_loop(self, connection_epoch: str) -> None:
        try:
            while (
                self.vci_connected.is_set()
                and self.vci_writer is not None
                and self._connection_epoch == connection_epoch
            ):
                await self._run_probe(connection_epoch)
                await asyncio.sleep(3.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[TUNNEL_PROBE] loop_stopped epoch=%s error=%s", connection_epoch, exc)

    async def _run_probe(self, connection_epoch: str) -> None:
        if self.vci_writer is None or self._connection_epoch != connection_epoch:
            return

        started_at = time.monotonic()
        sequence = self._next_sequence()
        future = asyncio.get_running_loop().create_future()
        self.response_futures[sequence] = future

        try:
            async with self.vci_lock:
                if self.vci_writer is None:
                    raise ConnectionError("VCI disconnected")
                self.vci_writer.write(ProtocolEncoder.encode_ping_req(sequence))
                await self.vci_writer.drain()

            resp_type, _resp_body, hw_ms = await asyncio.wait_for(future, timeout=5.0)
            if resp_type != MsgType.PING_RSP:
                raise RuntimeError(f"Unexpected probe response type: {resp_type}")

            duration_ms = (time.monotonic() - started_at) * 1000.0
            network_ms = max(0.0, duration_ms - float(hw_ms or 0.0))
            self._tunnel_quality.record_probe(network_ms)
            self._emit_tunnel_event(
                "tunnel.probe.success",
                connection_epoch=connection_epoch,
                operation_kind="tunnel_probe",
                duration_ms=duration_ms,
                hw_ms=hw_ms,
                network_ms=network_ms,
                reason="probe_ok",
                probe_sequence=sequence,
            )
            logger.debug(
                "[TUNNEL_PROBE] success epoch=%s seq=%s duration=%s hw=%s network=%s",
                connection_epoch,
                sequence,
                self._format_ms(duration_ms),
                self._format_ms(hw_ms),
                self._format_ms(network_ms),
            )
        except asyncio.CancelledError:
            self.response_futures.pop(sequence, None)
            raise
        except Exception as exc:
            self.response_futures.pop(sequence, None)
            self._tunnel_quality.record_probe_failure(reason="probe_failures")
            self._emit_tunnel_event(
                "tunnel.probe.failure",
                connection_epoch=connection_epoch,
                operation_kind="tunnel_probe",
                status="error",
                failure_code="probe_failure",
                failure_domain="cloud_proxy_tunnel",
                reason=str(exc) or type(exc).__name__.lower(),
                probe_sequence=sequence,
                probe_failures=self._tunnel_quality.probe_failures,
            )
            logger.warning(
                "[TUNNEL_PROBE] failure epoch=%s seq=%s error=%s failures=%s",
                connection_epoch,
                sequence,
                exc,
                self._tunnel_quality.probe_failures,
            )
        finally:
            self._write_tunnel_quality_snapshot()

    async def _handle_proxy_connection(self, reader: asyncio.StreamReader,
                                       writer: asyncio.StreamWriter):
        """处理本地代理连接"""
        addr = writer.get_extra_info('peername')
        logger.debug("Proxy client connected: %s", addr)

        # Disable Nagle algorithm for lower latency
        sock = writer.get_extra_info('socket')
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        try:
            while True:
                # 等待 VCI 连接
                if not self.vci_connected.is_set():
                    logger.debug("Waiting for VCI tunnel before serving proxy client")
                    await self.vci_connected.wait()

                if self.vci_writer is None:
                    logger.error("VCI tunnel unavailable while serving proxy client")
                    break

                # 读取请求
                header = await reader.readexactly(HEADER_SIZE)
                magic, length, msg_type, sequence = struct.unpack('>IIHI', header)

                if magic != MAGIC:
                    logger.warning(f"无效的 Magic: {magic:#x}")
                    break

                try:
                    body = await _read_frame_body(reader, length)
                except (ValueError, TimeoutError, ConnectionError) as exc:
                    logger.warning("代理请求帧被拒绝: %s", exc)
                    break

                msg_name = MSG_NAMES.get(msg_type, f"0x{msg_type:04x}")
                started_at_s = time.time()
                request_fields = self._request_observability_fields(msg_type, body)
                self._augment_live_cadence_fields(msg_type, sequence, request_fields)
                self._emit_proxy_request_event(
                    "proxy.request.received_from_dll",
                    dll_seq=sequence,
                    msg_name=msg_name,
                    reason="received_from_dll",
                    **request_fields,
                )
                self._emit_live_cadence_gap_if_needed(
                    dll_seq=sequence,
                    msg_name=msg_name,
                    request_fields=request_fields,
                )

                if msg_type == MsgType.READ_MSGS_REQ:
                    replay_pending = self._try_consume_active_replay_read(body)
                    if replay_pending is not None:
                        replay_response = Message(
                            MsgType.READ_MSGS_RSP,
                            sequence,
                            replay_pending.shadow_result.read_rsp_body,
                        ).encode()
                        resp_type, resp_body = decode_benchmark_response(replay_response)
                        response_fields = self._response_observability_fields(
                            resp_type,
                            resp_body,
                        )
                        self._augment_read_payload_delta_fields(
                            request_fields,
                            response_fields,
                        )
                        self._record_benchmark_event(
                            started_at_s=started_at_s,
                            duration_ms=0.0,
                            msg_type=msg_type,
                            req_body=body,
                            resp_type=resp_type,
                            resp_body=resp_body,
                            cache_hit=False,
                            status="success",
                        )
                        self._emit_proxy_request_event(
                            "proxy.request.active_replay_served",
                            dll_seq=sequence,
                            msg_name=msg_name,
                            reason="active_replay_read_served",
                            replay_signature_digest=(
                                replay_pending.observed.signature.signature_digest
                            ),
                            replay_shadow_age_ms=round(
                                replay_pending.shadow_result.age_ms,
                                3,
                            ),
                            replay_shadow_clean_match_streak=self._sweep_shadow_store.replay_match_streak(
                                replay_pending.observed.signature.signature_digest
                            ),
                            replay_min_clean_matches=self._active_replay_min_clean_matches(),
                            **request_fields,
                            **response_fields,
                        )
                        writer.write(replay_response)
                        await writer.drain()
                        self._emit_proxy_request_event(
                            "proxy.request.replied_to_dll",
                            dll_seq=sequence,
                            msg_name=msg_name,
                            reason="active_replay_read_served",
                            **request_fields,
                            **response_fields,
                        )
                        self._schedule_sweep_poll()
                        continue

                if msg_type == MsgType.WRITE_MSGS_REQ:
                    replay_pending = self._try_prepare_active_replay_write(body)
                    if replay_pending is not None:
                        channel_id = replay_pending.observed.signature.channel_id
                        self._active_replay_pending_by_channel[channel_id] = replay_pending
                        self._invalidate_caches(msg_type, body, sequence)
                        replay_response = ProtocolEncoder.encode_write_msgs_rsp(
                            0,
                            1,
                            sequence,
                        )
                        resp_type, resp_body = decode_benchmark_response(replay_response)
                        response_fields = self._response_observability_fields(
                            resp_type,
                            resp_body,
                        )
                        self._record_benchmark_event(
                            started_at_s=started_at_s,
                            duration_ms=0.0,
                            msg_type=msg_type,
                            req_body=body,
                            resp_type=resp_type,
                            resp_body=resp_body,
                            cache_hit=False,
                            status="success",
                        )
                        self._emit_proxy_request_event(
                            "proxy.request.active_replay_armed",
                            dll_seq=sequence,
                            msg_name=msg_name,
                            reason="active_replay_write_ack",
                            replay_signature_digest=(
                                replay_pending.observed.signature.signature_digest
                            ),
                            replay_shadow_age_ms=round(
                                replay_pending.shadow_result.age_ms,
                                3,
                            ),
                            replay_shadow_clean_match_streak=self._sweep_shadow_store.replay_match_streak(
                                replay_pending.observed.signature.signature_digest
                            ),
                            replay_min_clean_matches=self._active_replay_min_clean_matches(),
                            **request_fields,
                            **response_fields,
                        )
                        writer.write(replay_response)
                        await writer.drain()
                        self._emit_proxy_request_event(
                            "proxy.request.replied_to_dll",
                            dll_seq=sequence,
                            msg_name=msg_name,
                            reason="active_replay_write_ack",
                            **request_fields,
                            **response_fields,
                        )
                        self._schedule_sweep_poll()
                        continue

                self._observe_sweep_write(
                    msg_type,
                    body,
                    dll_seq=sequence,
                    msg_name=msg_name,
                )

                # Try serving from consume-once read-ahead FIFO before empty cache.
                cached = None
                ioctl_id = None
                cache_reason = None
                if msg_type == MsgType.READ_MSGS_REQ:
                    channel_id, num_msgs, timeout_ms = ProtocolDecoder.decode_read_msgs_req(body)
                    async with self._prefetch_read_lock(channel_id):
                        drain = self._prefetch_read_msgs.drain(channel_id, num_msgs)
                        request_fields.update(self._prefetch_drain_fields(drain))
                        request_fields.update(
                            self._prefetch_state_fields(
                                channel_id,
                                msg_type=msg_type,
                                body=body,
                                timeout_ms=timeout_ms,
                                drain=drain,
                            )
                        )
                        if drain.is_full:
                            cached = drain.to_response(sequence)
                            cache_reason = "prefetch_hit"
                            self._record_prefetch_drain_observation(
                                drain,
                                reason=cache_reason,
                            )
                        elif self._should_merge_partial_prefetch_with_tunnel(
                            msg_type,
                            body,
                            drain,
                            timeout_ms=timeout_ms,
                        ):
                            self._record_prefetch_drain_observation(
                                drain,
                                reason="prefetch_underfill_forwarded",
                            )
                            served = await self._serve_prefetch_underfill(
                                writer=writer,
                                body=body,
                                sequence=sequence,
                                msg_name=msg_name,
                                started_at_s=started_at_s,
                                request_fields=request_fields,
                                drain=drain,
                            )
                            if served:
                                continue
                            break
                        elif self._should_serve_partial_prefetch_without_tunnel(
                            drain,
                            timeout_ms=timeout_ms,
                        ):
                            request_fields["prefetch_partial_direct"] = True
                            request_fields["prefetch_partial_direct_reason"] = (
                                "oversized_nonblocking_read"
                            )
                            cached = drain.to_response(sequence)
                            cache_reason = "prefetch_partial_hit"
                            self._record_prefetch_drain_observation(
                                drain,
                                reason=cache_reason,
                            )
                        elif drain.is_partial:
                            self._record_prefetch_drain_observation(
                                drain,
                                reason="prefetch_underfill_forwarded",
                            )
                            served = await self._serve_prefetch_underfill(
                                writer=writer,
                                body=body,
                                sequence=sequence,
                                msg_name=msg_name,
                                started_at_s=started_at_s,
                                request_fields=request_fields,
                                drain=drain,
                            )
                            if served:
                                continue
                            break
                        else:
                            self._record_prefetch_drain_observation(
                                drain,
                                reason="prefetch_miss",
                            )

                if cached is None:
                    cached, ioctl_id, cache_reason = self._try_serve_cached(
                        msg_type, body, sequence
                    )
                if cached is not None:
                    self._emit_proxy_request_event(
                        "proxy.request.cache_decision",
                        dll_seq=sequence,
                        msg_name=msg_name,
                        cache_hit=True,
                        reason=cache_reason or "cache_hit",
                        **request_fields,
                    )
                    resp_type, resp_body = decode_benchmark_response(cached)
                    response_fields = self._response_observability_fields(resp_type, resp_body)
                    self._augment_read_payload_delta_fields(request_fields, response_fields)
                    self._observe_sweep_read_response(
                        msg_type,
                        body,
                        resp_type,
                        resp_body,
                        dll_seq=sequence,
                        msg_name=msg_name,
                        duration_ms=0.0,
                        network_ms=0.0,
                        cache_hit=True,
                    )
                    self._record_benchmark_event(
                        started_at_s=started_at_s,
                        duration_ms=0.0,
                        msg_type=msg_type,
                        req_body=body,
                        resp_type=resp_type,
                        resp_body=resp_body,
                        cache_hit=True,
                        status="success",
                    )
                    writer.write(cached)
                    await writer.drain()
                    self._emit_proxy_request_event(
                        "proxy.request.replied_to_dll",
                        dll_seq=sequence,
                        msg_name=msg_name,
                        cache_hit=True,
                        reason="cache_reply",
                        **request_fields,
                        **response_fields,
                    )
                    self._schedule_sweep_poll()
                    continue
                self._emit_proxy_request_event(
                    "proxy.request.cache_decision",
                    dll_seq=sequence,
                    msg_name=msg_name,
                    cache_hit=False,
                    reason=self._cache_miss_reason(msg_type, request_fields),
                    **request_fields,
                )

                # Invalidate caches as needed
                self._invalidate_caches(msg_type, body, sequence)

                # 转发请求到 VCI Proxy
                new_seq = self._next_sequence()
                fwd_start = time.monotonic()

                future = asyncio.get_running_loop().create_future()
                self.response_futures[new_seq] = future

                try:
                    transaction_fields: dict[str, object] = {}
                    tunnel_request, fwd_msg_type, _fwd_body, transaction_reason = self._build_tunnel_request(
                        msg_type,
                        body,
                        new_seq,
                        transaction_fields=transaction_fields,
                    )
                    if transaction_fields:
                        request_fields.update(transaction_fields)
                    if transaction_reason in {
                        "read_collect_transaction",
                        "write_collect_transaction",
                    }:
                        self._prefetch_bundle_source_by_proxy_seq[new_seq] = (
                            "read_collect"
                            if transaction_reason == "read_collect_transaction"
                            else "write_collect"
                        )
                    async with self.vci_lock:
                        if self.vci_writer is None:
                            raise ConnectionError("VCI 连接已断开")
                        self.vci_writer.write(tunnel_request)
                        await self.vci_writer.drain()
                    self._emit_proxy_request_event(
                        "proxy.request.forwarded_to_tunnel",
                        dll_seq=sequence,
                        proxy_seq=new_seq,
                        msg_name=msg_name,
                        reason=transaction_reason or "forwarded_to_tunnel",
                        forwarded_msg_type=int(fwd_msg_type),
                        forwarded_msg_name=MSG_NAMES.get(fwd_msg_type, f"0x{int(fwd_msg_type):04x}"),
                        **request_fields,
                        **self._read_ahead_transaction_guard_fields(),
                    )
                except Exception as e:
                    logger.error(f"转发请求失败: {e}")
                    self.response_futures.pop(new_seq, None)
                    self._prefetch_bundle_source_by_proxy_seq.pop(new_seq, None)
                    self._prefetch_empty_confirmations_by_proxy_seq.pop(new_seq, None)
                    self._clear_prefetch_after_failed_write(msg_type, body)
                    self._emit_proxy_request_event(
                        "proxy.request.failed",
                        dll_seq=sequence,
                        proxy_seq=new_seq,
                        msg_name=msg_name,
                        status="error",
                        failure_code="forward_failed",
                        failure_domain="cloud_proxy_tunnel",
                        reason=str(e),
                        **request_fields,
                    )
                    break

                # 等待响应
                try:
                    resp_type, resp_body, hw_ms = await asyncio.wait_for(future, timeout=30.0)
                    fwd_ms = (time.monotonic() - fwd_start) * 1000
                    network_ms = max(0.0, fwd_ms - float(hw_ms or 0.0))
                    self._arm_read_ahead_transaction_guard(
                        network_ms=network_ms,
                        duration_ms=fwd_ms,
                        dll_seq=sequence,
                        proxy_seq=new_seq,
                        msg_name=msg_name,
                        request_fields=request_fields,
                    )

                    self._observe_sweep_write_response(
                        msg_type,
                        body,
                        resp_type,
                        resp_body,
                        dll_seq=sequence,
                        msg_name=msg_name,
                        duration_ms=fwd_ms,
                        network_ms=network_ms,
                    )
                    self._record_in_caches(
                        msg_type,
                        body,
                        resp_type,
                        resp_body,
                        ioctl_id,
                        dll_seq=sequence,
                        proxy_seq=new_seq,
                    )
                    request_fields.update(
                        self._apply_prefetch_empty_confirmations(new_seq)
                    )
                    response_fields = self._response_observability_fields(resp_type, resp_body)
                    self._augment_read_payload_delta_fields(request_fields, response_fields)
                    self._observe_sweep_read_response(
                        msg_type,
                        body,
                        resp_type,
                        resp_body,
                        dll_seq=sequence,
                        msg_name=msg_name,
                        duration_ms=fwd_ms,
                        network_ms=network_ms,
                        cache_hit=False,
                    )
                    self._record_benchmark_event(
                        started_at_s=started_at_s,
                        duration_ms=fwd_ms,
                        msg_type=msg_type,
                        req_body=body,
                        resp_type=resp_type,
                        resp_body=resp_body,
                        cache_hit=False,
                        status="success",
                        hw_ms=hw_ms,
                    )
                    self._emit_proxy_request_event(
                        "proxy.request.response_received",
                        dll_seq=sequence,
                        proxy_seq=new_seq,
                        msg_name=msg_name,
                        duration_ms=fwd_ms,
                        hw_ms=hw_ms,
                        network_ms=network_ms,
                        reason="response_received",
                        **request_fields,
                        **response_fields,
                    )

                    # 发送响应给客户端（使用原始 sequence）
                    resp_header = struct.pack('>IIHI', MAGIC,
                                             HEADER_SIZE + len(resp_body),
                                             resp_type, sequence)
                    writer.write(resp_header + resp_body)
                    await writer.drain()
                    self._emit_proxy_request_event(
                        "proxy.request.replied_to_dll",
                        dll_seq=sequence,
                        proxy_seq=new_seq,
                        msg_name=msg_name,
                        duration_ms=fwd_ms,
                        hw_ms=hw_ms,
                        network_ms=network_ms,
                        reason="reply_sent",
                        **request_fields,
                        **response_fields,
                    )
                    self._schedule_sweep_poll()

                    if fwd_ms > 1000:
                        logger.warning(
                            "Slow proxy request: %s seq=%s duration=%.1fms",
                            msg_name,
                            sequence,
                            fwd_ms,
                        )

                except (asyncio.TimeoutError, ConnectionError) as e:
                    self.response_futures.pop(new_seq, None)
                    self._prefetch_bundle_source_by_proxy_seq.pop(new_seq, None)
                    self._prefetch_empty_confirmations_by_proxy_seq.pop(new_seq, None)
                    fwd_ms = (time.monotonic() - fwd_start) * 1000
                    reason = "VCI_DISCONNECTED" if isinstance(e, ConnectionError) else "TIMEOUT"
                    self._clear_prefetch_after_failed_write(msg_type, body)
                    self._cancel_sweep_plan("gds2_communication_error")
                    self._record_benchmark_event(
                        started_at_s=started_at_s,
                        duration_ms=fwd_ms,
                        msg_type=msg_type,
                        req_body=body,
                        resp_type=None,
                        resp_body=b"",
                        cache_hit=False,
                        status=reason.lower(),
                    )
                    self._emit_proxy_request_event(
                        "proxy.request.timeout" if isinstance(e, asyncio.TimeoutError) else "proxy.request.failed",
                        dll_seq=sequence,
                        proxy_seq=new_seq,
                        msg_name=msg_name,
                        status="error",
                        failure_code="timeout" if isinstance(e, asyncio.TimeoutError) else "vci_disconnected",
                        failure_domain="cloud_proxy_tunnel",
                        reason="wait_response_timeout" if isinstance(e, asyncio.TimeoutError) else "wait_response_connection_lost",
                        duration_ms=fwd_ms,
                        **request_fields,
                    )
                    logger.error(f"[PROXY] {msg_name} seq={sequence} {reason} after {fwd_ms:.0f}ms")
                    break

        except asyncio.CancelledError:
            self._shutting_down = True
            logger.info("Proxy client handler stopping during server shutdown: %s", addr)
        except asyncio.IncompleteReadError:
            logger.debug("Proxy client disconnected: %s", addr)
        except Exception as e:
            logger.error(f"代理连接错误: {e}")
        finally:
            writer.close()
def _install_runtime_log_observability(server: ReverseProxyServer) -> None:
    install_observability_log_handler(
        logging.getLogger(),
        component="reverse_server.runtime",
        writer=get_product_log_writer("reverse_server.runtime"),
        context_provider=lambda _record: server._current_observability_context(
            operation_kind="reverse_server_runtime"
        ),
    )


def _run_server_until_stopped(server: ReverseProxyServer) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    main_task = loop.create_task(server.start())
    previous_sigint_handler = signal.getsignal(signal.SIGINT)
    stop_requested = False

    def _handle_sigint(signum, frame):
        nonlocal stop_requested
        if stop_requested:
            logger.warning("强制停止服务器...")
            os._exit(130)
        stop_requested = True
        server._shutting_down = True
        logger.info("停止服务器...")
        if not main_task.done():
            loop.call_soon_threadsafe(main_task.cancel)

    signal.signal(signal.SIGINT, _handle_sigint)
    try:
        try:
            loop.run_until_complete(main_task)
        except asyncio.CancelledError:
            pass

        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        if pending:
            for task in pending:
                task.cancel()
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))

        loop.run_until_complete(loop.shutdown_asyncgens())
        if hasattr(loop, "shutdown_default_executor"):
            loop.run_until_complete(loop.shutdown_default_executor())
    finally:
        signal.signal(signal.SIGINT, previous_sigint_handler)
        asyncio.set_event_loop(None)
        loop.close()


def main():
    _disable_windows_quick_edit()
    parser = argparse.ArgumentParser(description='VCI Proxy 反向连接服务器')
    parser.add_argument('--listen-port', type=int, default=9000,
                       help='VCI Proxy 连接端口 (默认: 9000)')
    parser.add_argument('--proxy-port', type=int, default=9001,
                       help='本地代理端口 (默认: 9001)')
    parser.add_argument('--auth-token', type=str, default=None,
                       help='PSK authentication token')
    parser.add_argument('--tls', action='store_true',
                       help='Enable TLS for reverse-client connections')
    parser.add_argument('--tls-cert', type=str, default=None,
                       help='TLS certificate file for the reverse server')
    parser.add_argument('--tls-key', type=str, default=None,
                       help='TLS private key file for the reverse server')
    parser.add_argument('--tls-ca', type=str, default=None,
                       help='CA bundle for validating reverse-client certificates')
    parser.add_argument('--tls-require-client-cert', action='store_true',
                       help='Require reverse clients to present a trusted certificate')
    parser.add_argument('--no-read-cache', action='store_true',
                       help='Disable ReadMsgs BUFFER_EMPTY cache')
    parser.add_argument('--read-cache-ttl', type=int, default=150,
                       help='ReadMsgs cache TTL in ms (默认: 150)')
    parser.add_argument('--read-cache-post-write-bypass-ms', type=int, default=150,
                       help='Bypass ReadMsgs empty cache after same-channel writes in ms (default: 150; 0 disables)')
    parser.add_argument('--read-cache-active-ttl-ms', type=int, default=25,
                       help='ReadMsgs empty cache TTL while a channel is active in ms (default: 25)')
    parser.add_argument('--read-cache-active-adaptive-ttl-max-ms', type=int, default=70,
                       help='Maximum adaptive active ReadMsgs empty-cache TTL in ms after repeated confirmed empty reads (default: 70)')
    parser.add_argument('--read-cache-active-adaptive-ttl-margin-ms', type=int, default=8,
                       help='Margin added to the observed confirmed-empty polling gap for adaptive active TTL in ms (default: 8)')
    parser.add_argument('--read-cache-active-window-ms', type=int, default=500,
                       help='Window after writes/data/filter mutations that uses active TTL in ms (default: 500)')
    parser.add_argument('--read-cache-max-timeout-ms', type=int, default=25,
                       help='Only cache ReadMsgs polls whose timeout is at or below this value in ms (default: 25; -1 allows all)')
    parser.add_argument('--read-ahead', dest='read_ahead', action='store_true', default=None,
                       help='Enable consume-once ReadMsgs prefetch serving after successful writes (or VCI_PROXY_READ_AHEAD=1)')
    parser.add_argument('--no-read-ahead', dest='read_ahead', action='store_false',
                       help='Disable read-ahead even if VCI_PROXY_READ_AHEAD is set')
    parser.add_argument('--read-ahead-window-ms', type=int, default=None,
                       help='Read-ahead collection window in ms (default: 200 or VCI_PROXY_READ_AHEAD_WINDOW_MS)')
    parser.add_argument('--read-ahead-max-reads', type=int, default=None,
                       help='Maximum local ReadMsgs calls for generic/read-tail collection (default: 3 or VCI_PROXY_READ_AHEAD_MAX_READS)')
    parser.add_argument('--read-ahead-write-collect-max-reads', type=int, default=None,
                       help='Maximum local ReadMsgs calls after write-collect transactions (default: 6 or VCI_PROXY_READ_AHEAD_WRITE_COLLECT_MAX_READS)')
    parser.add_argument('--read-ahead-read-timeout-ms', type=int, default=None,
                       help='Timeout passed to local read-ahead ReadMsgs calls in ms (default: 0 or VCI_PROXY_READ_AHEAD_READ_TIMEOUT_MS)')
    parser.add_argument('--read-ahead-max-messages', type=int, default=None,
                       help='Maximum prefetched messages retained per channel (default: 16 or VCI_PROXY_READ_AHEAD_MAX_MESSAGES)')
    parser.add_argument('--read-ahead-max-empty-reads', type=int, default=None,
                       help='Stop local read-ahead after this many empty reads (default: 0 or VCI_PROXY_READ_AHEAD_MAX_EMPTY_READS; 0 disables)')
    parser.add_argument('--read-ahead-max-consecutive-empty-reads', type=int, default=None,
                       help='Stop local read-ahead after this many consecutive empty reads (default: 0 or VCI_PROXY_READ_AHEAD_MAX_CONSECUTIVE_EMPTY_READS; 0 disables)')
    parser.add_argument('--read-ahead-min-drain-ms', type=int, default=None,
                       help='Keep local read-ahead draining through early empty reads for at least this many ms (default: 0 or VCI_PROXY_READ_AHEAD_MIN_DRAIN_MS; 0 disables)')
    parser.add_argument('--read-ahead-transaction', dest='read_ahead_transaction', action='store_true', default=None,
                       help='Enable internal WRITE_AND_COLLECT_READS transaction RPC when the client advertises support (or VCI_PROXY_READ_AHEAD_TRANSACTION=1)')
    parser.add_argument('--no-read-ahead-transaction', dest='read_ahead_transaction', action='store_false',
                       help='Disable internal read-ahead transaction RPC even if VCI_PROXY_READ_AHEAD_TRANSACTION is set')
    parser.add_argument('--read-ahead-transaction-max-network-ms', type=int, default=None,
                       help='Arm no-collect transaction guard when a tunnel response reaches this network_ms (default: 400; 0 disables)')
    parser.add_argument('--read-ahead-transaction-cooldown-ms', type=int, default=None,
                       help='How long no-collect transaction guard remains active after a slow response (default: 10000; 0 disables)')
    parser.add_argument('--local-sweep', dest='local_sweep', action='store_true', default=None,
                       help='Enable guarded local sweep observe/shadow mode (or VCI_PROXY_LOCAL_SWEEP=1)')
    parser.add_argument('--no-local-sweep', dest='local_sweep', action='store_false',
                       help='Disable local sweep even if VCI_PROXY_LOCAL_SWEEP is set')
    parser.add_argument('--local-sweep-mode', choices=['observe_only', 'shadow_local', 'active_replay'], default=None,
                       help='Local sweep mode (default: observe_only or VCI_PROXY_LOCAL_SWEEP_MODE)')
    parser.add_argument('--local-sweep-min-cycles', type=int, default=None,
                       help='Minimum complete observed cycles before sweep candidacy')
    parser.add_argument('--local-sweep-max-items', type=int, default=None,
                       help='Maximum learned signatures in one shadow plan')
    parser.add_argument('--local-sweep-min-item-interval-ms', type=int, default=None,
                       help='Minimum delay between local shadow sweep items in ms')
    parser.add_argument('--local-sweep-read-timeout-ms', type=int, default=None,
                       help='Minimum timeout for local shadow ReadMsgs calls in ms')
    parser.add_argument('--local-sweep-shadow-allow-gm-a9-packet',
                       dest='local_sweep_shadow_allow_gm_a9_packet',
                       action='store_true', default=None,
                       help='Allow GM A9 packet signatures to run in shadow_local plans')
    parser.add_argument('--no-local-sweep-shadow-allow-gm-a9-packet',
                       dest='local_sweep_shadow_allow_gm_a9_packet',
                       action='store_false',
                       help='Keep GM A9 packet signatures observe-only even in shadow_local mode')
    parser.add_argument('--local-sweep-shadow-max-seconds', type=int, default=None,
                       help='Maximum duration for one shadow plan')
    parser.add_argument('--local-sweep-plan-delay-ms', type=int, default=None,
                       help='Delay before starting a shadow plan so newly learned signatures can join it')
    parser.add_argument('--local-sweep-include-uds-dids', type=str, default=None,
                       help='Comma-separated UDS DID allowlist for shadow plans, for example 0x000c,0x0031')
    parser.add_argument('--local-sweep-exclude-uds-dids', type=str, default=None,
                       help='Comma-separated UDS DID blocklist for shadow plans, for example 0x0031')
    parser.add_argument('--no-filter-dedup', action='store_true',
                       help='Disable StartFilter deduplication')
    parser.add_argument('--no-vbatt-cache', action='store_true',
                       help='Disable READ_VBATT response cache')
    parser.add_argument('--vbatt-ttl', type=int, default=5,
                       help='VBATT cache TTL in seconds (默认: 5)')
    parser.add_argument('--no-ioctl-cache', action='store_true',
                       help='Disable generalized read-only IOCTL cache')
    parser.add_argument('--ioctl-ttl', type=int, default=5,
                       help='IOCTL cache TTL in seconds (默认: 5)')
    parser.add_argument('--benchmark-log', type=str, default=None,
                       help='Write structured JSONL benchmark events to this file')
    parser.add_argument('--benchmark-label', type=str, default='proxy_run',
                       help='Run label stored in benchmark events')
    args = parser.parse_args()

    config = ProxyConfig.from_args(
        auth_token=args.auth_token,
        tls_enabled=args.tls,
        tls_certfile=args.tls_cert,
        tls_keyfile=args.tls_key,
        tls_ca_file=args.tls_ca,
        tls_require_client_cert=args.tls_require_client_cert,
        no_read_cache=args.no_read_cache,
        read_cache_ttl=args.read_cache_ttl,
        read_cache_post_write_bypass_ms=args.read_cache_post_write_bypass_ms,
        read_cache_active_ttl_ms=args.read_cache_active_ttl_ms,
        read_cache_active_adaptive_ttl_max_ms=(
            args.read_cache_active_adaptive_ttl_max_ms
        ),
        read_cache_active_adaptive_ttl_margin_ms=(
            args.read_cache_active_adaptive_ttl_margin_ms
        ),
        read_cache_active_window_ms=args.read_cache_active_window_ms,
        read_cache_max_timeout_ms=args.read_cache_max_timeout_ms,
        read_ahead_enabled=args.read_ahead,
        read_ahead_window_ms=args.read_ahead_window_ms,
        read_ahead_max_reads=args.read_ahead_max_reads,
        read_ahead_write_collect_max_reads=(
            args.read_ahead_write_collect_max_reads
        ),
        read_ahead_read_timeout_ms=args.read_ahead_read_timeout_ms,
        read_ahead_max_messages=args.read_ahead_max_messages,
        read_ahead_max_empty_reads=args.read_ahead_max_empty_reads,
        read_ahead_max_consecutive_empty_reads=(
            args.read_ahead_max_consecutive_empty_reads
        ),
        read_ahead_min_drain_ms=args.read_ahead_min_drain_ms,
        read_ahead_transaction_enabled=args.read_ahead_transaction,
        read_ahead_transaction_max_network_ms=args.read_ahead_transaction_max_network_ms,
        read_ahead_transaction_cooldown_ms=args.read_ahead_transaction_cooldown_ms,
        local_sweep_enabled=getattr(args, "local_sweep", None),
        local_sweep_mode=getattr(args, "local_sweep_mode", None),
        local_sweep_min_cycles=getattr(args, "local_sweep_min_cycles", None),
        local_sweep_max_items=getattr(args, "local_sweep_max_items", None),
        local_sweep_min_item_interval_ms=getattr(args, "local_sweep_min_item_interval_ms", None),
        local_sweep_read_timeout_ms=getattr(args, "local_sweep_read_timeout_ms", None),
        local_sweep_shadow_allow_gm_a9_packet=getattr(
            args,
            "local_sweep_shadow_allow_gm_a9_packet",
            None,
        ),
        local_sweep_shadow_max_seconds=getattr(args, "local_sweep_shadow_max_seconds", None),
        local_sweep_plan_delay_ms=getattr(args, "local_sweep_plan_delay_ms", None),
        local_sweep_include_uds_dids=(
            tuple(
                int(token.strip(), 16 if token.strip().lower().startswith("0x") else 10)
                for token in str(getattr(args, "local_sweep_include_uds_dids", None) or "").split(",")
                if token.strip()
            )
            if getattr(args, "local_sweep_include_uds_dids", None) is not None
            else None
        ),
        local_sweep_exclude_uds_dids=(
            tuple(
                int(token.strip(), 16 if token.strip().lower().startswith("0x") else 10)
                for token in str(getattr(args, "local_sweep_exclude_uds_dids", None) or "").split(",")
                if token.strip()
            )
            if getattr(args, "local_sweep_exclude_uds_dids", None) is not None
            else None
        ),
        no_filter_dedup=args.no_filter_dedup,
        no_vbatt_cache=args.no_vbatt_cache,
        vbatt_ttl=args.vbatt_ttl,
        no_ioctl_cache=args.no_ioctl_cache,
        ioctl_ttl=args.ioctl_ttl,
    )

    benchmark_writer = JsonlBenchmarkWriter(args.benchmark_log) if args.benchmark_log else None
    server = ReverseProxyServer(
        args.listen_port,
        args.proxy_port,
        config,
        benchmark_writer=benchmark_writer,
        benchmark_label=args.benchmark_label,
    )
    _install_runtime_log_observability(server)
    logger.info("=" * 50)
    logger.info("VCI Proxy reverse connection server")
    logger.info("=" * 50)
    logger.info("VCI Proxy listen port: %s", args.listen_port)
    logger.info("Local proxy port: %s", args.proxy_port)
    logger.info("Auth: %s", "enabled" if config.auth.enabled else "disabled")
    logger.info("TLS: %s", "enabled" if config.tls.enabled else "disabled")
    logger.info(
        "ReadMsgs cache: %s (idle TTL=%sms, active TTL=%sms, adaptive active max=%sms, adaptive margin=%sms, active window=%sms, post-write bypass=%sms, max timeout=%sms)",
        "enabled" if config.read_msgs_cache.enabled else "disabled",
        config.read_msgs_cache.ttl_ms,
        config.read_msgs_cache.active_ttl_ms,
        config.read_msgs_cache.active_adaptive_ttl_max_ms,
        config.read_msgs_cache.active_adaptive_ttl_margin_ms,
        config.read_msgs_cache.active_window_ms,
        config.read_msgs_cache.post_write_bypass_ms,
        config.read_msgs_cache.max_cacheable_timeout_ms,
    )
    logger.info(
        "Read-ahead: %s (window=%sms, max_reads=%s, write_collect_max_reads=%s, timeout=%sms, max_messages=%s, max_empty_reads=%s, max_consecutive_empty_reads=%s, min_drain=%sms, transaction=%s, transaction_guard=%sms/%sms)",
        "enabled" if config.read_ahead.enabled else "disabled",
        config.read_ahead.window_ms,
        config.read_ahead.max_reads,
        config.read_ahead.write_collect_max_reads,
        config.read_ahead.read_timeout_ms,
        config.read_ahead.max_messages,
        config.read_ahead.max_empty_reads,
        config.read_ahead.max_consecutive_empty_reads,
        config.read_ahead.min_drain_ms,
        "enabled" if config.read_ahead.transaction_enabled else "disabled",
        config.read_ahead.transaction_max_network_ms,
        config.read_ahead.transaction_cooldown_ms,
    )
    logger.info(
        "Local sweep: %s (mode=%s, min_cycles=%s, max_items=%s, shadow_allow_gm_a9_packet=%s, min_item_interval_ms=%s, shadow_max_seconds=%s, plan_delay_ms=%s)",
        "enabled" if config.local_sweep.enabled else "disabled",
        config.local_sweep.mode,
        config.local_sweep.min_cycles,
        config.local_sweep.max_items,
        config.local_sweep.shadow_allow_gm_a9_packet,
        config.local_sweep.min_item_interval_ms,
        config.local_sweep.shadow_max_seconds,
        config.local_sweep.plan_delay_ms,
    )
    logger.info(
        "Filter dedup: %s",
        "enabled" if config.filter_dedup.enabled else "disabled",
    )
    logger.info(
        "VBATT cache: %s (TTL=%ss)",
        "enabled" if config.vbatt_cache.enabled else "disabled",
        config.vbatt_cache.ttl_s,
    )
    logger.info("=" * 50)

    try:
        _run_server_until_stopped(server)
    except KeyboardInterrupt:
        logger.warning("强制停止服务器...")
    except Exception as exc:
        server._emit_process_lifecycle_event(
            "process.lifecycle.failed",
            status="error",
            failure_code=type(exc).__name__,
            reason=str(exc),
        )
        raise


if __name__ == '__main__':
    main()
