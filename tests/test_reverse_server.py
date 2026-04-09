from __future__ import annotations

import asyncio
import ssl
import struct
import types

import pytest

from vci_proxy.config import ProxyConfig
from vci_proxy.protocol import HEADER_SIZE, Message, MsgType, ProtocolDecoder, ProtocolEncoder
import vci_proxy.reverse_server as reverse_server_module
from vci_proxy.reverse_server import ReverseProxyServer


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


def test_authenticate_vci_accepts_valid_auth_request(monkeypatch) -> None:
    server = ReverseProxyServer(config=ProxyConfig.from_args(auth_token="secret"))
    reader = _FakeReader(ProtocolEncoder.encode_auth_req(123, b"x" * 32, sequence=7))
    writer = _FakeWriter()

    monkeypatch.setattr("vci_proxy.reverse_server.verify_signature", lambda token, timestamp, signature: (True, "ok"))

    accepted = asyncio.run(server._authenticate_vci(reader, writer))

    assert accepted is True
    assert len(writer.writes) == 1
    _magic, length, msg_type, sequence = Message.decode_header(writer.writes[0][:HEADER_SIZE])
    assert msg_type == MsgType.AUTH_RSP
    assert sequence == 7
    success, message = ProtocolDecoder.decode_auth_rsp(writer.writes[0][HEADER_SIZE:length])
    assert success is True
    assert message == "ok"


def test_authenticate_vci_rejects_legacy_heartbeat_when_auth_is_required() -> None:
    server = ReverseProxyServer(config=ProxyConfig.from_args(auth_token="secret"))
    reader = _FakeReader(ProtocolEncoder.encode_heartbeat(sequence=3))
    writer = _FakeWriter()

    accepted = asyncio.run(server._authenticate_vci(reader, writer))

    assert accepted is False
    assert writer.writes == []


def test_authenticate_vci_rejects_replayed_auth_request(monkeypatch) -> None:
    server = ReverseProxyServer(config=ProxyConfig.from_args(auth_token="secret"))
    writer_a = _FakeWriter()
    writer_b = _FakeWriter()
    message = ProtocolEncoder.encode_auth_req(123, b"x" * 32, sequence=7)

    monkeypatch.setattr(
        "vci_proxy.reverse_server.verify_signature",
        lambda token, timestamp, signature: (True, "ok"),
    )

    accepted_first = asyncio.run(server._authenticate_vci(_FakeReader(message), writer_a))
    accepted_second = asyncio.run(server._authenticate_vci(_FakeReader(message), writer_b))

    assert accepted_first is True
    assert accepted_second is False
    assert len(writer_b.writes) == 1
    _magic, length, msg_type, sequence = Message.decode_header(writer_b.writes[0][:HEADER_SIZE])
    assert msg_type == MsgType.AUTH_RSP
    assert sequence == 7
    success, reason = ProtocolDecoder.decode_auth_rsp(writer_b.writes[0][HEADER_SIZE:length])
    assert success is False
    assert "replay" in reason


def test_authenticate_vci_rejects_invalid_short_frame_length() -> None:
    server = ReverseProxyServer(config=ProxyConfig.from_args(auth_token="secret"))
    short_header = struct.pack(">IIHI", reverse_server_module.MAGIC, HEADER_SIZE - 1, MsgType.AUTH_REQ, 7)
    reader = _FakeReader(short_header)
    writer = _FakeWriter()

    accepted = asyncio.run(server._authenticate_vci(reader, writer))

    assert accepted is False
    assert writer.writes == []


def test_authenticate_vci_rejects_oversized_frame_length() -> None:
    server = ReverseProxyServer(config=ProxyConfig.from_args(auth_token="secret"))
    huge_header = struct.pack(">IIHI", reverse_server_module.MAGIC, HEADER_SIZE + 2_000_001, MsgType.AUTH_REQ, 7)
    reader = _FakeReader(huge_header)
    writer = _FakeWriter()

    accepted = asyncio.run(server._authenticate_vci(reader, writer))

    assert accepted is False
    assert writer.writes == []


def test_build_server_tls_context_loads_cert_chain_and_optional_client_ca(monkeypatch) -> None:
    observed: dict[str, object] = {}

    class _FakeContext:
        minimum_version = None

        def load_cert_chain(self, certfile, keyfile):
            observed["certfile"] = certfile
            observed["keyfile"] = keyfile

        def load_verify_locations(self, cafile=None):
            observed["cafile"] = cafile

        verify_mode = None

    def _fake_create_default_context(purpose):
        observed["purpose"] = purpose
        return _FakeContext()

    monkeypatch.setattr(
        "vci_proxy.reverse_server.ssl.create_default_context",
        _fake_create_default_context,
    )

    server = ReverseProxyServer(
        config=ProxyConfig.from_args(
            auth_token="secret",
            tls_enabled=True,
            tls_certfile="C:/certs/server.crt",
            tls_keyfile="C:/certs/server.key",
            tls_ca_file="C:/certs/ca.pem",
            tls_require_client_cert=True,
        )
    )

    context = server._build_tls_server_context()

    assert isinstance(context, _FakeContext)
    assert observed["purpose"] == ssl.Purpose.CLIENT_AUTH
    assert observed["certfile"] == "C:/certs/server.crt"
    assert observed["keyfile"] == "C:/certs/server.key"
    assert observed["cafile"] == "C:/certs/ca.pem"
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2


def test_build_server_tls_context_requires_cert_and_key() -> None:
    server = ReverseProxyServer(
        config=ProxyConfig.from_args(
            auth_token="secret",
            tls_enabled=True,
            tls_certfile="C:/certs/server.crt",
        )
    )

    with pytest.raises(ValueError, match="TLS certfile and keyfile are required"):
        server._build_tls_server_context()


def test_try_serve_cached_returns_cached_ioctl_response() -> None:
    server = ReverseProxyServer()
    server._ioctl_cache = types.SimpleNamespace(try_get_cached=lambda channel_id, ioctl_id: (0, b"\x01\x02"))
    body = ProtocolEncoder.encode_ioctl_req(9, 0x03, None, sequence=11)[HEADER_SIZE:]

    cached, ioctl_id = server._try_serve_cached(MsgType.IOCTL_REQ, body, sequence=11)

    assert ioctl_id == 0x03
    assert cached is not None
    _return_code, output_data = ProtocolDecoder.decode_ioctl_rsp(cached[HEADER_SIZE:])
    assert output_data == b"\x01\x02"


def test_cancel_pending_futures_clears_caches_and_fails_open_requests() -> None:
    async def _run() -> None:
        server = ReverseProxyServer()
        events: list[str] = []
        server._read_cache = types.SimpleNamespace(clear=lambda: events.append("read"))
        server._filter_cache = types.SimpleNamespace(clear=lambda: events.append("filter"))
        server._ioctl_cache = types.SimpleNamespace(invalidate=lambda: events.append("ioctl"))

        loop = asyncio.get_running_loop()
        pending = loop.create_future()
        completed = loop.create_future()
        completed.set_result(("done", b"", None))
        server.response_futures = {1: pending, 2: completed}

        server._cancel_pending_futures()

        assert events == ["read", "filter", "ioctl"]
        assert server.response_futures == {}
        assert pending.done() is True
        assert isinstance(pending.exception(), ConnectionError)
        assert "VCI Proxy" in str(pending.exception())

    asyncio.run(_run())


def test_run_probe_records_network_time_from_duration_minus_hw(monkeypatch) -> None:
    class _FakeTunnelQuality:
        def __init__(self) -> None:
            self.recorded: list[float] = []
            self.probe_failures = 0

        def record_probe(self, network_ms: float) -> None:
            self.recorded.append(network_ms)

        def record_probe_failure(self, reason: str = "") -> None:
            self.probe_failures += 1

    async def _run() -> None:
        server = ReverseProxyServer()
        server.vci_writer = _FakeWriter()
        server._connection_epoch = "epoch-1"
        server._tunnel_quality = _FakeTunnelQuality()
        server._write_tunnel_quality_snapshot = lambda: None
        server._next_sequence = lambda: 7

        class _FakeTime:
            def __init__(self) -> None:
                self._values = iter([100.0, 100.05])

            def monotonic(self) -> float:
                return next(self._values)

        monkeypatch.setattr(reverse_server_module, "time", _FakeTime())

        async def _fake_wait_for(future, timeout):
            assert server.response_futures[7] is future
            return (MsgType.PING_RSP, b"", 7.5)

        monkeypatch.setattr("vci_proxy.reverse_server.asyncio.wait_for", _fake_wait_for)

        await server._run_probe("epoch-1")

        assert len(server.vci_writer.writes) == 1
        assert server._tunnel_quality.recorded == [pytest.approx(42.5)]

    asyncio.run(_run())


def test_invalidate_caches_clears_channel_and_filter_entries() -> None:
    events: list[tuple[str, int]] = []
    server = ReverseProxyServer()
    server._read_cache = types.SimpleNamespace(invalidate_channel=lambda channel_id: events.append(("read", channel_id)))
    server._filter_cache = types.SimpleNamespace(
        invalidate_channel=lambda channel_id: events.append(("filter", channel_id)),
        on_stop_filter=lambda filter_id: events.append(("stop", filter_id)),
        clear=lambda: None,
    )
    server._ioctl_cache = types.SimpleNamespace(invalidate_channel=lambda channel_id: events.append(("ioctl", channel_id)), invalidate=lambda: None)

    server._invalidate_caches(MsgType.DISCONNECT_REQ, struct.pack(">I", 33))
    server._invalidate_caches(MsgType.STOP_FILTER_REQ, struct.pack(">II", 33, 88))

    assert events == [("read", 33), ("filter", 33), ("ioctl", 33), ("stop", 88)]
