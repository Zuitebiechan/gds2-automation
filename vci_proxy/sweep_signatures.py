"""Request signatures for the guarded local sweep scheduler."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import Literal


PAYLOAD_PREFIX_BYTES = 16


IdentifierKind = Literal["uds_did", "obd_pid", "gm_a9_packet"]


@dataclass(frozen=True)
class DiagnosticRequestShape:
    """Parsed read-only diagnostic request shape."""

    service_id: int
    identifier_kind: IdentifierKind
    identifier: int
    normalized_payload: bytes
    logical_ecu_target: int | None = None


@dataclass(frozen=True)
class SweepRequestSignature:
    """Exact in-memory match key for one read-only sweep item."""

    channel_id: int
    protocol_id: int
    tx_flags: int
    payload_digest: str
    service_id: int
    identifier_kind: IdentifierKind
    identifier: int
    connection_epoch: str | None
    filter_generation: int = 0
    session_generation: int = 0
    normalized_payload: bytes = b""
    logical_ecu_target: int | None = None

    @property
    def signature_digest(self) -> str:
        digest = hashlib.sha256()
        digest.update(int(self.channel_id).to_bytes(4, "big", signed=False))
        digest.update(int(self.protocol_id).to_bytes(4, "big", signed=False))
        digest.update(int(self.tx_flags).to_bytes(4, "big", signed=False))
        digest.update(str(self.connection_epoch or "").encode("utf-8"))
        digest.update(int(self.filter_generation).to_bytes(4, "big", signed=False))
        digest.update(int(self.session_generation).to_bytes(4, "big", signed=False))
        digest.update(int(self.service_id).to_bytes(1, "big", signed=False))
        digest.update(self.identifier_kind.encode("ascii"))
        digest.update(int(self.identifier).to_bytes(4, "big", signed=False))
        digest.update(self.normalized_payload)
        return digest.hexdigest()

    def to_observability(self) -> dict[str, object]:
        fields: dict[str, object] = {
            "sweep_signature_digest": self.signature_digest,
            "sweep_channel_id": self.channel_id,
            "sweep_protocol_id": self.protocol_id,
            "sweep_tx_flags": self.tx_flags,
            "sweep_payload_digest": self.payload_digest,
            "sweep_payload_prefix_hex": self.normalized_payload[:PAYLOAD_PREFIX_BYTES].hex(),
            "sweep_payload_length": len(self.normalized_payload),
            "sweep_service_id": self.service_id,
            "sweep_identifier_kind": self.identifier_kind,
            "sweep_identifier": self.identifier,
            "sweep_filter_generation": self.filter_generation,
            "sweep_session_generation": self.session_generation,
        }
        if self.logical_ecu_target is not None:
            fields["sweep_logical_ecu_target"] = self.logical_ecu_target
        return fields


@dataclass(frozen=True)
class SweepObservedRequest:
    """Allowlisted write request retained for later read-response correlation."""

    signature: SweepRequestSignature
    write_req_body: bytes
    read_num_msgs: int = 1
    read_timeout_ms: int = 0

    def with_read_request(
        self,
        *,
        read_num_msgs: int,
        read_timeout_ms: int,
    ) -> "SweepObservedRequest":
        return replace(
            self,
            read_num_msgs=max(1, int(read_num_msgs)),
            read_timeout_ms=max(0, int(read_timeout_ms)),
        )


def payload_digest(data: bytes) -> str:
    return hashlib.sha256(bytes(data)).hexdigest()


def parse_diagnostic_request_payload(data: bytes) -> DiagnosticRequestShape | None:
    """Parse exact read-only diagnostic request shapes.

    Accepted forms are either a raw diagnostic payload or a four-byte CAN ID
    prefix followed by the same payload, except GM A9 packet reads require the
    CAN ID prefix because that support is based on observed GDS2 Engine Data
    traffic:
    - 22 xx yy
    - can_id(4) 22 xx yy
    - 01 xx
    - can_id(4) 01 xx
    - can_id(4) a9 81 xx
    """
    raw = bytes(data or b"")
    logical_target = None
    payload = raw
    has_can_id_prefix = len(raw) in (6, 7)
    if has_can_id_prefix:
        logical_target = int.from_bytes(raw[:4], "big", signed=False)
        payload = raw[4:]

    if len(payload) == 3 and payload[0] == 0x22:
        return DiagnosticRequestShape(
            service_id=0x22,
            identifier_kind="uds_did",
            identifier=int.from_bytes(payload[1:3], "big", signed=False),
            normalized_payload=raw,
            logical_ecu_target=logical_target,
        )

    if len(payload) == 2 and payload[0] == 0x01:
        return DiagnosticRequestShape(
            service_id=0x01,
            identifier_kind="obd_pid",
            identifier=payload[1],
            normalized_payload=raw,
            logical_ecu_target=logical_target,
        )

    if (
        has_can_id_prefix
        and logical_target is not None
        and 0x7E0 <= logical_target <= 0x7EF
        and len(payload) == 3
        and payload[0] == 0xA9
        and payload[1] == 0x81
    ):
        return DiagnosticRequestShape(
            service_id=0xA9,
            identifier_kind="gm_a9_packet",
            identifier=int.from_bytes(payload[1:3], "big", signed=False),
            normalized_payload=raw,
            logical_ecu_target=logical_target,
        )

    return None


def make_signature(
    *,
    channel_id: int,
    message: dict,
    shape: DiagnosticRequestShape,
    connection_epoch: str | None,
    filter_generation: int = 0,
    session_generation: int = 0,
) -> SweepRequestSignature:
    payload = bytes(message.get("data", b"") or b"")
    return SweepRequestSignature(
        channel_id=int(channel_id),
        protocol_id=int(message.get("protocol_id", 0) or 0),
        tx_flags=int(message.get("tx_flags", 0) or 0),
        payload_digest=payload_digest(payload),
        service_id=shape.service_id,
        identifier_kind=shape.identifier_kind,
        identifier=shape.identifier,
        connection_epoch=connection_epoch,
        filter_generation=filter_generation,
        session_generation=session_generation,
        normalized_payload=shape.normalized_payload,
        logical_ecu_target=shape.logical_ecu_target,
    )
