"""VCI proxy wire protocol helpers."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import List, Optional, Tuple


MAGIC = 0x4A325334  # "J254" in ASCII
HEADER_SIZE = 14
PREFETCH_MAGIC = 0x50524630  # "PRF0"


class MsgType(IntEnum):
    # Requests (0x00xx)
    OPEN_REQ = 0x0001
    CLOSE_REQ = 0x0002
    CONNECT_REQ = 0x0003
    DISCONNECT_REQ = 0x0004
    READ_MSGS_REQ = 0x0005
    WRITE_MSGS_REQ = 0x0006
    IOCTL_REQ = 0x0007
    START_FILTER_REQ = 0x0010
    STOP_FILTER_REQ = 0x0011
    READ_VERSION_REQ = 0x0020
    GET_LAST_ERROR_REQ = 0x0021
    PING_REQ = 0x00FC
    AUTH_REQ = 0x00FE
    HEARTBEAT = 0x00FF
    WRITE_AND_COLLECT_READS_REQ = 0x0106

    # Responses (0x80xx)
    OPEN_RSP = 0x8001
    CLOSE_RSP = 0x8002
    CONNECT_RSP = 0x8003
    DISCONNECT_RSP = 0x8004
    READ_MSGS_RSP = 0x8005
    WRITE_MSGS_RSP = 0x8006
    IOCTL_RSP = 0x8007
    START_FILTER_RSP = 0x8010
    STOP_FILTER_RSP = 0x8011
    READ_VERSION_RSP = 0x8020
    GET_LAST_ERROR_RSP = 0x8021
    PING_RSP = 0x80FC
    AUTH_RSP = 0x80FE
    HEARTBEAT_ACK = 0x80FF


MSG_NAMES = {value: value.name for value in MsgType}


@dataclass
class Message:
    msg_type: int
    sequence: int
    body: bytes

    def encode(self) -> bytes:
        length = HEADER_SIZE + len(self.body)
        header = struct.pack(">IIHI", MAGIC, length, self.msg_type, self.sequence)
        return header + self.body

    @classmethod
    def decode_header(cls, data: bytes) -> Tuple[int, int, int, int]:
        if len(data) < HEADER_SIZE:
            raise ValueError(f"Header too short: {len(data)} < {HEADER_SIZE}")
        return struct.unpack(">IIHI", data[:HEADER_SIZE])


@dataclass(frozen=True)
class ReadMsgsPrefetchBundle:
    """Internal bundle of local-side ReadMsgs responses."""
    channel_id: int
    read_rsp_bodies: Tuple[bytes, ...]


@dataclass(frozen=True)
class WriteAndCollectReadsRequest:
    """Internal write transaction plus bounded local ReadMsgs collection."""
    collect_window_ms: int
    max_reads: int
    read_timeout_ms: int
    max_messages: int
    write_req_body: bytes


def attach_read_msgs_prefetch_bundle(
    encoded_response: bytes,
    *,
    channel_id: int,
    read_rsp_bodies: List[bytes] | Tuple[bytes, ...],
) -> bytes:
    """Append an internal read-ahead bundle to a WRITE_MSGS_RSP frame.

    The bundle lives inside the response body and is stripped by the reverse
    server before replying to the virtual DLL. It is not part of the public
    J2534-facing wire contract.
    """
    if not read_rsp_bodies or len(encoded_response) < HEADER_SIZE:
        return encoded_response

    magic, old_length, msg_type, _sequence = Message.decode_header(
        encoded_response[:HEADER_SIZE]
    )
    if magic != MAGIC or msg_type != MsgType.WRITE_MSGS_RSP:
        return encoded_response

    bundle = struct.pack(">III", PREFETCH_MAGIC, channel_id, len(read_rsp_bodies))
    for rsp_body in read_rsp_bodies:
        bundle += struct.pack(">I", len(rsp_body)) + rsp_body

    new_length = old_length + len(bundle)
    return (
        encoded_response[:4]
        + struct.pack(">I", new_length)
        + encoded_response[8:]
        + bundle
    )


def strip_read_msgs_prefetch_bundle(
    write_rsp_body: bytes,
) -> Tuple[bytes, Optional[ReadMsgsPrefetchBundle]]:
    """Strip an internal read-ahead bundle from a WRITE_MSGS_RSP body."""
    base_len = 8
    if len(write_rsp_body) < base_len + 12:
        return write_rsp_body, None

    marker = struct.unpack(">I", write_rsp_body[base_len:base_len + 4])[0]
    if marker != PREFETCH_MAGIC:
        return write_rsp_body, None

    channel_id, response_count = struct.unpack(
        ">II", write_rsp_body[base_len + 4:base_len + 12]
    )
    offset = base_len + 12
    read_rsp_bodies: list[bytes] = []
    for _ in range(response_count):
        if offset + 4 > len(write_rsp_body):
            raise ValueError("ReadMsgs prefetch bundle truncated before response length")
        rsp_len = struct.unpack(">I", write_rsp_body[offset:offset + 4])[0]
        offset += 4
        if offset + rsp_len > len(write_rsp_body):
            raise ValueError("ReadMsgs prefetch bundle truncated before response body")
        read_rsp_bodies.append(write_rsp_body[offset:offset + rsp_len])
        offset += rsp_len

    if offset != len(write_rsp_body):
        raise ValueError("ReadMsgs prefetch bundle has trailing bytes")

    return write_rsp_body[:base_len], ReadMsgsPrefetchBundle(
        channel_id=channel_id,
        read_rsp_bodies=tuple(read_rsp_bodies),
    )


class ProtocolEncoder:
    @staticmethod
    def encode_open_req(device_name: Optional[str] = None, sequence: int = 0) -> bytes:
        body = device_name.encode("utf-8") + b"\x00" if device_name else b""
        return Message(MsgType.OPEN_REQ, sequence, body).encode()

    @staticmethod
    def encode_open_rsp(return_code: int, device_id: int, sequence: int = 0) -> bytes:
        body = struct.pack(">II", return_code, device_id)
        return Message(MsgType.OPEN_RSP, sequence, body).encode()

    @staticmethod
    def encode_close_req(device_id: int, sequence: int = 0) -> bytes:
        body = struct.pack(">I", device_id)
        return Message(MsgType.CLOSE_REQ, sequence, body).encode()

    @staticmethod
    def encode_close_rsp(return_code: int, sequence: int = 0) -> bytes:
        body = struct.pack(">I", return_code)
        return Message(MsgType.CLOSE_RSP, sequence, body).encode()

    @staticmethod
    def encode_connect_req(
        device_id: int,
        protocol_id: int,
        flags: int,
        baudrate: int,
        sequence: int = 0,
    ) -> bytes:
        body = struct.pack(">IIII", device_id, protocol_id, flags, baudrate)
        return Message(MsgType.CONNECT_REQ, sequence, body).encode()

    @staticmethod
    def encode_connect_rsp(return_code: int, channel_id: int, sequence: int = 0) -> bytes:
        body = struct.pack(">II", return_code, channel_id)
        return Message(MsgType.CONNECT_RSP, sequence, body).encode()

    @staticmethod
    def encode_disconnect_req(channel_id: int, sequence: int = 0) -> bytes:
        body = struct.pack(">I", channel_id)
        return Message(MsgType.DISCONNECT_REQ, sequence, body).encode()

    @staticmethod
    def encode_disconnect_rsp(return_code: int, sequence: int = 0) -> bytes:
        body = struct.pack(">I", return_code)
        return Message(MsgType.DISCONNECT_RSP, sequence, body).encode()

    @staticmethod
    def encode_read_msgs_req(
        channel_id: int,
        num_msgs: int,
        timeout: int,
        sequence: int = 0,
    ) -> bytes:
        body = struct.pack(">III", channel_id, num_msgs, timeout)
        return Message(MsgType.READ_MSGS_REQ, sequence, body).encode()

    @staticmethod
    def encode_read_msgs_rsp(
        return_code: int,
        messages: List[dict],
        sequence: int = 0,
    ) -> bytes:
        body = struct.pack(">II", return_code, len(messages))
        for msg in messages:
            data = msg.get("data", b"")
            body += struct.pack(
                ">IIIII",
                msg.get("protocol_id", 0),
                msg.get("rx_status", 0),
                msg.get("tx_flags", 0),
                msg.get("timestamp", 0),
                len(data),
            )
            body += data
        return Message(MsgType.READ_MSGS_RSP, sequence, body).encode()

    @staticmethod
    def encode_write_msgs_req(
        channel_id: int,
        messages: List[dict],
        timeout: int,
        sequence: int = 0,
    ) -> bytes:
        body = struct.pack(">III", channel_id, len(messages), timeout)
        for msg in messages:
            data = msg.get("data", b"")
            body += struct.pack(
                ">IIIII",
                msg.get("protocol_id", 0),
                msg.get("rx_status", 0),
                msg.get("tx_flags", 0),
                msg.get("timestamp", 0),
                len(data),
            )
            body += data
        return Message(MsgType.WRITE_MSGS_REQ, sequence, body).encode()

    @staticmethod
    def encode_write_msgs_rsp(return_code: int, num_written: int, sequence: int = 0) -> bytes:
        body = struct.pack(">II", return_code, num_written)
        return Message(MsgType.WRITE_MSGS_RSP, sequence, body).encode()

    @staticmethod
    def encode_read_version_req(device_id: int, sequence: int = 0) -> bytes:
        body = struct.pack(">I", device_id)
        return Message(MsgType.READ_VERSION_REQ, sequence, body).encode()

    @staticmethod
    def encode_read_version_rsp(
        return_code: int,
        firmware_ver: str,
        dll_ver: str,
        api_ver: str,
        sequence: int = 0,
    ) -> bytes:
        fw_bytes = firmware_ver.encode("utf-8")[:80].ljust(80, b"\x00")
        dll_bytes = dll_ver.encode("utf-8")[:80].ljust(80, b"\x00")
        api_bytes = api_ver.encode("utf-8")[:80].ljust(80, b"\x00")
        body = struct.pack(">I", return_code) + fw_bytes + dll_bytes + api_bytes
        return Message(MsgType.READ_VERSION_RSP, sequence, body).encode()

    @staticmethod
    def encode_ping_req(sequence: int = 0) -> bytes:
        return Message(MsgType.PING_REQ, sequence, b"").encode()

    @staticmethod
    def encode_ping_rsp(sequence: int = 0) -> bytes:
        return Message(MsgType.PING_RSP, sequence, b"").encode()

    @staticmethod
    def encode_heartbeat(sequence: int = 0) -> bytes:
        return Message(MsgType.HEARTBEAT, sequence, b"").encode()

    @staticmethod
    def encode_heartbeat_ack(sequence: int = 0) -> bytes:
        return Message(MsgType.HEARTBEAT_ACK, sequence, b"").encode()

    @staticmethod
    def encode_auth_req(
        timestamp: int,
        signature: bytes,
        sequence: int = 0,
        capabilities: str = "",
    ) -> bytes:
        body = struct.pack(">Q", timestamp) + signature
        if capabilities:
            body += capabilities.encode("utf-8")
        return Message(MsgType.AUTH_REQ, sequence, body).encode()

    @staticmethod
    def encode_auth_rsp(success: bool, message: str = "", sequence: int = 0) -> bytes:
        msg_bytes = message.encode("utf-8")
        body = struct.pack(">B", 1 if success else 0) + msg_bytes
        return Message(MsgType.AUTH_RSP, sequence, body).encode()

    @staticmethod
    def encode_start_filter_req(
        channel_id: int,
        filter_type: int,
        mask_msg: Optional[dict],
        pattern_msg: Optional[dict],
        flow_control_msg: Optional[dict],
        sequence: int = 0,
    ) -> bytes:
        body = struct.pack(">II", channel_id, filter_type)
        for msg in [mask_msg, pattern_msg, flow_control_msg]:
            if msg is not None:
                data = msg.get("data", b"")
                body += struct.pack(">B", 1)
                body += struct.pack(
                    ">IIIII",
                    msg.get("protocol_id", 0),
                    msg.get("rx_status", 0),
                    msg.get("tx_flags", 0),
                    msg.get("timestamp", 0),
                    len(data),
                )
                body += data
            else:
                body += struct.pack(">B", 0)
        return Message(MsgType.START_FILTER_REQ, sequence, body).encode()

    @staticmethod
    def encode_start_filter_rsp(return_code: int, filter_id: int, sequence: int = 0) -> bytes:
        body = struct.pack(">II", return_code, filter_id)
        return Message(MsgType.START_FILTER_RSP, sequence, body).encode()

    @staticmethod
    def encode_stop_filter_req(channel_id: int, filter_id: int, sequence: int = 0) -> bytes:
        body = struct.pack(">II", channel_id, filter_id)
        return Message(MsgType.STOP_FILTER_REQ, sequence, body).encode()

    @staticmethod
    def encode_stop_filter_rsp(return_code: int, sequence: int = 0) -> bytes:
        body = struct.pack(">I", return_code)
        return Message(MsgType.STOP_FILTER_RSP, sequence, body).encode()

    @staticmethod
    def encode_ioctl_req(
        channel_id: int,
        ioctl_id: int,
        input_data: Optional[bytes] = None,
        sequence: int = 0,
    ) -> bytes:
        input_bytes = input_data if input_data else b""
        body = struct.pack(">III", channel_id, ioctl_id, len(input_bytes))
        body += input_bytes
        return Message(MsgType.IOCTL_REQ, sequence, body).encode()

    @staticmethod
    def encode_ioctl_rsp(
        return_code: int,
        output_data: Optional[bytes] = None,
        sequence: int = 0,
    ) -> bytes:
        output_bytes = output_data if output_data else b""
        body = struct.pack(">II", return_code, len(output_bytes))
        body += output_bytes
        return Message(MsgType.IOCTL_RSP, sequence, body).encode()

    @staticmethod
    def encode_write_and_collect_reads_req(
        write_req_body: bytes,
        *,
        collect_window_ms: int,
        max_reads: int,
        read_timeout_ms: int,
        max_messages: int,
        sequence: int = 0,
    ) -> bytes:
        body = struct.pack(
            ">IIII",
            max(0, int(collect_window_ms)),
            max(0, int(max_reads)),
            max(0, int(read_timeout_ms)),
            max(0, int(max_messages)),
        )
        body += write_req_body
        return Message(MsgType.WRITE_AND_COLLECT_READS_REQ, sequence, body).encode()


class ProtocolDecoder:
    @staticmethod
    def _check_min_len(body: bytes, min_len: int, context: str) -> None:
        if len(body) < min_len:
            raise ValueError(f"{context}: body too short ({len(body)} < {min_len})")

    @staticmethod
    def decode_open_req(body: bytes) -> Optional[str]:
        if not body:
            return None
        return body.rstrip(b"\x00").decode("utf-8", errors="replace")

    @staticmethod
    def decode_open_rsp(body: bytes) -> Tuple[int, int]:
        ProtocolDecoder._check_min_len(body, 8, "OpenRsp")
        return struct.unpack(">II", body[:8])

    @staticmethod
    def decode_close_req(body: bytes) -> int:
        ProtocolDecoder._check_min_len(body, 4, "CloseReq")
        return struct.unpack(">I", body[:4])[0]

    @staticmethod
    def decode_close_rsp(body: bytes) -> int:
        ProtocolDecoder._check_min_len(body, 4, "CloseRsp")
        return struct.unpack(">I", body[:4])[0]

    @staticmethod
    def decode_connect_req(body: bytes) -> Tuple[int, int, int, int]:
        ProtocolDecoder._check_min_len(body, 16, "ConnectReq")
        return struct.unpack(">IIII", body[:16])

    @staticmethod
    def decode_connect_rsp(body: bytes) -> Tuple[int, int]:
        ProtocolDecoder._check_min_len(body, 8, "ConnectRsp")
        return struct.unpack(">II", body[:8])

    @staticmethod
    def decode_disconnect_req(body: bytes) -> int:
        ProtocolDecoder._check_min_len(body, 4, "DisconnectReq")
        return struct.unpack(">I", body[:4])[0]

    @staticmethod
    def decode_disconnect_rsp(body: bytes) -> int:
        ProtocolDecoder._check_min_len(body, 4, "DisconnectRsp")
        return struct.unpack(">I", body[:4])[0]

    @staticmethod
    def decode_read_msgs_req(body: bytes) -> Tuple[int, int, int]:
        ProtocolDecoder._check_min_len(body, 12, "ReadMsgsReq")
        return struct.unpack(">III", body[:12])

    @staticmethod
    def decode_read_msgs_rsp(body: bytes) -> Tuple[int, List[dict]]:
        ProtocolDecoder._check_min_len(body, 8, "ReadMsgsRsp")
        return_code, num_msgs = struct.unpack(">II", body[:8])
        messages: list[dict] = []
        offset = 8
        for _ in range(num_msgs):
            if offset + 20 > len(body):
                break
            protocol_id, rx_status, tx_flags, timestamp, data_size = struct.unpack(
                ">IIIII", body[offset:offset + 20]
            )
            offset += 20
            if offset + data_size > len(body):
                break
            data = body[offset:offset + data_size]
            offset += data_size
            messages.append(
                {
                    "protocol_id": protocol_id,
                    "rx_status": rx_status,
                    "tx_flags": tx_flags,
                    "timestamp": timestamp,
                    "data": data,
                }
            )
        return return_code, messages

    @staticmethod
    def decode_write_msgs_req(body: bytes) -> Tuple[int, List[dict], int]:
        ProtocolDecoder._check_min_len(body, 12, "WriteMsgsReq")
        channel_id, num_msgs, timeout = struct.unpack(">III", body[:12])
        messages: list[dict] = []
        offset = 12
        for _ in range(num_msgs):
            if offset + 20 > len(body):
                break
            protocol_id, rx_status, tx_flags, timestamp, data_size = struct.unpack(
                ">IIIII", body[offset:offset + 20]
            )
            offset += 20
            if offset + data_size > len(body):
                break
            data = body[offset:offset + data_size]
            offset += data_size
            messages.append(
                {
                    "protocol_id": protocol_id,
                    "rx_status": rx_status,
                    "tx_flags": tx_flags,
                    "timestamp": timestamp,
                    "data": data,
                }
            )
        return channel_id, messages, timeout

    @staticmethod
    def decode_write_msgs_rsp(body: bytes) -> Tuple[int, int]:
        ProtocolDecoder._check_min_len(body, 8, "WriteMsgsRsp")
        return struct.unpack(">II", body[:8])

    @staticmethod
    def decode_read_version_req(body: bytes) -> int:
        ProtocolDecoder._check_min_len(body, 4, "ReadVersionReq")
        return struct.unpack(">I", body[:4])[0]

    @staticmethod
    def decode_read_version_rsp(body: bytes) -> Tuple[int, str, str, str]:
        ProtocolDecoder._check_min_len(body, 244, "ReadVersionRsp")
        return_code = struct.unpack(">I", body[:4])[0]
        fw_ver = body[4:84].rstrip(b"\x00").decode("utf-8", errors="replace")
        dll_ver = body[84:164].rstrip(b"\x00").decode("utf-8", errors="replace")
        api_ver = body[164:244].rstrip(b"\x00").decode("utf-8", errors="replace")
        return return_code, fw_ver, dll_ver, api_ver

    @staticmethod
    def decode_start_filter_req(
        body: bytes,
    ) -> Tuple[int, int, Optional[dict], Optional[dict], Optional[dict]]:
        ProtocolDecoder._check_min_len(body, 11, "StartFilterReq")
        channel_id, filter_type = struct.unpack(">II", body[:8])
        offset = 8
        messages: list[Optional[dict]] = []
        for _ in range(3):
            if offset >= len(body):
                messages.append(None)
                continue
            present = body[offset]
            offset += 1
            if present:
                if offset + 20 > len(body):
                    raise ValueError(f"StartFilterReq: truncated message at offset {offset}")
                protocol_id, rx_status, tx_flags, timestamp, data_size = struct.unpack(
                    ">IIIII", body[offset:offset + 20]
                )
                offset += 20
                if offset + data_size > len(body):
                    raise ValueError(f"StartFilterReq: truncated data at offset {offset}")
                data = body[offset:offset + data_size]
                offset += data_size
                messages.append(
                    {
                        "protocol_id": protocol_id,
                        "rx_status": rx_status,
                        "tx_flags": tx_flags,
                        "timestamp": timestamp,
                        "data": data,
                    }
                )
            else:
                messages.append(None)
        return channel_id, filter_type, messages[0], messages[1], messages[2]

    @staticmethod
    def decode_start_filter_rsp(body: bytes) -> Tuple[int, int]:
        ProtocolDecoder._check_min_len(body, 8, "StartFilterRsp")
        return struct.unpack(">II", body[:8])

    @staticmethod
    def decode_stop_filter_req(body: bytes) -> Tuple[int, int]:
        ProtocolDecoder._check_min_len(body, 8, "StopFilterReq")
        return struct.unpack(">II", body[:8])

    @staticmethod
    def decode_stop_filter_rsp(body: bytes) -> int:
        ProtocolDecoder._check_min_len(body, 4, "StopFilterRsp")
        return struct.unpack(">I", body[:4])[0]

    @staticmethod
    def decode_ioctl_req(body: bytes) -> Tuple[int, int, Optional[bytes]]:
        ProtocolDecoder._check_min_len(body, 12, "IoctlReq")
        channel_id, ioctl_id, input_len = struct.unpack(">III", body[:12])
        input_data = body[12:12 + input_len] if input_len > 0 else None
        return channel_id, ioctl_id, input_data

    @staticmethod
    def decode_ioctl_rsp(body: bytes) -> Tuple[int, Optional[bytes]]:
        ProtocolDecoder._check_min_len(body, 8, "IoctlRsp")
        return_code, output_len = struct.unpack(">II", body[:8])
        output_data = body[8:8 + output_len] if output_len > 0 else None
        return return_code, output_data

    @staticmethod
    def decode_auth_req(body: bytes) -> Tuple[int, bytes]:
        ProtocolDecoder._check_min_len(body, 40, "AuthReq")
        timestamp = struct.unpack(">Q", body[:8])[0]
        signature = body[8:40]
        return timestamp, signature

    @staticmethod
    def decode_auth_req_capabilities(body: bytes) -> str:
        ProtocolDecoder._check_min_len(body, 40, "AuthReq")
        return body[40:].decode("utf-8", errors="replace") if len(body) > 40 else ""

    @staticmethod
    def decode_auth_rsp(body: bytes) -> Tuple[bool, str]:
        ProtocolDecoder._check_min_len(body, 1, "AuthRsp")
        success = body[0] != 0
        message = body[1:].decode("utf-8", errors="replace") if len(body) > 1 else ""
        return success, message

    @staticmethod
    def decode_write_and_collect_reads_req(body: bytes) -> WriteAndCollectReadsRequest:
        ProtocolDecoder._check_min_len(body, 16, "WriteAndCollectReadsReq")
        collect_window_ms, max_reads, read_timeout_ms, max_messages = struct.unpack(
            ">IIII",
            body[:16],
        )
        write_req_body = body[16:]
        ProtocolDecoder.decode_write_msgs_req(write_req_body)
        return WriteAndCollectReadsRequest(
            collect_window_ms=collect_window_ms,
            max_reads=max_reads,
            read_timeout_ms=read_timeout_ms,
            max_messages=max_messages,
            write_req_body=write_req_body,
        )
