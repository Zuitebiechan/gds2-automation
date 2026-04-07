from __future__ import annotations

import asyncio
import types

from vci_proxy.config import ProxyConfig
from vci_proxy.protocol import HEADER_SIZE, Message, MsgType, ProtocolDecoder, ProtocolEncoder
from vci_proxy.reverse_client import ReverseProxyClient


class _FakeWriter:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None


class _FakeReader:
    def __init__(self, *messages: bytes) -> None:
        self._buffer = b"".join(messages)
        self._offset = 0

    async def readexactly(self, n: int) -> bytes:
        if self._offset + n > len(self._buffer):
            raise asyncio.IncompleteReadError(partial=self._buffer[self._offset :], expected=n)
        chunk = self._buffer[self._offset : self._offset + n]
        self._offset += n
        return chunk


def test_ensure_driver_notifies_error_when_driver_load_fails(monkeypatch) -> None:
    observed: list[tuple[str, str]] = []
    client = ReverseProxyClient("example.com", 9000, dll_path="C:/bad.dll")
    client._on_status_change = lambda status, detail: observed.append((status, detail))

    monkeypatch.setattr("vci_proxy.reverse_client.J2534Driver", lambda _path: (_ for _ in ()).throw(RuntimeError("boom")))

    assert client._ensure_driver() is False
    assert observed == [("error", "Failed to load J2534 driver: boom")]


def test_send_registration_auth_mode_accepts_auth_response(monkeypatch) -> None:
    config = ProxyConfig.from_args(auth_token="shared-secret")
    client = ReverseProxyClient("example.com", 9000, config=config)
    reader = _FakeReader(ProtocolEncoder.encode_auth_rsp(True, "ok", sequence=0))
    writer = _FakeWriter()

    monkeypatch.setattr("vci_proxy.reverse_client.time.time", lambda: 1_700_000_000)
    monkeypatch.setattr("vci_proxy.reverse_client.compute_signature", lambda token, timestamp: b"s" * 32)

    result = asyncio.run(client._send_registration(reader, writer))

    assert result is True
    assert len(writer.writes) == 1
    magic, length, msg_type, sequence = Message.decode_header(writer.writes[0][:HEADER_SIZE])
    assert msg_type == MsgType.AUTH_REQ
    assert sequence == 0
    body_len = length - HEADER_SIZE
    timestamp, signature = ProtocolDecoder.decode_auth_req(writer.writes[0][HEADER_SIZE:HEADER_SIZE + body_len])
    assert timestamp == 1_700_000_000
    assert signature == b"s" * 32


def test_send_registration_legacy_mode_performs_two_phase_heartbeat() -> None:
    client = ReverseProxyClient("example.com", 9000, config=ProxyConfig())
    reader = _FakeReader(ProtocolEncoder.encode_heartbeat_ack(sequence=99))
    writer = _FakeWriter()

    result = asyncio.run(client._send_registration(reader, writer))

    assert result is True
    assert len(writer.writes) == 2
    first = Message.decode_header(writer.writes[0][:HEADER_SIZE])
    second = Message.decode_header(writer.writes[1][:HEADER_SIZE])
    assert first[2] == MsgType.HEARTBEAT
    assert first[3] == 0
    assert second[2] == MsgType.HEARTBEAT
    assert second[3] == 1


def test_handle_open_prefers_prewarmed_device_id() -> None:
    client = ReverseProxyClient("example.com", 9000)
    client.driver = types.SimpleNamespace(open=lambda device_name: (_ for _ in ()).throw(AssertionError("should not call driver.open")))
    client._prewarm_device_id = 1234
    client._prewarm_ret = 0
    client._prewarm_task = None
    body = ProtocolEncoder.encode_open_req("Demo Device", sequence=7)[HEADER_SIZE:]

    response = asyncio.run(client._handle_open(body, sequence=7))

    return_code, device_id = ProtocolDecoder.decode_open_rsp(response[HEADER_SIZE:])
    assert return_code == 0
    assert device_id == 1234
    assert client._prewarm_device_id is None
    assert client._prewarm_ret is None


def test_handle_message_returns_ping_response() -> None:
    client = ReverseProxyClient("example.com", 9000)

    response = asyncio.run(client._handle_message(MsgType.PING_REQ, b"", sequence=5))

    magic, length, msg_type, sequence = Message.decode_header(response[:HEADER_SIZE])
    assert magic > 0
    assert length == HEADER_SIZE
    assert msg_type == MsgType.PING_RSP
    assert sequence == 5
