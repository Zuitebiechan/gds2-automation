"""Local allowlisted live-data decoding and snapshot persistence."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from diagnostic_platform.observability import LogContext
from vci_proxy.cache_read_msgs import BUFFER_EMPTY
from vci_proxy.config import LocalLiveDataConfig


SCHEMA_VERSION = "proxy.local_live_data.v1"
ENGINE_SPEED_SIGNAL_KEY = "engine_speed"
ENGINE_SPEED_DISPLAY_NAME = "Engine Speed"
ENGINE_SPEED_UNIT = "RPM"
ENGINE_SPEED_UDS_REQUEST_DATA = b"\x00\x00\x07\xe0\x22\x00\x0c"
ENGINE_SPEED_UDS_REQUEST_TX_FLAGS = 64
ENGINE_SPEED_UDS_REQUEST_PROTOCOL_ID = 6

RunDriverCall = Callable[..., Awaitable[Any]]
ContextFactory = Callable[[str], LogContext]
EventEmitter = Callable[..., None]
ForegroundIdle = Callable[[], bool]
ChannelProvider = Callable[[], "LocalLiveDataChannel | None"]
SampleCallback = Callable[[Mapping[str, object]], None]


@dataclass(frozen=True)
class EngineSpeedDecode:
    value: float
    source: str
    decoder_id: str
    raw_value: int
    message_index: int
    raw_offset: int
    raw_prefix_hex: str
    raw_length: int


@dataclass(frozen=True)
class LocalLiveDataChannel:
    channel_id: int
    protocol_id: int


@dataclass
class LocalLiveDataMonitor:
    latest_path: Path = field(default_factory=lambda: get_latest_snapshot_path())
    _sample_ages_ms: list[float] = field(default_factory=list)
    _poll_durations_ms: list[float] = field(default_factory=list)
    _decoder_counts: dict[str, int] = field(default_factory=dict)
    _latest_sample: dict[str, object] | None = None
    sample_count: int = 0
    unsupported_count: int = 0
    backoff_count: int = 0
    timeout_count: int = 0
    negative_response_count: int = 0
    return_code_warning_count: int = 0
    foreground_priority_pause_count: int = 0
    _sample_gap_ms: list[float] = field(default_factory=list)
    _latest_sample_monotonic_s: float | None = None

    def record_sweep_item_finished(
        self,
        *,
        write_messages: Sequence[Mapping[str, Any]],
        read_messages: Sequence[Mapping[str, Any]],
        return_code: int,
        started_at_s: float,
        finished_at_s: float,
        channel_id: int,
        sweep_plan_id: str,
        sweep_item_index: int,
        sweep_signature_digest: str,
        read_observability: Mapping[str, Any] | None = None,
    ) -> list[tuple[str, dict[str, object]]]:
        return self.record_poll_result(
            write_messages=write_messages,
            read_messages=read_messages,
            return_code=return_code,
            started_at_s=started_at_s,
            finished_at_s=finished_at_s,
            channel_id=channel_id,
            poll_id=f"{sweep_plan_id}:{sweep_item_index}",
            request_origin="shadow_local",
            sweep_plan_id=sweep_plan_id,
            sweep_item_index=sweep_item_index,
            sweep_signature_digest=sweep_signature_digest,
            read_observability=read_observability,
        )

    def record_poll_result(
        self,
        *,
        write_messages: Sequence[Mapping[str, Any]],
        read_messages: Sequence[Mapping[str, Any]],
        return_code: int,
        started_at_s: float,
        finished_at_s: float,
        channel_id: int,
        poll_id: str,
        request_origin: str,
        sweep_plan_id: str | None = None,
        sweep_item_index: int | None = None,
        sweep_signature_digest: str | None = None,
        read_observability: Mapping[str, Any] | None = None,
    ) -> list[tuple[str, dict[str, object]]]:
        request_kind = engine_speed_request_kind(write_messages)
        if request_kind is None:
            return []

        common = {
            "signal_key": ENGINE_SPEED_SIGNAL_KEY,
            "display_name": ENGINE_SPEED_DISPLAY_NAME,
            "unit": ENGINE_SPEED_UNIT,
            "request_kind": request_kind,
            "channel_id": int(channel_id),
            "request_origin": request_origin,
            "poll_id": poll_id,
            "return_code": int(return_code),
            "message_count": len(read_messages),
            "poll_duration_ms": round(max(0.0, (finished_at_s - started_at_s) * 1000.0), 3),
            "foreground_priority_state": "foreground_idle",
            "backoff_reason": None,
        }
        if sweep_plan_id is not None:
            common["sweep_plan_id"] = sweep_plan_id
        if sweep_item_index is not None:
            common["sweep_item_index"] = int(sweep_item_index)
        if sweep_signature_digest is not None:
            common["sweep_signature_digest"] = sweep_signature_digest
        if read_observability:
            common.update(
                {
                    "read_timeout_ms": read_observability.get("read_timeout_ms"),
                    "tail_read_triggered": read_observability.get("tail_read_triggered"),
                    "tail_read_attempts": read_observability.get("tail_read_attempts"),
                    "tail_read_data_reads": read_observability.get("tail_read_data_reads"),
                }
            )

        events: list[tuple[str, dict[str, object]]] = []
        decoded = decode_engine_speed_from_messages(read_messages)
        if decoded is None:
            if int(return_code) != 0:
                reason = "read_return_code"
                self.backoff_count += 1
                if _looks_like_timeout_return_code(return_code):
                    self.timeout_count += 1
                    reason = "timeout_return_code"
                events.append(
                    (
                        "proxy.local_live_data.backoff",
                        {
                            **common,
                            "status": "error",
                            "failure_code": reason,
                            "failure_domain": "vehicle_or_vci",
                            "reason": reason,
                            "backoff_reason": reason,
                            "raw_prefix_hex": _first_raw_prefix_hex(read_messages),
                        },
                    )
                )
                events.append(("proxy.local_live_data.summary", self.summary_fields(reason=reason)))
                return events

            negative = _contains_negative_response(read_messages)
            if negative:
                self.negative_response_count += 1
            else:
                self.unsupported_count += 1
            reason = "negative_response" if negative else "no_supported_engine_speed_shape"
            event_type = (
                "proxy.local_live_data.backoff"
                if negative or not read_messages
                else "proxy.local_live_data.unsupported"
            )
            if not read_messages:
                self.backoff_count += 1
                reason = "no_messages"
            fields = {
                **common,
                "status": "blocked" if event_type.endswith(".backoff") else "ok",
                "reason": reason,
                "backoff_reason": reason if event_type.endswith(".backoff") else None,
                "raw_prefix_hex": _first_raw_prefix_hex(read_messages),
            }
            events.append((event_type, fields))
            events.append(("proxy.local_live_data.summary", self.summary_fields(reason=reason)))
            return events

        return_code_warning = _sample_return_code_warning_reason(return_code)
        if return_code_warning is not None:
            self.return_code_warning_count += 1

        sample_age_ms = round(max(0.0, (time.time() - finished_at_s) * 1000.0), 3)
        now_mono = time.monotonic()
        sample_gap_ms = None
        if self._latest_sample_monotonic_s is not None:
            sample_gap_ms = round(max(0.0, (now_mono - self._latest_sample_monotonic_s) * 1000.0), 3)
            self._sample_gap_ms.append(sample_gap_ms)
        self._latest_sample_monotonic_s = now_mono
        self.sample_count += 1
        self._sample_ages_ms.append(sample_age_ms)
        self._poll_durations_ms.append(float(common["poll_duration_ms"]))
        self._decoder_counts[decoded.decoder_id] = self._decoder_counts.get(decoded.decoder_id, 0) + 1

        sample = {
            **common,
            "status": "ok",
            "reason": (
                "sample_decoded_with_return_code_warning"
                if return_code_warning is not None
                else "sample_decoded"
            ),
            "value": decoded.value,
            "source": decoded.source,
            "decoder_id": decoded.decoder_id,
            "j2534_return_code_warning": return_code_warning is not None,
            "j2534_return_code_warning_reason": return_code_warning,
            "sample_ts": _format_timestamp(finished_at_s),
            "sample_age_ms": sample_age_ms,
            "sample_gap_ms": sample_gap_ms,
            "raw_value": decoded.raw_value,
            "raw_offset": decoded.raw_offset,
            "raw_prefix_hex": decoded.raw_prefix_hex,
            "raw_length": decoded.raw_length,
            "message_index": decoded.message_index,
        }
        self._latest_sample = dict(sample)
        events.append(("proxy.local_live_data.sample", sample))
        try:
            self.write_latest_snapshot()
        except OSError as exc:
            self.backoff_count += 1
            events.append(
                (
                    "proxy.local_live_data.backoff",
                    {
                        **common,
                        "status": "error",
                        "failure_code": "snapshot_write_failed",
                        "failure_domain": "local_reverse_client",
                        "reason": "snapshot_write_failed",
                        "backoff_reason": "snapshot_write_failed",
                        "snapshot_path": str(self.latest_path),
                        "error": str(exc),
                    },
                )
            )
        events.append(("proxy.local_live_data.summary", self.summary_fields(reason="sample_decoded")))
        return events

    def summary_fields(self, *, reason: str = "summary") -> dict[str, object]:
        latest = self._latest_sample or {}
        return {
            "status": "ok",
            "reason": reason,
            "signal_key": ENGINE_SPEED_SIGNAL_KEY,
            "display_name": ENGINE_SPEED_DISPLAY_NAME,
            "sample_count": self.sample_count,
            "unsupported_count": self.unsupported_count,
            "backoff_count": self.backoff_count,
            "timeout_count": self.timeout_count,
            "negative_response_count": self.negative_response_count,
            "return_code_warning_count": self.return_code_warning_count,
            "foreground_priority_pause_count": self.foreground_priority_pause_count,
            "sample_age_ms_p50": _percentile(self._sample_ages_ms, 50),
            "sample_age_ms_p95": _percentile(self._sample_ages_ms, 95),
            "sample_age_ms_max": max(self._sample_ages_ms) if self._sample_ages_ms else None,
            "sample_gap_ms_p50": _percentile(self._sample_gap_ms, 50),
            "sample_gap_ms_p95": _percentile(self._sample_gap_ms, 95),
            "sample_gap_ms_max": max(self._sample_gap_ms) if self._sample_gap_ms else None,
            "poll_duration_ms_p50": _percentile(self._poll_durations_ms, 50),
            "poll_duration_ms_p95": _percentile(self._poll_durations_ms, 95),
            "poll_duration_ms_max": max(self._poll_durations_ms) if self._poll_durations_ms else None,
            "latest_value": latest.get("value"),
            "latest_source": latest.get("source"),
            "latest_decoder_id": latest.get("decoder_id"),
            "decoder_id_counts": dict(self._decoder_counts),
        }

    def snapshot_payload(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": _format_timestamp(time.time()),
            "latest_sample": self._latest_sample,
            "signals": (
                {ENGINE_SPEED_SIGNAL_KEY: self._latest_sample}
                if self._latest_sample is not None
                else {}
            ),
            "summary": self.summary_fields(reason="snapshot"),
        }

    def write_latest_snapshot(self) -> Path:
        return write_latest_snapshot(self.latest_path, self.snapshot_payload())

    def record_collector_pause(
        self,
        *,
        reason: str,
        channel_id: int | None = None,
        poll_id: str | None = None,
    ) -> list[tuple[str, dict[str, object]]]:
        if reason == "foreground_busy":
            self.foreground_priority_pause_count += 1
        else:
            self.backoff_count += 1
        fields: dict[str, object] = {
            "status": "blocked",
            "signal_key": ENGINE_SPEED_SIGNAL_KEY,
            "display_name": ENGINE_SPEED_DISPLAY_NAME,
            "unit": ENGINE_SPEED_UNIT,
            "request_kind": "uds_did_000c",
            "request_origin": "local_live_data_collector",
            "reason": reason,
            "backoff_reason": reason,
            "foreground_priority_state": (
                "foreground_busy" if reason == "foreground_busy" else "foreground_idle"
            ),
        }
        if channel_id is not None:
            fields["channel_id"] = int(channel_id)
        if poll_id is not None:
            fields["poll_id"] = poll_id
        return [
            ("proxy.local_live_data.backoff", fields),
            ("proxy.local_live_data.summary", self.summary_fields(reason=reason)),
        ]


class LocalLiveDataCollector:
    """Opportunistic local Engine Speed collector with foreground priority."""

    def __init__(
        self,
        *,
        config: LocalLiveDataConfig,
        monitor: LocalLiveDataMonitor,
        run_driver_call: RunDriverCall,
        context_factory: ContextFactory,
        emit_event: EventEmitter,
        foreground_idle: ForegroundIdle,
        channel_provider: ChannelProvider,
        on_sample: SampleCallback | None = None,
    ) -> None:
        self._config = config
        self._monitor = monitor
        self._run_driver_call = run_driver_call
        self._context_factory = context_factory
        self._emit_event = emit_event
        self._foreground_idle = foreground_idle
        self._channel_provider = channel_provider
        self._on_sample = on_sample
        self._task: asyncio.Task | None = None
        self._stop_requested = False
        self._poll_counter = 0
        self._consecutive_errors = 0
        self._stop_event: asyncio.Event | None = None
        self._stop_emitted = False

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, *, reason: str = "enabled") -> bool:
        if not self._config.enabled or self.running:
            return False
        self._stop_requested = False
        self._stop_emitted = False
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run_loop(), name="local-live-data-collector")
        self._emit_event(
            "proxy.local_live_data.collector.started",
            context=self._context_factory("LOCAL_LIVE_DATA"),
            impact_scope="proxy_local_live_data",
            reason=reason,
            source=self._config.source,
            interval_ms=self._config.interval_ms,
            read_timeout_ms=self._config.read_timeout_ms,
            max_consecutive_errors=self._config.max_consecutive_errors,
        )
        return True

    async def stop(self, reason: str = "stopped") -> None:
        task = self._task
        self._stop_requested = True
        stop_event = self._stop_event
        if stop_event is not None:
            stop_event.set()
        if task is None:
            return
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        if self._task is task:
            self._task = None
        self._stop_event = None
        self._emit_stopped(reason)

    def request_stop(self, reason: str = "stopped") -> None:
        task = self._task
        if task is None:
            return
        self._stop_requested = True
        stop_event = self._stop_event
        if stop_event is not None:
            stop_event.set()
        task.add_done_callback(self._clear_finished_task)
        self._emit_stopped(reason)

    async def poll_once(self) -> list[tuple[str, dict[str, object]]]:
        self._poll_counter += 1
        poll_id = f"local-live-data-{self._poll_counter}"
        channel = self._channel_provider()
        if channel is None:
            events = self._monitor.record_collector_pause(
                reason="no_channel",
                poll_id=poll_id,
            )
            self._consecutive_errors += 1
            self._emit_events(events)
            return events
        if channel.protocol_id != ENGINE_SPEED_UDS_REQUEST_PROTOCOL_ID:
            events = self._monitor.record_collector_pause(
                reason="unsupported_protocol",
                channel_id=channel.channel_id,
                poll_id=poll_id,
            )
            self._consecutive_errors += 1
            self._emit_events(events)
            return events
        if not self._foreground_idle():
            events = self._monitor.record_collector_pause(
                reason="foreground_busy",
                channel_id=channel.channel_id,
                poll_id=poll_id,
            )
            self._emit_events(events)
            return events

        context = self._context_factory("LOCAL_LIVE_DATA")
        request_message = engine_speed_uds_request_message(protocol_id=channel.protocol_id)
        started_mono = time.monotonic()
        started_at_s = time.time()
        write_ret, _num_written = await self._run_driver_call(
            "write_msgs",
            channel.channel_id,
            [request_message],
            self._config.write_timeout_ms,
            request_context=context,
            result_metadata={
                "channel_id": channel.channel_id,
                "proxy_local_live_data": True,
                "signal_key": ENGINE_SPEED_SIGNAL_KEY,
                "request_kind": "uds_did_000c",
            },
        )
        if int(write_ret) != 0:
            finished_at_s = time.time()
            events = self._monitor.record_poll_result(
                write_messages=[request_message],
                read_messages=[],
                return_code=int(write_ret),
                started_at_s=started_at_s,
                finished_at_s=finished_at_s,
                channel_id=channel.channel_id,
                poll_id=poll_id,
                request_origin="local_live_data_collector",
                read_observability={
                    "read_timeout_ms": self._config.read_timeout_ms,
                    "tail_read_triggered": False,
                    "tail_read_attempts": 0,
                    "tail_read_data_reads": 0,
                },
            )
            self._consecutive_errors += 1
            self._emit_events(events)
            return events

        current_channel = self._channel_provider()
        if (
            self._stop_requested
            or current_channel is None
            or current_channel.channel_id != channel.channel_id
            or current_channel.protocol_id != channel.protocol_id
        ):
            events = self._monitor.record_collector_pause(
                reason="stopped_before_read",
                channel_id=channel.channel_id,
                poll_id=poll_id,
            )
            self._emit_events(events)
            return events

        if not self._foreground_idle():
            events = self._monitor.record_collector_pause(
                reason="foreground_busy",
                channel_id=channel.channel_id,
                poll_id=poll_id,
            )
            self._emit_events(events)
            return events

        read_ret, read_messages = await self._run_driver_call(
            "read_msgs",
            channel.channel_id,
            self._config.read_num_msgs,
            self._config.read_timeout_ms,
            request_context=context,
            ok_codes=(0, BUFFER_EMPTY),
            warning_codes=(9,),
            warning_requires_payload=True,
            result_metadata={
                "channel_id": channel.channel_id,
                "proxy_local_live_data": True,
                "signal_key": ENGINE_SPEED_SIGNAL_KEY,
                "request_kind": "uds_did_000c",
            },
        )
        finished_at_s = time.time()
        elapsed_ms = round((time.monotonic() - started_mono) * 1000.0, 3)
        materialized = list(read_messages)
        events = self._monitor.record_poll_result(
            write_messages=[request_message],
            read_messages=materialized,
            return_code=int(read_ret),
            started_at_s=started_at_s,
            finished_at_s=finished_at_s,
            channel_id=channel.channel_id,
            poll_id=poll_id,
            request_origin="local_live_data_collector",
            read_observability={
                "read_timeout_ms": self._config.read_timeout_ms,
                "tail_read_triggered": False,
                "tail_read_attempts": 0,
                "tail_read_data_reads": 0,
                "collector_elapsed_ms": elapsed_ms,
            },
        )
        if any(event_type == "proxy.local_live_data.sample" for event_type, _fields in events):
            self._consecutive_errors = 0
        elif any(event_type == "proxy.local_live_data.backoff" for event_type, _fields in events):
            self._consecutive_errors += 1
        self._emit_events(events)
        return events

    async def _run_loop(self) -> None:
        interval_s = max(0.001, self._config.interval_ms / 1000.0)
        while not self._stop_requested:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._consecutive_errors += 1
                self._emit_event(
                    "proxy.local_live_data.backoff",
                    context=self._context_factory("LOCAL_LIVE_DATA"),
                    status="error",
                    failure_code="collector_poll_failed",
                    failure_domain="local_reverse_client",
                    reason="collector_poll_failed",
                    impact_scope="proxy_local_live_data",
                    error=f"{type(exc).__name__}:{exc}",
                    consecutive_errors=self._consecutive_errors,
                )
            sleep_s = interval_s
            if self._consecutive_errors >= self._config.max_consecutive_errors:
                sleep_s = max(sleep_s, interval_s * self._config.max_consecutive_errors)
            stop_event = self._stop_event
            if stop_event is None:
                await asyncio.sleep(sleep_s)
                continue
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=sleep_s)
            except asyncio.TimeoutError:
                pass

    def _clear_finished_task(self, task: asyncio.Task) -> None:
        _consume_finished_task(task)
        if self._task is task:
            self._task = None
            self._stop_event = None

    def _emit_stopped(self, reason: str) -> None:
        if self._stop_emitted:
            return
        self._stop_emitted = True
        self._emit_event(
            "proxy.local_live_data.collector.stopped",
            context=self._context_factory("LOCAL_LIVE_DATA"),
            impact_scope="proxy_local_live_data",
            reason=reason,
            sample_count=self._monitor.sample_count,
            backoff_count=self._monitor.backoff_count,
            foreground_priority_pause_count=self._monitor.foreground_priority_pause_count,
        )

    def _emit_events(self, events: Sequence[tuple[str, dict[str, object]]]) -> None:
        for event_type, fields in events:
            enriched = {
                "collector_source": self._config.source,
                "collector_interval_ms": self._config.interval_ms,
                "read_timeout_ms": self._config.read_timeout_ms,
                "consecutive_errors": self._consecutive_errors,
                **fields,
            }
            self._emit_event(
                event_type,
                context=self._context_factory("LOCAL_LIVE_DATA"),
                impact_scope="proxy_local_live_data",
                **enriched,
            )
            if event_type == "proxy.local_live_data.sample" and self._on_sample is not None:
                self._on_sample(enriched)


def get_latest_snapshot_path(appdata: str | Path | None = None) -> Path:
    base = Path(
        appdata
        if appdata is not None
        else os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
    )
    return base / "VCI_Proxy" / "live_data" / "latest.json"


def write_latest_snapshot(path: str | Path, payload: Mapping[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(f"{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(
            json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temp_path, target)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
    return target


def _consume_finished_task(task: asyncio.Task) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


def engine_speed_uds_request_message(*, protocol_id: int = ENGINE_SPEED_UDS_REQUEST_PROTOCOL_ID) -> dict[str, object]:
    return {
        "protocol_id": int(protocol_id),
        "rx_status": 0,
        "tx_flags": ENGINE_SPEED_UDS_REQUEST_TX_FLAGS,
        "timestamp": 0,
        "data": ENGINE_SPEED_UDS_REQUEST_DATA,
    }


def engine_speed_request_kind(messages: Sequence[Mapping[str, Any]]) -> str | None:
    for message in messages:
        data = _message_data(message)
        if _marker_offset(data, b"\x01\x0c") is not None:
            return "obd_mode01_pid_0c"
        if _marker_offset(data, b"\x22\x00\x0c") is not None:
            return "uds_did_000c"
    return None


def decode_engine_speed_from_messages(
    messages: Sequence[Mapping[str, Any]],
) -> EngineSpeedDecode | None:
    for index, message in enumerate(messages):
        decoded = decode_engine_speed_frame(_message_data(message), message_index=index)
        if decoded is not None:
            return decoded
    return None


def decode_engine_speed_frame(data: bytes, *, message_index: int = 0) -> EngineSpeedDecode | None:
    candidates = (
        (b"\x41\x0c", 2, "proxy_local_obd", "obd_mode01_pid_0c"),
        (b"\x62\x00\x0c", 3, "proxy_local_known_uds", "uds_did_000c_engine_speed"),
    )
    for marker, value_offset, source, decoder_id in candidates:
        marker_offset = _marker_offset(data, marker)
        if marker_offset is None:
            continue
        value_start = marker_offset + value_offset
        if value_start + 2 > len(data):
            continue
        raw_value = int.from_bytes(data[value_start:value_start + 2], "big")
        return EngineSpeedDecode(
            value=raw_value / 4.0,
            source=source,
            decoder_id=decoder_id,
            raw_value=raw_value,
            message_index=message_index,
            raw_offset=marker_offset,
            raw_prefix_hex=data[:16].hex(),
            raw_length=len(data),
        )
    return None


def _message_data(message: Mapping[str, Any]) -> bytes:
    return bytes(message.get("data", b"") or b"")


def _marker_offset(data: bytes, marker: bytes) -> int | None:
    for base_offset, payload in _payload_views(data):
        if payload.startswith(marker):
            return base_offset
        if (
            len(payload) > len(marker)
            and 0 <= payload[0] <= 0x0F
            and payload[1:].startswith(marker)
        ):
            return base_offset + 1
    return None


def _payload_views(data: bytes) -> tuple[tuple[int, bytes], ...]:
    views: list[tuple[int, bytes]] = [(0, data)]
    if len(data) >= 5:
        can_id = int.from_bytes(data[:4], "big", signed=False)
        if 0x500 <= can_id <= 0x7FF:
            views.insert(0, (4, data[4:]))
    return tuple(views)


def _contains_negative_response(messages: Sequence[Mapping[str, Any]]) -> bool:
    return any(_marker_offset(_message_data(message), b"\x7f") is not None for message in messages)


def _first_raw_prefix_hex(messages: Sequence[Mapping[str, Any]]) -> str | None:
    if not messages:
        return None
    return _message_data(messages[0])[:16].hex()


def _looks_like_timeout_return_code(return_code: int) -> bool:
    return int(return_code) in {0x10, 0x09}


def _sample_return_code_warning_reason(return_code: int) -> str | None:
    if int(return_code) == 0:
        return None
    if _looks_like_timeout_return_code(return_code):
        return "timeout_return_code_with_data"
    return "read_return_code_with_data"


def _percentile(values: Sequence[float], percentile: int) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = max(0, min(len(ordered) - 1, round((percentile / 100.0) * (len(ordered) - 1))))
    return ordered[rank]


def _format_timestamp(epoch_s: float) -> str:
    return datetime.fromtimestamp(epoch_s, tz=timezone.utc).isoformat().replace("+00:00", "Z")
