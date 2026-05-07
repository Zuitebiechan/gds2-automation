from __future__ import annotations

import asyncio
import logging
import ssl
import struct
import time
import types
import json

import pytest

from diagnostic_platform.observability import flush_product_log_writers
from vci_proxy.config import ProxyConfig
from vci_proxy.cache_read_msgs import BUFFER_EMPTY
from vci_proxy.protocol import HEADER_SIZE, Message, MsgType, ProtocolDecoder, ProtocolEncoder
import vci_proxy.reverse_server as reverse_server_module
from vci_proxy.reverse_server import ReverseProxyServer
from vci_proxy.sweep_protocol import (
    SweepPlanStartRequest,
    SweepRequestSpec,
    SweepResultRecord,
)


class _FakeWriter:
    def __init__(self, *, peername=None, closing: bool = False) -> None:
        self.writes: list[bytes] = []
        self._peername = peername
        self._closing = closing
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None

    def get_extra_info(self, name: str):
        if name == "peername":
            return self._peername
        return None

    def is_closing(self) -> bool:
        return self._closing

    def close(self) -> None:
        self.closed = True
        self._closing = True


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


class _ResettingReader:
    def __init__(self, exc: BaseException | None = None) -> None:
        self._exc = exc or ConnectionResetError(64, "指定的网络名不再可用。")

    async def readexactly(self, n: int) -> bytes:
        raise self._exc


class _StepReader:
    def __init__(self, *steps: object) -> None:
        self._steps = list(steps)

    async def readexactly(self, n: int) -> bytes:
        if not self._steps:
            raise AssertionError("unexpected read")
        step = self._steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        data = bytes(step)
        if len(data) != n:
            raise AssertionError(f"expected {n} bytes, got {len(data)}")
        return data


def _read_product_log_events(tmp_path) -> list[dict[str, object]]:
    flush_product_log_writers()
    raw_dir = tmp_path / "RPA_Diagnostic" / "observability" / "cloud" / "raw"
    records: list[dict[str, object]] = []
    for path in sorted(raw_dir.glob("*.jsonl")):
        records.extend(
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    return records


def test_resolve_reverse_server_log_path_prefers_log_dir(tmp_path) -> None:
    path = reverse_server_module._resolve_reverse_server_log_path(
        environ={"LOG_DIR": str(tmp_path / "logs")}
    )

    assert path == tmp_path / "logs" / "vci_proxy.log"
    assert path.parent.is_dir()


def test_authenticate_vci_accepts_valid_auth_request(monkeypatch) -> None:
    server = ReverseProxyServer(
        config=ProxyConfig.from_args(auth_token="secret", read_ahead_enabled=False)
    )
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


def test_authenticate_vci_advertises_read_ahead_capability(monkeypatch) -> None:
    server = ReverseProxyServer(
        config=ProxyConfig.from_args(auth_token="secret", read_ahead_enabled=True)
    )
    reader = _FakeReader(ProtocolEncoder.encode_auth_req(123, b"x" * 32, sequence=7))
    writer = _FakeWriter()

    monkeypatch.setattr("vci_proxy.reverse_server.verify_signature", lambda token, timestamp, signature: (True, "ok"))

    accepted = asyncio.run(server._authenticate_vci(reader, writer))

    assert accepted is True
    _magic, length, msg_type, _sequence = Message.decode_header(writer.writes[0][:HEADER_SIZE])
    assert msg_type == MsgType.AUTH_RSP
    success, message = ProtocolDecoder.decode_auth_rsp(writer.writes[0][HEADER_SIZE:length])
    assert success is True
    assert "read_ahead=1" in message


def test_authenticate_vci_advertises_connection_epoch_hint(monkeypatch) -> None:
    server = ReverseProxyServer(
        config=ProxyConfig.from_args(auth_token="secret", read_ahead_enabled=True)
    )
    reader = _FakeReader(ProtocolEncoder.encode_auth_req(123, b"x" * 32, sequence=7))
    writer = _FakeWriter()

    monkeypatch.setattr(
        "vci_proxy.reverse_server.verify_signature",
        lambda token, timestamp, signature: (True, "ok"),
    )

    accepted = asyncio.run(
        server._authenticate_vci(
            reader,
            writer,
            connection_epoch_hint="epoch-test-001",
        )
    )

    assert accepted is True
    success, message = ProtocolDecoder.decode_auth_rsp(writer.writes[0][HEADER_SIZE:])
    assert success is True
    assert "connection_epoch=epoch-test-001" in message


def test_authenticate_vci_negotiates_write_collect_capability(monkeypatch) -> None:
    server = ReverseProxyServer(
        config=ProxyConfig.from_args(
            auth_token="secret",
            read_ahead_enabled=True,
            read_ahead_transaction_enabled=True,
        )
    )
    reader = _FakeReader(
        ProtocolEncoder.encode_auth_req(
            123,
            b"x" * 32,
            sequence=7,
            capabilities="read_ahead=1;write_collect=1",
        )
    )
    writer = _FakeWriter()

    monkeypatch.setattr("vci_proxy.reverse_server.verify_signature", lambda token, timestamp, signature: (True, "ok"))

    accepted = asyncio.run(server._authenticate_vci(reader, writer))

    assert accepted is True
    assert server._vci_write_collect_supported is True
    _magic, length, msg_type, _sequence = Message.decode_header(writer.writes[0][:HEADER_SIZE])
    assert msg_type == MsgType.AUTH_RSP
    success, message = ProtocolDecoder.decode_auth_rsp(writer.writes[0][HEADER_SIZE:length])
    assert success is True
    assert "read_ahead=1" in message
    assert "write_collect=1" in message


def test_authenticate_vci_negotiates_sweep_shadow_capability(monkeypatch) -> None:
    server = ReverseProxyServer(
        config=ProxyConfig.from_args(
            auth_token="secret",
            local_sweep_enabled=True,
            local_sweep_mode="shadow_local",
        )
    )
    reader = _FakeReader(
        ProtocolEncoder.encode_auth_req(
            123,
            b"x" * 32,
            sequence=7,
            capabilities="sweep_shadow=1",
        )
    )
    writer = _FakeWriter()
    monkeypatch.setattr("vci_proxy.reverse_server.verify_signature", lambda token, timestamp, signature: (True, "ok"))

    accepted = asyncio.run(server._authenticate_vci(reader, writer))

    assert accepted is True
    assert server._vci_sweep_shadow_supported is True
    _magic, length, msg_type, _sequence = Message.decode_header(writer.writes[0][:HEADER_SIZE])
    assert msg_type == MsgType.AUTH_RSP
    success, message = ProtocolDecoder.decode_auth_rsp(writer.writes[0][HEADER_SIZE:length])
    assert success is True
    assert "sweep_shadow=1" in message


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


def test_authenticate_vci_handles_connection_reset_during_initial_read(caplog) -> None:
    server = ReverseProxyServer(config=ProxyConfig.from_args(auth_token="secret"))
    reader = _ResettingReader()
    writer = _FakeWriter(peername=("80.94.95.221", 64154))

    with caplog.at_level(logging.WARNING):
        accepted = asyncio.run(server._authenticate_vci(reader, writer))

    assert accepted is False
    assert "VCI client disconnected during auth" in caplog.text
    assert "80.94.95.221" in caplog.text


def test_handle_vci_connection_ignores_auth_stage_connection_reset(caplog) -> None:
    async def _run() -> None:
        server = ReverseProxyServer(config=ProxyConfig.from_args(auth_token="secret"))
        current_writer = _FakeWriter(peername=("61.173.158.139", 6481))
        server.vci_writer = current_writer
        server.vci_connected.set()
        server._connection_epoch = "epoch-existing"
        server._tunnel_quality = types.SimpleNamespace(
            snapshot=lambda: {"connected": True, "fresh": True}
        )

        incoming_writer = _FakeWriter(peername=("80.94.95.221", 64154))

        with caplog.at_level(logging.WARNING):
            await server._handle_vci_connection(_ResettingReader(), incoming_writer)

        assert server.vci_writer is current_writer
        assert current_writer.closed is False
        assert incoming_writer.closed is True

    asyncio.run(_run())

    assert "VCI client disconnected during auth" in caplog.text
    assert "VCI tunnel authentication failed" in caplog.text
    assert "Unhandled exception in client_connected_cb" not in caplog.text


def test_handle_vci_connection_treats_midstream_connection_reset_as_clean_disconnect(
    monkeypatch,
    caplog,
    tmp_path,
) -> None:
    async def _run() -> None:
        monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
        server = ReverseProxyServer(config=ProxyConfig.from_args(auth_token="secret"))
        auth_frame = ProtocolEncoder.encode_auth_req(123, b"x" * 32, sequence=7)
        reader = _StepReader(
            auth_frame[:HEADER_SIZE],
            auth_frame[HEADER_SIZE:],
            ConnectionResetError(64, "指定的网络名不再可用。"),
        )
        writer = _FakeWriter(peername=("61.173.158.139", 6535))

        monkeypatch.setattr(
            "vci_proxy.reverse_server.verify_signature",
            lambda token, timestamp, signature: (True, "ok"),
        )

        async def _probe_loop(epoch: str) -> None:
            return None

        server._probe_loop = _probe_loop
        server._write_tunnel_quality_snapshot = lambda: None

        with caplog.at_level(logging.WARNING):
            await server._handle_vci_connection(reader, writer)

        assert writer.closed is True
        assert server.vci_writer is None
        assert server.vci_connected.is_set() is False

    asyncio.run(_run())

    assert "VCI tunnel lost connection" in caplog.text
    assert "VCI tunnel disconnected" in caplog.text
    assert not any(record.exc_info for record in caplog.records)


def test_handle_vci_connection_skips_disconnect_event_during_shutdown(monkeypatch) -> None:
    async def _run() -> None:
        server = ReverseProxyServer(config=ProxyConfig.from_args(auth_token="secret"))
        auth_frame = ProtocolEncoder.encode_auth_req(123, b"x" * 32, sequence=7)
        reader = _StepReader(
            auth_frame[:HEADER_SIZE],
            auth_frame[HEADER_SIZE:],
            asyncio.CancelledError(),
        )
        writer = _FakeWriter(peername=("61.173.158.139", 6535))
        emitted: list[str] = []

        monkeypatch.setattr(
            "vci_proxy.reverse_server.verify_signature",
            lambda token, timestamp, signature: (True, "ok"),
        )

        async def _probe_loop(epoch: str) -> None:
            return None

        server._probe_loop = _probe_loop
        server._write_tunnel_quality_snapshot = lambda: None
        server._emit_tunnel_event = lambda event_type, **kwargs: emitted.append(event_type)
        server._shutting_down = True

        await server._handle_vci_connection(reader, writer)

        assert "tunnel.lifecycle.disconnected" not in emitted

    asyncio.run(_run())


def test_handle_proxy_connection_skips_cancelled_shutdown(monkeypatch) -> None:
    async def _run() -> None:
        server = ReverseProxyServer()
        server.vci_connected.set()
        server.vci_writer = _FakeWriter(peername=("10.0.0.9", 9000))
        server._connection_epoch = "epoch-1"
        server._shutting_down = True
        proxy_reader = _StepReader(asyncio.CancelledError())
        proxy_writer = _FakeWriter(peername=("127.0.0.1", 50000))

        await server._handle_proxy_connection(proxy_reader, proxy_writer)

        assert proxy_writer.closed is True

    asyncio.run(_run())


def test_emit_proxy_request_event_is_best_effort_during_shutdown(monkeypatch, caplog) -> None:
    server = ReverseProxyServer()
    server._shutting_down = True
    monkeypatch.setattr(
        reverse_server_module,
        "emit_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("writer unavailable")),
    )

    with caplog.at_level(logging.DEBUG, logger="vci_proxy.reverse_server"):
        server._emit_proxy_request_event(
            "proxy.request.replied_to_dll",
            dll_seq=1,
            msg_name="PING_REQ",
            reason="cache_reply",
        )

    assert "Skipping proxy observability during shutdown" in caplog.text


def test_start_handles_cancelled_gather_as_graceful_shutdown(monkeypatch) -> None:
    events: list[str] = []

    class _FakeServer:
        def __init__(self, name: str) -> None:
            self.name = name

        async def serve_forever(self) -> None:
            return None

        def close(self) -> None:
            events.append(f"close:{self.name}")

        async def wait_closed(self) -> None:
            events.append(f"wait_closed:{self.name}")

    created: list[_FakeServer] = []

    async def _fake_start_server(*args, **kwargs):
        name = "vci" if not created else "proxy"
        server = _FakeServer(name)
        created.append(server)
        return server

    async def _fake_gather(*args, **kwargs):
        for awaitable in args:
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
        raise asyncio.CancelledError()

    monkeypatch.setattr("vci_proxy.reverse_server.asyncio.start_server", _fake_start_server)
    monkeypatch.setattr("vci_proxy.reverse_server.asyncio.gather", _fake_gather)

    server = ReverseProxyServer(config=ProxyConfig.from_args(auth_token="secret"))

    asyncio.run(server.start())

    assert server._shutting_down is True
    assert events == [
        "close:vci",
        "wait_closed:vci",
        "close:proxy",
        "wait_closed:proxy",
    ]


def test_run_server_until_stopped_ignores_repeated_sigint(monkeypatch, caplog) -> None:
    class _FakeTask:
        def __init__(self) -> None:
            self.cancel_calls = 0

        def cancel(self):
            self.cancel_calls += 1

        def done(self):
            return False

    class _DoneAwaitable:
        def __await__(self):
            if False:
                yield None
            return None

    class _FakeLoop:
        def __init__(self) -> None:
            self.main_task = _FakeTask()
            self.created_coro = None

        def create_task(self, coro):
            self.created_coro = coro
            return self.main_task

        def call_soon_threadsafe(self, callback, *args):
            callback(*args)

        def run_until_complete(self, awaitable):
            if awaitable is self.main_task:
                return None
            return None

        def shutdown_asyncgens(self):
            return _DoneAwaitable()

        def shutdown_default_executor(self):
            return _DoneAwaitable()

        def close(self):
            if self.created_coro is not None:
                self.created_coro.close()
            return None

    fake_loop = _FakeLoop()
    signal_handlers: dict[object, object] = {}

    monkeypatch.setattr(reverse_server_module.asyncio, "new_event_loop", lambda: fake_loop)
    monkeypatch.setattr(reverse_server_module.asyncio, "set_event_loop", lambda loop: None)
    monkeypatch.setattr(reverse_server_module.asyncio, "all_tasks", lambda loop: set())
    monkeypatch.setattr(reverse_server_module.signal, "getsignal", lambda signum: "previous")
    monkeypatch.setattr(reverse_server_module.signal, "signal", lambda signum, handler: signal_handlers.setdefault(signum, handler))
    monkeypatch.setattr(reverse_server_module.os, "_exit", lambda code: (_ for _ in ()).throw(SystemExit(code)))

    server = ReverseProxyServer()
    reverse_server_module._run_server_until_stopped(server)

    sigint = reverse_server_module.signal.SIGINT
    handler = signal_handlers[sigint]
    handler(sigint, None)
    with pytest.raises(SystemExit) as exc:
        handler(sigint, None)

    captured = types.SimpleNamespace(out=caplog.text)
    assert "停止服务器..." in captured.out
    assert "强制停止服务器..." in captured.out
    assert fake_loop.main_task.cancel_calls == 1
    assert exc.value.code == 130


def test_handle_vci_connection_emits_observability_connected_and_disconnected_events(
    monkeypatch,
    tmp_path,
) -> None:
    async def _run() -> None:
        monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
        server = ReverseProxyServer(config=ProxyConfig.from_args(auth_token="secret"))
        auth_frame = ProtocolEncoder.encode_auth_req(123, b"x" * 32, sequence=7)
        reader = _StepReader(
            auth_frame[:HEADER_SIZE],
            auth_frame[HEADER_SIZE:],
            ConnectionResetError(64, "network dropped"),
        )
        writer = _FakeWriter(peername=("61.173.158.139", 6535))

        monkeypatch.setattr(
            "vci_proxy.reverse_server.verify_signature",
            lambda token, timestamp, signature: (True, "ok"),
        )

        async def _probe_loop(epoch: str) -> None:
            return None

        server._probe_loop = _probe_loop

        await server._handle_vci_connection(reader, writer)

    asyncio.run(_run())

    records = _read_product_log_events(tmp_path)
    connected = [record for record in records if record["event_type"] == "tunnel.lifecycle.connected"]
    disconnected = [
        record for record in records if record["event_type"] == "tunnel.lifecycle.disconnected"
    ]

    assert connected
    assert disconnected
    assert connected[-1]["connection_epoch"] == disconnected[-1]["connection_epoch"]
    assert disconnected[-1]["status"] == "error"
    assert "connection_lost" in str(disconnected[-1]["reason"])


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

    cached, ioctl_id, reason = server._try_serve_cached(MsgType.IOCTL_REQ, body, sequence=11)

    assert ioctl_id == 0x03
    assert reason == "cache_hit"
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


def test_run_probe_emits_observability_failure_event(monkeypatch, tmp_path) -> None:
    class _FakeTunnelQuality:
        def __init__(self) -> None:
            self.probe_failures = 0

        def record_probe(self, network_ms: float) -> None:
            raise AssertionError("record_probe should not be called on failure")

        def record_probe_failure(self, reason: str = "") -> None:
            self.probe_failures += 1

    async def _run() -> None:
        monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
        server = ReverseProxyServer()
        server.vci_writer = _FakeWriter()
        server._connection_epoch = "epoch-1"
        server._tunnel_quality = _FakeTunnelQuality()
        server._write_tunnel_quality_snapshot = lambda: None
        server._next_sequence = lambda: 9

        async def _failing_wait_for(future, timeout):
            raise asyncio.TimeoutError()

        monkeypatch.setattr("vci_proxy.reverse_server.asyncio.wait_for", _failing_wait_for)

        await server._run_probe("epoch-1")

    asyncio.run(_run())

    records = _read_product_log_events(tmp_path)
    failures = [record for record in records if record["event_type"] == "tunnel.probe.failure"]

    assert failures
    assert failures[-1]["connection_epoch"] == "epoch-1"
    assert failures[-1]["status"] == "error"
    assert failures[-1]["failure_code"] == "probe_failure"


def test_write_tunnel_quality_snapshot_skips_healthy_logs(monkeypatch, caplog) -> None:
    server = ReverseProxyServer()
    server._tunnel_quality = types.SimpleNamespace(
        snapshot=lambda: {
            "connection_epoch": "epoch-1",
            "connected": True,
            "fresh": True,
            "updated_at": "2026-04-15T00:00:00Z",
            "source": "probe",
            "sample_count": 5,
            "network_ms": {"last": 20.0, "p50": 20.0, "p95": 30.0},
            "grade": "good",
            "status": "healthy",
            "reason": "p95 within good threshold",
            "probe_failures": 0,
        }
    )
    monkeypatch.setattr(reverse_server_module, "write_tunnel_quality_snapshot", lambda snapshot: None)

    with caplog.at_level(logging.INFO, logger="vci_proxy.reverse_server"):
        server._write_tunnel_quality_snapshot()

    assert "[TUNNEL_QUALITY]" not in caplog.text


def test_write_tunnel_quality_snapshot_logs_blocked_state_once(monkeypatch, caplog) -> None:
    server = ReverseProxyServer()
    server._tunnel_quality = types.SimpleNamespace(
        snapshot=lambda: {
            "connection_epoch": "epoch-2",
            "connected": False,
            "fresh": False,
            "updated_at": "2026-04-15T00:00:00Z",
            "source": "probe",
            "sample_count": 0,
            "network_ms": {"last": None, "p50": None, "p95": None},
            "grade": "block",
            "status": "blocked",
            "reason": "tunnel_disconnected",
            "probe_failures": 0,
        }
    )
    monkeypatch.setattr(reverse_server_module, "write_tunnel_quality_snapshot", lambda snapshot: None)

    with caplog.at_level(logging.WARNING, logger="vci_proxy.reverse_server"):
        server._write_tunnel_quality_snapshot()
        server._write_tunnel_quality_snapshot()

    tunnel_logs = [record.message for record in caplog.records if "TUNNEL_QUALITY" in record.message]
    assert tunnel_logs == [
        "[TUNNEL_QUALITY] status=blocked connected=False reason=tunnel_disconnected"
    ]


def test_write_tunnel_quality_snapshot_tolerates_permission_error(monkeypatch, caplog) -> None:
    server = ReverseProxyServer()
    server._tunnel_quality = types.SimpleNamespace(
        snapshot=lambda: {
            "connection_epoch": "epoch-3",
            "connected": True,
            "fresh": True,
            "updated_at": "2026-04-15T00:00:00Z",
            "source": "probe",
            "sample_count": 1,
            "network_ms": {"last": 1.0, "p50": 1.0, "p95": 1.0},
            "grade": "good",
            "status": "healthy",
            "reason": "ok",
            "probe_failures": 0,
        }
    )
    monkeypatch.setattr(
        reverse_server_module,
        "write_tunnel_quality_snapshot",
        lambda snapshot: (_ for _ in ()).throw(PermissionError("denied")),
    )

    with caplog.at_level(logging.WARNING, logger="vci_proxy.reverse_server"):
        server._write_tunnel_quality_snapshot()

    assert "Failed to persist tunnel quality snapshot: denied" in caplog.text


def test_write_tunnel_quality_snapshot_deduplicates_repeated_permission_errors(monkeypatch, caplog) -> None:
    server = ReverseProxyServer()
    server._tunnel_quality = types.SimpleNamespace(
        snapshot=lambda: {
            "connection_epoch": "epoch-4",
            "connected": True,
            "fresh": True,
            "updated_at": "2026-04-15T00:00:00Z",
            "source": "probe",
            "sample_count": 1,
            "network_ms": {"last": 1.0, "p50": 1.0, "p95": 1.0},
            "grade": "good",
            "status": "healthy",
            "reason": "ok",
            "probe_failures": 0,
        }
    )
    monkeypatch.setattr(
        reverse_server_module,
        "write_tunnel_quality_snapshot",
        lambda snapshot: (_ for _ in ()).throw(PermissionError("denied")),
    )

    with caplog.at_level(logging.WARNING, logger="vci_proxy.reverse_server"):
        server._write_tunnel_quality_snapshot()
        server._write_tunnel_quality_snapshot()

    assert caplog.text.count("Failed to persist tunnel quality snapshot: denied") == 1


def test_invalidate_caches_clears_channel_and_filter_entries() -> None:
    events: list[tuple[str, int]] = []
    server = ReverseProxyServer()
    server._read_cache = types.SimpleNamespace(
        invalidate_channel=lambda channel_id: events.append(("read", channel_id)),
        mark_channel_active=lambda channel_id, **kwargs: events.append(("active", channel_id)),
    )
    server._filter_cache = types.SimpleNamespace(
        invalidate_channel=lambda channel_id: events.append(("filter", channel_id)),
        on_stop_filter=lambda filter_id: events.append(("stop", filter_id)),
        clear=lambda: None,
    )
    server._ioctl_cache = types.SimpleNamespace(
        invalidate_channel=lambda channel_id: events.append(("ioctl", channel_id)),
        invalidate=lambda: None,
        is_cacheable=lambda ioctl_id: False,
    )

    server._invalidate_caches(MsgType.DISCONNECT_REQ, struct.pack(">I", 33))
    server._invalidate_caches(
        MsgType.START_FILTER_REQ,
        ProtocolEncoder.encode_start_filter_req(33, 1, None, None, None)[HEADER_SIZE:],
    )
    server._invalidate_caches(MsgType.STOP_FILTER_REQ, struct.pack(">II", 33, 88))
    server._invalidate_caches(
        MsgType.IOCTL_REQ,
        ProtocolEncoder.encode_ioctl_req(33, 0x07, None)[HEADER_SIZE:],
    )

    assert events == [
        ("read", 33),
        ("filter", 33),
        ("ioctl", 33),
        ("active", 33),
        ("active", 33),
        ("stop", 88),
        ("active", 33),
    ]


def test_main_disables_windows_quick_edit_before_starting_server(monkeypatch) -> None:
    events: list[str] = []

    monkeypatch.setattr(
        reverse_server_module,
        "_disable_windows_quick_edit",
        lambda: events.append("quick-edit"),
        raising=False,
    )

    monkeypatch.setattr(
        reverse_server_module.argparse.ArgumentParser,
        "parse_args",
        lambda self: types.SimpleNamespace(
            listen_port=9000,
            proxy_port=9001,
            auth_token="secret",
            tls=False,
            tls_cert=None,
            tls_key=None,
            tls_ca=None,
            tls_require_client_cert=False,
            no_read_cache=False,
            read_cache_ttl=150,
            read_cache_post_write_bypass_ms=150,
            read_cache_active_ttl_ms=25,
            read_cache_active_window_ms=500,
            read_cache_max_timeout_ms=25,
            read_ahead=False,
            read_ahead_window_ms=200,
            read_ahead_max_reads=3,
            read_ahead_read_timeout_ms=0,
            read_ahead_max_messages=16,
            read_ahead_transaction=None,
            read_ahead_transaction_max_network_ms=None,
            read_ahead_transaction_cooldown_ms=None,
            no_filter_dedup=False,
            no_vbatt_cache=False,
            vbatt_ttl=5,
            no_ioctl_cache=False,
            ioctl_ttl=5,
            benchmark_log=None,
            benchmark_label="proxy_run",
        ),
    )

    monkeypatch.setattr(
        reverse_server_module,
        "_run_server_until_stopped",
        lambda server: events.append("run"),
    )

    reverse_server_module.main()

    assert events[:2] == ["quick-edit", "run"]


def test_new_vci_connection_is_rejected_when_existing_tunnel_is_healthy() -> None:
    server = ReverseProxyServer()
    server.vci_writer = _FakeWriter(peername=("1.1.1.1", 1111))
    server._connection_epoch = "epoch-1"
    server.vci_connected.set()
    server._tunnel_quality = types.SimpleNamespace(
        snapshot=lambda: {
            "connection_epoch": "epoch-1",
            "connected": True,
            "fresh": True,
            "status": "healthy",
            "reason": "p95 within good threshold",
        }
    )

    accepted, reason, existing_addr = server._should_accept_new_vci_connection(("2.2.2.2", 2222))

    assert accepted is False
    assert reason == "existing_tunnel_healthy"
    assert existing_addr == ("1.1.1.1", 1111)


def test_new_vci_connection_can_replace_stale_existing_tunnel() -> None:
    server = ReverseProxyServer()
    server.vci_writer = _FakeWriter(peername=("1.1.1.1", 1111))
    server._connection_epoch = "epoch-1"
    server.vci_connected.set()
    server._tunnel_quality = types.SimpleNamespace(
        snapshot=lambda: {
            "connection_epoch": "epoch-1",
            "connected": True,
            "fresh": False,
            "status": "blocked",
            "reason": "snapshot_stale",
        }
    )

    accepted, reason, existing_addr = server._should_accept_new_vci_connection(("2.2.2.2", 2222))

    assert accepted is True
    assert reason == "existing_tunnel_stale"
    assert existing_addr == ("1.1.1.1", 1111)


def test_new_vci_connection_can_replace_probe_failed_existing_tunnel() -> None:
    server = ReverseProxyServer()
    server.vci_writer = _FakeWriter(peername=("1.1.1.1", 1111))
    server._connection_epoch = "epoch-1"
    server.vci_connected.set()
    server._tunnel_quality = types.SimpleNamespace(
        snapshot=lambda: {
            "connection_epoch": "epoch-1",
            "connected": True,
            "fresh": True,
            "status": "blocked",
            "reason": "probe_failures",
            "probe_failures": 1,
        }
    )

    accepted, reason, existing_addr = server._should_accept_new_vci_connection(("2.2.2.2", 2222))

    assert accepted is True
    assert reason == "existing_tunnel_probe_failed"
    assert existing_addr == ("1.1.1.1", 1111)


def test_handle_proxy_connection_emits_staged_success_events(monkeypatch, tmp_path) -> None:
    async def _run() -> None:
        monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
        server = ReverseProxyServer()
        server.vci_connected.set()
        server.vci_writer = _FakeWriter(peername=("10.0.0.9", 9000))
        server._connection_epoch = "epoch-1"
        server._next_sequence = lambda: 77

        proxy_reader = _FakeReader(ProtocolEncoder.encode_disconnect_req(33, sequence=5))
        proxy_writer = _FakeWriter(peername=("127.0.0.1", 50000))

        async def _success_wait_for(awaitable, timeout):
            if isinstance(awaitable, asyncio.Future):
                return (MsgType.DISCONNECT_RSP, struct.pack(">I", 0), 12.5)
            return await awaitable

        monkeypatch.setattr("vci_proxy.reverse_server.asyncio.wait_for", _success_wait_for)

        await server._handle_proxy_connection(proxy_reader, proxy_writer)

    asyncio.run(_run())

    records = _read_product_log_events(tmp_path)
    event_types = [record["event_type"] for record in records]

    assert "proxy.request.received_from_dll" in event_types
    assert "proxy.request.cache_decision" in event_types
    assert "proxy.request.forwarded_to_tunnel" in event_types
    assert "proxy.request.response_received" in event_types
    assert "proxy.request.replied_to_dll" in event_types

    forwarded = next(record for record in records if record["event_type"] == "proxy.request.forwarded_to_tunnel")
    assert forwarded["connection_epoch"] == "epoch-1"
    assert forwarded["dll_seq"] == 5
    assert forwarded["proxy_seq"] == 77


def test_handle_proxy_connection_emits_read_msgs_cache_hit_metadata(monkeypatch, tmp_path) -> None:
    async def _run() -> None:
        monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
        server = ReverseProxyServer()
        server.vci_connected.set()
        server.vci_writer = _FakeWriter(peername=("10.0.0.9", 9000))
        server._connection_epoch = "epoch-read-cache"
        server._read_cache.record_result(77, BUFFER_EMPTY)

        proxy_reader = _FakeReader(
            ProtocolEncoder.encode_read_msgs_req(77, num_msgs=4, timeout=0, sequence=21)
        )
        proxy_writer = _FakeWriter(peername=("127.0.0.1", 50002))

        await server._handle_proxy_connection(proxy_reader, proxy_writer)

        assert server.vci_writer.writes == []
        assert len(proxy_writer.writes) == 1

    asyncio.run(_run())

    records = _read_product_log_events(tmp_path)
    event_types = [record["event_type"] for record in records]

    assert "proxy.request.forwarded_to_tunnel" not in event_types

    received = next(record for record in records if record["event_type"] == "proxy.request.received_from_dll")
    cache_decision = next(record for record in records if record["event_type"] == "proxy.request.cache_decision")
    replied = next(record for record in records if record["event_type"] == "proxy.request.replied_to_dll")

    assert received["connection_epoch"] == "epoch-read-cache"
    assert received["channel_id"] == 77
    assert received["num_msgs"] == 4
    assert received["timeout"] == 0
    assert cache_decision["cache_hit"] is True
    assert cache_decision["reason"] == "cache_hit"
    assert replied["cache_hit"] is True
    assert replied["return_code"] == BUFFER_EMPTY
    assert replied["message_count"] == 0
    assert replied["payload_bytes"] == 0
    assert replied["read_result"] == "empty"


def test_proxy_observability_summarizes_payloads_and_engine_speed_candidate() -> None:
    server = ReverseProxyServer()
    engine_speed_message = {
        "protocol_id": 6,
        "rx_status": 0,
        "tx_flags": 0,
        "timestamp": 123,
        "data": b"\x05\x62\xf4\x0c\x1f\x40",
    }

    write_body = ProtocolEncoder.encode_write_msgs_req(
        77,
        [engine_speed_message],
        timeout=25,
        sequence=10,
    )[HEADER_SIZE:]
    request_fields = server._request_observability_fields(
        MsgType.WRITE_MSGS_REQ,
        write_body,
    )

    assert request_fields["write_payload_bytes"] == 6
    assert request_fields["write_payload_digest"]
    assert request_fields["write_payload_sample_count"] == 1
    assert request_fields["write_payload_samples"][0]["data_prefix_hex"] == "0562f40c1f40"
    assert "can_id" not in request_fields["write_payload_samples"][0]
    assert request_fields["write_engine_speed_candidate_rpm"] == 2000.0
    assert request_fields["write_engine_speed_candidate_source"] == "uds_did_f40c"

    read_body = ProtocolEncoder.encode_read_msgs_rsp(
        0,
        [engine_speed_message],
        sequence=11,
    )[HEADER_SIZE:]
    response_fields = server._response_observability_fields(
        MsgType.READ_MSGS_RSP,
        read_body,
    )

    assert response_fields["return_code"] == 0
    assert response_fields["message_count"] == 1
    assert response_fields["payload_bytes"] == 6
    assert response_fields["read_result"] == "data"
    assert response_fields["read_payload_digest"]
    assert response_fields["read_payload_samples"][0]["data_digest"]
    assert response_fields["read_engine_speed_candidate_rpm"] == 2000.0

    gm_write_body = ProtocolEncoder.encode_write_msgs_req(
        77,
        [
            {
                "protocol_id": 6,
                "rx_status": 0,
                "tx_flags": 64,
                "timestamp": 124,
                "data": b"\x00\x00\x07\xe0\xa9\x81\x1a",
            }
        ],
        timeout=25,
        sequence=12,
    )[HEADER_SIZE:]
    gm_request_fields = server._request_observability_fields(
        MsgType.WRITE_MSGS_REQ,
        gm_write_body,
    )
    gm_request_sample = gm_request_fields["write_payload_samples"][0]

    assert gm_request_sample["can_id"] == 0x7E0
    assert gm_request_sample["can_id_hex"] == "000007e0"
    assert gm_request_sample["can_payload_length"] == 3
    assert gm_request_sample["can_payload_prefix_hex"] == "a9811a"
    assert gm_request_sample["gm_request_service_id"] == 0xA9
    assert gm_request_sample["gm_request_subfunction"] == 0x81
    assert gm_request_sample["gm_request_packet_id"] == 0x1A
    assert gm_request_sample["gm_request_packet_id_hex"] == "1a"

    gm_read_body = ProtocolEncoder.encode_read_msgs_rsp(
        0,
        [
            {
                "protocol_id": 6,
                "rx_status": 0,
                "tx_flags": 0,
                "timestamp": 125,
                "data": b"\x00\x00\x05\xe8\xf9\x00\x28\x21\x0c\x00\x00\x4c",
            }
        ],
        sequence=13,
    )[HEADER_SIZE:]
    gm_response_fields = server._response_observability_fields(
        MsgType.READ_MSGS_RSP,
        gm_read_body,
    )
    gm_response_sample = gm_response_fields["read_payload_samples"][0]

    assert gm_response_sample["can_id"] == 0x5E8
    assert gm_response_sample["can_id_hex"] == "000005e8"
    assert gm_response_sample["can_payload_length"] == 8
    assert gm_response_sample["can_payload_prefix_hex"] == "f90028210c00004c"
    assert gm_response_sample["gm_data_packet_id"] == 0xF9
    assert gm_response_sample["gm_data_packet_id_hex"] == "f9"


def test_proxy_observability_tracks_read_payload_changes(monkeypatch) -> None:
    server = ReverseProxyServer()
    request_fields = {"channel_id": 77}
    response_fields = {
        "read_result": "data",
        "read_payload_digest": "digest-a",
    }
    monotonic_values = iter([10.0, 10.5, 13.0])
    monkeypatch.setattr(reverse_server_module.time, "monotonic", lambda: next(monotonic_values))

    server._augment_read_payload_delta_fields(request_fields, response_fields)

    assert response_fields["read_payload_changed"] is True
    assert response_fields["read_payload_change_kind"] == "first_data_on_channel"

    same_response_fields = {
        "read_result": "data",
        "read_payload_digest": "digest-a",
    }
    server._augment_read_payload_delta_fields(request_fields, same_response_fields)

    assert same_response_fields["read_payload_changed"] is False
    assert same_response_fields["read_payload_change_kind"] == "unchanged"
    assert same_response_fields["read_payload_observation_gap_ms"] == pytest.approx(500.0)

    changed_response_fields = {
        "read_result": "data",
        "read_payload_digest": "digest-b",
    }
    server._augment_read_payload_delta_fields(request_fields, changed_response_fields)

    assert changed_response_fields["read_payload_changed"] is True
    assert changed_response_fields["read_payload_change_kind"] == "changed"
    assert changed_response_fields["read_payload_observation_gap_ms"] == pytest.approx(2500.0)


def test_proxy_observability_marks_multisecond_live_request_gap(monkeypatch) -> None:
    server = ReverseProxyServer()
    monotonic_values = iter([10.0, 13.2])
    monkeypatch.setattr(reverse_server_module.time, "monotonic", lambda: next(monotonic_values))

    write_fields = {"channel_id": 77}
    server._augment_live_cadence_fields(MsgType.WRITE_MSGS_REQ, 31, write_fields)

    assert write_fields["live_request_kind"] == "write"
    assert write_fields["live_inter_request_gap_bucket"] == "first_on_channel"

    read_fields = {"channel_id": 77}
    server._augment_live_cadence_fields(MsgType.READ_MSGS_REQ, 32, read_fields)

    assert read_fields["live_request_kind"] == "read"
    assert read_fields["previous_live_msg_name"] == "WRITE_MSGS_REQ"
    assert read_fields["previous_live_dll_seq"] == 31
    assert read_fields["live_inter_request_gap_ms"] == pytest.approx(3200.0)
    assert read_fields["live_inter_request_gap_bucket"] == "ge_3000ms"


def test_proxy_observability_emits_live_cadence_gap_event(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    server = ReverseProxyServer()
    server._connection_epoch = "epoch-live-gap"

    server._emit_live_cadence_gap_if_needed(
        dll_seq=32,
        msg_name="READ_MSGS_REQ",
        request_fields={
            "channel_id": 77,
            "num_msgs": 300,
            "timeout": 0,
            "live_request_kind": "read",
            "previous_live_msg_name": "WRITE_MSGS_REQ",
            "previous_live_dll_seq": 31,
            "live_inter_request_gap_ms": 3200.0,
            "live_inter_request_gap_bucket": "ge_3000ms",
        },
    )

    records = _read_product_log_events(tmp_path)
    gap_event = next(record for record in records if record["event_type"] == "proxy.j2534.cadence_gap")

    assert gap_event["connection_epoch"] == "epoch-live-gap"
    assert gap_event["dll_seq"] == 32
    assert gap_event["reason"] == "inter_request_gap_ge_3000ms"
    assert gap_event["channel_id"] == 77
    assert gap_event["previous_live_dll_seq"] == 31
    assert gap_event["live_gap_threshold_ms"] == 3000.0


def test_handle_proxy_connection_serves_prefetched_read_msgs_before_empty_cache(monkeypatch, tmp_path) -> None:
    async def _run() -> None:
        monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
        server = ReverseProxyServer(
            config=ProxyConfig.from_args(read_ahead_enabled=True, read_ahead_max_messages=4)
        )
        server.vci_connected.set()
        server.vci_writer = _FakeWriter(peername=("10.0.0.9", 9000))
        server._connection_epoch = "epoch-prefetch"
        server._read_cache.record_result(77, BUFFER_EMPTY)
        message = {
            "protocol_id": 6,
            "rx_status": 0,
            "tx_flags": 0,
            "timestamp": 123,
            "data": b"\x62\xf4\x0c",
        }
        server._prefetch_read_msgs.record_read_rsp_body(
            77,
            ProtocolEncoder.encode_read_msgs_rsp(0, [message], sequence=0)[HEADER_SIZE:],
        )

        proxy_reader = _FakeReader(
            ProtocolEncoder.encode_read_msgs_req(77, num_msgs=4, timeout=0, sequence=21)
        )
        proxy_writer = _FakeWriter(peername=("127.0.0.1", 50002))

        await server._handle_proxy_connection(proxy_reader, proxy_writer)

        assert server.vci_writer.writes == []
        assert len(proxy_writer.writes) == 1
        _magic, _length, msg_type, sequence = Message.decode_header(
            proxy_writer.writes[0][:HEADER_SIZE]
        )
        assert (msg_type, sequence) == (MsgType.READ_MSGS_RSP, 21)
        assert ProtocolDecoder.decode_read_msgs_rsp(proxy_writer.writes[0][HEADER_SIZE:]) == (
            0,
            [message],
        )
        assert server._prefetch_read_msgs.try_serve(77, 1, 22) is None

    asyncio.run(_run())

    records = _read_product_log_events(tmp_path)
    event_types = [record["event_type"] for record in records]

    assert "proxy.request.forwarded_to_tunnel" not in event_types

    cache_decision = next(record for record in records if record["event_type"] == "proxy.request.cache_decision")
    replied = next(record for record in records if record["event_type"] == "proxy.request.replied_to_dll")

    assert cache_decision["cache_hit"] is True
    assert cache_decision["reason"] == "prefetch_hit"
    assert replied["cache_hit"] is True
    assert replied["return_code"] == 0
    assert replied["message_count"] == 1
    assert replied["payload_bytes"] == 3
    assert replied["read_result"] == "data"


def test_invalidate_caches_preserves_prefetched_fifo_on_write_for_queue_order() -> None:
    server = ReverseProxyServer(
        config=ProxyConfig.from_args(read_ahead_enabled=True, read_ahead_max_messages=4)
    )
    message = {
        "protocol_id": 6,
        "rx_status": 0,
        "tx_flags": 0,
        "timestamp": 0,
        "data": b"\x62\xf4\x0c",
    }
    server._prefetch_read_msgs.record_read_rsp_body(
        44,
        ProtocolEncoder.encode_read_msgs_rsp(0, [message], sequence=0)[HEADER_SIZE:],
    )

    server._invalidate_caches(
        MsgType.WRITE_MSGS_REQ,
        ProtocolEncoder.encode_write_msgs_req(44, [{"protocol_id": 6, "data": b"\x22"}], 25)[HEADER_SIZE:],
        sequence=31,
    )

    response = server._prefetch_read_msgs.try_serve(44, num_msgs=1, sequence=32)
    assert response is not None
    assert ProtocolDecoder.decode_read_msgs_rsp(response[HEADER_SIZE:]) == (0, [message])


def test_invalidate_caches_clears_prefetched_fifo_on_mutating_ioctl() -> None:
    server = ReverseProxyServer(
        config=ProxyConfig.from_args(read_ahead_enabled=True, read_ahead_max_messages=4)
    )
    message = {"protocol_id": 6, "data": b"\x62\xf4\x0c"}
    server._prefetch_read_msgs.record_read_rsp_body(
        44,
        ProtocolEncoder.encode_read_msgs_rsp(0, [message], sequence=0)[HEADER_SIZE:],
    )

    server._invalidate_caches(
        MsgType.IOCTL_REQ,
        ProtocolEncoder.encode_ioctl_req(44, 0x1234, None)[HEADER_SIZE:],
    )

    assert server._prefetch_read_msgs.try_serve(44, num_msgs=1, sequence=32) is None


def test_record_in_caches_clears_prefetched_fifo_after_failed_write_response() -> None:
    server = ReverseProxyServer(
        config=ProxyConfig.from_args(read_ahead_enabled=True, read_ahead_max_messages=4)
    )
    message = {"protocol_id": 6, "data": b"\x62\xf4\x0c"}
    server._prefetch_read_msgs.record_read_rsp_body(
        44,
        ProtocolEncoder.encode_read_msgs_rsp(0, [message], sequence=0)[HEADER_SIZE:],
    )
    write_body = ProtocolEncoder.encode_write_msgs_req(
        44,
        [{"protocol_id": 6, "data": b"\x22"}],
        timeout=25,
    )[HEADER_SIZE:]

    server._record_in_caches(
        MsgType.WRITE_MSGS_REQ,
        write_body,
        MsgType.WRITE_MSGS_RSP,
        ProtocolEncoder.encode_write_msgs_rsp(7, 0, sequence=0)[HEADER_SIZE:],
        ioctl_id=None,
    )

    assert server._prefetch_read_msgs.try_serve(44, num_msgs=1, sequence=32) is None


def test_clear_prefetch_after_failed_write_ignores_non_write_requests() -> None:
    server = ReverseProxyServer(
        config=ProxyConfig.from_args(read_ahead_enabled=True, read_ahead_max_messages=4)
    )
    message = {"protocol_id": 6, "data": b"\x62\xf4\x0c"}
    server._prefetch_read_msgs.record_read_rsp_body(
        44,
        ProtocolEncoder.encode_read_msgs_rsp(0, [message], sequence=0)[HEADER_SIZE:],
    )

    server._clear_prefetch_after_failed_write(
        MsgType.READ_MSGS_REQ,
        ProtocolEncoder.encode_read_msgs_req(44, 1, 0)[HEADER_SIZE:],
    )

    assert server._prefetch_read_msgs.try_serve(44, num_msgs=1, sequence=32) is not None


def test_build_tunnel_request_wraps_write_when_transaction_enabled_and_supported() -> None:
    server = ReverseProxyServer(
        config=ProxyConfig.from_args(
            read_ahead_enabled=True,
            read_ahead_transaction_enabled=True,
            read_ahead_window_ms=180,
            read_ahead_max_reads=2,
            read_ahead_read_timeout_ms=5,
            read_ahead_max_messages=8,
        )
    )
    server._vci_write_collect_supported = True
    write_body = ProtocolEncoder.encode_write_msgs_req(
        44,
        [{"protocol_id": 6, "data": b"\x22"}],
        timeout=25,
    )[HEADER_SIZE:]

    encoded, fwd_type, fwd_body, reason = server._build_tunnel_request(
        MsgType.WRITE_MSGS_REQ,
        write_body,
        sequence=77,
    )
    _magic, length, msg_type, sequence = Message.decode_header(encoded[:HEADER_SIZE])
    request = ProtocolDecoder.decode_write_and_collect_reads_req(fwd_body)

    assert length == len(encoded)
    assert msg_type == MsgType.WRITE_AND_COLLECT_READS_REQ
    assert fwd_type == MsgType.WRITE_AND_COLLECT_READS_REQ
    assert sequence == 77
    assert reason == "write_collect_transaction"
    assert request.collect_window_ms == 180
    assert request.max_reads == 2
    assert request.read_timeout_ms == 5
    assert request.max_messages == 8
    assert request.write_req_body == write_body


def test_build_tunnel_request_uses_no_collect_transaction_when_guarded() -> None:
    server = ReverseProxyServer(
        config=ProxyConfig.from_args(
            read_ahead_enabled=True,
            read_ahead_transaction_enabled=True,
            read_ahead_window_ms=180,
            read_ahead_max_reads=2,
            read_ahead_read_timeout_ms=5,
            read_ahead_max_messages=8,
        )
    )
    server._vci_write_collect_supported = True
    server._read_ahead_transaction_guard_until_mono = time.monotonic() + 5.0
    server._read_ahead_transaction_guard_reason = "slow_tunnel_response"
    write_body = ProtocolEncoder.encode_write_msgs_req(
        44,
        [{"protocol_id": 6, "data": b"\x22"}],
        timeout=25,
    )[HEADER_SIZE:]

    encoded, fwd_type, fwd_body, reason = server._build_tunnel_request(
        MsgType.WRITE_MSGS_REQ,
        write_body,
        sequence=77,
    )
    _magic, _length, msg_type, sequence = Message.decode_header(encoded[:HEADER_SIZE])
    request = ProtocolDecoder.decode_write_and_collect_reads_req(fwd_body)

    assert msg_type == MsgType.WRITE_AND_COLLECT_READS_REQ
    assert fwd_type == MsgType.WRITE_AND_COLLECT_READS_REQ
    assert sequence == 77
    assert reason == "write_collect_guarded_no_collect"
    assert request.collect_window_ms == 0
    assert request.max_reads == 0
    assert request.read_timeout_ms == 0
    assert request.max_messages == 0
    assert request.write_req_body == write_body


def test_read_ahead_transaction_guard_arms_after_slow_tunnel_response(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    server = ReverseProxyServer(
        config=ProxyConfig.from_args(
            read_ahead_enabled=True,
            read_ahead_transaction_enabled=True,
            read_ahead_transaction_max_network_ms=500,
            read_ahead_transaction_cooldown_ms=2000,
        )
    )
    events: list[dict[str, object]] = []

    def _capture(event_type: str, **fields: object) -> None:
        events.append({"event_type": event_type, **fields})

    server._emit_proxy_request_event = _capture  # type: ignore[method-assign]

    server._arm_read_ahead_transaction_guard(
        network_ms=750.0,
        duration_ms=760.0,
        dll_seq=11,
        proxy_seq=22,
        msg_name="WRITE_MSGS_REQ",
        request_fields={"channel_id": 1},
    )

    assert server._read_ahead_transaction_guard_active()
    assert events == [
        {
            "event_type": "read_ahead.transaction.guard_armed",
            "dll_seq": 11,
            "proxy_seq": 22,
            "msg_name": "WRITE_MSGS_REQ",
            "reason": "slow_tunnel_response",
            "duration_ms": 760.0,
            "network_ms": 750.0,
            "read_ahead_transaction_guard_trigger_network_ms": 750.0,
            "channel_id": 1,
            "read_ahead_transaction_guard_active": True,
            "read_ahead_transaction_max_network_ms": 500,
            "read_ahead_transaction_cooldown_ms": 2000,
            "read_ahead_transaction_guard_reason": "slow_tunnel_response",
            "read_ahead_transaction_guard_network_ms": 750.0,
            "read_ahead_transaction_guard_remaining_ms": pytest.approx(
                2000.0,
                abs=20.0,
            ),
        }
    ]


def test_handle_proxy_connection_uses_no_collect_transaction_after_slow_response(
    monkeypatch,
    tmp_path,
) -> None:
    async def _run() -> None:
        monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
        server = ReverseProxyServer(
            config=ProxyConfig.from_args(
                read_ahead_enabled=True,
                read_ahead_transaction_enabled=True,
                read_ahead_window_ms=180,
                read_ahead_max_reads=2,
                read_ahead_read_timeout_ms=5,
                read_ahead_max_messages=8,
                read_ahead_transaction_max_network_ms=1,
                read_ahead_transaction_cooldown_ms=10000,
            )
        )
        server.vci_connected.set()
        server.vci_writer = _FakeWriter(peername=("10.0.0.9", 9000))
        server._connection_epoch = "epoch-guard"
        server._vci_write_collect_supported = True

        new_sequences = iter([201, 202])
        server._next_sequence = lambda: next(new_sequences)

        events: list[dict[str, object]] = []

        def _capture(event_type: str, **fields: object) -> None:
            events.append({"event_type": event_type, **fields})

        server._emit_proxy_request_event = _capture  # type: ignore[method-assign]

        first_write = ProtocolEncoder.encode_write_msgs_req(
            44,
            [{"protocol_id": 6, "timestamp": 1, "data": b"\x22\xf4\x0c"}],
            timeout=25,
            sequence=31,
        )
        second_write = ProtocolEncoder.encode_write_msgs_req(
            44,
            [{"protocol_id": 6, "timestamp": 2, "data": b"\x22\x13\x08"}],
            timeout=25,
            sequence=32,
        )
        proxy_reader = _FakeReader(first_write, second_write)
        proxy_writer = _FakeWriter(peername=("127.0.0.1", 50003))

        responses = iter(
            [
                (
                    MsgType.WRITE_MSGS_RSP,
                    ProtocolEncoder.encode_write_msgs_rsp(0, 1)[HEADER_SIZE:],
                    0.0,
                    0.005,
                ),
                (
                    MsgType.WRITE_MSGS_RSP,
                    ProtocolEncoder.encode_write_msgs_rsp(0, 1)[HEADER_SIZE:],
                    0.0,
                    0.0,
                ),
            ]
        )

        async def _wait_for(awaitable, timeout):
            if isinstance(awaitable, asyncio.Future):
                resp_type, resp_body, hw_ms, delay_s = next(responses)
                if delay_s:
                    await asyncio.sleep(delay_s)
                return resp_type, resp_body, hw_ms
            return await awaitable

        monkeypatch.setattr("vci_proxy.reverse_server.asyncio.wait_for", _wait_for)

        await server._handle_proxy_connection(proxy_reader, proxy_writer)

        forwarded_events = [
            event
            for event in events
            if event["event_type"] == "proxy.request.forwarded_to_tunnel"
        ]
        assert [event["reason"] for event in forwarded_events] == [
            "write_collect_transaction",
            "write_collect_guarded_no_collect",
        ]
        assert any(
            event["event_type"] == "read_ahead.transaction.guard_armed"
            for event in events
        )

        first_forwarded = server.vci_writer.writes[0]
        second_forwarded = server.vci_writer.writes[1]
        assert Message.decode_header(first_forwarded[:HEADER_SIZE])[2] == (
            MsgType.WRITE_AND_COLLECT_READS_REQ
        )
        assert Message.decode_header(second_forwarded[:HEADER_SIZE])[2] == (
            MsgType.WRITE_AND_COLLECT_READS_REQ
        )

        first_request = ProtocolDecoder.decode_write_and_collect_reads_req(
            first_forwarded[HEADER_SIZE:]
        )
        second_request = ProtocolDecoder.decode_write_and_collect_reads_req(
            second_forwarded[HEADER_SIZE:]
        )
        assert first_request.collect_window_ms == 180
        assert first_request.max_reads == 2
        assert first_request.read_timeout_ms == 5
        assert first_request.max_messages == 8
        assert second_request.collect_window_ms == 0
        assert second_request.max_reads == 0
        assert second_request.read_timeout_ms == 0
        assert second_request.max_messages == 0

    asyncio.run(_run())


def test_build_tunnel_request_falls_back_without_client_write_collect_support() -> None:
    server = ReverseProxyServer(
        config=ProxyConfig.from_args(
            read_ahead_enabled=True,
            read_ahead_transaction_enabled=True,
        )
    )
    write_body = ProtocolEncoder.encode_write_msgs_req(
        44,
        [{"protocol_id": 6, "data": b"\x22"}],
        timeout=25,
    )[HEADER_SIZE:]

    encoded, fwd_type, fwd_body, reason = server._build_tunnel_request(
        MsgType.WRITE_MSGS_REQ,
        write_body,
        sequence=77,
    )
    _magic, length, msg_type, sequence = Message.decode_header(encoded[:HEADER_SIZE])

    assert length == len(encoded)
    assert msg_type == MsgType.WRITE_MSGS_REQ
    assert fwd_type == MsgType.WRITE_MSGS_REQ
    assert sequence == 77
    assert fwd_body == write_body
    assert reason is None


def test_handle_proxy_connection_observe_only_learns_without_sweep_protocol(monkeypatch, tmp_path) -> None:
    async def _run() -> None:
        monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
        server = ReverseProxyServer(
            config=ProxyConfig.from_args(
                local_sweep_enabled=True,
                local_sweep_mode="observe_only",
                local_sweep_min_cycles=2,
            )
        )
        server.vci_connected.set()
        server.vci_writer = _FakeWriter(peername=("10.0.0.9", 9000))
        server._connection_epoch = "epoch-observe"
        new_sequences = iter([101, 102, 103, 104])
        server._next_sequence = lambda: next(new_sequences)

        write_req = ProtocolEncoder.encode_write_msgs_req(
            44,
            [{"protocol_id": 6, "rx_status": 0, "tx_flags": 0, "timestamp": 1, "data": b"\x22\xf4\x0c"}],
            timeout=25,
            sequence=31,
        )
        read_req = ProtocolEncoder.encode_read_msgs_req(44, num_msgs=1, timeout=0, sequence=32)
        proxy_reader = _FakeReader(write_req, read_req, write_req, read_req)
        proxy_writer = _FakeWriter(peername=("127.0.0.1", 50003))
        read_rsp_body = ProtocolEncoder.encode_read_msgs_rsp(
            0,
            [{"protocol_id": 6, "rx_status": 0, "tx_flags": 0, "timestamp": 2, "data": b"\x62\xf4\x0c\x12\x34"}],
        )[HEADER_SIZE:]
        responses = iter(
            [
                (MsgType.WRITE_MSGS_RSP, ProtocolEncoder.encode_write_msgs_rsp(0, 1)[HEADER_SIZE:], 1.0),
                (MsgType.READ_MSGS_RSP, read_rsp_body, 1.0),
                (MsgType.WRITE_MSGS_RSP, ProtocolEncoder.encode_write_msgs_rsp(0, 1)[HEADER_SIZE:], 1.0),
                (MsgType.READ_MSGS_RSP, read_rsp_body, 1.0),
            ]
        )

        async def _success_wait_for(awaitable, timeout):
            if isinstance(awaitable, asyncio.Future):
                return next(responses)
            return await awaitable

        monkeypatch.setattr("vci_proxy.reverse_server.asyncio.wait_for", _success_wait_for)

        await server._handle_proxy_connection(proxy_reader, proxy_writer)

        forwarded_types = [
            Message.decode_header(write[:HEADER_SIZE])[2]
            for write in server.vci_writer.writes
        ]
        dll_response_types = [
            Message.decode_header(write[:HEADER_SIZE])[2]
            for write in proxy_writer.writes
        ]
        assert forwarded_types == [
            MsgType.WRITE_MSGS_REQ,
            MsgType.READ_MSGS_REQ,
            MsgType.WRITE_MSGS_REQ,
            MsgType.READ_MSGS_REQ,
        ]
        assert all(
            msg_type
            not in {
                MsgType.SWEEP_PLAN_START_REQ,
                MsgType.SWEEP_PLAN_STOP_REQ,
                MsgType.SWEEP_STATUS_REQ,
                MsgType.SWEEP_DRAIN_RESULTS_REQ,
                MsgType.SWEEP_PLAN_START_RSP,
                MsgType.SWEEP_PLAN_STOP_RSP,
                MsgType.SWEEP_STATUS_RSP,
                MsgType.SWEEP_DRAIN_RESULTS_RSP,
            }
            for msg_type in forwarded_types
        )
        assert dll_response_types == [
            MsgType.WRITE_MSGS_RSP,
            MsgType.READ_MSGS_RSP,
            MsgType.WRITE_MSGS_RSP,
            MsgType.READ_MSGS_RSP,
        ]

    asyncio.run(_run())

    records = _read_product_log_events(tmp_path)
    event_types = [record["event_type"] for record in records]
    assert "sweep.pattern.observed" in event_types
    assert "sweep.pattern.learned" in event_types
    assert "sweep.plan.started" not in event_types


def test_shadow_plan_start_waits_for_delay_window_to_include_more_learned_items(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))

    async def _run() -> None:
        server = ReverseProxyServer(
            config=ProxyConfig.from_args(
                local_sweep_enabled=True,
                local_sweep_mode="shadow_local",
                local_sweep_min_cycles=1,
                local_sweep_plan_delay_ms=40,
            )
        )
        server._connection_epoch = "epoch-delay"
        server._vci_sweep_shadow_supported = True
        sent_plans: list[SweepPlanStartRequest] = []

        async def _record_plan_start(plan: SweepPlanStartRequest) -> None:
            sent_plans.append(plan)

        server._send_sweep_plan_start = _record_plan_start

        def _observe_pair(payload: bytes, response: bytes, dll_seq: int) -> None:
            write_body = ProtocolEncoder.encode_write_msgs_req(
                44,
                [{"protocol_id": 6, "rx_status": 0, "tx_flags": 0, "timestamp": 1, "data": payload}],
                timeout=25,
            )[HEADER_SIZE:]
            read_body = ProtocolEncoder.encode_read_msgs_req(
                44,
                num_msgs=1,
                timeout=0,
            )[HEADER_SIZE:]
            read_rsp_body = ProtocolEncoder.encode_read_msgs_rsp(
                0,
                [{"protocol_id": 6, "rx_status": 0, "tx_flags": 0, "timestamp": 2, "data": response}],
            )[HEADER_SIZE:]
            server._observe_sweep_write(
                MsgType.WRITE_MSGS_REQ,
                write_body,
                dll_seq=dll_seq,
                msg_name="WRITE_MSGS_REQ",
            )
            server._observe_sweep_read_response(
                MsgType.READ_MSGS_REQ,
                read_body,
                MsgType.READ_MSGS_RSP,
                read_rsp_body,
                dll_seq=dll_seq + 1,
                msg_name="READ_MSGS_REQ",
            )

        _observe_pair(b"\x22\xf4\x0c", b"\x62\xf4\x0c\x12\x34", 31)
        assert server._sweep_active_plan is None
        assert server._sweep_plan_start_pending() is True

        _observe_pair(b"\x22\x13\x08", b"\x62\x13\x08\x56\x78", 33)
        await asyncio.sleep(0.08)

        assert sent_plans
        assert server._sweep_active_plan is sent_plans[0]
        assert len(sent_plans[0].requests) == 2

    asyncio.run(_run())


def test_shadow_plan_skips_gm_a9_by_default_but_keeps_observing(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    server = ReverseProxyServer(
        config=ProxyConfig.from_args(
            local_sweep_enabled=True,
            local_sweep_mode="shadow_local",
            local_sweep_min_cycles=1,
            local_sweep_plan_delay_ms=0,
        )
    )
    server._connection_epoch = "epoch-gm-a9"
    server._vci_sweep_shadow_supported = True

    write_body = ProtocolEncoder.encode_write_msgs_req(
        44,
        [
            {
                "protocol_id": 6,
                "rx_status": 0,
                "tx_flags": 0,
                "timestamp": 1,
                "data": b"\x00\x00\x07\xe0\xa9\x81\x1a",
            }
        ],
        timeout=25,
    )[HEADER_SIZE:]
    read_body = ProtocolEncoder.encode_read_msgs_req(44, num_msgs=1, timeout=0)[
        HEADER_SIZE:
    ]
    read_rsp_body = ProtocolEncoder.encode_read_msgs_rsp(
        0,
        [
            {
                "protocol_id": 6,
                "rx_status": 0,
                "tx_flags": 0,
                "timestamp": 2,
                "data": b"\x00\x00\x05\xe8\xa9\x81\x1a\x00",
            }
        ],
    )[HEADER_SIZE:]

    server._observe_sweep_write(
        MsgType.WRITE_MSGS_REQ,
        write_body,
        dll_seq=51,
        msg_name="WRITE_MSGS_REQ",
    )
    server._observe_sweep_read_response(
        MsgType.READ_MSGS_REQ,
        read_body,
        MsgType.READ_MSGS_RSP,
        read_rsp_body,
        dll_seq=52,
        msg_name="READ_MSGS_REQ",
    )

    assert server._sweep_active_plan is None

    records = _read_product_log_events(tmp_path)
    event_types = [record["event_type"] for record in records]
    assert "sweep.pattern.learned" in event_types
    assert "sweep.plan.started" not in event_types
    skipped = [record for record in records if record["event_type"] == "sweep.plan.skipped"]
    assert skipped
    assert skipped[-1]["reason"] == "gm_a9_packet_shadow_disabled"
    assert skipped[-1]["sweep_identifier_kind"] == "gm_a9_packet"
    assert skipped[-1]["sweep_shadow_allow_gm_a9_packet"] is False


def test_active_replay_mode_allows_gm_a9_shadow_plan_start(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))

    async def _run() -> None:
        server = ReverseProxyServer(
            config=ProxyConfig.from_args(
                local_sweep_enabled=True,
                local_sweep_mode="active_replay",
                local_sweep_min_cycles=1,
                local_sweep_plan_delay_ms=0,
            )
        )
        server._connection_epoch = "epoch-gm-a9-replay"
        server._vci_sweep_shadow_supported = True
        sent_plans: list[SweepPlanStartRequest] = []

        async def _record_plan_start(plan: SweepPlanStartRequest) -> None:
            sent_plans.append(plan)

        server._send_sweep_plan_start = _record_plan_start

        write_body = ProtocolEncoder.encode_write_msgs_req(
            44,
            [
                {
                    "protocol_id": 6,
                    "rx_status": 0,
                    "tx_flags": 0,
                    "timestamp": 1,
                    "data": b"\x00\x00\x07\xe0\xa9\x81\x1a",
                }
            ],
            timeout=25,
        )[HEADER_SIZE:]
        read_body = ProtocolEncoder.encode_read_msgs_req(44, num_msgs=1, timeout=0)[
            HEADER_SIZE:
        ]
        read_rsp_body = ProtocolEncoder.encode_read_msgs_rsp(
            0,
            [
                {
                    "protocol_id": 6,
                    "rx_status": 0,
                    "tx_flags": 0,
                    "timestamp": 2,
                    "data": b"\x00\x00\x05\xe8\xa9\x81\x1a\x00",
                }
            ],
        )[HEADER_SIZE:]

        server._observe_sweep_write(
            MsgType.WRITE_MSGS_REQ,
            write_body,
            dll_seq=61,
            msg_name="WRITE_MSGS_REQ",
        )
        server._observe_sweep_read_response(
            MsgType.READ_MSGS_REQ,
            read_body,
            MsgType.READ_MSGS_RSP,
            read_rsp_body,
            dll_seq=62,
            msg_name="READ_MSGS_REQ",
        )
        await asyncio.sleep(0)

        assert sent_plans
        assert server._sweep_active_plan is sent_plans[0]
        assert sent_plans[0].requests

    asyncio.run(_run())

    records = _read_product_log_events(tmp_path)
    event_types = [record["event_type"] for record in records]
    assert "sweep.plan.started" in event_types
    assert "sweep.plan.skipped" not in event_types


def test_shadow_missing_before_plan_is_logged_as_not_ready(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    server = ReverseProxyServer(
        config=ProxyConfig.from_args(
            local_sweep_enabled=True,
            local_sweep_mode="shadow_local",
            local_sweep_min_cycles=1,
        )
    )
    server._connection_epoch = "epoch-not-ready"

    write_body = ProtocolEncoder.encode_write_msgs_req(
        44,
        [{"protocol_id": 6, "rx_status": 0, "tx_flags": 0, "timestamp": 1, "data": b"\x22\xf4\x0c"}],
        timeout=25,
    )[HEADER_SIZE:]
    read_body = ProtocolEncoder.encode_read_msgs_req(44, num_msgs=1, timeout=0)[HEADER_SIZE:]
    read_rsp_body = ProtocolEncoder.encode_read_msgs_rsp(
        0,
        [{"protocol_id": 6, "rx_status": 0, "tx_flags": 0, "timestamp": 2, "data": b"\x62\xf4\x0c\x12\x34"}],
    )[HEADER_SIZE:]

    server._observe_sweep_write(
        MsgType.WRITE_MSGS_REQ,
        write_body,
        dll_seq=41,
        msg_name="WRITE_MSGS_REQ",
    )
    server._observe_sweep_read_response(
        MsgType.READ_MSGS_REQ,
        read_body,
        MsgType.READ_MSGS_RSP,
        read_rsp_body,
        dll_seq=42,
        msg_name="READ_MSGS_REQ",
    )

    records = _read_product_log_events(tmp_path)
    event_types = [record["event_type"] for record in records]
    assert "sweep.shadow.not_ready" in event_types
    assert "sweep.shadow.missing" not in event_types
    not_ready = [record for record in records if record["event_type"] == "sweep.shadow.not_ready"][-1]
    assert not_ready["sweep_shadow_not_ready_reason"] == "no_active_plan"
    assert not_ready["sweep_plan_active"] is False
    assert not_ready["sweep_store_pending_count"] == 0


def test_try_serve_cached_never_uses_shadow_store() -> None:
    server = ReverseProxyServer(
        config=ProxyConfig.from_args(
            local_sweep_enabled=True,
            local_sweep_mode="shadow_local",
        )
    )
    server._sweep_shadow_store.record_result(
        SweepResultRecord(
            plan_id="plan-1",
            signature_digest="sig",
            return_code=0,
            read_rsp_body=ProtocolEncoder.encode_read_msgs_rsp(
                0,
                [{"protocol_id": 6, "data": b"\x62\xf4\x0c"}],
            )[HEADER_SIZE:],
            started_at_s=1.0,
            finished_at_s=1.0,
        ),
        channel_id=44,
    )
    body = ProtocolEncoder.encode_read_msgs_req(44, 1, 0, sequence=7)[HEADER_SIZE:]

    cached, _ioctl_id, reason = server._try_serve_cached(
        MsgType.READ_MSGS_REQ,
        body,
        sequence=7,
    )

    assert cached is None
    assert reason is None


def test_handle_proxy_connection_active_replay_serves_write_read_from_shadow_store(
    monkeypatch,
    tmp_path,
) -> None:
    async def _run() -> None:
        monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
        server = ReverseProxyServer(
            config=ProxyConfig.from_args(
                local_sweep_enabled=True,
                local_sweep_mode="active_replay",
                local_sweep_min_cycles=1,
            )
        )
        server.vci_connected.set()
        server.vci_writer = _FakeWriter(peername=("10.0.0.9", 9000))
        server._connection_epoch = "epoch-replay"

        write_request = ProtocolEncoder.encode_write_msgs_req(
            44,
            [
                {
                    "protocol_id": 6,
                    "rx_status": 0,
                    "tx_flags": 0,
                    "timestamp": 1,
                    "data": b"\x00\x00\x07\xe0\xa9\x81\x1a",
                }
            ],
            timeout=25,
            sequence=31,
        )
        write_body = write_request[HEADER_SIZE:]
        read_request = ProtocolEncoder.encode_read_msgs_req(
            44,
            num_msgs=1,
            timeout=0,
            sequence=32,
        )
        read_body = read_request[HEADER_SIZE:]
        read_rsp_body = ProtocolEncoder.encode_read_msgs_rsp(
            0,
            [
                {
                    "protocol_id": 6,
                    "rx_status": 0,
                    "tx_flags": 0,
                    "timestamp": 2,
                    "data": b"\x00\x00\x05\xe8\xa9\x81\x1a\x00",
                }
            ],
        )[HEADER_SIZE:]

        server._observe_sweep_write(
            MsgType.WRITE_MSGS_REQ,
            write_body,
            dll_seq=10,
            msg_name="WRITE_MSGS_REQ",
        )
        server._observe_sweep_read_response(
            MsgType.READ_MSGS_REQ,
            read_body,
            MsgType.READ_MSGS_RSP,
            read_rsp_body,
            dll_seq=11,
            msg_name="READ_MSGS_REQ",
        )
        observed = server._sweep_learner.replay_candidate_for_write(
            write_body,
            connection_epoch="epoch-replay",
            read_num_msgs=1,
            read_timeout_ms=server.config.local_sweep.read_timeout_ms,
        )
        assert observed is not None
        server._sweep_shadow_store.record_result(
            SweepResultRecord(
                plan_id="plan-1",
                signature_digest=observed.signature.signature_digest,
                return_code=0,
                read_rsp_body=read_rsp_body,
                started_at_s=time.time(),
                finished_at_s=time.time(),
            ),
            channel_id=observed.signature.channel_id,
        )

        proxy_reader = _FakeReader(write_request, read_request)
        proxy_writer = _FakeWriter(peername=("127.0.0.1", 50003))

        await server._handle_proxy_connection(proxy_reader, proxy_writer)

        assert server.vci_writer.writes == []
        response_types = [
            Message.decode_header(chunk[:HEADER_SIZE])[2]
            for chunk in proxy_writer.writes
        ]
        assert response_types == [
            MsgType.WRITE_MSGS_RSP,
            MsgType.READ_MSGS_RSP,
        ]
        assert ProtocolDecoder.decode_write_msgs_rsp(
            proxy_writer.writes[0][HEADER_SIZE:]
        ) == (0, 1)
        assert ProtocolDecoder.decode_read_msgs_rsp(
            proxy_writer.writes[1][HEADER_SIZE:]
        ) == (0, [
            {
                "protocol_id": 6,
                "rx_status": 0,
                "tx_flags": 0,
                "timestamp": 2,
                "data": b"\x00\x00\x05\xe8\xa9\x81\x1a\x00",
            }
        ])

    asyncio.run(_run())

    records = _read_product_log_events(tmp_path)
    event_types = [record["event_type"] for record in records]
    assert "proxy.request.active_replay_armed" in event_types
    assert "proxy.request.active_replay_served" in event_types


def test_invalidate_caches_cancels_shadow_plan_and_clears_shadow_store() -> None:
    write_body = ProtocolEncoder.encode_write_msgs_req(
        44,
        [{"protocol_id": 6, "data": b"\x22\xf4\x0c"}],
        timeout=25,
    )[HEADER_SIZE:]
    server = ReverseProxyServer(
        config=ProxyConfig.from_args(
            local_sweep_enabled=True,
            local_sweep_mode="shadow_local",
        )
    )
    server._sweep_active_plan = SweepPlanStartRequest(
        plan_id="plan-1",
        connection_epoch="epoch-1",
        channel_id=44,
        max_result_age_ms=1000,
        min_item_interval_ms=5,
        shadow_max_seconds=120,
        requests=(SweepRequestSpec("sig", write_body),),
    )
    server._sweep_shadow_store.record_result(
        SweepResultRecord(
            plan_id="plan-1",
            signature_digest="sig",
            return_code=0,
            read_rsp_body=ProtocolEncoder.encode_read_msgs_rsp(0, [])[HEADER_SIZE:],
            started_at_s=1.0,
            finished_at_s=1.0,
        ),
        channel_id=44,
    )

    server._invalidate_caches(
        MsgType.DISCONNECT_REQ,
        ProtocolEncoder.encode_disconnect_req(44)[HEADER_SIZE:],
    )

    assert server._sweep_active_plan is None
    assert server._sweep_shadow_store.pending_count() == 0


def test_handle_proxy_connection_bypasses_read_cache_after_same_channel_write(monkeypatch, tmp_path) -> None:
    async def _run() -> None:
        monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
        server = ReverseProxyServer()
        server.vci_connected.set()
        server.vci_writer = _FakeWriter(peername=("10.0.0.9", 9000))
        server._connection_epoch = "epoch-post-write"
        server._read_cache.record_result(88, BUFFER_EMPTY)

        new_sequences = iter([101, 102])
        server._next_sequence = lambda: next(new_sequences)

        proxy_reader = _FakeReader(
            ProtocolEncoder.encode_write_msgs_req(
                88,
                [{"protocol_id": 6, "rx_status": 0, "tx_flags": 0, "timestamp": 1, "data": b"\x22"}],
                timeout=25,
                sequence=31,
            ),
            ProtocolEncoder.encode_read_msgs_req(88, num_msgs=1, timeout=0, sequence=32),
        )
        proxy_writer = _FakeWriter(peername=("127.0.0.1", 50003))

        responses = iter(
            [
                (MsgType.WRITE_MSGS_RSP, struct.pack(">II", 0, 1), 2.0),
                (MsgType.READ_MSGS_RSP, struct.pack(">II", BUFFER_EMPTY, 0), 3.0),
            ]
        )

        async def _success_wait_for(awaitable, timeout):
            if isinstance(awaitable, asyncio.Future):
                return next(responses)
            return await awaitable

        monkeypatch.setattr("vci_proxy.reverse_server.asyncio.wait_for", _success_wait_for)

        await server._handle_proxy_connection(proxy_reader, proxy_writer)

        assert len(server.vci_writer.writes) == 2
        _magic, _length, first_type, first_seq = Message.decode_header(server.vci_writer.writes[0][:HEADER_SIZE])
        _magic, _length, second_type, second_seq = Message.decode_header(server.vci_writer.writes[1][:HEADER_SIZE])
        assert (first_type, first_seq) == (MsgType.WRITE_MSGS_REQ, 101)
        assert (second_type, second_seq) == (MsgType.READ_MSGS_REQ, 102)

    asyncio.run(_run())

    records = _read_product_log_events(tmp_path)
    read_decisions = [
        record
        for record in records
        if record["event_type"] == "proxy.request.cache_decision"
        and record.get("msg_name") == "READ_MSGS_REQ"
    ]
    assert read_decisions
    assert read_decisions[-1]["cache_hit"] is False
    assert read_decisions[-1]["reason"] == "post_write_bypass"
    assert read_decisions[-1]["channel_id"] == 88
    assert read_decisions[-1]["last_write_seq"] == 31
    assert read_decisions[-1]["post_write_age_ms"] >= 0

    read_responses = [
        record
        for record in records
        if record["event_type"] == "proxy.request.response_received"
        and record.get("msg_name") == "READ_MSGS_REQ"
    ]
    assert read_responses[-1]["return_code"] == BUFFER_EMPTY
    assert read_responses[-1]["message_count"] == 0
    assert read_responses[-1]["read_result"] == "empty"


def test_handle_proxy_connection_emits_timeout_event(monkeypatch, tmp_path) -> None:
    async def _run() -> None:
        monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
        server = ReverseProxyServer()
        server.vci_connected.set()
        server.vci_writer = _FakeWriter(peername=("10.0.0.9", 9000))
        server._connection_epoch = "epoch-9"
        server._next_sequence = lambda: 88

        proxy_reader = _FakeReader(ProtocolEncoder.encode_disconnect_req(33, sequence=6))
        proxy_writer = _FakeWriter(peername=("127.0.0.1", 50001))

        async def _timeout_wait_for(awaitable, timeout):
            if isinstance(awaitable, asyncio.Future):
                raise asyncio.TimeoutError()
            return await awaitable

        monkeypatch.setattr("vci_proxy.reverse_server.asyncio.wait_for", _timeout_wait_for)

        await server._handle_proxy_connection(proxy_reader, proxy_writer)

    asyncio.run(_run())

    records = _read_product_log_events(tmp_path)
    timeouts = [record for record in records if record["event_type"] == "proxy.request.timeout"]

    assert timeouts
    assert timeouts[-1]["connection_epoch"] == "epoch-9"
    assert timeouts[-1]["dll_seq"] == 6
    assert timeouts[-1]["proxy_seq"] == 88
    assert timeouts[-1]["status"] == "error"
    assert timeouts[-1]["failure_code"] == "timeout"


def test_shutdown_servers_emits_process_lifecycle_events(monkeypatch) -> None:
    async def _run() -> None:
        server = ReverseProxyServer(config=ProxyConfig())
        emitted: list[str] = []

        class _ClosableServer:
            def close(self) -> None:
                return None

            async def wait_closed(self) -> None:
                return None

        server._vci_server = _ClosableServer()
        server._proxy_server = _ClosableServer()
        monkeypatch.setattr(
            server,
            "_emit_process_lifecycle_event",
            lambda event_type, **_kwargs: emitted.append(event_type),
        )

        await server._shutdown_servers()

        assert emitted == [
            "process.lifecycle.shutdown_started",
            "process.lifecycle.shutdown_finished",
        ]

    asyncio.run(_run())


def test_main_emits_process_failed_event_on_run_server_error(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    monkeypatch.setattr(reverse_server_module, "_disable_windows_quick_edit", lambda: None)
    monkeypatch.setattr(
        reverse_server_module,
        "_run_server_until_stopped",
        lambda _server: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr("builtins.print", lambda *args, **kwargs: None)
    monkeypatch.setattr("sys.argv", ["reverse_server.py"])

    with pytest.raises(RuntimeError, match="boom"):
        reverse_server_module.main()

    records = _read_product_log_events(tmp_path)
    failed = [
        record
        for record in records
        if record["event_type"] == "process.lifecycle.failed"
    ]
    assert failed
    assert failed[-1]["failure_code"] == "RuntimeError"
