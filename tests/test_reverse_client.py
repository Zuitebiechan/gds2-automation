from __future__ import annotations

import asyncio
import ssl
import types

import pytest

from vci_proxy.config import ProxyConfig
from vci_proxy.protocol import HEADER_SIZE, Message, MsgType, ProtocolDecoder, ProtocolEncoder
from vci_proxy.reverse_client import ReverseProxyClient


class _FakeWriter:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False
        self.wait_closed_called = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.wait_closed_called = True

    def get_extra_info(self, _name, default=None):
        return default


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
    client = ReverseProxyClient(
        "example.com",
        9000,
        dll_path="C:/bad.dll",
        driver_loader=lambda _path: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    client._on_status_change = lambda status, detail: observed.append((status, detail))

    assert client._ensure_driver() is False
    assert observed == [("error", "Failed to load J2534 driver: boom")]


def test_ensure_driver_uses_injected_driver_loader() -> None:
    sentinel_driver = object()
    cleanup_called: list[str] = []
    observed: list[tuple[str, str]] = []
    client = ReverseProxyClient(
        "example.com",
        9000,
        dll_path="C:/driver.dll",
        driver_loader=lambda dll_path: (
            sentinel_driver,
            (lambda: cleanup_called.append("done")) if dll_path == "C:/driver.dll" else None,
        ),
    )
    client._on_status_change = lambda status, detail: observed.append((status, detail))

    assert client._ensure_driver() is True
    assert client.driver is sentinel_driver
    assert observed == []


def test_shutdown_invokes_driver_runtime_cleanup() -> None:
    cleaned: list[str] = []
    client = ReverseProxyClient("example.com", 9000, config=ProxyConfig.from_args(auth_token="secret"))
    client.running = True
    client._ioctl_cache = types.SimpleNamespace(invalidate=lambda: None)
    client._driver_cleanup = lambda: cleaned.append("done")

    asyncio.run(client.shutdown())

    assert cleaned == ["done"]


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


def test_proxy_config_from_args_populates_tls_settings() -> None:
    config = ProxyConfig.from_args(
        auth_token="shared-secret",
        tls_enabled=True,
        tls_ca_file="C:/certs/ca.pem",
        tls_server_name="diag.example",
    )

    assert config.tls.enabled is True
    assert config.tls.ca_file == "C:/certs/ca.pem"
    assert config.tls.server_name == "diag.example"


def test_build_tls_connection_options_uses_client_ca_and_server_name(monkeypatch) -> None:
    created: dict[str, object] = {}

    class _FakeContext:
        minimum_version = None

    def _fake_create_default_context(purpose, cafile=None):
        created["purpose"] = purpose
        created["cafile"] = cafile
        return _FakeContext()

    monkeypatch.setattr("vci_proxy.reverse_client.ssl.create_default_context", _fake_create_default_context)

    client = ReverseProxyClient(
        "diag.example",
        9000,
        config=ProxyConfig.from_args(
            auth_token="shared-secret",
            tls_enabled=True,
            tls_ca_file="C:/certs/ca.pem",
            tls_server_name="diag.example",
        ),
    )

    ssl_context, server_hostname = client._build_tls_connection_options()

    assert isinstance(ssl_context, _FakeContext)
    assert server_hostname == "diag.example"
    assert created == {
        "purpose": ssl.Purpose.SERVER_AUTH,
        "cafile": "C:/certs/ca.pem",
    }
    assert ssl_context.minimum_version == ssl.TLSVersion.TLSv1_2


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


def test_connect_and_serve_reports_error_when_auth_token_is_missing(monkeypatch) -> None:
    observed: list[tuple[str, str]] = []
    client = ReverseProxyClient("example.com", 9000, config=ProxyConfig())
    client._on_status_change = lambda status, detail: observed.append((status, detail))

    monkeypatch.setattr(client, "_ensure_driver", lambda: True)
    monkeypatch.setattr(
        "vci_proxy.reverse_client.asyncio.open_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("open_connection should not run")),
    )

    asyncio.run(client.connect_and_serve())

    assert observed[-1] == ("error", "Authentication token required")


def test_connect_and_serve_passes_tls_context_to_open_connection(monkeypatch) -> None:
    observed: dict[str, object] = {}
    fake_writer = _FakeWriter()

    async def _fake_open_connection(host, port, **kwargs):
        observed["host"] = host
        observed["port"] = port
        observed["ssl"] = kwargs.get("ssl")
        observed["server_hostname"] = kwargs.get("server_hostname")
        return _FakeReader(), fake_writer

    async def _fake_send_registration(reader, writer, **_kwargs):
        assert writer is fake_writer
        client.running = False
        return False

    client = ReverseProxyClient(
        "diag.example",
        9000,
        config=ProxyConfig.from_args(
            auth_token="shared-secret",
            tls_enabled=True,
            tls_server_name="diag.example",
        ),
    )

    monkeypatch.setattr(client, "_ensure_driver", lambda: True)
    monkeypatch.setattr(client, "_send_registration", _fake_send_registration)
    monkeypatch.setattr(
        client,
        "_build_tls_connection_options",
        lambda: ("tls-context", "diag.example"),
    )
    monkeypatch.setattr("vci_proxy.reverse_client.asyncio.open_connection", _fake_open_connection)

    asyncio.run(client.connect_and_serve())

    assert observed == {
        "host": "diag.example",
        "port": 9000,
        "ssl": "tls-context",
        "server_hostname": "diag.example",
    }


def test_handle_requests_raises_connection_error_with_reason_on_server_eof() -> None:
    client = ReverseProxyClient("example.com", 9000, config=ProxyConfig.from_args(auth_token="secret"))
    client.running = True

    with pytest.raises(ConnectionError, match="reverse server disconnected"):
        asyncio.run(client._handle_requests(_FakeReader(), _FakeWriter(), attempt_label="attempt=1"))


def test_connect_and_serve_reports_clear_disconnect_reason(monkeypatch) -> None:
    observed: list[tuple[str, str]] = []
    fake_writer = _FakeWriter()
    client = ReverseProxyClient("diag.example", 9000, config=ProxyConfig.from_args(auth_token="secret"))
    client._on_status_change = lambda status, detail: observed.append((status, detail))

    async def _fake_open_connection(*_args, **_kwargs):
        return _FakeReader(), fake_writer

    async def _fake_send_registration(reader, writer, **_kwargs):
        return True

    async def _fake_handle_requests(reader, writer, **_kwargs):
        raise ConnectionError("reverse server disconnected: EOF while waiting for messages")

    async def _fake_sleep(_seconds):
        client.running = False
        return None

    monkeypatch.setattr(client, "_ensure_driver", lambda: True)
    monkeypatch.setattr(client, "_send_registration", _fake_send_registration)
    monkeypatch.setattr(client, "_handle_requests", _fake_handle_requests)
    monkeypatch.setattr("vci_proxy.reverse_client.asyncio.open_connection", _fake_open_connection)
    monkeypatch.setattr("vci_proxy.reverse_client.asyncio.sleep", _fake_sleep)

    asyncio.run(client.connect_and_serve())

    assert (
        "disconnected",
        "Reverse server disconnected: EOF while waiting for messages, retrying in 5s",
    ) in observed


def test_shutdown_closes_active_writer_and_cancels_prewarm_task() -> None:
    class _FakeTask:
        def __init__(self) -> None:
            self.cancelled = False

        def cancel(self) -> None:
            self.cancelled = True

    closed_device_ids: list[int] = []
    client = ReverseProxyClient("example.com", 9000, config=ProxyConfig.from_args(auth_token="secret"))
    client.running = True
    client._ioctl_cache = types.SimpleNamespace(invalidate=lambda: None)
    client._active_writer = _FakeWriter()
    client._prewarm_task = _FakeTask()
    client._prewarm_device_id = 1234
    client._prewarm_ret = 0
    client.driver = types.SimpleNamespace(
        close=lambda device_id: closed_device_ids.append(device_id) or 0,
        get_error_name=lambda code: f"ERROR_{code:#x}",
    )

    asyncio.run(client.shutdown())

    assert client.running is False
    assert client._active_writer is None
    assert client._prewarm_task is None
    assert client._prewarm_device_id is None
    assert client._prewarm_ret is None
    assert closed_device_ids == [1234]


def test_stop_schedules_shutdown_on_running_loop(monkeypatch) -> None:
    client = ReverseProxyClient("example.com", 9000, config=ProxyConfig.from_args(auth_token="secret"))
    scheduled: dict[str, object] = {}
    sentinel_future = object()

    class _FakeLoop:
        def is_running(self) -> bool:
            return True

    def _fake_run_coroutine_threadsafe(coro, loop):
        scheduled["loop"] = loop
        scheduled["coro_name"] = coro.cr_code.co_name
        coro.close()
        return sentinel_future

    client._loop = _FakeLoop()
    monkeypatch.setattr(
        "vci_proxy.reverse_client.asyncio.run_coroutine_threadsafe",
        _fake_run_coroutine_threadsafe,
    )

    result = client.stop()

    assert result is sentinel_future
    assert scheduled == {
        "loop": client._loop,
        "coro_name": "shutdown",
    }


def test_stop_returns_completed_future_without_running_loop() -> None:
    client = ReverseProxyClient("example.com", 9000, config=ProxyConfig.from_args(auth_token="secret"))

    future = client.stop()

    assert future.done() is True
    assert future.result() is None


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
