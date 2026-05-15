from __future__ import annotations

import asyncio
import ssl
import types
import json
import threading
import time
from pathlib import Path

import pytest

from diagnostic_platform.observability import LogContext, flush_product_log_writers
from vci_proxy.cache_read_msgs import BUFFER_EMPTY
from vci_proxy.config import LOCAL_SWEEP_MIN_ITEM_INTERVAL_FLOOR_MS, ProxyConfig
from vci_proxy.protocol import (
    HEADER_SIZE,
    Message,
    MsgType,
    ProtocolDecoder,
    ProtocolEncoder,
    strip_read_msgs_prefetch_bundle,
)
from vci_proxy.reverse_client import ReverseProxyClient
from vci_proxy.sweep_executor import LocalSweepExecutor
from vci_proxy.sweep_protocol import (
    SweepPlanStartRequest,
    SweepRequestSpec,
    decode_sweep_drain_results_rsp,
    decode_sweep_plan_start_rsp,
    decode_sweep_status_rsp,
    encode_sweep_drain_results_req,
    encode_sweep_plan_start_req,
    encode_sweep_status_req,
)


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


def _read_local_events(tmp_path: Path) -> list[dict[str, object]]:
    flush_product_log_writers()
    raw_dir = tmp_path / "VCI_Proxy" / "observability" / "raw"
    rows: list[dict[str, object]] = []
    for path in sorted(raw_dir.glob("*.jsonl")):
        rows.extend(
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    return rows


def _read_rsp_data_payloads(read_rsp_bodies: tuple[bytes, ...]) -> list[bytes]:
    payloads: list[bytes] = []
    for read_rsp_body in read_rsp_bodies:
        return_code, messages = ProtocolDecoder.decode_read_msgs_rsp(read_rsp_body)
        if return_code == BUFFER_EMPTY and not messages:
            continue
        payloads.extend(message["data"] for message in messages)
    return payloads


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
    assert client._server_read_ahead_enabled is False


def test_send_registration_auth_mode_enables_read_ahead_from_server_capability(monkeypatch) -> None:
    config = ProxyConfig.from_args(auth_token="shared-secret", read_ahead_enabled=True)
    client = ReverseProxyClient("example.com", 9000, config=config)
    reader = _FakeReader(ProtocolEncoder.encode_auth_rsp(True, "ok;read_ahead=1", sequence=0))
    writer = _FakeWriter()

    monkeypatch.setattr("vci_proxy.reverse_client.time.time", lambda: 1_700_000_000)
    monkeypatch.setattr("vci_proxy.reverse_client.compute_signature", lambda token, timestamp: b"s" * 32)

    result = asyncio.run(client._send_registration(reader, writer))

    assert result is True
    assert client._server_read_ahead_enabled is True


def test_send_registration_auth_mode_records_connection_epoch(monkeypatch) -> None:
    config = ProxyConfig.from_args(auth_token="shared-secret", read_ahead_enabled=True)
    client = ReverseProxyClient("example.com", 9000, config=config)
    reader = _FakeReader(
        ProtocolEncoder.encode_auth_rsp(
            True,
            "ok;read_ahead=1;connection_epoch=epoch-rc-1",
            sequence=0,
        )
    )
    writer = _FakeWriter()

    monkeypatch.setattr("vci_proxy.reverse_client.time.time", lambda: 1_700_000_000)
    monkeypatch.setattr("vci_proxy.reverse_client.compute_signature", lambda token, timestamp: b"s" * 32)

    result = asyncio.run(client._send_registration(reader, writer))

    assert result is True
    assert client._server_connection_epoch == "epoch-rc-1"


def test_send_registration_auth_mode_advertises_read_and_write_collect_capabilities(monkeypatch) -> None:
    config = ProxyConfig.from_args(
        auth_token="shared-secret",
        read_ahead_enabled=True,
        read_ahead_transaction_enabled=True,
    )
    client = ReverseProxyClient("example.com", 9000, config=config)
    reader = _FakeReader(
        ProtocolEncoder.encode_auth_rsp(
            True,
            "ok;read_ahead=1;read_collect=1;write_collect=1",
            sequence=0,
        )
    )
    writer = _FakeWriter()

    monkeypatch.setattr("vci_proxy.reverse_client.time.time", lambda: 1_700_000_000)
    monkeypatch.setattr("vci_proxy.reverse_client.compute_signature", lambda token, timestamp: b"s" * 32)

    result = asyncio.run(client._send_registration(reader, writer))

    assert result is True
    assert client._server_read_ahead_enabled is True
    assert client._server_read_collect_enabled is True
    assert client._server_write_collect_enabled is True
    body = writer.writes[0][HEADER_SIZE:]
    assert ProtocolDecoder.decode_auth_req_capabilities(body) == (
        "read_ahead=1;read_collect=1;write_collect=1"
    )


def test_send_registration_auth_mode_advertises_sweep_shadow_capability(monkeypatch) -> None:
    config = ProxyConfig.from_args(
        auth_token="shared-secret",
        local_sweep_enabled=True,
        local_sweep_mode="shadow_local",
    )
    client = ReverseProxyClient("example.com", 9000, config=config)
    reader = _FakeReader(ProtocolEncoder.encode_auth_rsp(True, "ok;sweep_shadow=1", sequence=0))
    writer = _FakeWriter()

    monkeypatch.setattr("vci_proxy.reverse_client.time.time", lambda: 1_700_000_000)
    monkeypatch.setattr("vci_proxy.reverse_client.compute_signature", lambda token, timestamp: b"s" * 32)

    result = asyncio.run(client._send_registration(reader, writer))

    assert result is True
    assert client._server_sweep_shadow_enabled is True
    body = writer.writes[0][HEADER_SIZE:]
    assert "sweep_shadow=1" in ProtocolDecoder.decode_auth_req_capabilities(body)


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


def test_handle_write_msgs_preserves_response_when_read_ahead_disabled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    observed: list[str] = []
    client = ReverseProxyClient("example.com", 9000, config=ProxyConfig())
    client.driver = types.SimpleNamespace(
        write_msgs=lambda channel_id, messages, timeout: (
            observed.append("write_msgs") or (0, len(messages))
        ),
        read_msgs=lambda channel_id, num_msgs, timeout: (
            observed.append("read_msgs") or (0, [{"protocol_id": 6, "data": b"\x62"}])
        ),
    )
    body = ProtocolEncoder.encode_write_msgs_req(
        44,
        [{"protocol_id": 6, "data": b"\x22"}],
        timeout=25,
        sequence=7,
    )[HEADER_SIZE:]

    response = asyncio.run(client._handle_write_msgs(body, sequence=7))

    assert response == ProtocolEncoder.encode_write_msgs_rsp(0, 1, sequence=7)
    assert observed == ["write_msgs"]


def test_handle_write_msgs_requires_server_read_ahead_capability(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    observed: list[str] = []
    client = ReverseProxyClient(
        "example.com",
        9000,
        config=ProxyConfig.from_args(read_ahead_enabled=True),
    )
    client.driver = types.SimpleNamespace(
        write_msgs=lambda channel_id, messages, timeout: (
            observed.append("write_msgs") or (0, len(messages))
        ),
        read_msgs=lambda channel_id, num_msgs, timeout: (
            observed.append("read_msgs") or (0, [{"protocol_id": 6, "data": b"\x62"}])
        ),
    )
    body = ProtocolEncoder.encode_write_msgs_req(
        44,
        [{"protocol_id": 6, "data": b"\x22"}],
        timeout=25,
        sequence=7,
    )[HEADER_SIZE:]

    response = asyncio.run(client._handle_write_msgs(body, sequence=7))

    assert response == ProtocolEncoder.encode_write_msgs_rsp(0, 1, sequence=7)
    assert observed == ["write_msgs"]


def test_handle_write_msgs_read_ahead_window_zero_disables_collection(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    observed: list[str] = []
    client = ReverseProxyClient(
        "example.com",
        9000,
        config=ProxyConfig.from_args(
            read_ahead_enabled=True,
            read_ahead_window_ms=0,
        ),
    )
    client._server_read_ahead_enabled = True
    client.driver = types.SimpleNamespace(
        write_msgs=lambda channel_id, messages, timeout: (
            observed.append("write_msgs") or (0, len(messages))
        ),
        read_msgs=lambda channel_id, num_msgs, timeout: (
            observed.append("read_msgs") or (0, [{"protocol_id": 6, "data": b"\x62"}])
        ),
    )
    body = ProtocolEncoder.encode_write_msgs_req(
        44,
        [{"protocol_id": 6, "data": b"\x22"}],
        timeout=25,
        sequence=7,
    )[HEADER_SIZE:]

    response = asyncio.run(client._handle_write_msgs(body, sequence=7))

    assert response == ProtocolEncoder.encode_write_msgs_rsp(0, 1, sequence=7)
    assert observed == ["write_msgs"]


def test_handle_write_msgs_attaches_prefetch_bundle_when_read_ahead_enabled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    observed: list[tuple[str, int, int, int]] = []
    prefetched_message = {
        "protocol_id": 6,
        "rx_status": 0,
        "tx_flags": 0,
        "timestamp": 123,
        "data": b"\x62\xf4\x0c",
    }
    read_results = iter([(0, [prefetched_message]), (BUFFER_EMPTY, [])])
    client = ReverseProxyClient(
        "example.com",
        9000,
        config=ProxyConfig.from_args(
            read_ahead_enabled=True,
            read_ahead_max_reads=2,
            read_ahead_max_messages=2,
            read_ahead_read_timeout_ms=0,
        ),
    )
    client._server_read_ahead_enabled = True
    client.driver = types.SimpleNamespace(
        write_msgs=lambda channel_id, messages, timeout: (
            observed.append(("write_msgs", channel_id, len(messages), timeout))
            or (0, len(messages))
        ),
        read_msgs=lambda channel_id, num_msgs, timeout: (
            observed.append(("read_msgs", channel_id, num_msgs, timeout))
            or next(read_results)
        ),
    )
    body = ProtocolEncoder.encode_write_msgs_req(
        44,
        [{"protocol_id": 6, "data": b"\x22"}],
        timeout=25,
        sequence=7,
    )[HEADER_SIZE:]

    response = asyncio.run(client._handle_write_msgs(body, sequence=7))
    clean_body, bundle = strip_read_msgs_prefetch_bundle(response[HEADER_SIZE:])

    assert clean_body == ProtocolEncoder.encode_write_msgs_rsp(0, 1, sequence=7)[HEADER_SIZE:]
    assert bundle is not None
    assert bundle.channel_id == 44
    assert _read_rsp_data_payloads(bundle.read_rsp_bodies) == [b"\x62\xf4\x0c"]
    assert observed == [
        ("write_msgs", 44, 1, 25),
        ("read_msgs", 44, 2, 0),
        ("read_msgs", 44, 1, 0),
    ]


def test_handle_write_msgs_read_ahead_stops_on_consecutive_empty_reads(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    observed: list[tuple[str, int, int, int]] = []
    read_results = iter([(BUFFER_EMPTY, []), (BUFFER_EMPTY, []), (0, [{"protocol_id": 6, "data": b"\x62"}])])
    client = ReverseProxyClient(
        "example.com",
        9000,
        config=ProxyConfig.from_args(
            read_ahead_enabled=True,
            read_ahead_max_reads=5,
            read_ahead_max_messages=4,
            read_ahead_read_timeout_ms=0,
            read_ahead_max_consecutive_empty_reads=2,
        ),
    )
    client._server_read_ahead_enabled = True
    client.driver = types.SimpleNamespace(
        write_msgs=lambda channel_id, messages, timeout: (
            observed.append(("write_msgs", channel_id, len(messages), timeout))
            or (0, len(messages))
        ),
        read_msgs=lambda channel_id, num_msgs, timeout: (
            observed.append(("read_msgs", channel_id, num_msgs, timeout))
            or next(read_results)
        ),
    )
    body = ProtocolEncoder.encode_write_msgs_req(
        44,
        [{"protocol_id": 6, "data": b"\x22"}],
        timeout=25,
        sequence=7,
    )[HEADER_SIZE:]

    response = asyncio.run(client._handle_write_msgs(body, sequence=7))
    clean_body, bundle = strip_read_msgs_prefetch_bundle(response[HEADER_SIZE:])

    assert clean_body == ProtocolEncoder.encode_write_msgs_rsp(0, 1, sequence=7)[HEADER_SIZE:]
    assert bundle is None
    assert observed == [
        ("write_msgs", 44, 1, 25),
        ("read_msgs", 44, 4, 0),
        ("read_msgs", 44, 4, 0),
    ]


def test_handle_write_msgs_read_ahead_min_drain_keeps_trying_after_early_empty_reads(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    observed: list[tuple[str, int, int, int]] = []
    prefetched_message = {"protocol_id": 6, "data": b"\x62"}
    read_results = iter(
        [
            (BUFFER_EMPTY, []),
            (BUFFER_EMPTY, []),
            (0, [prefetched_message]),
        ]
    )
    client = ReverseProxyClient(
        "example.com",
        9000,
        config=ProxyConfig.from_args(
            read_ahead_enabled=True,
            read_ahead_max_reads=3,
            read_ahead_max_messages=4,
            read_ahead_read_timeout_ms=0,
            read_ahead_max_consecutive_empty_reads=2,
            read_ahead_min_drain_ms=50,
        ),
    )
    client._server_read_ahead_enabled = True
    client.driver = types.SimpleNamespace(
        write_msgs=lambda channel_id, messages, timeout: (
            observed.append(("write_msgs", channel_id, len(messages), timeout))
            or (0, len(messages))
        ),
        read_msgs=lambda channel_id, num_msgs, timeout: (
            observed.append(("read_msgs", channel_id, num_msgs, timeout))
            or next(read_results)
        ),
    )

    async def _fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("vci_proxy.reverse_client.asyncio.sleep", _fake_sleep)
    body = ProtocolEncoder.encode_write_msgs_req(
        44,
        [{"protocol_id": 6, "data": b"\x22"}],
        timeout=25,
        sequence=7,
    )[HEADER_SIZE:]

    response = asyncio.run(client._handle_write_msgs(body, sequence=7))
    clean_body, bundle = strip_read_msgs_prefetch_bundle(response[HEADER_SIZE:])

    assert clean_body == ProtocolEncoder.encode_write_msgs_rsp(0, 1, sequence=7)[HEADER_SIZE:]
    assert bundle is not None
    assert ProtocolDecoder.decode_read_msgs_rsp(bundle.read_rsp_bodies[0]) == (
        0,
        [
            {
                "protocol_id": 6,
                "rx_status": 0,
                "tx_flags": 0,
                "timestamp": 0,
                "data": b"\x62",
            }
        ],
    )
    assert observed == [
        ("write_msgs", 44, 1, 25),
        ("read_msgs", 44, 4, 0),
        ("read_msgs", 44, 4, 0),
        ("read_msgs", 44, 4, 0),
    ]


def test_handle_write_and_collect_reads_uses_transaction_limits(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    observed: list[tuple[str, int, int, int]] = []
    prefetched_message = {
        "protocol_id": 6,
        "rx_status": 0,
        "tx_flags": 0,
        "timestamp": 123,
        "data": b"\x62\xf4\x0c",
    }
    read_results = iter([(0, [prefetched_message]), (BUFFER_EMPTY, [])])
    client = ReverseProxyClient(
        "example.com",
        9000,
        config=ProxyConfig.from_args(
            read_ahead_enabled=True,
            read_ahead_transaction_enabled=True,
            read_ahead_window_ms=200,
            read_ahead_max_reads=3,
            read_ahead_max_messages=8,
            read_ahead_read_timeout_ms=10,
        ),
    )
    client._server_read_ahead_enabled = True
    client._server_write_collect_enabled = True
    client.driver = types.SimpleNamespace(
        write_msgs=lambda channel_id, messages, timeout: (
            observed.append(("write_msgs", channel_id, len(messages), timeout))
            or (0, len(messages))
        ),
        read_msgs=lambda channel_id, num_msgs, timeout: (
            observed.append(("read_msgs", channel_id, num_msgs, timeout))
            or next(read_results)
        ),
    )
    write_body = ProtocolEncoder.encode_write_msgs_req(
        44,
        [{"protocol_id": 6, "data": b"\x22"}],
        timeout=25,
        sequence=7,
    )[HEADER_SIZE:]
    transaction_body = ProtocolEncoder.encode_write_and_collect_reads_req(
        write_body,
        collect_window_ms=150,
        max_reads=2,
        read_timeout_ms=5,
        max_messages=4,
        sequence=7,
    )[HEADER_SIZE:]

    response = asyncio.run(
        client._handle_message(MsgType.WRITE_AND_COLLECT_READS_REQ, transaction_body, sequence=7)
    )
    clean_body, bundle = strip_read_msgs_prefetch_bundle(response[HEADER_SIZE:])

    assert clean_body == ProtocolEncoder.encode_write_msgs_rsp(0, 1, sequence=7)[HEADER_SIZE:]
    assert bundle is not None
    assert bundle.channel_id == 44
    assert ProtocolDecoder.decode_read_msgs_rsp(bundle.read_rsp_bodies[0]) == (
        0,
        [prefetched_message],
    )
    assert observed == [
        ("write_msgs", 44, 1, 25),
        ("read_msgs", 44, 4, 5),
        ("read_msgs", 44, 3, 5),
    ]


def test_handle_write_and_collect_reads_uses_write_collect_read_budget(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    observed: list[tuple[str, int, int, int]] = []
    read_results = iter(
        [
            (0, [{"protocol_id": 6, "data": bytes([index])}])
            for index in range(6)
        ]
    )
    client = ReverseProxyClient(
        "example.com",
        9000,
        config=ProxyConfig.from_args(
            read_ahead_enabled=True,
            read_ahead_transaction_enabled=True,
            read_ahead_max_reads=6,
            read_ahead_write_collect_max_reads=6,
            read_ahead_max_messages=8,
            read_ahead_read_timeout_ms=0,
        ),
    )
    client._server_read_ahead_enabled = True
    client._server_write_collect_enabled = True
    client.driver = types.SimpleNamespace(
        write_msgs=lambda channel_id, messages, timeout: (
            observed.append(("write_msgs", channel_id, len(messages), timeout))
            or (0, len(messages))
        ),
        read_msgs=lambda channel_id, num_msgs, timeout: (
            observed.append(("read_msgs", channel_id, num_msgs, timeout))
            or next(read_results)
        ),
    )
    write_body = ProtocolEncoder.encode_write_msgs_req(
        44,
        [{"protocol_id": 6, "data": b"\x22"}],
        timeout=25,
        sequence=7,
    )[HEADER_SIZE:]
    transaction_body = ProtocolEncoder.encode_write_and_collect_reads_req(
        write_body,
        collect_window_ms=150,
        max_reads=6,
        read_timeout_ms=0,
        max_messages=8,
        sequence=7,
    )[HEADER_SIZE:]

    response = asyncio.run(
        client._handle_message(MsgType.WRITE_AND_COLLECT_READS_REQ, transaction_body, sequence=7)
    )
    _clean_body, bundle = strip_read_msgs_prefetch_bundle(response[HEADER_SIZE:])

    assert bundle is not None
    assert len(bundle.read_rsp_bodies) == 6
    assert observed == [
        ("write_msgs", 44, 1, 25),
        ("read_msgs", 44, 8, 0),
        ("read_msgs", 44, 7, 0),
        ("read_msgs", 44, 6, 0),
        ("read_msgs", 44, 5, 0),
        ("read_msgs", 44, 4, 0),
        ("read_msgs", 44, 3, 0),
    ]


def test_handle_write_and_collect_reads_stops_at_soft_read_budget_after_data(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    observed: list[tuple[str, int, int, int]] = []
    read_results = iter(
        [
            (0, [{"protocol_id": 6, "data": bytes([index])}])
            for index in range(6)
        ]
    )
    client = ReverseProxyClient(
        "example.com",
        9000,
        config=ProxyConfig.from_args(
            read_ahead_enabled=True,
            read_ahead_transaction_enabled=True,
            read_ahead_max_reads=3,
            read_ahead_write_collect_max_reads=6,
            read_ahead_max_messages=8,
            read_ahead_read_timeout_ms=0,
        ),
    )
    client._server_read_ahead_enabled = True
    client._server_write_collect_enabled = True
    client.driver = types.SimpleNamespace(
        write_msgs=lambda channel_id, messages, timeout: (
            observed.append(("write_msgs", channel_id, len(messages), timeout))
            or (0, len(messages))
        ),
        read_msgs=lambda channel_id, num_msgs, timeout: (
            observed.append(("read_msgs", channel_id, num_msgs, timeout))
            or next(read_results)
        ),
    )
    write_body = ProtocolEncoder.encode_write_msgs_req(
        44,
        [{"protocol_id": 6, "data": b"\x22"}],
        timeout=25,
        sequence=7,
    )[HEADER_SIZE:]
    transaction_body = ProtocolEncoder.encode_write_and_collect_reads_req(
        write_body,
        collect_window_ms=150,
        max_reads=6,
        read_timeout_ms=0,
        max_messages=8,
        sequence=7,
    )[HEADER_SIZE:]

    response = asyncio.run(
        client._handle_message(MsgType.WRITE_AND_COLLECT_READS_REQ, transaction_body, sequence=7)
    )
    _clean_body, bundle = strip_read_msgs_prefetch_bundle(response[HEADER_SIZE:])

    assert bundle is not None
    assert len(bundle.read_rsp_bodies) == 3
    assert observed == [
        ("write_msgs", 44, 1, 25),
        ("read_msgs", 44, 8, 0),
        ("read_msgs", 44, 7, 0),
        ("read_msgs", 44, 6, 0),
    ]
    collection_events = [
        event
        for event in _read_local_events(tmp_path)
        if event.get("event_type") == "read_ahead.collection_finished"
    ]
    assert collection_events[-1]["reason"] == "soft_max_reads_after_data"
    assert collection_events[-1]["attempted_reads"] == 3
    assert collection_events[-1]["max_reads"] == 6
    assert collection_events[-1]["soft_max_reads_after_data"] == 3


def test_write_collect_soft_budget_stops_empty_drain_after_data(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    observed: list[tuple[str, int, int, int]] = []
    first_message = {"protocol_id": 6, "data": b"\x62\x01"}
    read_results = iter(
        [
            (0, [first_message]),
            (BUFFER_EMPTY, []),
            (BUFFER_EMPTY, []),
            (0, [{"protocol_id": 6, "data": b"\x62\x02"}]),
        ]
    )
    client = ReverseProxyClient(
        "example.com",
        9000,
        config=ProxyConfig.from_args(
            read_ahead_enabled=True,
            read_ahead_transaction_enabled=True,
            read_ahead_max_reads=3,
            read_ahead_write_collect_max_reads=6,
            read_ahead_max_messages=8,
            read_ahead_read_timeout_ms=0,
            read_ahead_min_drain_ms=40,
        ),
    )
    client._server_read_ahead_enabled = True
    client._server_write_collect_enabled = True
    client.driver = types.SimpleNamespace(
        write_msgs=lambda channel_id, messages, timeout: (
            observed.append(("write_msgs", channel_id, len(messages), timeout))
            or (0, len(messages))
        ),
        read_msgs=lambda channel_id, num_msgs, timeout: (
            observed.append(("read_msgs", channel_id, num_msgs, timeout))
            or next(read_results)
        ),
    )
    write_body = ProtocolEncoder.encode_write_msgs_req(
        44,
        [{"protocol_id": 6, "data": b"\x22"}],
        timeout=25,
        sequence=7,
    )[HEADER_SIZE:]
    transaction_body = ProtocolEncoder.encode_write_and_collect_reads_req(
        write_body,
        collect_window_ms=150,
        max_reads=6,
        read_timeout_ms=0,
        max_messages=8,
        sequence=7,
    )[HEADER_SIZE:]

    response = asyncio.run(
        client._handle_message(MsgType.WRITE_AND_COLLECT_READS_REQ, transaction_body, sequence=7)
    )
    _clean_body, bundle = strip_read_msgs_prefetch_bundle(response[HEADER_SIZE:])

    assert bundle is not None
    assert _read_rsp_data_payloads(bundle.read_rsp_bodies) == [b"\x62\x01"]
    assert observed == [
        ("write_msgs", 44, 1, 25),
        ("read_msgs", 44, 8, 0),
        ("read_msgs", 44, 7, 0),
        ("read_msgs", 44, 7, 0),
    ]
    collection_events = [
        event
        for event in _read_local_events(tmp_path)
        if event.get("event_type") == "read_ahead.collection_finished"
    ]
    assert collection_events[-1]["reason"] == "soft_max_reads_after_data"
    assert collection_events[-1]["attempted_reads"] == 3
    assert collection_events[-1]["empty_reads"] == 2
    assert collection_events[-1]["empty_after_data_grace_used"] is False


def test_handle_read_and_collect_reads_returns_foreground_read_and_prefetches_tail(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    observed: list[tuple[str, int, int, int]] = []
    foreground_message = {"protocol_id": 6, "data": b"\x62\xf4\x0c"}
    prefetched_message = {"protocol_id": 6, "data": b"\x62\x13\x08"}
    read_results = iter(
        [
            (0, [foreground_message]),
            (0, [prefetched_message]),
            (BUFFER_EMPTY, []),
            (BUFFER_EMPTY, []),
        ]
    )
    client = ReverseProxyClient(
        "example.com",
        9000,
        config=ProxyConfig.from_args(
            read_ahead_enabled=True,
            read_ahead_transaction_enabled=True,
            read_ahead_window_ms=200,
            read_ahead_max_reads=3,
            read_ahead_max_messages=8,
            read_ahead_read_timeout_ms=0,
            read_ahead_min_drain_ms=40,
        ),
    )
    client._server_read_ahead_enabled = True
    client._server_read_collect_enabled = True
    client.driver = types.SimpleNamespace(
        read_msgs=lambda channel_id, num_msgs, timeout: (
            observed.append(("read_msgs", channel_id, num_msgs, timeout))
            or next(read_results)
        ),
    )
    read_body = ProtocolEncoder.encode_read_msgs_req(
        44,
        num_msgs=300,
        timeout=0,
        sequence=7,
    )[HEADER_SIZE:]
    transaction_body = ProtocolEncoder.encode_read_and_collect_reads_req(
        read_body,
        collect_window_ms=150,
        max_reads=2,
        read_timeout_ms=0,
        max_messages=4,
        sequence=7,
    )[HEADER_SIZE:]

    response = asyncio.run(
        client._handle_message(MsgType.READ_AND_COLLECT_READS_REQ, transaction_body, sequence=7)
    )
    clean_body, bundle = strip_read_msgs_prefetch_bundle(response[HEADER_SIZE:])

    assert ProtocolDecoder.decode_read_msgs_rsp(clean_body) == (
        0,
        [
            {
                "protocol_id": 6,
                "rx_status": 0,
                "tx_flags": 0,
                "timestamp": 0,
                "data": b"\x62\xf4\x0c",
            }
        ],
    )
    assert bundle is not None
    assert bundle.channel_id == 44
    assert ProtocolDecoder.decode_read_msgs_rsp(bundle.read_rsp_bodies[0]) == (
        0,
        [
            {
                "protocol_id": 6,
                "rx_status": 0,
                "tx_flags": 0,
                "timestamp": 0,
                "data": b"\x62\x13\x08",
            }
        ],
    )
    assert observed == [
        ("read_msgs", 44, 300, 0),
        ("read_msgs", 44, 4, 0),
        ("read_msgs", 44, 3, 0),
        ("read_msgs", 44, 3, 0),
    ]


def test_handle_read_and_collect_reads_prefetches_tail_after_foreground_empty(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    observed: list[tuple[str, int, int, int]] = []
    prefetched_message = {"protocol_id": 6, "data": b"\x62\x13\x08"}
    read_results = iter(
        [
            (BUFFER_EMPTY, []),
            (0, [prefetched_message]),
            (BUFFER_EMPTY, []),
            (BUFFER_EMPTY, []),
        ]
    )
    client = ReverseProxyClient(
        "example.com",
        9000,
        config=ProxyConfig.from_args(
            read_ahead_enabled=True,
            read_ahead_transaction_enabled=True,
            read_ahead_window_ms=200,
            read_ahead_max_reads=3,
            read_ahead_max_messages=8,
            read_ahead_read_timeout_ms=0,
        ),
    )
    client._server_read_ahead_enabled = True
    client._server_read_collect_enabled = True
    client.driver = types.SimpleNamespace(
        read_msgs=lambda channel_id, num_msgs, timeout: (
            observed.append(("read_msgs", channel_id, num_msgs, timeout))
            or next(read_results)
        ),
    )
    read_body = ProtocolEncoder.encode_read_msgs_req(
        44,
        num_msgs=300,
        timeout=0,
        sequence=7,
    )[HEADER_SIZE:]
    transaction_body = ProtocolEncoder.encode_read_and_collect_reads_req(
        read_body,
        collect_window_ms=40,
        max_reads=2,
        read_timeout_ms=0,
        max_messages=4,
        sequence=7,
    )[HEADER_SIZE:]

    response = asyncio.run(
        client._handle_message(MsgType.READ_AND_COLLECT_READS_REQ, transaction_body, sequence=7)
    )
    clean_body, bundle = strip_read_msgs_prefetch_bundle(response[HEADER_SIZE:])

    assert ProtocolDecoder.decode_read_msgs_rsp(clean_body) == (BUFFER_EMPTY, [])
    assert bundle is not None
    assert bundle.channel_id == 44
    assert ProtocolDecoder.decode_read_msgs_rsp(bundle.read_rsp_bodies[0]) == (
        0,
        [
            {
                "protocol_id": 6,
                "rx_status": 0,
                "tx_flags": 0,
                "timestamp": 0,
                "data": b"\x62\x13\x08",
            }
        ],
    )
    assert observed == [
        ("read_msgs", 44, 300, 0),
        ("read_msgs", 44, 4, 0),
        ("read_msgs", 44, 3, 0),
        ("read_msgs", 44, 3, 0),
    ]


def test_handle_read_and_collect_reads_caps_configured_min_drain(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    client = ReverseProxyClient(
        "example.com",
        9000,
        config=ProxyConfig.from_args(
            read_ahead_enabled=True,
            read_ahead_transaction_enabled=True,
            read_ahead_window_ms=200,
            read_ahead_max_reads=3,
            read_ahead_max_messages=8,
            read_ahead_read_timeout_ms=0,
            read_ahead_min_drain_ms=40,
        ),
    )
    captured: dict[str, object] = {}
    client._server_read_ahead_enabled = True
    client._server_read_collect_enabled = True
    client.driver = types.SimpleNamespace(
        read_msgs=lambda _channel_id, _num_msgs, _timeout: (BUFFER_EMPTY, []),
    )

    async def _fake_collect_read_ahead_bodies(*_args, **kwargs) -> list[bytes]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(
        client,
        "_collect_read_ahead_bodies",
        _fake_collect_read_ahead_bodies,
    )
    read_body = ProtocolEncoder.encode_read_msgs_req(
        44,
        num_msgs=300,
        timeout=0,
        sequence=7,
    )[HEADER_SIZE:]
    transaction_body = ProtocolEncoder.encode_read_and_collect_reads_req(
        read_body,
        collect_window_ms=40,
        max_reads=3,
        read_timeout_ms=0,
        max_messages=4,
        sequence=7,
    )[HEADER_SIZE:]

    response = asyncio.run(
        client._handle_message(MsgType.READ_AND_COLLECT_READS_REQ, transaction_body, sequence=7)
    )
    clean_body, bundle = strip_read_msgs_prefetch_bundle(response[HEADER_SIZE:])

    assert ProtocolDecoder.decode_read_msgs_rsp(clean_body) == (BUFFER_EMPTY, [])
    assert bundle is None
    assert captured["min_drain_ms"] == 8
    assert captured["stop_after_empty_once_min_drain_elapsed"] is True
    assert captured["extra_read_after_data_at_max"] is True


def test_read_collect_min_drain_uses_zero_when_not_configured() -> None:
    client = ReverseProxyClient(
        "example.com",
        9000,
        config=ProxyConfig.from_args(
            read_ahead_enabled=True,
            read_ahead_transaction_enabled=True,
            read_ahead_min_drain_ms=0,
        ),
    )

    assert client._read_collect_min_drain_ms(40) == 0


def test_handle_read_and_collect_reads_stops_tail_after_first_empty(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    observed: list[tuple[str, int, int, int]] = []
    foreground_message = {"protocol_id": 6, "data": b"\x62\xf4\x0c"}
    late_tail_message = {"protocol_id": 6, "data": b"\x62\x13\x08"}
    read_results = iter(
        [
            (0, [foreground_message]),
            (BUFFER_EMPTY, []),
            (0, [late_tail_message]),
        ]
    )
    client = ReverseProxyClient(
        "example.com",
        9000,
        config=ProxyConfig.from_args(
            read_ahead_enabled=True,
            read_ahead_transaction_enabled=True,
            read_ahead_window_ms=200,
            read_ahead_max_reads=3,
            read_ahead_max_messages=8,
            read_ahead_read_timeout_ms=0,
        ),
    )
    client._server_read_ahead_enabled = True
    client._server_read_collect_enabled = True
    client.driver = types.SimpleNamespace(
        read_msgs=lambda channel_id, num_msgs, timeout: (
            observed.append(("read_msgs", channel_id, num_msgs, timeout))
            or next(read_results)
        ),
    )
    read_body = ProtocolEncoder.encode_read_msgs_req(
        44,
        num_msgs=300,
        timeout=0,
        sequence=7,
    )[HEADER_SIZE:]
    transaction_body = ProtocolEncoder.encode_read_and_collect_reads_req(
        read_body,
        collect_window_ms=40,
        max_reads=3,
        read_timeout_ms=0,
        max_messages=4,
        sequence=7,
    )[HEADER_SIZE:]

    response = asyncio.run(
        client._handle_message(MsgType.READ_AND_COLLECT_READS_REQ, transaction_body, sequence=7)
    )
    clean_body, bundle = strip_read_msgs_prefetch_bundle(response[HEADER_SIZE:])

    assert ProtocolDecoder.decode_read_msgs_rsp(clean_body)[0] == 0
    assert bundle is not None
    assert ProtocolDecoder.decode_read_msgs_rsp(bundle.read_rsp_bodies[0]) == (
        BUFFER_EMPTY,
        [],
    )
    assert observed == [
        ("read_msgs", 44, 300, 0),
        ("read_msgs", 44, 4, 0),
    ]


def test_handle_read_and_collect_reads_allows_one_empty_grace_after_tail_data(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    observed: list[tuple[str, int, int, int]] = []
    foreground_message = {"protocol_id": 6, "data": b"\x62\xf4\x0c"}
    first_tail_message = {"protocol_id": 6, "data": b"\x62\x13\x08"}
    second_tail_message = {"protocol_id": 6, "data": b"\x62\x13\x09"}
    read_results = iter(
        [
            (0, [foreground_message]),
            (0, [first_tail_message]),
            (BUFFER_EMPTY, []),
            (0, [second_tail_message]),
        ]
    )
    client = ReverseProxyClient(
        "example.com",
        9000,
        config=ProxyConfig.from_args(
            read_ahead_enabled=True,
            read_ahead_transaction_enabled=True,
            read_ahead_window_ms=200,
            read_ahead_max_reads=3,
            read_ahead_max_messages=8,
            read_ahead_read_timeout_ms=0,
        ),
    )
    client._server_read_ahead_enabled = True
    client._server_read_collect_enabled = True
    client.driver = types.SimpleNamespace(
        read_msgs=lambda channel_id, num_msgs, timeout: (
            observed.append(("read_msgs", channel_id, num_msgs, timeout))
            or next(read_results)
        ),
    )
    read_body = ProtocolEncoder.encode_read_msgs_req(
        44,
        num_msgs=300,
        timeout=0,
        sequence=7,
    )[HEADER_SIZE:]
    transaction_body = ProtocolEncoder.encode_read_and_collect_reads_req(
        read_body,
        collect_window_ms=40,
        max_reads=3,
        read_timeout_ms=0,
        max_messages=4,
        sequence=7,
    )[HEADER_SIZE:]

    response = asyncio.run(
        client._handle_message(MsgType.READ_AND_COLLECT_READS_REQ, transaction_body, sequence=7)
    )
    _clean_body, bundle = strip_read_msgs_prefetch_bundle(response[HEADER_SIZE:])

    assert bundle is not None
    assert [
        ProtocolDecoder.decode_read_msgs_rsp(read_rsp_body)[1][0]["data"]
        for read_rsp_body in bundle.read_rsp_bodies
    ] == [b"\x62\x13\x08", b"\x62\x13\x09"]
    assert observed == [
        ("read_msgs", 44, 300, 0),
        ("read_msgs", 44, 4, 0),
        ("read_msgs", 44, 3, 0),
        ("read_msgs", 44, 3, 0),
    ]


def test_handle_read_and_collect_reads_honors_deep_server_budget(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    observed: list[tuple[str, int, int, int]] = []
    foreground_message = {"protocol_id": 6, "data": b"\x62\xf4\x0c"}
    tail_messages = [
        {"protocol_id": 6, "data": bytes([0x62, 0x13, index])}
        for index in range(1, 7)
    ]
    read_results = iter(
        [(0, [foreground_message])]
        + [(0, [message]) for message in tail_messages]
        + [(BUFFER_EMPTY, [])]
    )
    client = ReverseProxyClient(
        "example.com",
        9000,
        config=ProxyConfig.from_args(
            read_ahead_enabled=True,
            read_ahead_transaction_enabled=True,
            read_ahead_window_ms=200,
            read_ahead_max_reads=3,
            read_ahead_write_collect_max_reads=6,
            read_ahead_max_messages=8,
            read_ahead_read_timeout_ms=0,
        ),
    )
    client._server_read_ahead_enabled = True
    client._server_read_collect_enabled = True
    client.driver = types.SimpleNamespace(
        read_msgs=lambda channel_id, num_msgs, timeout: (
            observed.append(("read_msgs", channel_id, num_msgs, timeout))
            or next(read_results)
        ),
    )
    read_body = ProtocolEncoder.encode_read_msgs_req(
        44,
        num_msgs=300,
        timeout=0,
        sequence=7,
    )[HEADER_SIZE:]
    transaction_body = ProtocolEncoder.encode_read_and_collect_reads_req(
        read_body,
        collect_window_ms=40,
        max_reads=6,
        read_timeout_ms=0,
        max_messages=8,
        sequence=7,
    )[HEADER_SIZE:]

    response = asyncio.run(
        client._handle_message(
            MsgType.READ_AND_COLLECT_READS_REQ,
            transaction_body,
            sequence=7,
        )
    )
    _clean_body, bundle = strip_read_msgs_prefetch_bundle(response[HEADER_SIZE:])

    assert bundle is not None
    assert _read_rsp_data_payloads(bundle.read_rsp_bodies) == [
        message["data"] for message in tail_messages
    ]
    assert observed == [
        ("read_msgs", 44, 300, 0),
        ("read_msgs", 44, 8, 0),
        ("read_msgs", 44, 7, 0),
        ("read_msgs", 44, 6, 0),
        ("read_msgs", 44, 5, 0),
        ("read_msgs", 44, 4, 0),
        ("read_msgs", 44, 3, 0),
        ("read_msgs", 44, 2, 0),
    ]
    collection_events = [
        event
        for event in _read_local_events(tmp_path)
        if event.get("event_type") == "read_ahead.collection_finished"
    ]
    assert collection_events[-1]["reason"] == "extra_read_after_data_at_max_empty"
    assert collection_events[-1]["attempted_reads"] == 7
    assert collection_events[-1]["max_reads"] == 6
    assert collection_events[-1]["local_max_reads"] == 6


def test_read_collect_empty_after_data_grace_waits_briefly_before_retry(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    first_tail_message = {"protocol_id": 6, "data": b"\x62\x13\x08"}
    second_tail_message = {"protocol_id": 6, "data": b"\x62\x13\x09"}
    read_results = iter(
        [
            (0, [first_tail_message]),
            (BUFFER_EMPTY, []),
            (0, [second_tail_message]),
        ]
    )
    sleeps: list[float] = []
    client = ReverseProxyClient(
        "example.com",
        9000,
        config=ProxyConfig.from_args(
            read_ahead_enabled=True,
            read_ahead_transaction_enabled=True,
            read_ahead_window_ms=200,
            read_ahead_max_reads=3,
            read_ahead_max_messages=8,
            read_ahead_read_timeout_ms=0,
            read_ahead_min_drain_ms=40,
        ),
    )
    client._server_read_ahead_enabled = True

    async def _fake_run_driver_call(*_args, **_kwargs):
        return next(read_results)

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(client, "_run_driver_call", _fake_run_driver_call)
    monkeypatch.setattr("vci_proxy.reverse_client.asyncio.sleep", _fake_sleep)

    bodies = asyncio.run(
        client._collect_read_ahead_bodies(
            44,
            LogContext(operation_kind="j2534:READ_AND_COLLECT_READS_REQ"),
            collect_window_ms=40,
            max_reads=3,
            read_timeout_ms=0,
            max_messages=4,
            min_drain_ms=8,
            stop_after_empty_once_min_drain_elapsed=True,
        )
    )

    assert sleeps
    assert sleeps[0] <= 0.008
    assert [
        ProtocolDecoder.decode_read_msgs_rsp(read_rsp_body)[1][0]["data"]
        for read_rsp_body in bodies
    ] == [b"\x62\x13\x08", b"\x62\x13\x09"]


def test_read_collect_empty_after_data_uses_one_extra_grace_read_after_budget(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    first_tail_message = {"protocol_id": 6, "data": b"\x62\x13\x08"}
    second_tail_message = {"protocol_id": 6, "data": b"\x62\x13\x09"}
    read_results = iter(
        [
            (0, [first_tail_message]),
            (BUFFER_EMPTY, []),
            (0, [second_tail_message]),
            (BUFFER_EMPTY, []),
        ]
    )
    client = ReverseProxyClient(
        "example.com",
        9000,
        config=ProxyConfig.from_args(
            read_ahead_enabled=True,
            read_ahead_transaction_enabled=True,
            read_ahead_window_ms=200,
            read_ahead_max_reads=3,
            read_ahead_max_messages=8,
            read_ahead_read_timeout_ms=0,
        ),
    )
    client._server_read_ahead_enabled = True

    async def _fake_run_driver_call(*_args, **_kwargs):
        return next(read_results)

    monkeypatch.setattr(client, "_run_driver_call", _fake_run_driver_call)

    bodies = asyncio.run(
        client._collect_read_ahead_bodies(
            44,
            LogContext(operation_kind="j2534:READ_AND_COLLECT_READS_REQ"),
            collect_window_ms=40,
            max_reads=2,
            read_timeout_ms=0,
            max_messages=4,
            min_drain_ms=0,
            stop_after_empty_once_min_drain_elapsed=True,
            extra_read_after_data_at_max=True,
        )
    )

    assert [
        ProtocolDecoder.decode_read_msgs_rsp(read_rsp_body)[1][0]["data"]
        for read_rsp_body in bodies
    ] == [b"\x62\x13\x08", b"\x62\x13\x09"]
    collection_events = [
        event
        for event in _read_local_events(tmp_path)
        if event.get("event_type") == "read_ahead.collection_finished"
    ]
    assert collection_events[-1]["reason"] == (
        "empty_after_data_grace_data_extra_empty"
    )
    assert collection_events[-1]["attempted_reads"] == 4
    assert collection_events[-1]["max_reads"] == 2
    assert collection_events[-1]["empty_after_data_grace_used"] is True
    assert collection_events[-1]["empty_after_data_grace_extra_read_used"] is True
    assert collection_events[-1]["empty_after_data_grace_skipped_reason"] is None
    assert (
        collection_events[-1]["empty_after_data_grace_data_extra_read_used"]
        is True
    )
    assert (
        collection_events[-1]["empty_after_data_grace_data_extra_read_attempts"]
        == 1
    )
    assert collection_events[-1]["empty_after_data_grace_data_extra_read_limit"] == 1
    assert collection_events[-1]["extra_read_after_data_at_max_used"] is False
    assert collection_events[-1]["extra_read_after_data_at_max_attempts"] == 0
    assert collection_events[-1]["extra_read_after_data_at_max_limit"] == 2


def test_read_collect_grace_data_extra_stops_after_one_data_probe(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    first_tail_message = {"protocol_id": 6, "data": b"\x62\x13\x08"}
    second_tail_message = {"protocol_id": 6, "data": b"\x62\x13\x09"}
    third_tail_message = {"protocol_id": 6, "data": b"\x62\x13\x0a"}
    read_results = iter(
        [
            (0, [first_tail_message]),
            (BUFFER_EMPTY, []),
            (0, [second_tail_message]),
            (0, [third_tail_message]),
        ]
    )
    client = ReverseProxyClient(
        "example.com",
        9000,
        config=ProxyConfig.from_args(
            read_ahead_enabled=True,
            read_ahead_transaction_enabled=True,
            read_ahead_window_ms=200,
            read_ahead_max_reads=3,
            read_ahead_max_messages=8,
            read_ahead_read_timeout_ms=0,
        ),
    )
    client._server_read_ahead_enabled = True

    async def _fake_run_driver_call(*_args, **_kwargs):
        return next(read_results)

    monkeypatch.setattr(client, "_run_driver_call", _fake_run_driver_call)

    bodies = asyncio.run(
        client._collect_read_ahead_bodies(
            44,
            LogContext(operation_kind="j2534:READ_AND_COLLECT_READS_REQ"),
            collect_window_ms=40,
            max_reads=2,
            read_timeout_ms=0,
            max_messages=8,
            min_drain_ms=0,
            stop_after_empty_once_min_drain_elapsed=True,
            extra_read_after_data_at_max=True,
        )
    )

    assert [
        ProtocolDecoder.decode_read_msgs_rsp(read_rsp_body)[1][0]["data"]
        for read_rsp_body in bodies
    ] == [b"\x62\x13\x08", b"\x62\x13\x09", b"\x62\x13\x0a"]
    collection_events = [
        event
        for event in _read_local_events(tmp_path)
        if event.get("event_type") == "read_ahead.collection_finished"
    ]
    assert collection_events[-1]["reason"] == (
        "empty_after_data_grace_data_extra_limit"
    )
    assert collection_events[-1]["attempted_reads"] == 4
    assert collection_events[-1]["max_reads"] == 2
    assert collection_events[-1]["empty_after_data_grace_used"] is True
    assert collection_events[-1]["empty_after_data_grace_extra_read_used"] is True
    assert (
        collection_events[-1]["empty_after_data_grace_data_extra_read_used"]
        is True
    )
    assert (
        collection_events[-1]["empty_after_data_grace_data_extra_read_attempts"]
        == 1
    )
    assert collection_events[-1]["empty_after_data_grace_data_extra_read_limit"] == 1
    assert collection_events[-1]["extra_read_after_data_at_max_used"] is False
    assert collection_events[-1]["extra_read_after_data_at_max_attempts"] == 0


def test_read_collect_data_at_max_uses_bounded_extra_reads(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    first_tail_message = {"protocol_id": 6, "data": b"\x62\x13\x08"}
    second_tail_message = {"protocol_id": 6, "data": b"\x62\x13\x09"}
    third_tail_message = {"protocol_id": 6, "data": b"\x62\x13\x0a"}
    fourth_tail_message = {"protocol_id": 6, "data": b"\x62\x13\x0b"}
    read_results = iter(
        [
            (0, [first_tail_message]),
            (0, [second_tail_message]),
            (0, [third_tail_message]),
            (0, [fourth_tail_message]),
        ]
    )
    client = ReverseProxyClient(
        "example.com",
        9000,
        config=ProxyConfig.from_args(
            read_ahead_enabled=True,
            read_ahead_transaction_enabled=True,
            read_ahead_window_ms=200,
            read_ahead_max_reads=3,
            read_ahead_max_messages=8,
            read_ahead_read_timeout_ms=0,
        ),
    )
    client._server_read_ahead_enabled = True

    async def _fake_run_driver_call(*_args, **_kwargs):
        return next(read_results)

    monkeypatch.setattr(client, "_run_driver_call", _fake_run_driver_call)

    bodies = asyncio.run(
        client._collect_read_ahead_bodies(
            44,
            LogContext(operation_kind="j2534:READ_AND_COLLECT_READS_REQ"),
            collect_window_ms=40,
            max_reads=2,
            read_timeout_ms=0,
            max_messages=8,
            min_drain_ms=0,
            stop_after_empty_once_min_drain_elapsed=True,
            extra_read_after_data_at_max=True,
        )
    )

    assert [
        ProtocolDecoder.decode_read_msgs_rsp(read_rsp_body)[1][0]["data"]
        for read_rsp_body in bodies
    ] == [
        b"\x62\x13\x08",
        b"\x62\x13\x09",
        b"\x62\x13\x0a",
        b"\x62\x13\x0b",
    ]
    collection_events = [
        event
        for event in _read_local_events(tmp_path)
        if event.get("event_type") == "read_ahead.collection_finished"
    ]
    assert collection_events[-1]["reason"] == "extra_read_after_data_at_max_limit"
    assert collection_events[-1]["attempted_reads"] == 4
    assert collection_events[-1]["max_reads"] == 2
    assert collection_events[-1]["extra_read_after_data_at_max_used"] is True
    assert collection_events[-1]["extra_read_after_data_at_max_attempts"] == 2
    assert collection_events[-1]["extra_read_after_data_at_max_limit"] == 2
    assert collection_events[-1]["empty_after_data_grace_used"] is False


def test_read_collect_data_at_max_extra_empty_stops_without_grace_stack(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    first_tail_message = {"protocol_id": 6, "data": b"\x62\x13\x08"}
    second_tail_message = {"protocol_id": 6, "data": b"\x62\x13\x09"}
    read_results = iter(
        [
            (0, [first_tail_message]),
            (0, [second_tail_message]),
            (BUFFER_EMPTY, []),
        ]
    )
    client = ReverseProxyClient(
        "example.com",
        9000,
        config=ProxyConfig.from_args(
            read_ahead_enabled=True,
            read_ahead_transaction_enabled=True,
            read_ahead_window_ms=200,
            read_ahead_max_reads=3,
            read_ahead_max_messages=8,
            read_ahead_read_timeout_ms=0,
        ),
    )
    client._server_read_ahead_enabled = True

    async def _fake_run_driver_call(*_args, **_kwargs):
        return next(read_results)

    monkeypatch.setattr(client, "_run_driver_call", _fake_run_driver_call)

    bodies = asyncio.run(
        client._collect_read_ahead_bodies(
            44,
            LogContext(operation_kind="j2534:READ_AND_COLLECT_READS_REQ"),
            collect_window_ms=40,
            max_reads=2,
            read_timeout_ms=0,
            max_messages=4,
            min_drain_ms=0,
            stop_after_empty_once_min_drain_elapsed=True,
            extra_read_after_data_at_max=True,
        )
    )

    assert [
        ProtocolDecoder.decode_read_msgs_rsp(read_rsp_body)[1][0]["data"]
        for read_rsp_body in bodies
    ] == [b"\x62\x13\x08", b"\x62\x13\x09"]
    collection_events = [
        event
        for event in _read_local_events(tmp_path)
        if event.get("event_type") == "read_ahead.collection_finished"
    ]
    assert collection_events[-1]["reason"] == "extra_read_after_data_at_max_empty"
    assert collection_events[-1]["attempted_reads"] == 3
    assert collection_events[-1]["max_reads"] == 2
    assert collection_events[-1]["extra_read_after_data_at_max_used"] is True
    assert collection_events[-1]["extra_read_after_data_at_max_attempts"] == 1
    assert collection_events[-1]["extra_read_after_data_at_max_limit"] == 2
    assert collection_events[-1]["empty_after_data_grace_used"] is False


def test_read_collect_can_attach_tail_empty_confirmation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    tail_message = {"protocol_id": 6, "data": b"\x62\x13\x08"}
    read_results = iter(
        [
            (0, [tail_message]),
            (BUFFER_EMPTY, []),
            (BUFFER_EMPTY, []),
        ]
    )
    client = ReverseProxyClient(
        "example.com",
        9000,
        config=ProxyConfig.from_args(
            read_ahead_enabled=True,
            read_ahead_transaction_enabled=True,
            read_ahead_window_ms=200,
            read_ahead_max_reads=3,
            read_ahead_max_messages=8,
            read_ahead_read_timeout_ms=0,
        ),
    )
    client._server_read_ahead_enabled = True

    async def _fake_run_driver_call(*_args, **_kwargs):
        return next(read_results)

    monkeypatch.setattr(client, "_run_driver_call", _fake_run_driver_call)

    bodies = asyncio.run(
        client._collect_read_ahead_bodies(
            44,
            LogContext(operation_kind="j2534:READ_AND_COLLECT_READS_REQ"),
            collect_window_ms=40,
            max_reads=2,
            read_timeout_ms=0,
            max_messages=4,
            min_drain_ms=0,
            stop_after_empty_once_min_drain_elapsed=True,
            include_empty_confirmations=True,
        )
    )

    assert [ProtocolDecoder.decode_read_msgs_rsp(body) for body in bodies] == [
        (
            0,
            [
                {
                    "protocol_id": 6,
                    "rx_status": 0,
                    "tx_flags": 0,
                    "timestamp": 0,
                    "data": b"\x62\x13\x08",
                }
            ],
        ),
        (BUFFER_EMPTY, []),
    ]


def test_read_collect_does_not_attach_immediate_empty_without_drain(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    client = ReverseProxyClient(
        "example.com",
        9000,
        config=ProxyConfig.from_args(
            read_ahead_enabled=True,
            read_ahead_transaction_enabled=True,
            read_ahead_window_ms=200,
            read_ahead_max_reads=3,
            read_ahead_max_messages=8,
            read_ahead_read_timeout_ms=0,
        ),
    )
    client._server_read_ahead_enabled = True

    async def _fake_run_driver_call(*_args, **_kwargs):
        return BUFFER_EMPTY, []

    monkeypatch.setattr(client, "_run_driver_call", _fake_run_driver_call)

    bodies = asyncio.run(
        client._collect_read_ahead_bodies(
            44,
            LogContext(operation_kind="j2534:READ_AND_COLLECT_READS_REQ"),
            collect_window_ms=40,
            max_reads=1,
            read_timeout_ms=0,
            max_messages=4,
            min_drain_ms=0,
            stop_after_empty_once_min_drain_elapsed=True,
            include_empty_confirmations=True,
        )
    )

    assert bodies == []


def test_handle_read_and_collect_reads_falls_back_without_negotiated_capability() -> None:
    observed: list[tuple[str, int, int, int]] = []
    foreground_message = {"protocol_id": 6, "data": b"\x62\xf4\x0c"}
    client = ReverseProxyClient(
        "example.com",
        9000,
        config=ProxyConfig.from_args(
            read_ahead_enabled=True,
            read_ahead_transaction_enabled=True,
        ),
    )
    client._server_read_ahead_enabled = True
    client._server_read_collect_enabled = False
    client.driver = types.SimpleNamespace(
        read_msgs=lambda channel_id, num_msgs, timeout: (
            observed.append(("read_msgs", channel_id, num_msgs, timeout))
            or (0, [foreground_message])
        ),
    )
    read_body = ProtocolEncoder.encode_read_msgs_req(44, 300, 0, sequence=7)[HEADER_SIZE:]
    transaction_body = ProtocolEncoder.encode_read_and_collect_reads_req(
        read_body,
        collect_window_ms=150,
        max_reads=2,
        read_timeout_ms=0,
        max_messages=4,
        sequence=7,
    )[HEADER_SIZE:]

    response = asyncio.run(
        client._handle_message(MsgType.READ_AND_COLLECT_READS_REQ, transaction_body, sequence=7)
    )
    clean_body, bundle = strip_read_msgs_prefetch_bundle(response[HEADER_SIZE:])

    assert ProtocolDecoder.decode_read_msgs_rsp(clean_body)[0] == 0
    assert bundle is None
    assert observed == [("read_msgs", 44, 300, 0)]


def test_handle_message_returns_ping_response() -> None:
    client = ReverseProxyClient("example.com", 9000)

    response = asyncio.run(client._handle_message(MsgType.PING_REQ, b"", sequence=5))

    magic, length, msg_type, sequence = Message.decode_header(response[:HEADER_SIZE])
    assert magic > 0
    assert length == HEADER_SIZE
    assert msg_type == MsgType.PING_RSP
    assert sequence == 5


def test_handle_sweep_status_and_drain_are_immediate_when_empty() -> None:
    client = ReverseProxyClient(
        "example.com",
        9000,
        config=ProxyConfig.from_args(
            local_sweep_enabled=True,
            local_sweep_mode="shadow_local",
        ),
    )
    client._server_sweep_shadow_enabled = True

    status = asyncio.run(
        client._handle_message(
            MsgType.SWEEP_STATUS_REQ,
            encode_sweep_status_req(sequence=7)[HEADER_SIZE:],
            sequence=7,
        )
    )
    drain = asyncio.run(
        client._handle_message(
            MsgType.SWEEP_DRAIN_RESULTS_REQ,
            encode_sweep_drain_results_req(sequence=8)[HEADER_SIZE:],
            sequence=8,
        )
    )

    _magic, _length, status_type, _sequence = Message.decode_header(status[:HEADER_SIZE])
    _magic, _length, drain_type, _sequence = Message.decode_header(drain[:HEADER_SIZE])
    assert status_type == MsgType.SWEEP_STATUS_RSP
    assert decode_sweep_status_rsp(status[HEADER_SIZE:]).state == "idle"
    assert drain_type == MsgType.SWEEP_DRAIN_RESULTS_RSP
    assert decode_sweep_drain_results_rsp(drain[HEADER_SIZE:]) == ()


def test_handle_sweep_plan_runs_shadow_executor_without_overlapping_foreground_calls() -> None:
    async def _run() -> None:
        active = 0
        max_active = 0
        guard = threading.Lock()
        observed: list[str] = []

        def _enter(name: str):
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
                observed.append(name)
            time.sleep(0.01)
            with guard:
                active -= 1

        client = ReverseProxyClient(
            "example.com",
            9000,
            config=ProxyConfig.from_args(
                local_sweep_enabled=True,
                local_sweep_mode="shadow_local",
                local_sweep_shadow_max_seconds=1,
                local_sweep_min_item_interval_ms=1,
            ),
        )
        client._server_sweep_shadow_enabled = True
        client.driver = types.SimpleNamespace(
            write_msgs=lambda channel_id, messages, timeout: (
                _enter("write_msgs") or (0, len(messages))
            ),
            read_msgs=lambda channel_id, num_msgs, timeout: (
                _enter("read_msgs")
                or (
                    0,
                    [
                        {
                            "protocol_id": 6,
                            "rx_status": 0,
                            "tx_flags": 0,
                            "timestamp": 1,
                            "data": b"\x62\xf4\x0c",
                        }
                    ],
                )
            ),
        )
        write_body = ProtocolEncoder.encode_write_msgs_req(
            44,
            [{"protocol_id": 6, "data": b"\x22\xf4\x0c"}],
            timeout=25,
        )[HEADER_SIZE:]
        plan = SweepPlanStartRequest(
            plan_id="plan-1",
            connection_epoch="epoch-1",
            channel_id=44,
            max_result_age_ms=1000,
            min_item_interval_ms=1,
            shadow_max_seconds=1,
            requests=(SweepRequestSpec("sig", write_body, 1, 0),),
        )

        start_response = await client._handle_message(
            MsgType.SWEEP_PLAN_START_REQ,
            encode_sweep_plan_start_req(plan, sequence=11)[HEADER_SIZE:],
            sequence=11,
        )
        await asyncio.sleep(0.005)
        foreground_response = await client._handle_message(
            MsgType.READ_MSGS_REQ,
            ProtocolEncoder.encode_read_msgs_req(44, 1, 0, sequence=12)[HEADER_SIZE:],
            sequence=12,
        )
        await asyncio.sleep(0.05)
        drain_response = await client._handle_message(
            MsgType.SWEEP_DRAIN_RESULTS_REQ,
            encode_sweep_drain_results_req(sequence=13)[HEADER_SIZE:],
            sequence=13,
        )
        client._sweep_executor.stop("test_finished")
        await asyncio.sleep(0)

        assert decode_sweep_plan_start_rsp(start_response[HEADER_SIZE:]).success is True
        assert Message.decode_header(foreground_response[:HEADER_SIZE])[2] == MsgType.READ_MSGS_RSP
        assert decode_sweep_drain_results_rsp(drain_response[HEADER_SIZE:])
        assert max_active == 1
        assert "write_msgs" in observed
        assert "read_msgs" in observed

    asyncio.run(_run())


def test_shadow_executor_enforces_local_interval_floor_for_old_plans() -> None:
    client = ReverseProxyClient(
        "example.com",
        9000,
        config=ProxyConfig.from_args(
            local_sweep_enabled=True,
            local_sweep_mode="shadow_local",
            local_sweep_min_item_interval_ms=1,
        ),
    )
    plan = SweepPlanStartRequest(
        plan_id="plan-1",
        connection_epoch="epoch-1",
        channel_id=44,
        max_result_age_ms=1000,
        min_item_interval_ms=1,
        shadow_max_seconds=1,
        requests=(SweepRequestSpec("sig", b"", 1, 0),),
    )

    assert client._sweep_executor._effective_min_item_interval_ms(plan) == (
        LOCAL_SWEEP_MIN_ITEM_INTERVAL_FLOOR_MS
    )


def test_foreground_invalidation_does_not_overlap_cancelled_shadow_driver_call() -> None:
    async def _run() -> None:
        active = 0
        max_active = 0
        guard = threading.Lock()
        shadow_entered = threading.Event()
        release_shadow = threading.Event()
        foreground_entered = threading.Event()

        def _enter(name: str) -> None:
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
            if name == "shadow_write":
                shadow_entered.set()
                release_shadow.wait(timeout=1.0)
            if name == "foreground_ioctl":
                foreground_entered.set()
            with guard:
                active -= 1

        client = ReverseProxyClient(
            "example.com",
            9000,
            config=ProxyConfig.from_args(
                local_sweep_enabled=True,
                local_sweep_mode="shadow_local",
                local_sweep_shadow_max_seconds=1,
                local_sweep_min_item_interval_ms=1,
            ),
        )
        client._server_sweep_shadow_enabled = True
        client.driver = types.SimpleNamespace(
            write_msgs=lambda channel_id, messages, timeout: (
                _enter("shadow_write") or (0, len(messages))
            ),
            read_msgs=lambda channel_id, num_msgs, timeout: (0, []),
            ioctl=lambda channel_id, ioctl_id, input_data: (
                _enter("foreground_ioctl") or (0, b"")
            ),
        )
        write_body = ProtocolEncoder.encode_write_msgs_req(
            44,
            [{"protocol_id": 6, "data": b"\x22\xf4\x0c"}],
            timeout=25,
        )[HEADER_SIZE:]
        plan = SweepPlanStartRequest(
            plan_id="plan-1",
            connection_epoch="epoch-1",
            channel_id=44,
            max_result_age_ms=1000,
            min_item_interval_ms=1,
            shadow_max_seconds=1,
            requests=(SweepRequestSpec("sig", write_body, 1, 0),),
        )

        start_response = await client._handle_message(
            MsgType.SWEEP_PLAN_START_REQ,
            encode_sweep_plan_start_req(plan, sequence=21)[HEADER_SIZE:],
            sequence=21,
        )
        assert decode_sweep_plan_start_rsp(start_response[HEADER_SIZE:]).success is True
        assert await asyncio.to_thread(shadow_entered.wait, 1.0)

        foreground_task = asyncio.create_task(
            client._handle_message(
                MsgType.IOCTL_REQ,
                ProtocolEncoder.encode_ioctl_req(44, 0x1234, b"", sequence=22)[HEADER_SIZE:],
                sequence=22,
            )
        )
        await asyncio.sleep(0)
        assert not await asyncio.to_thread(foreground_entered.wait, 0.03)

        release_shadow.set()
        foreground_response = await asyncio.wait_for(foreground_task, timeout=1.0)
        assert Message.decode_header(foreground_response[:HEADER_SIZE])[2] == MsgType.IOCTL_RSP
        assert foreground_entered.is_set()
        assert max_active == 1

    asyncio.run(_run())


def test_cacheable_foreground_ioctl_pauses_shadow_without_cancelling_plan() -> None:
    async def _run() -> None:
        active = 0
        max_active = 0
        guard = threading.Lock()
        shadow_write_entered = threading.Event()
        release_shadow_write = threading.Event()
        foreground_entered = threading.Event()
        shadow_read_entered = threading.Event()

        def _enter(name: str) -> None:
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
            if name == "shadow_write":
                shadow_write_entered.set()
                release_shadow_write.wait(timeout=1.0)
            if name == "foreground_ioctl":
                foreground_entered.set()
            if name == "shadow_read":
                shadow_read_entered.set()
            with guard:
                active -= 1

        client = ReverseProxyClient(
            "example.com",
            9000,
            config=ProxyConfig.from_args(
                local_sweep_enabled=True,
                local_sweep_mode="shadow_local",
                local_sweep_shadow_max_seconds=1,
                local_sweep_min_item_interval_ms=1,
            ),
        )
        client._server_sweep_shadow_enabled = True
        client.driver = types.SimpleNamespace(
            write_msgs=lambda channel_id, messages, timeout: (
                _enter("shadow_write") or (0, len(messages))
            ),
            read_msgs=lambda channel_id, num_msgs, timeout: (
                _enter("shadow_read") or (
                    0,
                    [{"protocol_id": 6, "data": b"\x62\xf4\x0c"}],
                )
            ),
            ioctl=lambda channel_id, ioctl_id, input_data: (
                _enter("foreground_ioctl") or (0, b"\x12\x34")
            ),
        )
        write_body = ProtocolEncoder.encode_write_msgs_req(
            44,
            [{"protocol_id": 6, "data": b"\x22\xf4\x0c"}],
            timeout=25,
        )[HEADER_SIZE:]
        plan = SweepPlanStartRequest(
            plan_id="plan-1",
            connection_epoch="epoch-1",
            channel_id=44,
            max_result_age_ms=1000,
            min_item_interval_ms=1,
            shadow_max_seconds=1,
            requests=(SweepRequestSpec("sig", write_body, 1, 0),),
        )

        start_response = await client._handle_message(
            MsgType.SWEEP_PLAN_START_REQ,
            encode_sweep_plan_start_req(plan, sequence=31)[HEADER_SIZE:],
            sequence=31,
        )
        assert decode_sweep_plan_start_rsp(start_response[HEADER_SIZE:]).success is True
        assert await asyncio.to_thread(shadow_write_entered.wait, 1.0)

        foreground_task = asyncio.create_task(
            client._handle_message(
                MsgType.IOCTL_REQ,
                ProtocolEncoder.encode_ioctl_req(44, 0x03, b"", sequence=32)[HEADER_SIZE:],
                sequence=32,
            )
        )
        await asyncio.sleep(0)
        assert not await asyncio.to_thread(foreground_entered.wait, 0.03)

        release_shadow_write.set()
        foreground_response = await asyncio.wait_for(foreground_task, timeout=1.0)
        assert Message.decode_header(foreground_response[:HEADER_SIZE])[2] == MsgType.IOCTL_RSP
        assert await asyncio.to_thread(shadow_read_entered.wait, 1.0)

        drain_response = await client._handle_message(
            MsgType.SWEEP_DRAIN_RESULTS_REQ,
            encode_sweep_drain_results_req(sequence=33)[HEADER_SIZE:],
            sequence=33,
        )
        client._sweep_executor.stop("test_finished")

        assert decode_sweep_drain_results_rsp(drain_response[HEADER_SIZE:])
        assert max_active == 1

    asyncio.run(_run())


def test_shadow_executor_tail_reads_after_echo_only_result() -> None:
    async def _run() -> None:
        read_calls: list[tuple[int, int]] = []
        emitted: list[tuple[str, dict[str, object]]] = []

        client = ReverseProxyClient(
            "example.com",
            9000,
            config=ProxyConfig.from_args(
                local_sweep_enabled=True,
                local_sweep_mode="shadow_local",
                local_sweep_shadow_max_seconds=1,
                local_sweep_min_item_interval_ms=1,
            ),
        )
        client._server_sweep_shadow_enabled = True
        client._sweep_executor._emit_event = (
            lambda event_type, **fields: emitted.append((event_type, fields))
        )

        def _read_msgs(channel_id: int, num_msgs: int, timeout: int):
            read_calls.append((num_msgs, timeout))
            if len(read_calls) == 1:
                return (
                    0,
                    [
                        {
                            "protocol_id": 6,
                            "rx_status": 9,
                            "tx_flags": 0,
                            "timestamp": 1,
                            "data": b"\x00\x00\x07\xe0",
                        }
                    ],
                )
            return (
                0,
                [
                    {
                        "protocol_id": 6,
                        "rx_status": 0,
                        "tx_flags": 0,
                        "timestamp": 2,
                        "data": b"\x00\x00\x07\xe8\x62\x00\x0c\x12\x34",
                    }
                ],
            )

        client.driver = types.SimpleNamespace(
            write_msgs=lambda channel_id, messages, timeout: (0, len(messages)),
            read_msgs=_read_msgs,
        )
        write_body = ProtocolEncoder.encode_write_msgs_req(
            44,
            [{"protocol_id": 6, "data": b"\x00\x00\x07\xe0\x22\x00\x0c"}],
            timeout=25,
        )[HEADER_SIZE:]
        plan = SweepPlanStartRequest(
            plan_id="plan-tail",
            connection_epoch="epoch-1",
            channel_id=44,
            max_result_age_ms=1000,
            min_item_interval_ms=1,
            shadow_max_seconds=1,
            requests=(SweepRequestSpec("sig", write_body, 300, 1),),
        )

        start_response = await client._handle_message(
            MsgType.SWEEP_PLAN_START_REQ,
            encode_sweep_plan_start_req(plan, sequence=41)[HEADER_SIZE:],
            sequence=41,
        )
        await asyncio.sleep(0.05)
        drain_response = await client._handle_message(
            MsgType.SWEEP_DRAIN_RESULTS_REQ,
            encode_sweep_drain_results_req(sequence=42)[HEADER_SIZE:],
            sequence=42,
        )
        client._sweep_executor.stop("test_finished")

        assert decode_sweep_plan_start_rsp(start_response[HEADER_SIZE:]).success is True
        results = decode_sweep_drain_results_rsp(drain_response[HEADER_SIZE:])
        assert results
        return_code, messages = ProtocolDecoder.decode_read_msgs_rsp(results[0].read_rsp_body)
        assert return_code == 0
        assert [message["data"] for message in messages] == [
            b"\x00\x00\x07\xe0",
            b"\x00\x00\x07\xe8\x62\x00\x0c\x12\x34",
        ]
        assert read_calls[:2] == [(300, 1), (300, 1)]
        finished = [
            fields
            for event_type, fields in emitted
            if event_type == "sweep.item.finished"
        ][0]
        assert finished["message_lengths"] == [4, 9]
        assert finished["tail_read_triggered"] is True
        assert finished["tail_read_attempts"] == 1
        assert finished["tail_read_data_reads"] == 1
        assert finished["read_timeout_ms"] == 1
        assert len(finished["read_attempts"]) == 2

    asyncio.run(_run())


def test_shadow_executor_queues_replacement_plan_while_old_plan_stops() -> None:
    async def _run() -> None:
        reads: list[bytes] = []
        release_first_read = asyncio.Event()

        async def _run_driver_call(method_name: str, *args, **_kwargs):
            if method_name == "write_msgs":
                return 0, len(args[1])
            if method_name == "read_msgs":
                channel_id, _num_msgs, _timeout = args
                assert channel_id == 44
                if not reads:
                    await release_first_read.wait()
                data = b"\x62\x00\x31\x12\x34" if len(reads) else b"\x62\x00\x0c\x12\x34"
                reads.append(data)
                return 0, [{"protocol_id": 6, "data": data}]
            raise AssertionError(method_name)

        emitted: list[tuple[str, dict[str, object]]] = []
        executor = LocalSweepExecutor(
            config=ProxyConfig.from_args(
                local_sweep_enabled=True,
                local_sweep_mode="shadow_local",
                local_sweep_min_item_interval_ms=1,
            ).local_sweep,
            run_driver_call=_run_driver_call,
            context_factory=lambda msg_name: LogContext(operation_kind=f"j2534:{msg_name}"),
            emit_event=lambda event_type, **fields: emitted.append((event_type, fields)),
            foreground_idle=lambda: True,
        )
        write_000c = ProtocolEncoder.encode_write_msgs_req(
            44,
            [{"protocol_id": 6, "data": b"\x22\x00\x0c"}],
            timeout=25,
        )[HEADER_SIZE:]
        write_0031 = ProtocolEncoder.encode_write_msgs_req(
            44,
            [{"protocol_id": 6, "data": b"\x22\x00\x31"}],
            timeout=25,
        )[HEADER_SIZE:]
        plan1 = SweepPlanStartRequest(
            plan_id="plan-1",
            connection_epoch="epoch-1",
            channel_id=44,
            max_result_age_ms=1000,
            min_item_interval_ms=1,
            shadow_max_seconds=1,
            requests=(SweepRequestSpec("sig-000c", write_000c, 1, 0),),
        )
        plan2 = SweepPlanStartRequest(
            plan_id="plan-2",
            connection_epoch="epoch-1",
            channel_id=44,
            max_result_age_ms=1000,
            min_item_interval_ms=1,
            shadow_max_seconds=1,
            requests=(SweepRequestSpec("sig-0031", write_0031, 1, 0),),
        )

        assert executor.start(plan1) == (True, "started")
        await asyncio.sleep(0)
        assert executor.start(plan2) == (True, "pending_start_after_stop")
        release_first_read.set()
        for _ in range(50):
            if executor.status().queued_results > 0:
                break
            await asyncio.sleep(0.01)
        results = executor.drain()
        executor.stop("test_finished")

        assert results
        assert results[-1].plan_id == "plan-2"
        assert results[-1].signature_digest == "sig-0031"
        assert any(
            event_type == "sweep.executor.stopped"
            and fields.get("sweep_plan_id") == "plan-1"
            and fields.get("reason") == "superseded_by_new_plan"
            for event_type, fields in emitted
        )

    asyncio.run(_run())


def test_handle_requests_emits_proxy_and_j2534_events(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    client = ReverseProxyClient("example.com", 9000, config=ProxyConfig.from_args(auth_token="secret"))
    client.running = True
    client.driver = types.SimpleNamespace(
        disconnect=lambda channel_id: 0,
        get_error_name=lambda code: f"ERR_{code}",
    )

    async def _run() -> None:
        reader = _FakeReader(ProtocolEncoder.encode_disconnect_req(33, sequence=7))
        writer = _FakeWriter()
        with pytest.raises(ConnectionError):
            await client._handle_requests(reader, writer, attempt_label="attempt=1")

    asyncio.run(_run())

    rows = _read_local_events(tmp_path)
    event_types = [row["event_type"] for row in rows]
    assert "proxy.request.client_received" in event_types
    assert "j2534.call.started" in event_types
    assert "j2534.call.finished" in event_types
    finished = next(row for row in rows if row["event_type"] == "j2534.call.finished")
    assert finished["proxy_seq"] == 7
    assert finished["worker_request_id"]


def test_handle_requests_emits_j2534_error_name_on_failed_return_code(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    client = ReverseProxyClient("example.com", 9000, config=ProxyConfig.from_args(auth_token="secret"))
    client.running = True
    client.driver = types.SimpleNamespace(
        disconnect=lambda channel_id: 7,
        get_error_name=lambda code: "ERR_DEVICE_NOT_CONNECTED" if code == 7 else f"ERR_{code}",
    )

    async def _run() -> None:
        reader = _FakeReader(ProtocolEncoder.encode_disconnect_req(33, sequence=9))
        writer = _FakeWriter()
        with pytest.raises(ConnectionError):
            await client._handle_requests(reader, writer, attempt_label="attempt=1")

    asyncio.run(_run())

    rows = _read_local_events(tmp_path)
    failed = next(row for row in rows if row["event_type"] == "j2534.call.failed")
    assert failed["error_name"] == "ERR_DEVICE_NOT_CONNECTED"


def test_connect_and_serve_emits_lifecycle_events(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    fake_writer = _FakeWriter()
    client = ReverseProxyClient("diag.example", 9000, config=ProxyConfig.from_args(auth_token="secret"))

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

    rows = _read_local_events(tmp_path)
    event_types = [row["event_type"] for row in rows]
    assert "reverse_client.lifecycle.connecting" in event_types
    assert "reverse_client.lifecycle.connected" in event_types
    assert "reverse_client.lifecycle.registration_succeeded" in event_types
    assert "reverse_client.lifecycle.disconnected" in event_types
    assert "reverse_client.lifecycle.cleanup" in event_types
