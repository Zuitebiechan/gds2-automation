"""Read-only request classifier for local sweep observe/shadow modes."""

from __future__ import annotations

from dataclasses import dataclass

from .config import LocalSweepConfig
from .protocol import ProtocolDecoder
from .sweep_signatures import (
    SweepObservedRequest,
    make_signature,
    parse_diagnostic_request_payload,
)


@dataclass(frozen=True)
class SweepClassification:
    accepted: bool
    reason: str
    observed: SweepObservedRequest | None = None


class SweepReadOnlyClassifier:
    """Allowlist-only classifier for read-only diagnostic write requests."""

    def __init__(self, config: LocalSweepConfig):
        self._config = config

    def classify_write_request_body(
        self,
        body: bytes,
        *,
        connection_epoch: str | None,
        filter_generation: int = 0,
        session_generation: int = 0,
        read_num_msgs: int = 1,
        read_timeout_ms: int = 0,
    ) -> SweepClassification:
        if not self._config.enabled:
            return SweepClassification(False, "local_sweep_disabled")

        try:
            channel_id, messages, _timeout = ProtocolDecoder.decode_write_msgs_req(body)
        except Exception:
            return SweepClassification(False, "malformed_write_request")

        if len(messages) != 1:
            return SweepClassification(False, "write_message_count_not_one")

        message = messages[0]
        payload = bytes(message.get("data", b"") or b"")
        shape = parse_diagnostic_request_payload(payload)
        if shape is None:
            return SweepClassification(False, "not_allowlisted_read_only_shape")

        if shape.identifier_kind == "uds_did" and not self._config.allow_uds_rdbi:
            return SweepClassification(False, "uds_rdbi_disabled")
        if shape.identifier_kind == "obd_pid" and not self._config.allow_obd_mode01:
            return SweepClassification(False, "obd_mode01_disabled")

        signature = make_signature(
            channel_id=channel_id,
            message=message,
            shape=shape,
            connection_epoch=connection_epoch,
            filter_generation=filter_generation,
            session_generation=session_generation,
        )
        return SweepClassification(
            True,
            "allowlisted_read_only",
            SweepObservedRequest(
                signature=signature,
                write_req_body=bytes(body),
                read_num_msgs=max(1, int(read_num_msgs)),
                read_timeout_ms=max(0, int(read_timeout_ms)),
            ),
        )
