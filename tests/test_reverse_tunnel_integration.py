from __future__ import annotations

import asyncio
import tempfile
import types
import threading
import time

from vci_proxy.cache_read_msgs import BUFFER_EMPTY
from vci_proxy.config import ProxyConfig
from vci_proxy.protocol import (
    HEADER_SIZE,
    Message,
    MsgType,
    ProtocolDecoder,
    ProtocolEncoder,
)
from vci_proxy.reverse_client import ReverseProxyClient
from vci_proxy.reverse_server import ReverseProxyServer


async def _start_reverse_tunnel(monkeypatch, fake_driver, config: ProxyConfig | None = None):
    monkeypatch.setenv("PROGRAMDATA", tempfile.mkdtemp(prefix="rpa-demo-observability-"))
    config = config or ProxyConfig.from_args(
        auth_token="shared-secret",
        read_ahead_enabled=False,
    )
    server = ReverseProxyServer(config=config)
    vci_server = await asyncio.start_server(
        server._handle_vci_connection,
        "127.0.0.1",
        0,
    )
    proxy_server = await asyncio.start_server(
        server._handle_proxy_connection,
        "127.0.0.1",
        0,
    )
    await vci_server.start_serving()
    await proxy_server.start_serving()

    vci_port = vci_server.sockets[0].getsockname()[1]
    proxy_port = proxy_server.sockets[0].getsockname()[1]

    def _fake_driver_loader(_dll_path):
        return fake_driver, None

    client = ReverseProxyClient(
        "127.0.0.1",
        vci_port,
        config=config,
        driver_loader=_fake_driver_loader,
    )
    client_task = asyncio.create_task(client.connect_and_serve())
    await asyncio.sleep(0.1)
    await asyncio.wait_for(server.vci_connected.wait(), timeout=5.0)

    return {
        "server": server,
        "config": config,
        "client": client,
        "client_task": client_task,
        "vci_server": vci_server,
        "proxy_server": proxy_server,
        "proxy_port": proxy_port,
    }


async def _stop_reverse_tunnel(bundle) -> None:
    await bundle["client"].shutdown()
    await asyncio.wait_for(bundle["client_task"], timeout=5.0)

    bundle["vci_server"].close()
    bundle["proxy_server"].close()
    await bundle["vci_server"].wait_closed()
    await bundle["proxy_server"].wait_closed()


async def _proxy_round_trip(proxy_port: int, request: bytes):
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
    try:
        writer.write(request)
        await writer.drain()

        header = await asyncio.wait_for(
            reader.readexactly(HEADER_SIZE),
            timeout=5.0,
        )
        magic, length, msg_type, sequence = Message.decode_header(header)
        body = await reader.readexactly(length - HEADER_SIZE)
        return magic, msg_type, sequence, body
    finally:
        writer.close()
        await writer.wait_closed()


def test_reverse_tunnel_ping_round_trip_over_local_in_memory_servers(monkeypatch) -> None:
    async def _run() -> None:
        fake_driver = types.SimpleNamespace(
            dll_path="C:/fake/j2534.dll",
            open=lambda device_name=None: (0, 1234),
        )
        bundle = await _start_reverse_tunnel(monkeypatch, fake_driver)
        try:
            magic, msg_type, sequence, body = await _proxy_round_trip(
                bundle["proxy_port"],
                ProtocolEncoder.encode_ping_req(sequence=41),
            )

            assert magic > 0
            assert msg_type == MsgType.PING_RSP
            assert sequence == 41
            assert body == b""
        finally:
            await _stop_reverse_tunnel(bundle)

    asyncio.run(_run())


def test_reverse_tunnel_read_version_round_trip_calls_driver(monkeypatch) -> None:
    async def _run() -> None:
        observed: list[int] = []

        fake_driver = types.SimpleNamespace(
            dll_path="C:/fake/j2534.dll",
            open=lambda device_name=None: (0, 1234),
            read_version=lambda device_id: (
                observed.append(device_id) or (0, "FW-1.2", "DLL-2.3", "API-04.04")
            ),
        )
        bundle = await _start_reverse_tunnel(monkeypatch, fake_driver)
        try:
            magic, msg_type, sequence, body = await _proxy_round_trip(
                bundle["proxy_port"],
                ProtocolEncoder.encode_read_version_req(77, sequence=52),
            )

            assert magic > 0
            assert msg_type == MsgType.READ_VERSION_RSP
            assert sequence == 52
            assert ProtocolDecoder.decode_read_version_rsp(body) == (
                0,
                "FW-1.2",
                "DLL-2.3",
                "API-04.04",
            )
            assert observed == [77]
        finally:
            await _stop_reverse_tunnel(bundle)

    asyncio.run(_run())


def test_reverse_tunnel_ioctl_round_trip_uses_server_cache(monkeypatch) -> None:
    async def _run() -> None:
        observed: list[tuple[int, int, bytes | None]] = []

        fake_driver = types.SimpleNamespace(
            dll_path="C:/fake/j2534.dll",
            open=lambda device_name=None: (0, 1234),
            ioctl=lambda channel_id, ioctl_id, input_data=None: (
                observed.append((channel_id, ioctl_id, input_data)) or (0, b"\x12\x34")
            ),
        )
        bundle = await _start_reverse_tunnel(monkeypatch, fake_driver)
        try:
            request = ProtocolEncoder.encode_ioctl_req(9, 0x03, None, sequence=61)

            first = await _proxy_round_trip(bundle["proxy_port"], request)
            second = await _proxy_round_trip(bundle["proxy_port"], request)

            assert first[0] > 0
            assert first[1] == MsgType.IOCTL_RSP
            assert first[2] == 61
            assert ProtocolDecoder.decode_ioctl_rsp(first[3]) == (0, b"\x12\x34")

            assert second[0] > 0
            assert second[1] == MsgType.IOCTL_RSP
            assert second[2] == 61
            assert ProtocolDecoder.decode_ioctl_rsp(second[3]) == (0, b"\x12\x34")

            assert observed == [(9, 0x03, None)]
        finally:
            await _stop_reverse_tunnel(bundle)

    asyncio.run(_run())


def test_reverse_tunnel_open_round_trip_uses_prewarmed_device(monkeypatch) -> None:
    async def _run() -> None:
        observed: list[str | None] = []

        fake_driver = types.SimpleNamespace(
            dll_path="C:/fake/j2534.dll",
            open=lambda device_name=None: (
                observed.append(device_name) or (0, 4321)
            ),
        )
        bundle = await _start_reverse_tunnel(monkeypatch, fake_driver)
        try:
            while bundle["client"]._prewarm_task is None:
                await asyncio.sleep(0.01)
            await asyncio.wait_for(bundle["client"]._prewarm_task, timeout=5.0)

            magic, msg_type, sequence, body = await _proxy_round_trip(
                bundle["proxy_port"],
                ProtocolEncoder.encode_open_req("Demo Device", sequence=71),
            )

            assert magic > 0
            assert msg_type == MsgType.OPEN_RSP
            assert sequence == 71
            assert ProtocolDecoder.decode_open_rsp(body) == (0, 4321)
            assert observed == [None]
        finally:
            await _stop_reverse_tunnel(bundle)

    asyncio.run(_run())


def test_reverse_tunnel_read_msgs_round_trip_uses_buffer_empty_cache(monkeypatch) -> None:
    async def _run() -> None:
        observed: list[tuple[int, int, int]] = []

        fake_driver = types.SimpleNamespace(
            dll_path="C:/fake/j2534.dll",
            open=lambda device_name=None: (0, 1234),
            read_msgs=lambda channel_id, num_msgs, timeout: (
                observed.append((channel_id, num_msgs, timeout)) or (BUFFER_EMPTY, [])
            ),
        )
        bundle = await _start_reverse_tunnel(monkeypatch, fake_driver)
        try:
            request = ProtocolEncoder.encode_read_msgs_req(33, 4, 25, sequence=81)

            first = await _proxy_round_trip(bundle["proxy_port"], request)
            second = await _proxy_round_trip(bundle["proxy_port"], request)

            assert first[1] == MsgType.READ_MSGS_RSP
            assert first[2] == 81
            assert ProtocolDecoder.decode_read_msgs_rsp(first[3]) == (BUFFER_EMPTY, [])

            assert second[1] == MsgType.READ_MSGS_RSP
            assert second[2] == 81
            assert ProtocolDecoder.decode_read_msgs_rsp(second[3]) == (BUFFER_EMPTY, [])

            assert observed == [(33, 4, 25)]
        finally:
            await _stop_reverse_tunnel(bundle)

    asyncio.run(_run())


def test_reverse_tunnel_write_bypasses_same_channel_read_msgs_empty_cache(monkeypatch) -> None:
    async def _run() -> None:
        observed_reads: list[tuple[int, int, int]] = []
        observed_writes: list[tuple[int, list[dict], int]] = []

        fake_driver = types.SimpleNamespace(
            dll_path="C:/fake/j2534.dll",
            open=lambda device_name=None: (0, 1234),
            read_msgs=lambda channel_id, num_msgs, timeout: (
                observed_reads.append((channel_id, num_msgs, timeout)) or (BUFFER_EMPTY, [])
            ),
            write_msgs=lambda channel_id, messages, timeout: (
                observed_writes.append((channel_id, messages, timeout)) or (0, len(messages))
            ),
        )
        bundle = await _start_reverse_tunnel(monkeypatch, fake_driver)
        try:
            read_request = ProtocolEncoder.encode_read_msgs_req(33, 4, 25, sequence=81)

            first = await _proxy_round_trip(bundle["proxy_port"], read_request)
            second = await _proxy_round_trip(bundle["proxy_port"], read_request)
            write_rsp = await _proxy_round_trip(
                bundle["proxy_port"],
                ProtocolEncoder.encode_write_msgs_req(
                    33,
                    [{"protocol_id": 6, "rx_status": 0, "tx_flags": 0, "timestamp": 1, "data": b"\x2a"}],
                    timeout=25,
                    sequence=82,
                ),
            )
            third = await _proxy_round_trip(bundle["proxy_port"], read_request)

            assert ProtocolDecoder.decode_read_msgs_rsp(first[3]) == (BUFFER_EMPTY, [])
            assert ProtocolDecoder.decode_read_msgs_rsp(second[3]) == (BUFFER_EMPTY, [])
            assert ProtocolDecoder.decode_write_msgs_rsp(write_rsp[3]) == (0, 1)
            assert ProtocolDecoder.decode_read_msgs_rsp(third[3]) == (BUFFER_EMPTY, [])

            assert observed_reads == [(33, 4, 25), (33, 4, 25)]
            assert len(observed_writes) == 1
            assert observed_writes[0][0] == 33
        finally:
            await _stop_reverse_tunnel(bundle)

    asyncio.run(_run())


def test_reverse_tunnel_start_filter_round_trip_uses_server_dedup(monkeypatch) -> None:
    async def _run() -> None:
        observed: list[tuple[int, int, dict | None, dict | None, dict | None]] = []

        fake_driver = types.SimpleNamespace(
            dll_path="C:/fake/j2534.dll",
            open=lambda device_name=None: (0, 1234),
            start_msg_filter=lambda channel_id, filter_type, mask_msg, pattern_msg, flow_control_msg: (
                observed.append((channel_id, filter_type, mask_msg, pattern_msg, flow_control_msg))
                or (0, 501)
            ),
        )
        bundle = await _start_reverse_tunnel(monkeypatch, fake_driver)
        try:
            request = ProtocolEncoder.encode_start_filter_req(
                12,
                7,
                {"protocol_id": 6, "data": b"\x01\x02"},
                {"protocol_id": 6, "data": b"\x03\x04"},
                None,
                sequence=91,
            )

            first = await _proxy_round_trip(bundle["proxy_port"], request)
            second = await _proxy_round_trip(bundle["proxy_port"], request)

            assert first[1] == MsgType.START_FILTER_RSP
            assert first[2] == 91
            assert ProtocolDecoder.decode_start_filter_rsp(first[3]) == (0, 501)

            assert second[1] == MsgType.START_FILTER_RSP
            assert second[2] == 91
            assert ProtocolDecoder.decode_start_filter_rsp(second[3]) == (0, 501)

            assert observed == [
                (
                    12,
                    7,
                    {"protocol_id": 6, "rx_status": 0, "tx_flags": 0, "timestamp": 0, "data": b"\x01\x02"},
                    {"protocol_id": 6, "rx_status": 0, "tx_flags": 0, "timestamp": 0, "data": b"\x03\x04"},
                    None,
                )
            ]
        finally:
            await _stop_reverse_tunnel(bundle)

    asyncio.run(_run())


def test_reverse_tunnel_connect_write_and_disconnect_round_trip_calls_driver(monkeypatch) -> None:
    async def _run() -> None:
        observed: list[tuple[str, object]] = []

        fake_driver = types.SimpleNamespace(
            dll_path="C:/fake/j2534.dll",
            open=lambda device_name=None: (0, 1234),
            connect=lambda device_id, protocol_id, flags, baudrate: (
                observed.append(("connect", (device_id, protocol_id, flags, baudrate))) or (0, 9001)
            ),
            write_msgs=lambda channel_id, messages, timeout: (
                observed.append(("write_msgs", (channel_id, messages, timeout))) or (0, len(messages))
            ),
            disconnect=lambda channel_id: (
                observed.append(("disconnect", channel_id)) or 0
            ),
        )
        bundle = await _start_reverse_tunnel(monkeypatch, fake_driver)
        try:
            connect_rsp = await _proxy_round_trip(
                bundle["proxy_port"],
                ProtocolEncoder.encode_connect_req(1234, 6, 0, 500000, sequence=101),
            )
            write_rsp = await _proxy_round_trip(
                bundle["proxy_port"],
                ProtocolEncoder.encode_write_msgs_req(
                    9001,
                    [{"protocol_id": 6, "timestamp": 1, "data": b"\x10\x20\x30"}],
                    timeout=200,
                    sequence=102,
                ),
            )
            disconnect_rsp = await _proxy_round_trip(
                bundle["proxy_port"],
                ProtocolEncoder.encode_disconnect_req(9001, sequence=103),
            )

            assert connect_rsp[1] == MsgType.CONNECT_RSP
            assert connect_rsp[2] == 101
            assert ProtocolDecoder.decode_connect_rsp(connect_rsp[3]) == (0, 9001)

            assert write_rsp[1] == MsgType.WRITE_MSGS_RSP
            assert write_rsp[2] == 102
            assert ProtocolDecoder.decode_write_msgs_rsp(write_rsp[3]) == (0, 1)

            assert disconnect_rsp[1] == MsgType.DISCONNECT_RSP
            assert disconnect_rsp[2] == 103
            assert ProtocolDecoder.decode_disconnect_rsp(disconnect_rsp[3]) == 0

            assert observed == [
                ("connect", (1234, 6, 0, 500000)),
                (
                    "write_msgs",
                    (
                        9001,
                        [
                            {
                                "protocol_id": 6,
                                "rx_status": 0,
                                "tx_flags": 0,
                                "timestamp": 1,
                                "data": b"\x10\x20\x30",
                            }
                        ],
                        200,
                    ),
                ),
                ("disconnect", 9001),
            ]
        finally:
            await _stop_reverse_tunnel(bundle)

    asyncio.run(_run())


def test_reverse_tunnel_read_ahead_serves_following_read_from_prefetch_fifo(monkeypatch) -> None:
    async def _run() -> None:
        observed: list[tuple[str, object]] = []
        prefetched_message = {
            "protocol_id": 6,
            "rx_status": 0,
            "tx_flags": 0,
            "timestamp": 321,
            "data": b"\x62\xf4\x0c",
        }
        read_results = iter([(0, [prefetched_message]), (BUFFER_EMPTY, []), (BUFFER_EMPTY, [])])

        fake_driver = types.SimpleNamespace(
            dll_path="C:/fake/j2534.dll",
            write_msgs=lambda channel_id, messages, timeout: (
                observed.append(("write_msgs", (channel_id, messages, timeout))) or (0, len(messages))
            ),
            read_msgs=lambda channel_id, num_msgs, timeout: (
                observed.append(("read_msgs", (channel_id, num_msgs, timeout))) or next(read_results)
            ),
        )
        config = ProxyConfig.from_args(
            auth_token="shared-secret",
            read_ahead_enabled=True,
            read_ahead_transaction_enabled=False,
            read_ahead_max_reads=2,
            read_ahead_max_messages=4,
            read_ahead_read_timeout_ms=0,
        )
        bundle = await _start_reverse_tunnel(monkeypatch, fake_driver, config=config)
        try:
            write_rsp = await _proxy_round_trip(
                bundle["proxy_port"],
                ProtocolEncoder.encode_write_msgs_req(
                    9001,
                    [{"protocol_id": 6, "timestamp": 1, "data": b"\x22"}],
                    timeout=200,
                    sequence=102,
                ),
            )
            read_rsp = await _proxy_round_trip(
                bundle["proxy_port"],
                ProtocolEncoder.encode_read_msgs_req(9001, num_msgs=4, timeout=0, sequence=103),
            )

            assert write_rsp[1] == MsgType.WRITE_MSGS_RSP
            assert write_rsp[2] == 102
            assert ProtocolDecoder.decode_write_msgs_rsp(write_rsp[3]) == (0, 1)
            assert read_rsp[1] == MsgType.READ_MSGS_RSP
            assert read_rsp[2] == 103
            assert ProtocolDecoder.decode_read_msgs_rsp(read_rsp[3]) == (0, [prefetched_message])
            assert observed == [
                (
                    "write_msgs",
                    (
                        9001,
                        [
                            {
                                "protocol_id": 6,
                                "rx_status": 0,
                                "tx_flags": 0,
                                "timestamp": 1,
                                "data": b"\x22",
                            }
                        ],
                        200,
                    ),
                ),
                ("read_msgs", (9001, 4, 0)),
                ("read_msgs", (9001, 3, 0)),
                ("read_msgs", (9001, 3, 0)),
            ]
        finally:
            await _stop_reverse_tunnel(bundle)

    asyncio.run(_run())


def test_reverse_tunnel_read_ahead_merges_partial_fifo_with_tunnel_data(monkeypatch) -> None:
    async def _run() -> None:
        observed: list[tuple[str, object]] = []
        prefetched_message = {
            "protocol_id": 6,
            "rx_status": 0,
            "tx_flags": 0,
            "timestamp": 321,
            "data": b"\x62\x01",
        }
        tunnel_message = {
            "protocol_id": 6,
            "rx_status": 0,
            "tx_flags": 0,
            "timestamp": 322,
            "data": b"\x62\x02",
        }
        read_results = iter([(0, [prefetched_message]), (0, [tunnel_message])])

        fake_driver = types.SimpleNamespace(
            dll_path="C:/fake/j2534.dll",
            write_msgs=lambda channel_id, messages, timeout: (
                observed.append(("write_msgs", (channel_id, messages, timeout))) or (0, len(messages))
            ),
            read_msgs=lambda channel_id, num_msgs, timeout: (
                observed.append(("read_msgs", (channel_id, num_msgs, timeout))) or next(read_results)
            ),
        )
        config = ProxyConfig.from_args(
            auth_token="shared-secret",
            read_ahead_enabled=True,
            read_ahead_transaction_enabled=False,
            read_ahead_max_reads=1,
            read_ahead_max_messages=4,
            read_ahead_read_timeout_ms=0,
        )
        bundle = await _start_reverse_tunnel(monkeypatch, fake_driver, config=config)
        try:
            await _proxy_round_trip(
                bundle["proxy_port"],
                ProtocolEncoder.encode_write_msgs_req(
                    9001,
                    [{"protocol_id": 6, "timestamp": 1, "data": b"\x22"}],
                    timeout=200,
                    sequence=102,
                ),
            )
            read_rsp = await _proxy_round_trip(
                bundle["proxy_port"],
                ProtocolEncoder.encode_read_msgs_req(9001, num_msgs=2, timeout=0, sequence=103),
            )

            assert read_rsp[1] == MsgType.READ_MSGS_RSP
            assert read_rsp[2] == 103
            assert ProtocolDecoder.decode_read_msgs_rsp(read_rsp[3]) == (
                0,
                [prefetched_message, tunnel_message],
            )
            assert [name for name, _value in observed] == [
                "write_msgs",
                "read_msgs",
                "read_msgs",
            ]
            assert observed[-1] == ("read_msgs", (9001, 1, 0))
        finally:
            await _stop_reverse_tunnel(bundle)

    asyncio.run(_run())


def test_reverse_tunnel_read_ahead_serves_oversized_nonblocking_read_without_merge(monkeypatch) -> None:
    async def _run() -> None:
        observed: list[tuple[str, object]] = []
        prefetched_message = {
            "protocol_id": 6,
            "rx_status": 0,
            "tx_flags": 0,
            "timestamp": 321,
            "data": b"\x62\x01",
        }
        read_results = iter([(0, [prefetched_message])])

        fake_driver = types.SimpleNamespace(
            dll_path="C:/fake/j2534.dll",
            write_msgs=lambda channel_id, messages, timeout: (
                observed.append(("write_msgs", (channel_id, messages, timeout))) or (0, len(messages))
            ),
            read_msgs=lambda channel_id, num_msgs, timeout: (
                observed.append(("read_msgs", (channel_id, num_msgs, timeout))) or next(read_results)
            ),
        )
        config = ProxyConfig.from_args(
            auth_token="shared-secret",
            read_ahead_enabled=True,
            read_ahead_transaction_enabled=False,
            read_ahead_max_reads=1,
            read_ahead_max_messages=4,
            read_ahead_read_timeout_ms=0,
        )
        bundle = await _start_reverse_tunnel(monkeypatch, fake_driver, config=config)
        try:
            await _proxy_round_trip(
                bundle["proxy_port"],
                ProtocolEncoder.encode_write_msgs_req(
                    9001,
                    [{"protocol_id": 6, "timestamp": 1, "data": b"\x22"}],
                    timeout=200,
                    sequence=102,
                ),
            )
            read_rsp = await _proxy_round_trip(
                bundle["proxy_port"],
                ProtocolEncoder.encode_read_msgs_req(9001, num_msgs=300, timeout=0, sequence=103),
            )

            assert read_rsp[1] == MsgType.READ_MSGS_RSP
            assert read_rsp[2] == 103
            assert ProtocolDecoder.decode_read_msgs_rsp(read_rsp[3]) == (
                0,
                [prefetched_message],
            )
            assert [name for name, _value in observed] == [
                "write_msgs",
                "read_msgs",
            ]
            assert observed[-1] == ("read_msgs", (9001, 4, 0))
        finally:
            await _stop_reverse_tunnel(bundle)

    asyncio.run(_run())


def test_reverse_tunnel_read_ahead_preserves_fifo_order_across_writes(monkeypatch) -> None:
    async def _run() -> None:
        observed: list[tuple[str, object]] = []
        first_message = {
            "protocol_id": 6,
            "rx_status": 0,
            "tx_flags": 0,
            "timestamp": 1,
            "data": b"\x62\x01",
        }
        second_message = {
            "protocol_id": 6,
            "rx_status": 0,
            "tx_flags": 0,
            "timestamp": 2,
            "data": b"\x62\x02",
        }
        read_results = iter([(0, [first_message]), (0, [second_message])])

        fake_driver = types.SimpleNamespace(
            dll_path="C:/fake/j2534.dll",
            write_msgs=lambda channel_id, messages, timeout: (
                observed.append(("write_msgs", (channel_id, messages, timeout))) or (0, len(messages))
            ),
            read_msgs=lambda channel_id, num_msgs, timeout: (
                observed.append(("read_msgs", (channel_id, num_msgs, timeout))) or next(read_results)
            ),
        )
        config = ProxyConfig.from_args(
            auth_token="shared-secret",
            read_ahead_enabled=True,
            read_ahead_transaction_enabled=False,
            read_ahead_max_reads=1,
            read_ahead_max_messages=4,
            read_ahead_read_timeout_ms=0,
        )
        bundle = await _start_reverse_tunnel(monkeypatch, fake_driver, config=config)
        try:
            await _proxy_round_trip(
                bundle["proxy_port"],
                ProtocolEncoder.encode_write_msgs_req(
                    9001,
                    [{"protocol_id": 6, "timestamp": 1, "data": b"\x22\x01"}],
                    timeout=200,
                    sequence=102,
                ),
            )
            await _proxy_round_trip(
                bundle["proxy_port"],
                ProtocolEncoder.encode_write_msgs_req(
                    9001,
                    [{"protocol_id": 6, "timestamp": 2, "data": b"\x22\x02"}],
                    timeout=200,
                    sequence=103,
                ),
            )
            first_read = await _proxy_round_trip(
                bundle["proxy_port"],
                ProtocolEncoder.encode_read_msgs_req(9001, num_msgs=1, timeout=0, sequence=104),
            )
            second_read = await _proxy_round_trip(
                bundle["proxy_port"],
                ProtocolEncoder.encode_read_msgs_req(9001, num_msgs=1, timeout=0, sequence=105),
            )

            assert ProtocolDecoder.decode_read_msgs_rsp(first_read[3]) == (0, [first_message])
            assert ProtocolDecoder.decode_read_msgs_rsp(second_read[3]) == (0, [second_message])
            assert [name for name, _value in observed] == [
                "write_msgs",
                "read_msgs",
                "write_msgs",
                "read_msgs",
            ]
        finally:
            await _stop_reverse_tunnel(bundle)

    asyncio.run(_run())


def test_reverse_tunnel_write_collect_transaction_serves_following_read(monkeypatch) -> None:
    async def _run() -> None:
        observed: list[tuple[str, object]] = []
        prefetched_message = {
            "protocol_id": 6,
            "rx_status": 0,
            "tx_flags": 0,
            "timestamp": 321,
            "data": b"\x62\xf4\x0c",
        }
        read_results = iter([(0, [prefetched_message]), (BUFFER_EMPTY, []), (BUFFER_EMPTY, [])])

        fake_driver = types.SimpleNamespace(
            dll_path="C:/fake/j2534.dll",
            write_msgs=lambda channel_id, messages, timeout: (
                observed.append(("write_msgs", (channel_id, messages, timeout))) or (0, len(messages))
            ),
            read_msgs=lambda channel_id, num_msgs, timeout: (
                observed.append(("read_msgs", (channel_id, num_msgs, timeout))) or next(read_results)
            ),
        )
        config = ProxyConfig.from_args(
            auth_token="shared-secret",
            read_ahead_enabled=True,
            read_ahead_transaction_enabled=True,
            read_ahead_max_reads=2,
            read_ahead_write_collect_max_reads=2,
            read_ahead_max_messages=4,
            read_ahead_read_timeout_ms=0,
        )
        bundle = await _start_reverse_tunnel(monkeypatch, fake_driver, config=config)
        try:
            assert bundle["server"]._vci_write_collect_supported is True

            write_rsp = await _proxy_round_trip(
                bundle["proxy_port"],
                ProtocolEncoder.encode_write_msgs_req(
                    9001,
                    [{"protocol_id": 6, "timestamp": 1, "data": b"\x22"}],
                    timeout=200,
                    sequence=102,
                ),
            )
            read_rsp = await _proxy_round_trip(
                bundle["proxy_port"],
                ProtocolEncoder.encode_read_msgs_req(9001, num_msgs=4, timeout=0, sequence=103),
            )

            assert write_rsp[1] == MsgType.WRITE_MSGS_RSP
            assert len(write_rsp[3]) == 8
            assert ProtocolDecoder.decode_write_msgs_rsp(write_rsp[3]) == (0, 1)
            assert read_rsp[1] == MsgType.READ_MSGS_RSP
            assert ProtocolDecoder.decode_read_msgs_rsp(read_rsp[3]) == (0, [prefetched_message])
            assert [name for name, _value in observed] == [
                "write_msgs",
                "read_msgs",
                "read_msgs",
                "read_msgs",
            ]
        finally:
            await _stop_reverse_tunnel(bundle)

    asyncio.run(_run())


def test_reverse_tunnel_read_collect_prefetches_tail_after_foreground_empty(monkeypatch) -> None:
    async def _run() -> None:
        observed: list[tuple[str, object]] = []
        prefetched_message = {
            "protocol_id": 6,
            "rx_status": 0,
            "tx_flags": 0,
            "timestamp": 321,
            "data": b"\x62\x13\x08",
        }
        read_results = iter(
            [
                (BUFFER_EMPTY, []),
                (0, [prefetched_message]),
                (BUFFER_EMPTY, []),
            ]
        )

        fake_driver = types.SimpleNamespace(
            dll_path="C:/fake/j2534.dll",
            read_msgs=lambda channel_id, num_msgs, timeout: (
                observed.append(("read_msgs", (channel_id, num_msgs, timeout)))
                or next(read_results)
            ),
        )
        config = ProxyConfig.from_args(
            auth_token="shared-secret",
            read_ahead_enabled=True,
            read_ahead_transaction_enabled=True,
            read_ahead_max_reads=2,
            read_ahead_max_messages=4,
            read_ahead_read_timeout_ms=0,
        )
        bundle = await _start_reverse_tunnel(monkeypatch, fake_driver, config=config)
        try:
            assert bundle["server"]._vci_read_collect_supported is True

            first_read = await _proxy_round_trip(
                bundle["proxy_port"],
                ProtocolEncoder.encode_read_msgs_req(9001, num_msgs=4, timeout=0, sequence=102),
            )
            second_read = await _proxy_round_trip(
                bundle["proxy_port"],
                ProtocolEncoder.encode_read_msgs_req(9001, num_msgs=1, timeout=0, sequence=103),
            )

            assert first_read[1] == MsgType.READ_MSGS_RSP
            assert ProtocolDecoder.decode_read_msgs_rsp(first_read[3]) == (BUFFER_EMPTY, [])
            assert second_read[1] == MsgType.READ_MSGS_RSP
            assert ProtocolDecoder.decode_read_msgs_rsp(second_read[3]) == (
                0,
                [prefetched_message],
            )
            assert observed == [
                ("read_msgs", (9001, 4, 0)),
                ("read_msgs", (9001, 4, 0)),
                ("read_msgs", (9001, 3, 0)),
            ]
        finally:
            await _stop_reverse_tunnel(bundle)

    asyncio.run(_run())


def test_reverse_tunnel_shadow_local_keeps_dll_facing_flow_unchanged(monkeypatch) -> None:
    async def _run() -> None:
        active = 0
        max_active = 0
        guard = threading.Lock()

        def _enter():
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.01)
            with guard:
                active -= 1

        observed_reads: list[tuple[int, int, int]] = []
        observed_writes: list[tuple[int, list[dict], int]] = []

        fake_driver = types.SimpleNamespace(
            dll_path="C:/fake/j2534.dll",
            open=lambda device_name=None: (0, 1234),
            write_msgs=lambda channel_id, messages, timeout: (
                _enter() or observed_writes.append((channel_id, messages, timeout)) or (0, len(messages))
            ),
            read_msgs=lambda channel_id, num_msgs, timeout: (
                _enter() or observed_reads.append((channel_id, num_msgs, timeout)) or (
                    0,
                    [
                        {
                            "protocol_id": 6,
                            "rx_status": 0,
                            "tx_flags": 0,
                            "timestamp": 1,
                            "data": b"\x62\xf4\x0c\x00\x80",
                        }
                    ],
                )
            ),
        )
        config = ProxyConfig.from_args(
            auth_token="shared-secret",
            read_ahead_enabled=False,
            local_sweep_enabled=True,
            local_sweep_mode="shadow_local",
            local_sweep_min_cycles=1,
            local_sweep_shadow_max_seconds=1,
            local_sweep_plan_delay_ms=0,
        )
        bundle = await _start_reverse_tunnel(monkeypatch, fake_driver, config=config)
        try:
            write_rsp = await _proxy_round_trip(
                bundle["proxy_port"],
                ProtocolEncoder.encode_write_msgs_req(
                    9001,
                    [{"protocol_id": 6, "timestamp": 1, "data": b"\x22\xf4\x0c"}],
                    timeout=200,
                    sequence=101,
                ),
            )
            read_rsp = await _proxy_round_trip(
                bundle["proxy_port"],
                ProtocolEncoder.encode_read_msgs_req(9001, num_msgs=1, timeout=0, sequence=102),
            )
            await asyncio.sleep(0.1)

            assert write_rsp[1] == MsgType.WRITE_MSGS_RSP
            assert read_rsp[1] == MsgType.READ_MSGS_RSP
            assert ProtocolDecoder.decode_write_msgs_rsp(write_rsp[3]) == (0, 1)
            assert ProtocolDecoder.decode_read_msgs_rsp(read_rsp[3])[0] == 0
            assert max_active == 1
        finally:
            await _stop_reverse_tunnel(bundle)

    asyncio.run(_run())


def test_reverse_tunnel_shadow_local_skips_gm_a9_without_extra_local_calls(monkeypatch) -> None:
    async def _run() -> None:
        observed: list[str] = []

        fake_driver = types.SimpleNamespace(
            dll_path="C:/fake/j2534.dll",
            write_msgs=lambda channel_id, messages, timeout: (
                observed.append("write_msgs") or (0, len(messages))
            ),
            read_msgs=lambda channel_id, num_msgs, timeout: (
                observed.append("read_msgs")
                or (
                    0,
                    [
                        {
                            "protocol_id": 6,
                            "rx_status": 0,
                            "tx_flags": 0,
                            "timestamp": 1,
                            "data": b"\x00\x00\x05\xe8\xa9\x81\x1a\x00",
                        }
                    ],
                )
            ),
        )
        config = ProxyConfig.from_args(
            auth_token="shared-secret",
            read_ahead_enabled=False,
            local_sweep_enabled=True,
            local_sweep_mode="shadow_local",
            local_sweep_min_cycles=1,
            local_sweep_plan_delay_ms=0,
            local_sweep_shadow_max_seconds=1,
            local_sweep_shadow_allow_gm_a9_packet=False,
        )
        bundle = await _start_reverse_tunnel(monkeypatch, fake_driver, config=config)
        try:
            assert bundle["server"]._vci_sweep_shadow_supported is True

            write_rsp = await _proxy_round_trip(
                bundle["proxy_port"],
                ProtocolEncoder.encode_write_msgs_req(
                    9001,
                    [
                        {
                            "protocol_id": 6,
                            "timestamp": 1,
                            "data": b"\x00\x00\x07\xe0\xa9\x81\x1a",
                        }
                    ],
                    timeout=200,
                    sequence=201,
                ),
            )
            read_rsp = await _proxy_round_trip(
                bundle["proxy_port"],
                ProtocolEncoder.encode_read_msgs_req(
                    9001,
                    num_msgs=1,
                    timeout=0,
                    sequence=202,
                ),
            )
            await asyncio.sleep(0.1)

            assert write_rsp[1] == MsgType.WRITE_MSGS_RSP
            assert read_rsp[1] == MsgType.READ_MSGS_RSP
            assert ProtocolDecoder.decode_write_msgs_rsp(write_rsp[3]) == (0, 1)
            assert ProtocolDecoder.decode_read_msgs_rsp(read_rsp[3])[0] == 0
            assert bundle["server"]._sweep_active_plan is None
            assert observed == ["write_msgs", "read_msgs"]
        finally:
            await _stop_reverse_tunnel(bundle)

    asyncio.run(_run())


def test_reverse_tunnel_disconnect_invalidates_read_msgs_cache(monkeypatch) -> None:
    async def _run() -> None:
        observed_reads: list[tuple[int, int, int]] = []
        observed_disconnects: list[int] = []

        fake_driver = types.SimpleNamespace(
            dll_path="C:/fake/j2534.dll",
            open=lambda device_name=None: (0, 1234),
            read_msgs=lambda channel_id, num_msgs, timeout: (
                observed_reads.append((channel_id, num_msgs, timeout)) or (BUFFER_EMPTY, [])
            ),
            disconnect=lambda channel_id: (
                observed_disconnects.append(channel_id) or 0
            ),
        )
        bundle = await _start_reverse_tunnel(monkeypatch, fake_driver)
        try:
            request = ProtocolEncoder.encode_read_msgs_req(44, 2, 10, sequence=111)

            first = await _proxy_round_trip(bundle["proxy_port"], request)
            second = await _proxy_round_trip(bundle["proxy_port"], request)
            disconnect_rsp = await _proxy_round_trip(
                bundle["proxy_port"],
                ProtocolEncoder.encode_disconnect_req(44, sequence=112),
            )
            third = await _proxy_round_trip(bundle["proxy_port"], request)

            assert ProtocolDecoder.decode_read_msgs_rsp(first[3]) == (BUFFER_EMPTY, [])
            assert ProtocolDecoder.decode_read_msgs_rsp(second[3]) == (BUFFER_EMPTY, [])
            assert ProtocolDecoder.decode_disconnect_rsp(disconnect_rsp[3]) == 0
            assert ProtocolDecoder.decode_read_msgs_rsp(third[3]) == (BUFFER_EMPTY, [])

            assert observed_disconnects == [44]
            assert observed_reads == [(44, 2, 10), (44, 2, 10)]
        finally:
            await _stop_reverse_tunnel(bundle)

    asyncio.run(_run())


def test_reverse_tunnel_stop_filter_invalidates_server_dedup(monkeypatch) -> None:
    async def _run() -> None:
        observed_starts: list[tuple[int, int, dict | None, dict | None, dict | None]] = []
        observed_stops: list[tuple[int, int]] = []

        fake_driver = types.SimpleNamespace(
            dll_path="C:/fake/j2534.dll",
            open=lambda device_name=None: (0, 1234),
            start_msg_filter=lambda channel_id, filter_type, mask_msg, pattern_msg, flow_control_msg: (
                observed_starts.append((channel_id, filter_type, mask_msg, pattern_msg, flow_control_msg))
                or (0, 501)
            ),
            stop_msg_filter=lambda channel_id, filter_id: (
                observed_stops.append((channel_id, filter_id)) or 0
            ),
        )
        bundle = await _start_reverse_tunnel(monkeypatch, fake_driver)
        try:
            request = ProtocolEncoder.encode_start_filter_req(
                12,
                7,
                {"protocol_id": 6, "data": b"\x01\x02"},
                {"protocol_id": 6, "data": b"\x03\x04"},
                None,
                sequence=121,
            )

            first = await _proxy_round_trip(bundle["proxy_port"], request)
            second = await _proxy_round_trip(bundle["proxy_port"], request)
            stop_rsp = await _proxy_round_trip(
                bundle["proxy_port"],
                ProtocolEncoder.encode_stop_filter_req(12, 501, sequence=122),
            )
            third = await _proxy_round_trip(bundle["proxy_port"], request)

            assert ProtocolDecoder.decode_start_filter_rsp(first[3]) == (0, 501)
            assert ProtocolDecoder.decode_start_filter_rsp(second[3]) == (0, 501)
            assert ProtocolDecoder.decode_stop_filter_rsp(stop_rsp[3]) == 0
            assert ProtocolDecoder.decode_start_filter_rsp(third[3]) == (0, 501)

            assert observed_stops == [(12, 501)]
            assert len(observed_starts) == 2
        finally:
            await _stop_reverse_tunnel(bundle)

    asyncio.run(_run())


def test_reverse_tunnel_close_invalidates_ioctl_cache(monkeypatch) -> None:
    async def _run() -> None:
        observed_ioctls: list[tuple[int, int, bytes | None]] = []
        observed_closes: list[int] = []

        fake_driver = types.SimpleNamespace(
            dll_path="C:/fake/j2534.dll",
            open=lambda device_name=None: (0, 1234),
            ioctl=lambda channel_id, ioctl_id, input_data=None: (
                observed_ioctls.append((channel_id, ioctl_id, input_data)) or (0, b"\x56\x78")
            ),
            close=lambda device_id: (
                observed_closes.append(device_id) or 0
            ),
        )
        bundle = await _start_reverse_tunnel(monkeypatch, fake_driver)
        try:
            request = ProtocolEncoder.encode_ioctl_req(9, 0x03, None, sequence=131)

            first = await _proxy_round_trip(bundle["proxy_port"], request)
            second = await _proxy_round_trip(bundle["proxy_port"], request)
            close_rsp = await _proxy_round_trip(
                bundle["proxy_port"],
                ProtocolEncoder.encode_close_req(1234, sequence=132),
            )
            third = await _proxy_round_trip(bundle["proxy_port"], request)

            assert ProtocolDecoder.decode_ioctl_rsp(first[3]) == (0, b"\x56\x78")
            assert ProtocolDecoder.decode_ioctl_rsp(second[3]) == (0, b"\x56\x78")
            assert ProtocolDecoder.decode_close_rsp(close_rsp[3]) == 0
            assert ProtocolDecoder.decode_ioctl_rsp(third[3]) == (0, b"\x56\x78")

            assert observed_closes == [1234]
            assert observed_ioctls == [(9, 0x03, None), (9, 0x03, None)]
        finally:
            await _stop_reverse_tunnel(bundle)

    asyncio.run(_run())
