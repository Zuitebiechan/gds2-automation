"""
VCI Proxy 反向连接服务器（运行在阿里云）

接受本地 VCI Proxy 的反向连接，并提供本地代理服务。

架构:
  本地 VCI Proxy ──连接──▶ 此服务器 (端口 9000)
                              ↑
                         本地测试程序连接 localhost:9001
"""

import asyncio
import struct
import time
import logging
import argparse
from typing import Optional

from .config import ProxyConfig
from .cache_read_msgs import ReadMsgsCache
from .cache_filter_dedup import FilterDeduplicationCache
from .cache_vbatt import VbattCache
from .auth import verify_signature
from .protocol import MAGIC, HEADER_SIZE, MsgType, MSG_NAMES, ProtocolDecoder, ProtocolEncoder

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


class ReverseProxyServer:
    """反向代理服务器 - 接受 VCI Proxy 的连接"""

    def __init__(self, listen_port: int = 9000, proxy_port: int = 9001,
                 config: Optional[ProxyConfig] = None):
        self.listen_port = listen_port  # VCI Proxy 连接的端口
        self.proxy_port = proxy_port    # 本地程序连接的端口
        self.config = config or ProxyConfig()
        self.vci_reader: Optional[asyncio.StreamReader] = None
        self.vci_writer: Optional[asyncio.StreamWriter] = None
        self.vci_connected = asyncio.Event()
        self.vci_lock = asyncio.Lock()  # 保护 VCI 写操作
        self.request_queue = asyncio.Queue()
        self.response_futures: dict[int, asyncio.Future] = {}
        self.sequence = 0

        # P1-1: ReadMsgs BUFFER_EMPTY cache
        self._read_cache = ReadMsgsCache(self.config.read_msgs_cache)
        # P2-1: Filter deduplication cache
        self._filter_cache = FilterDeduplicationCache(self.config.filter_dedup)
        # P2-2: VBATT cache (server-side)
        self._vbatt_cache = VbattCache(self.config.vbatt_cache)

    async def start(self):
        """启动服务器"""
        # 启动 VCI 监听服务
        vci_server = await asyncio.start_server(
            self._handle_vci_connection, '0.0.0.0', self.listen_port
        )
        logger.info(f"等待 VCI Proxy 连接到端口 {self.listen_port}...")

        # 启动代理服务
        proxy_server = await asyncio.start_server(
            self._handle_proxy_connection, '127.0.0.1', self.proxy_port
        )
        logger.info(f"代理服务监听端口 {self.proxy_port}")

        if self.config.auth.enabled:
            logger.info("PSK authentication enabled")
        if self.config.read_msgs_cache.enabled:
            logger.info(
                f"ReadMsgs cache enabled (TTL={self.config.read_msgs_cache.ttl_ms}ms)"
            )
        if self.config.filter_dedup.enabled:
            logger.info("Filter deduplication enabled")
        if self.config.vbatt_cache.enabled:
            logger.info(
                f"VBATT cache enabled (TTL={self.config.vbatt_cache.ttl_s}s)"
            )

        print(f"\n等待本地 VCI Proxy 连接到端口 {self.listen_port}...")
        print(f"连接后，可以通过 localhost:{self.proxy_port} 访问 J2534 设备\n")

        await asyncio.gather(
            vci_server.serve_forever(),
            proxy_server.serve_forever()
        )

    def _cancel_pending_futures(self):
        """取消所有挂起的 Future（VCI 断开时调用）"""
        pending = list(self.response_futures.items())
        self.response_futures.clear()
        self._read_cache.clear()
        self._filter_cache.clear()
        self._vbatt_cache.invalidate()
        for seq, future in pending:
            if not future.done():
                future.set_exception(ConnectionError("VCI Proxy 已断开"))
                logger.debug(f"取消挂起的 Future: seq={seq}")
        if pending:
            logger.info(f"已取消 {len(pending)} 个挂起的请求")

    async def _authenticate_vci(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> bool:
        """Authenticate the VCI connection.

        Reads the first message from the VCI client. If auth is enabled,
        expects AUTH_REQ with valid HMAC. If auth is disabled, accepts
        either AUTH_REQ or legacy HEARTBEAT with two-phase handshake.

        The two-phase handshake for legacy heartbeat registration works as:
          1. Client sends HEARTBEAT
          2. Server replies HEARTBEAT_ACK
          3. Client must send a second HEARTBEAT within 5s
        This prevents port scanners (which send random bytes) from
        accidentally registering as a VCI client.

        Returns True if authenticated, False otherwise.
        """
        try:
            header = await asyncio.wait_for(
                reader.readexactly(HEADER_SIZE),
                timeout=self.config.auth.auth_timeout_s,
            )
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            logger.warning("VCI client did not send registration message in time")
            return False

        magic, length, msg_type, sequence = struct.unpack('>IIHI', header)
        if magic != MAGIC:
            logger.warning(f"Invalid magic during auth: {magic:#x}")
            return False

        body_len = length - HEADER_SIZE
        body = await reader.readexactly(body_len) if body_len > 0 else b''

        if msg_type == MsgType.AUTH_REQ:
            if not self.config.auth.enabled:
                # Auth not required, but client sent AUTH_REQ -- accept it
                logger.info("Auth not required, accepting AUTH_REQ")
                rsp = ProtocolEncoder.encode_auth_rsp(True, "ok", sequence)
                writer.write(rsp)
                await writer.drain()
                return True

            timestamp, signature = ProtocolDecoder.decode_auth_req(body)
            success, reason = verify_signature(
                self.config.auth.token, timestamp, signature
            )
            rsp = ProtocolEncoder.encode_auth_rsp(success, reason, sequence)
            writer.write(rsp)
            await writer.drain()

            if success:
                logger.info("VCI client authenticated successfully")
            else:
                logger.warning(f"VCI client authentication failed: {reason}")
            return success

        if msg_type == MsgType.HEARTBEAT:
            if self.config.auth.enabled:
                logger.warning(
                    "Auth required but VCI client sent HEARTBEAT (legacy client)"
                )
                return False

            # Phase 1: reply ACK to the first heartbeat
            ack_header = struct.pack(
                '>IIHI', MAGIC, HEADER_SIZE, MsgType.HEARTBEAT_ACK, sequence
            )
            writer.write(ack_header)
            await writer.drain()

            # Phase 2: require a second heartbeat within 5 seconds.
            # Real VCI clients will respond; port scanners won't.
            try:
                header2 = await asyncio.wait_for(
                    reader.readexactly(HEADER_SIZE), timeout=5.0
                )
            except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                logger.warning(
                    "VCI client did not complete two-phase handshake (no 2nd heartbeat)"
                )
                return False

            magic2, length2, msg_type2, seq2 = struct.unpack('>IIHI', header2)
            if magic2 != MAGIC:
                logger.warning(f"Invalid magic in handshake phase 2: {magic2:#x}")
                return False

            body2_len = length2 - HEADER_SIZE
            if body2_len > 0:
                await reader.readexactly(body2_len)  # drain body

            if msg_type2 not in (MsgType.HEARTBEAT, MsgType.HEARTBEAT_ACK):
                logger.warning(
                    f"Unexpected message in handshake phase 2: {msg_type2:#x}"
                )
                return False

            # Phase 2 ACK
            ack2 = struct.pack(
                '>IIHI', MAGIC, HEADER_SIZE, MsgType.HEARTBEAT_ACK, seq2
            )
            writer.write(ack2)
            await writer.drain()

            logger.info("VCI client registered via two-phase heartbeat handshake")
            return True

        logger.warning(f"Unexpected first message type during auth: {msg_type:#x}")
        return False

    async def _handle_vci_connection(self, reader: asyncio.StreamReader,
                                     writer: asyncio.StreamWriter):
        """处理 VCI Proxy 的连接"""
        addr = writer.get_extra_info('peername')
        logger.info(f"VCI Proxy 已连接: {addr}")

        # Authenticate before accepting the connection
        if not await self._authenticate_vci(reader, writer):
            logger.warning(f"VCI Proxy authentication failed, closing: {addr}")
            writer.close()
            return

        # 关闭已有的 VCI 连接
        if self.vci_writer is not None:
            logger.warning(f"替换已有 VCI 连接，新连接: {addr}")
            old_writer = self.vci_writer
            self.vci_connected.clear()
            self._cancel_pending_futures()
            self.vci_reader = None
            self.vci_writer = None
            try:
                old_writer.close()
            except Exception:
                pass

        print(f"\n*** VCI Proxy 已连接: {addr} ***\n")

        self.vci_reader = reader
        self.vci_writer = writer
        self.vci_connected.set()

        try:
            while True:
                # 读取消息
                header = await reader.readexactly(HEADER_SIZE)
                magic, length, msg_type, sequence = struct.unpack('>IIHI', header)

                if magic != MAGIC:
                    logger.warning(f"无效的 Magic: {magic:#x}")
                    break

                body_len = length - HEADER_SIZE
                body = await reader.readexactly(body_len) if body_len > 0 else b''

                # 处理心跳消息 - 发送 ACK
                if msg_type == MsgType.HEARTBEAT:
                    logger.debug(f"收到心跳, seq={sequence}")
                    ack_header = struct.pack('>IIHI', MAGIC, HEADER_SIZE, MsgType.HEARTBEAT_ACK, sequence)
                    async with self.vci_lock:
                        writer.write(ack_header)
                        await writer.drain()
                    continue

                # 心跳 ACK - 忽略
                if msg_type == MsgType.HEARTBEAT_ACK:
                    logger.debug(f"收到心跳ACK, seq={sequence}")
                    continue

                # 查找对应的 Future（响应消息）
                if sequence in self.response_futures:
                    future = self.response_futures.pop(sequence)
                    future.set_result((msg_type, body))
                else:
                    logger.warning(f"收到未知消息: type={msg_type:#x}, seq={sequence}")

        except asyncio.IncompleteReadError:
            logger.info(f"VCI Proxy 断开连接: {addr}")
        except Exception as e:
            logger.error(f"VCI 连接错误: {e}")
        finally:
            self.vci_connected.clear()
            self._cancel_pending_futures()
            self.vci_reader = None
            self.vci_writer = None
            writer.close()
            print(f"\n*** VCI Proxy 已断开 ***\n")

    def _try_serve_cached(self, msg_type: int, body: bytes,
                          sequence: int) -> tuple[Optional[bytes], Optional[int]]:
        """Try to serve the request from cache.

        Returns (cached_response, ioctl_id).
        cached_response is None if cache miss.
        ioctl_id is set when msg_type is IOCTL_REQ (needed for recording later).
        """
        ioctl_id = None

        if msg_type == MsgType.READ_MSGS_REQ:
            channel_id, num_msgs, timeout = ProtocolDecoder.decode_read_msgs_req(body)
            cached = self._read_cache.try_serve_from_cache(
                channel_id, num_msgs, timeout, sequence
            )
            if cached is not None:
                return cached, ioctl_id

        elif msg_type == MsgType.START_FILTER_REQ:
            cached = self._filter_cache.try_dedup(body, sequence)
            if cached is not None:
                return cached, ioctl_id

        elif msg_type == MsgType.IOCTL_REQ:
            _ch, ioctl_id, _input = ProtocolDecoder.decode_ioctl_req(body)
            cached = self._vbatt_cache.try_get_cached(ioctl_id)
            if cached is not None:
                ret, output_data = cached
                resp = ProtocolEncoder.encode_ioctl_rsp(ret, output_data, sequence)
                return resp, ioctl_id

        return None, ioctl_id

    def _invalidate_caches(self, msg_type: int, body: bytes):
        """Invalidate caches based on the request type."""
        if msg_type == MsgType.DISCONNECT_REQ:
            channel_id = ProtocolDecoder.decode_disconnect_req(body)
            self._read_cache.invalidate_channel(channel_id)
            self._filter_cache.invalidate_channel(channel_id)
            self._vbatt_cache.invalidate()
        elif msg_type == MsgType.CLOSE_REQ:
            self._read_cache.clear()
            self._filter_cache.clear()
            self._vbatt_cache.invalidate()
        elif msg_type == MsgType.STOP_FILTER_REQ:
            _ch_id, filter_id = ProtocolDecoder.decode_stop_filter_req(body)
            self._filter_cache.on_stop_filter(filter_id)

    def _record_in_caches(self, msg_type: int, body: bytes,
                          resp_type: int, resp_body: bytes,
                          ioctl_id: Optional[int]):
        """Record response in caches for future lookups."""
        if msg_type == MsgType.READ_MSGS_REQ:
            channel_id = struct.unpack('>I', body[:4])[0]
            return_code = struct.unpack('>I', resp_body[:4])[0]
            self._read_cache.record_result(channel_id, return_code)

        elif msg_type == MsgType.START_FILTER_REQ and resp_type == MsgType.START_FILTER_RSP:
            return_code, filter_id = ProtocolDecoder.decode_start_filter_rsp(resp_body)
            self._filter_cache.record_result(body, filter_id, return_code, resp_body)

        elif msg_type == MsgType.IOCTL_REQ and ioctl_id is not None and resp_type == MsgType.IOCTL_RSP:
            ret, output_data = ProtocolDecoder.decode_ioctl_rsp(resp_body)
            self._vbatt_cache.record_result(ioctl_id, ret, output_data)

    async def _handle_proxy_connection(self, reader: asyncio.StreamReader,
                                       writer: asyncio.StreamWriter):
        """处理本地代理连接"""
        addr = writer.get_extra_info('peername')
        logger.info(f"代理客户端连接: {addr}")

        try:
            while True:
                # 等待 VCI 连接
                if not self.vci_connected.is_set():
                    logger.info("等待 VCI Proxy 连接...")
                    await self.vci_connected.wait()

                if self.vci_writer is None:
                    logger.error("VCI writer 无效")
                    break

                # 读取请求
                header = await reader.readexactly(HEADER_SIZE)
                magic, length, msg_type, sequence = struct.unpack('>IIHI', header)

                if magic != MAGIC:
                    logger.warning(f"无效的 Magic: {magic:#x}")
                    break

                body_len = length - HEADER_SIZE
                body = await reader.readexactly(body_len) if body_len > 0 else b''

                msg_name = MSG_NAMES.get(msg_type, f"0x{msg_type:04x}")

                # Try serving from cache
                cached, ioctl_id = self._try_serve_cached(msg_type, body, sequence)
                if cached is not None:
                    writer.write(cached)
                    await writer.drain()
                    continue

                # Invalidate caches as needed
                self._invalidate_caches(msg_type, body)

                # 转发请求到 VCI Proxy
                self.sequence = (self.sequence + 1) & 0xFFFFFFFF
                new_seq = self.sequence
                fwd_start = time.monotonic()

                future = asyncio.get_running_loop().create_future()
                self.response_futures[new_seq] = future

                try:
                    new_header = struct.pack('>IIHI', MAGIC, length, msg_type, new_seq)
                    async with self.vci_lock:
                        if self.vci_writer is None:
                            raise ConnectionError("VCI 连接已断开")
                        self.vci_writer.write(new_header + body)
                        await self.vci_writer.drain()
                except Exception as e:
                    logger.error(f"转发请求失败: {e}")
                    self.response_futures.pop(new_seq, None)
                    break

                # 等待响应
                try:
                    resp_type, resp_body = await asyncio.wait_for(future, timeout=30.0)
                    fwd_ms = (time.monotonic() - fwd_start) * 1000

                    self._record_in_caches(msg_type, body, resp_type, resp_body, ioctl_id)

                    # 发送响应给客户端（使用原始 sequence）
                    resp_header = struct.pack('>IIHI', MAGIC,
                                             HEADER_SIZE + len(resp_body),
                                             resp_type, sequence)
                    writer.write(resp_header + resp_body)
                    await writer.drain()

                    if msg_type != MsgType.READ_MSGS_REQ or fwd_ms > 200:
                        logger.info(f"[PROXY] {msg_name} seq={sequence} -> {fwd_ms:.1f}ms")

                except (asyncio.TimeoutError, ConnectionError) as e:
                    self.response_futures.pop(new_seq, None)
                    fwd_ms = (time.monotonic() - fwd_start) * 1000
                    reason = "VCI_DISCONNECTED" if isinstance(e, ConnectionError) else "TIMEOUT"
                    logger.error(f"[PROXY] {msg_name} seq={sequence} {reason} after {fwd_ms:.0f}ms")
                    break

        except asyncio.IncompleteReadError:
            logger.info(f"代理客户端断开: {addr}")
        except Exception as e:
            logger.error(f"代理连接错误: {e}")
        finally:
            writer.close()


def main():
    parser = argparse.ArgumentParser(description='VCI Proxy 反向连接服务器')
    parser.add_argument('--listen-port', type=int, default=9000,
                       help='VCI Proxy 连接端口 (默认: 9000)')
    parser.add_argument('--proxy-port', type=int, default=9001,
                       help='本地代理端口 (默认: 9001)')
    parser.add_argument('--auth-token', type=str, default=None,
                       help='PSK authentication token')
    parser.add_argument('--no-read-cache', action='store_true',
                       help='Disable ReadMsgs BUFFER_EMPTY cache')
    parser.add_argument('--read-cache-ttl', type=int, default=50,
                       help='ReadMsgs cache TTL in ms (默认: 50)')
    parser.add_argument('--no-filter-dedup', action='store_true',
                       help='Disable StartFilter deduplication')
    parser.add_argument('--no-vbatt-cache', action='store_true',
                       help='Disable READ_VBATT response cache')
    parser.add_argument('--vbatt-ttl', type=int, default=5,
                       help='VBATT cache TTL in seconds (默认: 5)')
    args = parser.parse_args()

    config = ProxyConfig.from_args(
        auth_token=args.auth_token,
        no_read_cache=args.no_read_cache,
        read_cache_ttl=args.read_cache_ttl,
        no_filter_dedup=args.no_filter_dedup,
        no_vbatt_cache=args.no_vbatt_cache,
        vbatt_ttl=args.vbatt_ttl,
    )

    print("=" * 50)
    print("VCI Proxy 反向连接服务器")
    print("=" * 50)
    print(f"VCI Proxy 连接端口: {args.listen_port}")
    print(f"本地代理端口: {args.proxy_port}")
    print(f"Auth: {'enabled' if config.auth.enabled else 'disabled'}")
    print(f"ReadMsgs cache: {'enabled' if config.read_msgs_cache.enabled else 'disabled'}"
          f" (TTL={config.read_msgs_cache.ttl_ms}ms)")
    print(f"Filter dedup: {'enabled' if config.filter_dedup.enabled else 'disabled'}")
    print(f"VBATT cache: {'enabled' if config.vbatt_cache.enabled else 'disabled'}"
          f" (TTL={config.vbatt_cache.ttl_s}s)")
    print("=" * 50)

    server = ReverseProxyServer(args.listen_port, args.proxy_port, config)

    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        print("\n停止服务器...")


if __name__ == '__main__':
    main()
