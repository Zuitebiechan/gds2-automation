"""JSON payloads for internal SWEEP_* tunnel control frames."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from .protocol import HEADER_SIZE, Message, MsgType


@dataclass(frozen=True)
class SweepRequestSpec:
    signature_digest: str
    write_req_body: bytes
    read_num_msgs: int = 1
    read_timeout_ms: int = 0

    def to_json(self) -> dict[str, object]:
        return {
            "signature_digest": self.signature_digest,
            "write_req_body_hex": self.write_req_body.hex(),
            "read_num_msgs": self.read_num_msgs,
            "read_timeout_ms": self.read_timeout_ms,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "SweepRequestSpec":
        return cls(
            signature_digest=str(data["signature_digest"]),
            write_req_body=bytes.fromhex(str(data["write_req_body_hex"])),
            read_num_msgs=max(1, int(data.get("read_num_msgs", 1))),
            read_timeout_ms=max(0, int(data.get("read_timeout_ms", 0))),
        )


@dataclass(frozen=True)
class SweepPlanStartRequest:
    plan_id: str
    connection_epoch: str
    channel_id: int
    max_result_age_ms: int
    min_item_interval_ms: int
    shadow_max_seconds: int
    requests: tuple[SweepRequestSpec, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "connection_epoch": self.connection_epoch,
            "channel_id": self.channel_id,
            "max_result_age_ms": self.max_result_age_ms,
            "min_item_interval_ms": self.min_item_interval_ms,
            "shadow_max_seconds": self.shadow_max_seconds,
            "requests": [request.to_json() for request in self.requests],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "SweepPlanStartRequest":
        return cls(
            plan_id=str(data["plan_id"]),
            connection_epoch=str(data["connection_epoch"]),
            channel_id=int(data["channel_id"]),
            max_result_age_ms=max(1, int(data["max_result_age_ms"])),
            min_item_interval_ms=max(0, int(data["min_item_interval_ms"])),
            shadow_max_seconds=max(1, int(data["shadow_max_seconds"])),
            requests=tuple(
                SweepRequestSpec.from_json(item)
                for item in data.get("requests", [])
            ),
        )


@dataclass(frozen=True)
class SweepPlanResponse:
    success: bool
    plan_id: str
    reason: str = "ok"

    def to_json(self) -> dict[str, object]:
        return {
            "success": self.success,
            "plan_id": self.plan_id,
            "reason": self.reason,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "SweepPlanResponse":
        return cls(
            success=bool(data.get("success")),
            plan_id=str(data.get("plan_id", "")),
            reason=str(data.get("reason", "")),
        )


@dataclass(frozen=True)
class SweepPlanStopRequest:
    plan_id: str
    reason: str

    def to_json(self) -> dict[str, object]:
        return {"plan_id": self.plan_id, "reason": self.reason}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "SweepPlanStopRequest":
        return cls(plan_id=str(data.get("plan_id", "")), reason=str(data.get("reason", "")))


@dataclass(frozen=True)
class SweepStatus:
    active_plan_id: str | None
    state: str
    queued_results: int
    error_count: int = 0

    def to_json(self) -> dict[str, object]:
        return {
            "active_plan_id": self.active_plan_id,
            "state": self.state,
            "queued_results": self.queued_results,
            "error_count": self.error_count,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "SweepStatus":
        raw_plan = data.get("active_plan_id")
        return cls(
            active_plan_id=str(raw_plan) if raw_plan else None,
            state=str(data.get("state", "unknown")),
            queued_results=int(data.get("queued_results", 0)),
            error_count=int(data.get("error_count", 0)),
        )


@dataclass(frozen=True)
class SweepResultRecord:
    plan_id: str
    signature_digest: str
    return_code: int
    read_rsp_body: bytes
    started_at_s: float
    finished_at_s: float
    error_name: str | None = None

    @property
    def age_ms(self) -> float:
        return max(0.0, (time.time() - self.finished_at_s) * 1000.0)

    def to_json(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "signature_digest": self.signature_digest,
            "return_code": self.return_code,
            "read_rsp_body_hex": self.read_rsp_body.hex(),
            "started_at_s": self.started_at_s,
            "finished_at_s": self.finished_at_s,
            "error_name": self.error_name,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "SweepResultRecord":
        return cls(
            plan_id=str(data["plan_id"]),
            signature_digest=str(data["signature_digest"]),
            return_code=int(data.get("return_code", 0)),
            read_rsp_body=bytes.fromhex(str(data.get("read_rsp_body_hex", ""))),
            started_at_s=float(data.get("started_at_s", 0.0)),
            finished_at_s=float(data.get("finished_at_s", 0.0)),
            error_name=(
                None
                if data.get("error_name") in (None, "")
                else str(data.get("error_name"))
            ),
        )


def _encode_json_payload(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _decode_json_payload(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    decoded = json.loads(body.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("Sweep payload must be a JSON object")
    return decoded


def encode_sweep_plan_start_req(request: SweepPlanStartRequest, *, sequence: int = 0) -> bytes:
    return Message(
        MsgType.SWEEP_PLAN_START_REQ,
        sequence,
        _encode_json_payload(request.to_json()),
    ).encode()


def decode_sweep_plan_start_req(body: bytes) -> SweepPlanStartRequest:
    return SweepPlanStartRequest.from_json(_decode_json_payload(body))


def encode_sweep_plan_start_rsp(response: SweepPlanResponse, *, sequence: int = 0) -> bytes:
    return Message(
        MsgType.SWEEP_PLAN_START_RSP,
        sequence,
        _encode_json_payload(response.to_json()),
    ).encode()


def decode_sweep_plan_start_rsp(body: bytes) -> SweepPlanResponse:
    return SweepPlanResponse.from_json(_decode_json_payload(body))


def encode_sweep_plan_stop_req(request: SweepPlanStopRequest, *, sequence: int = 0) -> bytes:
    return Message(
        MsgType.SWEEP_PLAN_STOP_REQ,
        sequence,
        _encode_json_payload(request.to_json()),
    ).encode()


def decode_sweep_plan_stop_req(body: bytes) -> SweepPlanStopRequest:
    return SweepPlanStopRequest.from_json(_decode_json_payload(body))


def encode_sweep_plan_stop_rsp(response: SweepPlanResponse, *, sequence: int = 0) -> bytes:
    return Message(
        MsgType.SWEEP_PLAN_STOP_RSP,
        sequence,
        _encode_json_payload(response.to_json()),
    ).encode()


def decode_sweep_plan_stop_rsp(body: bytes) -> SweepPlanResponse:
    return SweepPlanResponse.from_json(_decode_json_payload(body))


def encode_sweep_status_req(*, sequence: int = 0) -> bytes:
    return Message(MsgType.SWEEP_STATUS_REQ, sequence, b"").encode()


def encode_sweep_status_rsp(status: SweepStatus, *, sequence: int = 0) -> bytes:
    return Message(
        MsgType.SWEEP_STATUS_RSP,
        sequence,
        _encode_json_payload(status.to_json()),
    ).encode()


def decode_sweep_status_rsp(body: bytes) -> SweepStatus:
    return SweepStatus.from_json(_decode_json_payload(body))


def encode_sweep_drain_results_req(*, sequence: int = 0) -> bytes:
    return Message(MsgType.SWEEP_DRAIN_RESULTS_REQ, sequence, b"").encode()


def encode_sweep_drain_results_rsp(
    results: list[SweepResultRecord] | tuple[SweepResultRecord, ...],
    *,
    sequence: int = 0,
) -> bytes:
    return Message(
        MsgType.SWEEP_DRAIN_RESULTS_RSP,
        sequence,
        _encode_json_payload({"results": [result.to_json() for result in results]}),
    ).encode()


def decode_sweep_drain_results_rsp(body: bytes) -> tuple[SweepResultRecord, ...]:
    payload = _decode_json_payload(body)
    return tuple(
        SweepResultRecord.from_json(item)
        for item in payload.get("results", [])
    )


def is_sweep_message_type(msg_type: int) -> bool:
    return (
        MsgType.SWEEP_PLAN_START_REQ <= msg_type <= MsgType.SWEEP_DRAIN_RESULTS_REQ
        or MsgType.SWEEP_PLAN_START_RSP <= msg_type <= MsgType.SWEEP_DRAIN_RESULTS_RSP
    )


SWEEP_INTERNAL_REQUEST_RANGE = (
    int(MsgType.SWEEP_PLAN_START_REQ),
    int(MsgType.SWEEP_DRAIN_RESULTS_REQ),
)
SWEEP_INTERNAL_RESPONSE_RANGE = (
    int(MsgType.SWEEP_PLAN_START_RSP),
    int(MsgType.SWEEP_DRAIN_RESULTS_RSP),
)
