"""Reverse-tunnel client for the local VCI proxy."""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import logging
import os
import socket
import time
from typing import Callable, Optional

from vci_proxy.auth import compute_signature
from vci_proxy.benchmark import attach_timing_trailer
from vci_proxy.cache_ioctl import IoctlCache
from vci_proxy.config import ProxyConfig
from vci_proxy.j2534_driver import J2534Driver
from vci_proxy.protocol import (
    HEADER_SIZE,
    MAGIC,
    Message,
    MsgType,
    ProtocolDecoder,
    ProtocolEncoder,
)


logger = logging.getLogger(__name__)


class ReverseProxyClient:
    """Local-side reverse tunnel client that executes J2534 requests."""

    def __init__(
        self,
        server_host: str,
        server_port: int,
        dll_path: Optional[str] = None,
        config: Optional[ProxyConfig] = None,
        on_status_change: Optional[Callable[[str, str], None]] = None,
    ):
        self.server_host = server_host
        self.server_port = server_port
        self.dll_path = dll_path
        self.config = config or ProxyConfig()
        self.driver: Optional[J2534Driver] = None
        self.running = False
        self._on_status_change = on_status_change
        self._prewarm_device_id: Optional[int] = None
        self._prewarm_ret: Optional[int] = None
        self._prewarm_task: Optional[asyncio.Task] = None
        self._ioctl_cache = IoctlCache(self.config.ioctl_cache)
        self._active_writer: Optional[asyncio.StreamWriter] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._backoff_sleep_task: Optional[asyncio.Task] = None

    def _notify_status(self, status: str, detail: str = "") -> None:
        if self._on_status_change:
            try:
                self._on_status_change(status, detail)
            except Exception:
                logger.debug("status callback failed", exc_info=True)

    def _ensure_driver(self) -> bool:
        if self.driver is None:
            try:
                self.driver = J2534Driver(self.dll_path)
                logger.info("J2534 driver loaded: %s", self.driver.dll_path)
                return True
            except Exception as exc:
                logger.error("Failed to load J2534 driver: %s", exc)
                self._notify_status("error", f"Failed to load J2534 driver: {exc}")
                return False
        return True

    async def _close_writer(self) -> None:
        writer = self._active_writer
        if writer is None:
            return

        self._active_writer = None
        try:
            writer.close()
        except Exception:
            return

        wait_closed = getattr(writer, "wait_closed", None)
        if wait_closed is None:
            return

        try:
            await wait_closed()
        except Exception:
            pass

    async def _cancel_prewarm_task(self) -> None:
        prewarm_task = self._prewarm_task
        self._prewarm_task = None
        if prewarm_task is None:
            return

        try:
            prewarm_task.cancel()
        except Exception:
            return

        if isinstance(prewarm_task, asyncio.Future):
            try:
                await prewarm_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

    async def shutdown(self) -> None:
        """Gracefully stop background work and close the active tunnel."""
        self.running = False

        backoff_sleep_task = self._backoff_sleep_task
        self._backoff_sleep_task = None
        if backoff_sleep_task is not None:
            backoff_sleep_task.cancel()

        await self._cancel_prewarm_task()
        await self._close_writer()

        self._prewarm_device_id = None
        self._prewarm_ret = None
        self._ioctl_cache.invalidate()

    async def connect_and_serve(self) -> None:
        """Connect to the reverse server and serve requests until stopped."""
        if not self._ensure_driver():
            self._notify_status("error", "J2534 driver not available")
            return
        if self.config.auth.enabled and not self.config.auth.token:
            self._notify_status("error", "Authentication token required")
            return

        self._loop = asyncio.get_running_loop()
        self.running = True
        backoff_seconds = 5.0
        max_backoff = 60.0

        try:
            while self.running:
                try:
                    self._notify_status("connecting", f"{self.server_host}:{self.server_port}")
                    logger.info("Connecting to %s:%s", self.server_host, self.server_port)

                    reader, writer = await asyncio.open_connection(
                        self.server_host,
                        self.server_port,
                    )
                    self._active_writer = writer

                    sock = writer.get_extra_info("socket")
                    if sock is not None:
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                        if hasattr(socket, "TCP_KEEPIDLE"):
                            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 20)
                        if hasattr(socket, "TCP_KEEPINTVL"):
                            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
                        if hasattr(socket, "TCP_KEEPCNT"):
                            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)

                    logger.info("Connected to reverse server")
                    self._notify_status("connected", f"{self.server_host}:{self.server_port}")
                    backoff_seconds = 5.0

                    if await self._send_registration(reader, writer):
                        self._prewarm_task = asyncio.create_task(self._prewarm_open())
                        await self._handle_requests(reader, writer)
                    else:
                        logger.warning("Authentication failed, reconnecting")

                except ConnectionRefusedError:
                    self._notify_status(
                        "disconnected",
                        f"Connection refused, retrying in {backoff_seconds:.0f}s",
                    )
                    logger.warning(
                        "Connection refused, retrying in %.0fs",
                        backoff_seconds,
                    )
                except Exception as exc:
                    if self.running:
                        self._notify_status(
                            "disconnected",
                            f"Error: {exc}, retrying in {backoff_seconds:.0f}s",
                        )
                    logger.error(
                        "Connection error: %s, retrying in %.0fs",
                        exc,
                        backoff_seconds,
                    )
                finally:
                    await self._cancel_prewarm_task()
                    await self._close_writer()
                    self._prewarm_device_id = None
                    self._prewarm_ret = None
                    self._ioctl_cache.invalidate()

                if self.running:
                    self._backoff_sleep_task = asyncio.create_task(
                        asyncio.sleep(backoff_seconds)
                    )
                    try:
                        await self._backoff_sleep_task
                    except asyncio.CancelledError:
                        pass
                    finally:
                        self._backoff_sleep_task = None
                    backoff_seconds = min(backoff_seconds * 2, max_backoff)
        finally:
            await self.shutdown()
            self._loop = None
            logger.info("Client stopped")

    async def _send_registration(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> bool:
        """Register with the server using auth or the legacy heartbeat path."""
        if self.config.auth.enabled and self.config.auth.token:
            timestamp = int(time.time())
            signature = compute_signature(self.config.auth.token, timestamp)
            msg = ProtocolEncoder.encode_auth_req(timestamp, signature, 0)
            writer.write(msg)
            await writer.drain()
            logger.info("Sent auth request")

            try:
                header = await asyncio.wait_for(
                    reader.readexactly(HEADER_SIZE),
                    timeout=self.config.auth.auth_timeout_s,
                )
                magic, length, msg_type, _sequence = Message.decode_header(header)
                if magic != MAGIC:
                    logger.error("Invalid magic in auth response: %#x", magic)
                    return False

                body_len = length - HEADER_SIZE
                body = await reader.readexactly(body_len) if body_len > 0 else b""

                if msg_type == MsgType.AUTH_RSP:
                    success, message = ProtocolDecoder.decode_auth_rsp(body)
                    if success:
                        logger.info("Authentication succeeded: %s", message)
                    else:
                        logger.error("Authentication failed: %s", message)
                    return success

                if msg_type == MsgType.HEARTBEAT_ACK:
                    logger.info("Server accepted auth as legacy heartbeat")
                    return True

                logger.warning("Unexpected auth response type: %#x", msg_type)
                return False
            except asyncio.TimeoutError:
                logger.error("Auth response timeout")
                return False

        msg = ProtocolEncoder.encode_heartbeat(0)
        writer.write(msg)
        await writer.drain()
        logger.info("Sent registration heartbeat (phase 1)")

        try:
            header = await asyncio.wait_for(reader.readexactly(HEADER_SIZE), timeout=5.0)
            magic, length, msg_type, _sequence = Message.decode_header(header)
            body_len = length - HEADER_SIZE
            if body_len > 0:
                await reader.readexactly(body_len)

            if magic != MAGIC:
                logger.error("Invalid magic in registration ack: %#x", magic)
                return False
            if msg_type not in (MsgType.HEARTBEAT_ACK, MsgType.HEARTBEAT):
                logger.warning("Unexpected registration response: %#x", msg_type)
                return False
        except asyncio.TimeoutError:
            logger.error("Registration ack timeout")
            return False

        msg2 = ProtocolEncoder.encode_heartbeat(1)
        writer.write(msg2)
        await writer.drain()
        logger.info("Sent registration heartbeat (phase 2)")
        return True

    async def _handle_requests(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        while self.running:
            try:
                header = await asyncio.wait_for(reader.readexactly(HEADER_SIZE), timeout=25.0)
                magic, length, msg_type, sequence = Message.decode_header(header)

                if magic != MAGIC:
                    logger.warning("Invalid magic: %#x", magic)
                    break

                body_len = length - HEADER_SIZE
                body = await reader.readexactly(body_len) if body_len > 0 else b""

                t0 = time.monotonic()
                response = await self._handle_message(msg_type, body, sequence)
                hw_ms = (time.monotonic() - t0) * 1000

                if response:
                    response = attach_timing_trailer(response, hw_ms)
                    writer.write(response)
                    await writer.drain()

            except asyncio.TimeoutError:
                writer.write(ProtocolEncoder.encode_heartbeat(0))
                await writer.drain()
            except asyncio.IncompleteReadError:
                logger.info("Server disconnected")
                break
            except Exception as exc:
                logger.error("Request handling error: %s", exc)
                break

    async def _prewarm_open(self) -> None:
        if self.driver is None:
            return

        logger.info("Pre-warm: calling PassThruOpen in background")
        loop = asyncio.get_running_loop()
        try:
            ret, device_id = await loop.run_in_executor(None, self.driver.open, None)
            if ret == 0:
                self._prewarm_device_id = device_id
                self._prewarm_ret = ret
                logger.info("Pre-warm: PassThruOpen OK, device_id=%s", device_id)
            else:
                logger.warning("Pre-warm: PassThruOpen failed, ret=%s", ret)
                self._prewarm_device_id = None
                self._prewarm_ret = None
        except Exception as exc:
            logger.error("Pre-warm: PassThruOpen error: %s", exc)
            self._prewarm_device_id = None
            self._prewarm_ret = None

    async def _handle_open(self, body: bytes, sequence: int) -> bytes:
        device_name = ProtocolDecoder.decode_open_req(body)
        logger.info(">> PassThruOpen(%s)", device_name)

        if self._prewarm_task is not None and not self._prewarm_task.done():
            logger.info("OPEN_REQ arrived while pre-warm was in progress")
            try:
                await self._prewarm_task
            except Exception:
                pass

        if self._prewarm_device_id is not None and self._prewarm_ret == 0:
            device_id = self._prewarm_device_id
            ret = self._prewarm_ret
            self._prewarm_device_id = None
            self._prewarm_ret = None
            self._prewarm_task = None
            logger.info("<< PassThruOpen -> ret=%s, id=%s [pre-warmed]", ret, device_id)
            return ProtocolEncoder.encode_open_rsp(ret, device_id, sequence)

        loop = asyncio.get_running_loop()
        ret, device_id = await loop.run_in_executor(None, self.driver.open, device_name)
        logger.info("<< PassThruOpen -> ret=%s, id=%s", ret, device_id)
        return ProtocolEncoder.encode_open_rsp(ret, device_id, sequence)

    async def _handle_close(self, body: bytes, sequence: int) -> bytes:
        device_id = ProtocolDecoder.decode_close_req(body)
        logger.info(">> PassThruClose(%s)", device_id)
        loop = asyncio.get_running_loop()
        ret = await loop.run_in_executor(None, self.driver.close, device_id)
        logger.info("<< PassThruClose -> ret=%s", ret)
        self._ioctl_cache.invalidate()
        self._prewarm_device_id = None
        self._prewarm_ret = None
        return ProtocolEncoder.encode_close_rsp(ret, sequence)

    async def _handle_connect(self, body: bytes, sequence: int) -> bytes:
        device_id, protocol_id, flags, baudrate = ProtocolDecoder.decode_connect_req(body)
        logger.info(
            ">> PassThruConnect(dev=%s, proto=%s, baud=%s)",
            device_id,
            protocol_id,
            baudrate,
        )
        loop = asyncio.get_running_loop()
        ret, channel_id = await loop.run_in_executor(
            None,
            self.driver.connect,
            device_id,
            protocol_id,
            flags,
            baudrate,
        )
        logger.info("<< PassThruConnect -> ret=%s, ch=%s", ret, channel_id)
        return ProtocolEncoder.encode_connect_rsp(ret, channel_id, sequence)

    async def _handle_disconnect(self, body: bytes, sequence: int) -> bytes:
        channel_id = ProtocolDecoder.decode_disconnect_req(body)
        logger.info(">> PassThruDisconnect(%s)", channel_id)
        loop = asyncio.get_running_loop()
        ret = await loop.run_in_executor(None, self.driver.disconnect, channel_id)
        logger.info("<< PassThruDisconnect -> ret=%s", ret)
        self._ioctl_cache.invalidate()
        return ProtocolEncoder.encode_disconnect_rsp(ret, sequence)

    async def _handle_read_msgs(self, body: bytes, sequence: int) -> bytes:
        channel_id, num_msgs, timeout = ProtocolDecoder.decode_read_msgs_req(body)
        loop = asyncio.get_running_loop()
        ret, messages = await loop.run_in_executor(
            None,
            self.driver.read_msgs,
            channel_id,
            num_msgs,
            timeout,
        )
        if ret != 0x10:
            logger.info("<< ReadMsgs(ch=%s) -> ret=%s, n=%s", channel_id, ret, len(messages))
        return ProtocolEncoder.encode_read_msgs_rsp(ret, messages, sequence)

    async def _handle_write_msgs(self, body: bytes, sequence: int) -> bytes:
        channel_id, messages, timeout = ProtocolDecoder.decode_write_msgs_req(body)
        logger.info(">> WriteMsgs(ch=%s, n=%s, t=%s)", channel_id, len(messages), timeout)
        loop = asyncio.get_running_loop()
        ret, num_written = await loop.run_in_executor(
            None,
            self.driver.write_msgs,
            channel_id,
            messages,
            timeout,
        )
        logger.info("<< WriteMsgs -> ret=%s, written=%s", ret, num_written)
        return ProtocolEncoder.encode_write_msgs_rsp(ret, num_written, sequence)

    async def _handle_read_version(self, body: bytes, sequence: int) -> bytes:
        device_id = ProtocolDecoder.decode_read_version_req(body)
        logger.info(">> PassThruReadVersion(%s)", device_id)
        loop = asyncio.get_running_loop()
        ret, fw, dll, api = await loop.run_in_executor(
            None,
            self.driver.read_version,
            device_id,
        )
        logger.info("<< ReadVersion -> ret=%s", ret)
        return ProtocolEncoder.encode_read_version_rsp(ret, fw, dll, api, sequence)

    async def _handle_start_filter(self, body: bytes, sequence: int) -> bytes:
        channel_id, filter_type, mask_msg, pattern_msg, flow_msg = (
            ProtocolDecoder.decode_start_filter_req(body)
        )
        logger.info(">> StartMsgFilter(ch=%s, type=%s)", channel_id, filter_type)
        loop = asyncio.get_running_loop()
        ret, filter_id = await loop.run_in_executor(
            None,
            self.driver.start_msg_filter,
            channel_id,
            filter_type,
            mask_msg,
            pattern_msg,
            flow_msg,
        )
        logger.info("<< StartMsgFilter -> ret=%s, fid=%s", ret, filter_id)
        return ProtocolEncoder.encode_start_filter_rsp(ret, filter_id, sequence)

    async def _handle_stop_filter(self, body: bytes, sequence: int) -> bytes:
        channel_id, filter_id = ProtocolDecoder.decode_stop_filter_req(body)
        logger.info(">> StopMsgFilter(ch=%s, filter=%s)", channel_id, filter_id)
        loop = asyncio.get_running_loop()
        ret = await loop.run_in_executor(
            None,
            self.driver.stop_msg_filter,
            channel_id,
            filter_id,
        )
        logger.info("<< StopMsgFilter -> ret=%s", ret)
        return ProtocolEncoder.encode_stop_filter_rsp(ret, sequence)

    async def _handle_ioctl(self, body: bytes, sequence: int) -> bytes:
        channel_id, ioctl_id, input_data = ProtocolDecoder.decode_ioctl_req(body)

        cached = self._ioctl_cache.try_get_cached(channel_id, ioctl_id)
        if cached is not None:
            ret, output_data = cached
            logger.debug("<< Ioctl(%#x) -> [cached] ret=%s", ioctl_id, ret)
            return ProtocolEncoder.encode_ioctl_rsp(ret, output_data, sequence)

        logger.info(">> PassThruIoctl(ch=%s, ioctl=%#x)", channel_id, ioctl_id)
        loop = asyncio.get_running_loop()
        ret, output_data = await loop.run_in_executor(
            None,
            self.driver.ioctl,
            channel_id,
            ioctl_id,
            input_data,
        )
        logger.info("<< Ioctl(%#x) -> ret=%s", ioctl_id, ret)
        self._ioctl_cache.record_result(channel_id, ioctl_id, ret, output_data)
        return ProtocolEncoder.encode_ioctl_rsp(ret, output_data, sequence)

    _DISPATCH = {
        MsgType.OPEN_REQ: _handle_open,
        MsgType.CLOSE_REQ: _handle_close,
        MsgType.CONNECT_REQ: _handle_connect,
        MsgType.DISCONNECT_REQ: _handle_disconnect,
        MsgType.READ_MSGS_REQ: _handle_read_msgs,
        MsgType.WRITE_MSGS_REQ: _handle_write_msgs,
        MsgType.READ_VERSION_REQ: _handle_read_version,
        MsgType.START_FILTER_REQ: _handle_start_filter,
        MsgType.STOP_FILTER_REQ: _handle_stop_filter,
        MsgType.IOCTL_REQ: _handle_ioctl,
    }

    async def _handle_message(
        self,
        msg_type: int,
        body: bytes,
        sequence: int,
    ) -> Optional[bytes]:
        if msg_type == MsgType.PING_REQ:
            return ProtocolEncoder.encode_ping_rsp(sequence)
        if msg_type == MsgType.HEARTBEAT:
            return ProtocolEncoder.encode_heartbeat_ack(sequence)
        if msg_type == MsgType.HEARTBEAT_ACK:
            return None

        handler = self._DISPATCH.get(msg_type)
        if handler is None:
            logger.warning("Unknown message type: %#x", msg_type)
            return None
        return await handler(self, body, sequence)

    def stop(self) -> concurrent.futures.Future[None]:
        """Request a graceful shutdown and return a future for completion."""
        loop = self._loop
        if loop is not None and loop.is_running():
            return asyncio.run_coroutine_threadsafe(self.shutdown(), loop)

        self.running = False
        future: concurrent.futures.Future[None] = concurrent.futures.Future()
        future.set_result(None)
        return future


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="VCI Proxy reverse client")
    parser.add_argument(
        "--host",
        default=os.environ.get("VCI_PROXY_HOST", "127.0.0.1"),
        help="Reverse server host (or VCI_PROXY_HOST)",
    )
    parser.add_argument(
        "--port",
        "-p",
        type=int,
        default=9000,
        help="Reverse server port",
    )
    parser.add_argument("--dll", default=None, help="J2534 DLL path")
    parser.add_argument("--auth-token", type=str, default=None, help="PSK auth token")
    parser.add_argument(
        "--no-vbatt-cache",
        action="store_true",
        help="Disable READ_VBATT response cache (legacy)",
    )
    parser.add_argument(
        "--vbatt-ttl",
        type=int,
        default=5,
        help="VBATT cache TTL in seconds",
    )
    parser.add_argument(
        "--no-ioctl-cache",
        action="store_true",
        help="Disable generalized read-only IOCTL cache",
    )
    parser.add_argument(
        "--ioctl-ttl",
        type=int,
        default=5,
        help="IOCTL cache TTL in seconds",
    )
    args = parser.parse_args()

    config = ProxyConfig.from_args(
        auth_token=args.auth_token,
        no_vbatt_cache=args.no_vbatt_cache,
        vbatt_ttl=args.vbatt_ttl,
        no_ioctl_cache=args.no_ioctl_cache,
        ioctl_ttl=args.ioctl_ttl,
    )

    print("=" * 50)
    print("VCI Proxy Reverse Client")
    print("=" * 50)
    print(f"Target server: {args.host}:{args.port}")
    print(f"Auth: {'enabled' if config.auth.enabled else 'disabled'}")
    print(
        f"IOCTL cache: {'enabled' if config.ioctl_cache.enabled else 'disabled'} "
        f"(TTL={config.ioctl_cache.ttl_s}s)"
    )
    print("Press Ctrl+C to stop")
    print("=" * 50)
    print()

    client = ReverseProxyClient(args.host, args.port, args.dll, config)

    try:
        asyncio.run(client.connect_and_serve())
    except KeyboardInterrupt:
        print("\nStopping...")
        client.stop()


if __name__ == "__main__":
    main()
