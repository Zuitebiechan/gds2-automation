"""Local allowlisted live-data decoding and snapshot persistence."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "proxy.local_live_data.v1"
ENGINE_SPEED_SIGNAL_KEY = "engine_speed"
ENGINE_SPEED_DISPLAY_NAME = "Engine Speed"
ENGINE_SPEED_UNIT = "RPM"


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
    foreground_priority_pause_count: int = 0

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
        request_kind = engine_speed_request_kind(write_messages)
        if request_kind is None:
            return []

        common = {
            "signal_key": ENGINE_SPEED_SIGNAL_KEY,
            "display_name": ENGINE_SPEED_DISPLAY_NAME,
            "unit": ENGINE_SPEED_UNIT,
            "request_kind": request_kind,
            "channel_id": int(channel_id),
            "sweep_plan_id": sweep_plan_id,
            "sweep_item_index": int(sweep_item_index),
            "sweep_signature_digest": sweep_signature_digest,
            "return_code": int(return_code),
            "message_count": len(read_messages),
            "poll_duration_ms": round(max(0.0, (finished_at_s - started_at_s) * 1000.0), 3),
            "foreground_priority_state": "foreground_idle",
            "backoff_reason": None,
        }
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
                    },
                )
            )
            events.append(("proxy.local_live_data.summary", self.summary_fields(reason=reason)))
            return events

        decoded = decode_engine_speed_from_messages(read_messages)
        if decoded is None:
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

        sample_age_ms = round(max(0.0, (time.time() - finished_at_s) * 1000.0), 3)
        self.sample_count += 1
        self._sample_ages_ms.append(sample_age_ms)
        self._poll_durations_ms.append(float(common["poll_duration_ms"]))
        self._decoder_counts[decoded.decoder_id] = self._decoder_counts.get(decoded.decoder_id, 0) + 1

        sample = {
            **common,
            "status": "ok",
            "reason": "sample_decoded",
            "value": decoded.value,
            "source": decoded.source,
            "decoder_id": decoded.decoder_id,
            "sample_ts": _format_timestamp(finished_at_s),
            "sample_age_ms": sample_age_ms,
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
            "foreground_priority_pause_count": self.foreground_priority_pause_count,
            "sample_age_ms_p50": _percentile(self._sample_ages_ms, 50),
            "sample_age_ms_p95": _percentile(self._sample_ages_ms, 95),
            "sample_age_ms_max": max(self._sample_ages_ms) if self._sample_ages_ms else None,
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
