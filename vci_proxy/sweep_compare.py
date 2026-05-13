"""Shadow-vs-real comparison helpers for local sweep validation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .protocol import ProtocolDecoder
from .sweep_protocol import SweepResultRecord


@dataclass(frozen=True)
class SweepCompareResult:
    outcome: str
    fields: dict[str, object]


def _read_response_shape(body: bytes) -> dict[str, object]:
    return_code, messages = ProtocolDecoder.decode_read_msgs_rsp(body)
    digest = hashlib.sha256()
    message_lengths: list[int] = []
    message_prefixes: list[str] = []
    for message in messages:
        data = bytes(message.get("data", b"") or b"")
        digest.update(int(message.get("protocol_id", 0) or 0).to_bytes(4, "big"))
        digest.update(int(message.get("rx_status", 0) or 0).to_bytes(4, "big"))
        digest.update(int(message.get("tx_flags", 0) or 0).to_bytes(4, "big"))
        digest.update(len(data).to_bytes(4, "big"))
        digest.update(data)
        message_lengths.append(len(data))
        message_prefixes.append(data[:16].hex())
    return {
        "return_code": return_code,
        "message_count": len(messages),
        "payload_digest": digest.hexdigest() if messages else None,
        "message_lengths": tuple(message_lengths),
        "message_prefixes": tuple(message_prefixes),
    }


def compare_shadow_to_real(
    *,
    signature_digest: str,
    real_read_rsp_body: bytes,
    shadow_result: SweepResultRecord | None,
    max_result_age_ms: int,
) -> SweepCompareResult:
    real_shape = _read_response_shape(real_read_rsp_body)
    common: dict[str, object] = {
        "sweep_signature_digest": signature_digest,
        "sweep_real_return_code": real_shape["return_code"],
        "sweep_real_message_count": real_shape["message_count"],
        "sweep_real_payload_digest": real_shape["payload_digest"],
        "sweep_real_message_lengths": list(real_shape["message_lengths"]),
        "sweep_real_message_prefixes": list(real_shape["message_prefixes"]),
    }
    if shadow_result is None:
        return SweepCompareResult("missing", common)

    common.update(
        {
            "sweep_plan_id": shadow_result.plan_id,
            "sweep_shadow_age_ms": round(shadow_result.age_ms, 3),
            "sweep_shadow_return_code": shadow_result.return_code,
        }
    )
    if shadow_result.age_ms > max_result_age_ms:
        return SweepCompareResult("stale", common)
    if shadow_result.error_name:
        return SweepCompareResult(
            "error",
            {**common, "sweep_shadow_error_name": shadow_result.error_name},
        )

    shadow_shape = _read_response_shape(shadow_result.read_rsp_body)
    common.update(
        {
            "sweep_shadow_message_count": shadow_shape["message_count"],
            "sweep_shadow_payload_digest": shadow_shape["payload_digest"],
            "sweep_shadow_message_lengths": list(shadow_shape["message_lengths"]),
            "sweep_shadow_message_prefixes": list(shadow_shape["message_prefixes"]),
        }
    )
    if shadow_shape == real_shape:
        return SweepCompareResult("match", common)
    return SweepCompareResult("mismatch", common)
