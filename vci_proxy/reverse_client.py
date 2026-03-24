"""
VCI Proxy 反向连接模式

本地主动连接到阿里云，建立隧道，让云端可以使用本地的 J2534 设备。

架构:
  本地 VCI Proxy ──主动连接──▶ 阿里云:9000
                                   ↑
                              云端程序连接这里
"""

import asyncio
import socket
import struct
import time
import logging
import argparse
import os
from typing import Optional, Callable

from vci_proxy.protocol import MAGIC, HEADER_SIZE, MsgType, MSG_NAMES, Message, ProtocolEncoder, ProtocolDecoder
from vci_proxy.j2534_driver import J2534Driver
from vci_proxy.config import ProxyConfig
from vci_proxy.cache_ioctl import IoctlCache
from vci_proxy.auth import compute_signature
from vci_proxy.benchmark import attach_timing_trailer

# NOTE: logging.basicConfig is intentionally NOT called here.
# When used as a library (imported by client_gui.py), the GUI's main()
# configures logging with both console + file handlers.
# When run standalone (__main__), main() below calls basicConfig.
logger = logging.getLogger(__name__)


class ReverseProxyClient:
    """反向代理客户端 - 主动连接到云服务器"""

    def __init__(self, server_host: str, server_port: int,
                 dll_path: Optional[str] = None,
                 config: Optional[ProxyConfig] = None,
                 on_status_change: Optional[Callable[[str, str], None]] = None):
        self.server_host = server_host
        self.server_port = server_port
        self.dll_path = dll_path
        self.config = config or ProxyConfig()
        self.driver: Optional[J2534Driver] = None
        self.running = False
        self._on_status_change = on_status_change
        # Pre-warm: cached PassThruOpen result
        self._prewarm_device_id: Optional[int] = None
        self._prewarm_ret: Optional[int] = None
        self._prewarm_task: Optional[asyncio.Task] = None
        # P2-2: VBATT cache

    def _notify_status(self, status: str, detail: str = ""):
        """Notify GUI of status changes. status: 'connected'|'connecting'|'disconnected'|'error'"""
        if self._on_status_change:
            try:
                self._on_status_change(status, detail)
            except Exception:
                pass
        self._ioctl_cache = IoctlCache(self.config.ioctl_cache)

    def _ensure_driver(self) -> bool:
        """确保驱动已加载"""
        if self.driver is None:
            try:
                self.driver = J2534Driver(self.dll_path)
                logger.info(f"J2534 驱动已加载: {self.driver.dll_path}")
                return True
            except Exception as e:
                logger.error(f"加载 J2534 驱动失败: {e}")
                self._notify_status('error', f'Failed to load J2534 driver: {e}')
                return False
        return True

    async def connect_and_serve(self):
        """连接到服务器并处理请求（无限重试，指数退避）"""
        if not self._ensure_driver():
            self._notify_status('error', 'J2534 driver not available')
            return

        self.running = True
        backoff_seconds = 5.0
        max_backoff = 60.0

        while self.running:
            try:
                self._notify_status('connecting', f'{self.server_host}:{self.server_port}')
                logger.info(f"正在连接到 {self.server_host}:{self.server_port}...")

                reader, writer = await asyncio.open_connection(
                    self.server_host, self.server_port
                )

                # Set TCP keepalive to prevent NAT timeout
                sock = writer.get_extra_info('socket')
                if sock is not None:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    if hasattr(socket, 'TCP_KEEPIDLE'):
                        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 20)
                    if hasattr(socket, 'TCP_KEEPINTVL'):
                        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
                    if hasattr(socket, 'TCP_KEEPCNT'):
                        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)

                logger.info("已连接到云服务器!")
                self._notify_status('connected', f'{self.server_host}:{self.server_port}')
                backoff_seconds = 5.0  # 连接成功，重置退避

                # 发送注册/认证消息
                if not await self._send_registration(reader, writer):
                    logger.warning("Authentication failed, reconnecting...")
                    writer.close()
                    if self.running:
                        await asyncio.sleep(backoff_seconds)
                        backoff_seconds = min(backoff_seconds * 2, max_backoff)
                    continue

                # Pre-warm: fire PassThruOpen as background task (don't await)
                # so _handle_requests can start immediately and read messages.
                # SM2 takes ~23s; if GDS2 OPEN_REQ arrives before pre-warm
                # completes, _handle_open will wait for the running pre-warm.
                self._prewarm_task = asyncio.ensure_future(self._prewarm_open())

                # 处理请求循环
                await self._handle_requests(reader, writer)

            except ConnectionRefusedError:
                self._notify_status('disconnected', f'Connection refused, retrying in {backoff_seconds:.0f}s')
                logger.warning(f"连接被拒绝，{backoff_seconds:.0f}秒后重试...")
            except Exception as e:
                self._notify_status('disconnected', f'Error: {e}, retrying in {backoff_seconds:.0f}s')
                logger.error(f"连接错误: {e}，{backoff_seconds:.0f}秒后重试...")

            # Invalidate caches on disconnect
            self._ioctl_cache.invalidate()

            if self.running:
                await asyncio.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, max_backoff)

        logger.info("已停止")

    async def _send_registration(self, reader: asyncio.StreamReader,
                                 writer: asyncio.StreamWriter) -> bool:
        """Send registration/authentication message.

        If auth is enabled, sends AUTH_REQ and waits for AUTH_RSP.
        Otherwise sends legacy heartbeat.

        Returns True if registration succeeded, False otherwise.
        """
        if self.config.auth.enabled and self.config.auth.token:
            timestamp = int(time.time())
            signature = compute_signature(self.config.auth.token, timestamp)
            msg = ProtocolEncoder.encode_auth_req(timestamp, signature, 0)
            writer.write(msg)
            await writer.drain()
            logger.info("已发送认证请求")

            # Wait for AUTH_RSP
            try:
                header = await asyncio.wait_for(
                    reader.readexactly(HEADER_SIZE),
                    timeout=self.config.auth.auth_timeout_s,
                )
                magic, length, msg_type, sequence = Message.decode_header(header)
                if magic != MAGIC:
                    logger.error(f"Invalid magic in auth response: {magic:#x}")
                    return False

                body_len = length - HEADER_SIZE
                body = await reader.readexactly(body_len) if body_len > 0 else b''

                if msg_type == MsgType.AUTH_RSP:
                    success, message = ProtocolDecoder.decode_auth_rsp(body)
                    if success:
                        logger.info(f"认证成功: {message}")
                        return True
                    else:
                        logger.error(f"认证失败: {message}")
                        return False
                elif msg_type == MsgType.HEARTBEAT_ACK:
                    # Server doesn't support auth, accepted as legacy
                    logger.info("Server accepted auth as heartbeat (legacy mode)")
                    return True
                else:
                    logger.warning(f"Unexpected auth response type: {msg_type:#x}")
                    return False

            except asyncio.TimeoutError:
                logger.error("Auth response timeout")
                return False
        else:
            # Two-phase heartbeat handshake:
            #   1. Send HEARTBEAT
            #   2. Wait for HEARTBEAT_ACK from server
            #   3. Send a second HEARTBEAT to confirm
            # This prevents port scanners from being accepted as VCI clients.
            msg = ProtocolEncoder.encode_heartbeat(0)
            writer.write(msg)
            await writer.drain()
            logger.info("已发送注册心跳 (phase 1)")

            # Wait for ACK
            try:
                header = await asyncio.wait_for(
                    reader.readexactly(HEADER_SIZE), timeout=5.0
                )
                magic, length, msg_type, sequence = Message.decode_header(header)
                body_len = length - HEADER_SIZE
                if body_len > 0:
                    await reader.readexactly(body_len)  # drain body

                if magic != MAGIC:
                    logger.error(f"Invalid magic in registration ACK: {magic:#x}")
                    return False
                if msg_type not in (MsgType.HEARTBEAT_ACK, MsgType.HEARTBEAT):
                    logger.warning(f"Unexpected registration response: {msg_type:#x}")
                    return False
            except asyncio.TimeoutError:
                logger.error("Registration ACK timeout")
                return False

            # Send confirmation heartbeat (phase 2)
            msg2 = ProtocolEncoder.encode_heartbeat(1)
            writer.write(msg2)
            await writer.drain()
            logger.info("已发送确认心跳 (phase 2) — 注册完成")
            return True

    async def _handle_requests(self, reader: asyncio.StreamReader,
                               writer: asyncio.StreamWriter):
        """处理来自服务器的请求"""
        while self.running:
            try:
                # 读取消息头
                header = await asyncio.wait_for(
                    reader.readexactly(HEADER_SIZE),
                    timeout=25.0  # Reduced from 60s to prevent NAT timeout
                )

                magic, length, msg_type, sequence = Message.decode_header(header)

                if magic != MAGIC:
                    logger.warning(f"无效的 Magic: {magic:#x}")
                    break

                # 读取消息体
                body_len = length - HEADER_SIZE
                body = await reader.readexactly(body_len) if body_len > 0 else b''

                # 处理请求 — measure J2534 execution time
                t0 = time.monotonic()
                response = await self._handle_message(msg_type, body, sequence)
                hw_ms = (time.monotonic() - t0) * 1000

                if response:
                    # Attach timing trailer so the server can separate
                    # hardware execution time from network transit time.
                    response = attach_timing_trailer(response, hw_ms)
                    writer.write(response)
                    await writer.drain()

            except asyncio.TimeoutError:
                # 发送心跳保活
                msg = ProtocolEncoder.encode_heartbeat(0)
                writer.write(msg)
                await writer.drain()
            except asyncio.IncompleteReadError:
                logger.info("服务器断开连接")
                break
            except Exception as e:
                logger.error(f"处理请求错误: {e}")
                break

    # --- Individual message handlers ---

    async def _prewarm_open(self):
        """Pre-warm PassThruOpen in background thread.

        SM2's smj2534.dll takes ~23s for PassThruOpen. By calling it
        proactively after connecting to the cloud server, the device
        handle is ready when GDS2's OPEN_REQ arrives.
        """
        if self.driver is None:
            return
        logger.info("Pre-warm: calling PassThruOpen in background...")
        loop = asyncio.get_running_loop()
        try:
            ret, device_id = await loop.run_in_executor(
                None, self.driver.open, None
            )
            if ret == 0:  # STATUS_NOERROR
                self._prewarm_device_id = device_id
                self._prewarm_ret = ret
                logger.info(f"Pre-warm: PassThruOpen OK, device_id={device_id}")
            else:
                logger.warning(f"Pre-warm: PassThruOpen failed, ret={ret}")
                self._prewarm_device_id = None
                self._prewarm_ret = None
        except Exception as e:
            logger.error(f"Pre-warm: PassThruOpen error: {e}")
            self._prewarm_device_id = None
            self._prewarm_ret = None

    async def _handle_open(self, body: bytes, sequence: int) -> bytes:
        device_name = ProtocolDecoder.decode_open_req(body)
        logger.info(f">> PassThruOpen({device_name})")

        # If pre-warm is still running, wait for it to complete
        if self._prewarm_task is not None and not self._prewarm_task.done():
            logger.info("OPEN_REQ arrived while pre-warm in progress, waiting...")
            try:
                await self._prewarm_task
            except Exception:
                pass  # errors already logged in _prewarm_open

        # Use pre-warmed handle if available
        if self._prewarm_device_id is not None and self._prewarm_ret == 0:
            device_id = self._prewarm_device_id
            ret = self._prewarm_ret
            self._prewarm_device_id = None  # consume it
            self._prewarm_ret = None
            self._prewarm_task = None
            logger.info(f"<< PassThruOpen -> ret={ret}, id={device_id} [PRE-WARMED]")
            return ProtocolEncoder.encode_open_rsp(ret, device_id, sequence)

        # Fallback: call PassThruOpen normally
        loop = asyncio.get_running_loop()
        ret, device_id = await loop.run_in_executor(
            None, self.driver.open, device_name
        )
        logger.info(f"<< PassThruOpen -> ret={ret}, id={device_id}")
        return ProtocolEncoder.encode_open_rsp(ret, device_id, sequence)

    async def _handle_close(self, body: bytes, sequence: int) -> bytes:
        device_id = ProtocolDecoder.decode_close_req(body)
        logger.info(f">> PassThruClose({device_id})")
        loop = asyncio.get_running_loop()
        ret = await loop.run_in_executor(None, self.driver.close, device_id)
        logger.info(f"<< PassThruClose -> ret={ret}")
        self._ioctl_cache.invalidate()
        # Invalidate pre-warm cache on close
        self._prewarm_device_id = None
        self._prewarm_ret = None
        return ProtocolEncoder.encode_close_rsp(ret, sequence)

    async def _handle_connect(self, body: bytes, sequence: int) -> bytes:
        device_id, protocol_id, flags, baudrate = ProtocolDecoder.decode_connect_req(body)
        logger.info(f">> PassThruConnect(dev={device_id}, proto={protocol_id}, baud={baudrate})")
        loop = asyncio.get_running_loop()
        ret, channel_id = await loop.run_in_executor(
            None, self.driver.connect, device_id, protocol_id, flags, baudrate
        )
        logger.info(f"<< PassThruConnect -> ret={ret}, ch={channel_id}")
        return ProtocolEncoder.encode_connect_rsp(ret, channel_id, sequence)

    async def _handle_disconnect(self, body: bytes, sequence: int) -> bytes:
        channel_id = ProtocolDecoder.decode_disconnect_req(body)
        logger.info(f">> PassThruDisconnect({channel_id})")
        loop = asyncio.get_running_loop()
        ret = await loop.run_in_executor(None, self.driver.disconnect, channel_id)
        logger.info(f"<< PassThruDisconnect -> ret={ret}")
        self._ioctl_cache.invalidate()
        return ProtocolEncoder.encode_disconnect_rsp(ret, sequence)

    async def _handle_read_msgs(self, body: bytes, sequence: int) -> bytes:
        channel_id, num_msgs, timeout = ProtocolDecoder.decode_read_msgs_req(body)
        loop = asyncio.get_running_loop()
        ret, messages = await loop.run_in_executor(
            None, self.driver.read_msgs, channel_id, num_msgs, timeout
        )
        if ret != 0x10:  # Skip BUFFER_EMPTY noise
            logger.info(f"<< ReadMsgs(ch={channel_id}) -> ret={ret}, n={len(messages)}")
        return ProtocolEncoder.encode_read_msgs_rsp(ret, messages, sequence)

    async def _handle_write_msgs(self, body: bytes, sequence: int) -> bytes:
        channel_id, messages, timeout = ProtocolDecoder.decode_write_msgs_req(body)
        logger.info(f">> WriteMsgs(ch={channel_id}, n={len(messages)}, t={timeout})")
        loop = asyncio.get_running_loop()
        ret, num_written = await loop.run_in_executor(
            None, self.driver.write_msgs, channel_id, messages, timeout
        )
        logger.info(f"<< WriteMsgs -> ret={ret}, written={num_written}")
        return ProtocolEncoder.encode_write_msgs_rsp(ret, num_written, sequence)

    async def _handle_read_version(self, body: bytes, sequence: int) -> bytes:
        device_id = ProtocolDecoder.decode_read_version_req(body)
        logger.info(f">> PassThruReadVersion({device_id})")
        loop = asyncio.get_running_loop()
        ret, fw, dll, api = await loop.run_in_executor(
            None, self.driver.read_version, device_id
        )
        logger.info(f"<< ReadVersion -> ret={ret}")
        return ProtocolEncoder.encode_read_version_rsp(ret, fw, dll, api, sequence)

    async def _handle_start_filter(self, body: bytes, sequence: int) -> bytes:
        channel_id, filter_type, mask_msg, pattern_msg, flow_msg = \
            ProtocolDecoder.decode_start_filter_req(body)
        logger.info(f">> StartMsgFilter(ch={channel_id}, type={filter_type})")
        loop = asyncio.get_running_loop()
        ret, filter_id = await loop.run_in_executor(
            None, self.driver.start_msg_filter,
            channel_id, filter_type, mask_msg, pattern_msg, flow_msg
        )
        logger.info(f"<< StartMsgFilter -> ret={ret}, fid={filter_id}")
        return ProtocolEncoder.encode_start_filter_rsp(ret, filter_id, sequence)

    async def _handle_stop_filter(self, body: bytes, sequence: int) -> bytes:
        channel_id, filter_id = ProtocolDecoder.decode_stop_filter_req(body)
        logger.info(f">> StopMsgFilter(ch={channel_id}, filter={filter_id})")
        loop = asyncio.get_running_loop()
        ret = await loop.run_in_executor(
            None, self.driver.stop_msg_filter, channel_id, filter_id
        )
        logger.info(f"<< StopMsgFilter -> ret={ret}")
        return ProtocolEncoder.encode_stop_filter_rsp(ret, sequence)

    async def _handle_ioctl(self, body: bytes, sequence: int) -> bytes:
        channel_id, ioctl_id, input_data = ProtocolDecoder.decode_ioctl_req(body)

        cached = self._ioctl_cache.try_get_cached(channel_id, ioctl_id)
        if cached is not None:
            ret, output_data = cached
            logger.debug(f"<< Ioctl(0x{ioctl_id:02x}) -> [CACHED] ret={ret}")
            return ProtocolEncoder.encode_ioctl_rsp(ret, output_data, sequence)

        logger.info(f">> PassThruIoctl(ch={channel_id}, ioctl=0x{ioctl_id:02x})")
        loop = asyncio.get_running_loop()
        ret, output_data = await loop.run_in_executor(
            None, self.driver.ioctl, channel_id, ioctl_id, input_data
        )
        logger.info(f"<< Ioctl(0x{ioctl_id:02x}) -> ret={ret}")

        self._ioctl_cache.record_result(channel_id, ioctl_id, ret, output_data)
        return ProtocolEncoder.encode_ioctl_rsp(ret, output_data, sequence)

    # Dispatch table: msg_type -> handler method
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

    async def _handle_message(self, msg_type: int, body: bytes,
                             sequence: int) -> Optional[bytes]:
        """Dispatch message to the appropriate handler."""
        if msg_type == MsgType.HEARTBEAT:
            return ProtocolEncoder.encode_heartbeat_ack(sequence)
        if msg_type == MsgType.HEARTBEAT_ACK:
            return None

        handler = self._DISPATCH.get(msg_type)
        if handler is None:
            logger.warning(f"未知消息类型: {msg_type:#x}")
            return None
        return await handler(self, body, sequence)

    def stop(self):
        """停止客户端"""
        self.running = False


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s',
        datefmt='%H:%M:%S'
    )
    parser = argparse.ArgumentParser(description='VCI Proxy 反向连接客户端')
    parser.add_argument('--host', default=os.environ.get('VCI_PROXY_HOST', '127.0.0.1'),
                       help='云服务器地址 (或设置 VCI_PROXY_HOST 环境变量)')
    parser.add_argument('--port', '-p', type=int, default=9000, help='云服务器端口')
    parser.add_argument('--dll', default=None, help='J2534 DLL 路径')
    parser.add_argument('--auth-token', type=str, default=None,
                       help='PSK authentication token')
    parser.add_argument('--no-vbatt-cache', action='store_true',
                       help='Disable READ_VBATT response cache (legacy)')
    parser.add_argument('--vbatt-ttl', type=int, default=5,
                       help='VBATT cache TTL in seconds (默认: 5)')
    parser.add_argument('--no-ioctl-cache', action='store_true',
                       help='Disable generalized read-only IOCTL cache')
    parser.add_argument('--ioctl-ttl', type=int, default=5,
                       help='IOCTL cache TTL in seconds (默认: 5)')
    args = parser.parse_args()

    config = ProxyConfig.from_args(
        auth_token=args.auth_token,
        no_vbatt_cache=args.no_vbatt_cache,
        vbatt_ttl=args.vbatt_ttl,
        no_ioctl_cache=args.no_ioctl_cache,
        ioctl_ttl=args.ioctl_ttl,
    )

    print("=" * 50)
    print("VCI Proxy 反向连接模式")
    print("=" * 50)
    print(f"目标服务器: {args.host}:{args.port}")
    print(f"Auth: {'enabled' if config.auth.enabled else 'disabled'}")
    print(f"IOCTL cache: {'enabled' if config.ioctl_cache.enabled else 'disabled'}"
          f" (TTL={config.ioctl_cache.ttl_s}s)")
    print("按 Ctrl+C 停止")
    print("=" * 50)
    print()

    client = ReverseProxyClient(args.host, args.port, args.dll, config)

    try:
        asyncio.run(client.connect_and_serve())
    except KeyboardInterrupt:
        print("\n正在停止...")
        client.stop()


if __name__ == '__main__':
    main()
