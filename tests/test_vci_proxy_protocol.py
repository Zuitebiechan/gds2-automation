from __future__ import annotations

import pytest

from vci_proxy.protocol import (
    HEADER_SIZE,
    MAGIC,
    PREFETCH_MAGIC,
    Message,
    MsgType,
    ProtocolDecoder,
    ProtocolEncoder,
    attach_read_msgs_prefetch_bundle,
    strip_read_msgs_prefetch_bundle,
)


def test_message_encode_and_decode_header_round_trip() -> None:
    encoded = Message(MsgType.PING_REQ, 7, b"abc").encode()

    assert Message.decode_header(encoded[:HEADER_SIZE]) == (
        MAGIC,
        HEADER_SIZE + 3,
        MsgType.PING_REQ,
        7,
    )


def test_decode_header_rejects_short_headers() -> None:
    with pytest.raises(ValueError, match="Header too short"):
        Message.decode_header(b"\x00" * (HEADER_SIZE - 1))


def test_protocol_round_trips_read_and_write_messages() -> None:
    messages = [
        {
            "protocol_id": 6,
            "rx_status": 1,
            "tx_flags": 2,
            "timestamp": 123,
            "data": b"\x01\x02\x03",
        },
        {
            "protocol_id": 7,
            "rx_status": 0,
            "tx_flags": 4,
            "timestamp": 456,
            "data": b"\xAA",
        },
    ]

    read_rsp = ProtocolEncoder.encode_read_msgs_rsp(0, messages, sequence=5)
    write_req = ProtocolEncoder.encode_write_msgs_req(9, messages, timeout=200, sequence=8)

    assert ProtocolDecoder.decode_read_msgs_rsp(read_rsp[HEADER_SIZE:]) == (0, messages)
    assert ProtocolDecoder.decode_write_msgs_req(write_req[HEADER_SIZE:]) == (9, messages, 200)


def test_protocol_round_trips_start_filter_with_optional_messages() -> None:
    mask_msg = {
        "protocol_id": 1,
        "rx_status": 2,
        "tx_flags": 3,
        "timestamp": 4,
        "data": b"\x10\x20",
    }
    pattern_msg = {
        "protocol_id": 5,
        "rx_status": 6,
        "tx_flags": 7,
        "timestamp": 8,
        "data": b"\x30",
    }

    encoded = ProtocolEncoder.encode_start_filter_req(
        channel_id=11,
        filter_type=99,
        mask_msg=mask_msg,
        pattern_msg=pattern_msg,
        flow_control_msg=None,
        sequence=3,
    )

    assert ProtocolDecoder.decode_start_filter_req(encoded[HEADER_SIZE:]) == (
        11,
        99,
        mask_msg,
        pattern_msg,
        None,
    )


def test_protocol_round_trips_ioctl_and_auth_messages() -> None:
    ioctl_req = ProtocolEncoder.encode_ioctl_req(3, 0x09, b"\x01\x02", sequence=1)
    ioctl_rsp = ProtocolEncoder.encode_ioctl_rsp(0, b"\xAA\xBB", sequence=1)
    auth_req = ProtocolEncoder.encode_auth_req(
        1_700_000_000,
        b"x" * 32,
        sequence=2,
        capabilities="read_ahead=1;write_collect=1",
    )
    auth_rsp = ProtocolEncoder.encode_auth_rsp(True, "ok", sequence=2)

    assert ProtocolDecoder.decode_ioctl_req(ioctl_req[HEADER_SIZE:]) == (3, 0x09, b"\x01\x02")
    assert ProtocolDecoder.decode_ioctl_rsp(ioctl_rsp[HEADER_SIZE:]) == (0, b"\xAA\xBB")
    assert ProtocolDecoder.decode_auth_req(auth_req[HEADER_SIZE:]) == (1_700_000_000, b"x" * 32)
    assert ProtocolDecoder.decode_auth_req_capabilities(auth_req[HEADER_SIZE:]) == "read_ahead=1;write_collect=1"
    assert ProtocolDecoder.decode_auth_rsp(auth_rsp[HEADER_SIZE:]) == (True, "ok")


def test_protocol_decoder_validates_lengths_for_fixed_size_messages() -> None:
    with pytest.raises(ValueError, match="CloseReq: body too short"):
        ProtocolDecoder.decode_close_req(b"\x00\x00\x00")

    with pytest.raises(ValueError, match="AuthReq: body too short"):
        ProtocolDecoder.decode_auth_req(b"\x00" * 39)

    with pytest.raises(ValueError, match="ReadVersionRsp: body too short"):
        ProtocolDecoder.decode_read_version_rsp(b"\x00" * 243)


def test_start_filter_decoder_rejects_truncated_embedded_message() -> None:
    encoded = ProtocolEncoder.encode_start_filter_req(
        channel_id=1,
        filter_type=2,
        mask_msg={"protocol_id": 1, "rx_status": 0, "tx_flags": 0, "timestamp": 0, "data": b"\x01"},
        pattern_msg=None,
        flow_control_msg=None,
        sequence=0,
    )

    truncated_body = encoded[HEADER_SIZE:HEADER_SIZE + 29]

    with pytest.raises(ValueError, match="StartFilterReq: truncated data"):
        ProtocolDecoder.decode_start_filter_req(truncated_body)


def test_read_msgs_prefetch_bundle_round_trips_inside_write_response() -> None:
    messages = [
        {
            "protocol_id": 6,
            "rx_status": 0,
            "tx_flags": 0,
            "timestamp": 123,
            "data": b"\x62\xf4\x0c",
        }
    ]
    write_rsp = ProtocolEncoder.encode_write_msgs_rsp(0, 1, sequence=17)
    read_rsp_body = ProtocolEncoder.encode_read_msgs_rsp(0, messages, sequence=0)[HEADER_SIZE:]

    bundled = attach_read_msgs_prefetch_bundle(
        write_rsp,
        channel_id=99,
        read_rsp_bodies=[read_rsp_body],
    )

    magic, length, msg_type, sequence = Message.decode_header(bundled[:HEADER_SIZE])
    assert magic == MAGIC
    assert length == len(bundled)
    assert msg_type == MsgType.WRITE_MSGS_RSP
    assert sequence == 17
    assert PREFETCH_MAGIC.to_bytes(4, "big") in bundled[HEADER_SIZE:]

    clean_body, bundle = strip_read_msgs_prefetch_bundle(bundled[HEADER_SIZE:])
    assert clean_body == write_rsp[HEADER_SIZE:]
    assert bundle is not None
    assert bundle.channel_id == 99
    assert bundle.read_rsp_bodies == (read_rsp_body,)
    assert ProtocolDecoder.decode_read_msgs_rsp(bundle.read_rsp_bodies[0]) == (0, messages)


def test_read_msgs_prefetch_bundle_rejects_truncated_bundle() -> None:
    write_rsp = ProtocolEncoder.encode_write_msgs_rsp(0, 1, sequence=17)
    bundled = attach_read_msgs_prefetch_bundle(
        write_rsp,
        channel_id=99,
        read_rsp_bodies=[b"\x00\x00\x00\x00"],
    )

    with pytest.raises(ValueError, match="truncated before response body"):
        strip_read_msgs_prefetch_bundle(bundled[HEADER_SIZE:-1])


def test_write_and_collect_reads_request_wraps_write_body_and_limits() -> None:
    messages = [
        {
            "protocol_id": 6,
            "rx_status": 0,
            "tx_flags": 0,
            "timestamp": 123,
            "data": b"\x22\xf4\x0c",
        }
    ]
    write_req_body = ProtocolEncoder.encode_write_msgs_req(
        44,
        messages,
        timeout=25,
        sequence=7,
    )[HEADER_SIZE:]

    encoded = ProtocolEncoder.encode_write_and_collect_reads_req(
        write_req_body,
        collect_window_ms=200,
        max_reads=3,
        read_timeout_ms=0,
        max_messages=16,
        sequence=99,
    )
    magic, length, msg_type, sequence = Message.decode_header(encoded[:HEADER_SIZE])
    request = ProtocolDecoder.decode_write_and_collect_reads_req(encoded[HEADER_SIZE:])

    assert magic == MAGIC
    assert length == len(encoded)
    assert msg_type == MsgType.WRITE_AND_COLLECT_READS_REQ
    assert sequence == 99
    assert request.collect_window_ms == 200
    assert request.max_reads == 3
    assert request.read_timeout_ms == 0
    assert request.max_messages == 16
    assert request.write_req_body == write_req_body
    assert ProtocolDecoder.decode_write_msgs_req(request.write_req_body) == (44, messages, 25)


def test_write_and_collect_reads_request_rejects_truncated_write_body() -> None:
    with pytest.raises(ValueError, match="WriteAndCollectReadsReq: body too short"):
        ProtocolDecoder.decode_write_and_collect_reads_req(b"\x00" * 15)

    encoded = ProtocolEncoder.encode_write_and_collect_reads_req(
        b"\x00\x00\x00\x01",
        collect_window_ms=200,
        max_reads=3,
        read_timeout_ms=0,
        max_messages=16,
        sequence=99,
    )
    with pytest.raises(ValueError, match="WriteMsgsReq: body too short"):
        ProtocolDecoder.decode_write_and_collect_reads_req(encoded[HEADER_SIZE:])
