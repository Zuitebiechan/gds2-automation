"""
VCI Proxy 反向连接服务器（运行在阿里云）

接受本地 VCI Proxy 的反向连接，并提供本地代理服务。

架构:
  本地 VCI Proxy ──连接──▶ 此服务器 (端口 9000)
                              ↑
                         本地测试程序连接 localhost:9001
"""

import asyncio
import socket
import struct
import time
import logging
import argparse
import ssl
from typing import Optional

from .config import ProxyConfig
from .cache_read_msgs import ReadMsgsCache
from .cache_filter_dedup import FilterDeduplicationCache
from .cache_ioctl import IoctlCache
from .auth import MAX_DRIFT_S, verify_signature
from .benchmark import (
    JsonlBenchmarkWriter,
    decode_benchmark_response,
    make_proxy_benchmark_event,
    strip_timing_trailer,
)
from .protocol import MAGIC, HEADER_SIZE, MsgType, MSG_NAMES, ProtocolDecoder, ProtocolEncoder
from .tls_utils import harden_tls_context
from .tunnel_quality import (
    TunnelQualityTracker,
    write_tunnel_quality_snapshot,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

MAX_FRAME_BODY_BYTES = 1_000_000
DEFAULT_FRAME_BODY_READ_TIMEOUT_S = 10.0


def _validated_body_length(length: int) -> int:
    if length < HEADER_SIZE:
        raise ValueError(f"Invalid frame length: {length} < {HEADER_SIZE}")

    body_len = length - HEADER_SIZE
    if body_len > MAX_FRAME_BODY_BYTES:
        raise ValueError(
            f"Frame body too large: {body_len} > {MAX_FRAME_BODY_BYTES}"
        )
    return body_len


async def _read_frame_body(
    reader: asyncio.StreamReader,
    length: int,
    *,
    timeout: float = DEFAULT_FRAME_BODY_READ_TIMEOUT_S,
) -> bytes:
    body_len = _validated_body_length(length)
    if body_len <= 0:
        return b""

    try:
        return await asyncio.wait_for(reader.readexactly(body_len), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise TimeoutError(f"Timed out reading frame body ({body_len} bytes)") from exc
    except asyncio.IncompleteReadError as exc:
        raise ConnectionError(
            f"Incomplete frame body ({len(exc.partial)} of {body_len} bytes)"
        ) from exc


class ReverseProxyServer:
    """反向代理服务器 - 接受 VCI Proxy 的连接"""

    def __init__(self, listen_port: int = 9000, proxy_port: int = 9001,
                 config: Optional[ProxyConfig] = None,
                 benchmark_writer: Optional[JsonlBenchmarkWriter] = None,
                 benchmark_label: str = "proxy_run"):
        self.listen_port = listen_port  # VCI Proxy 连接的端口
        self.proxy_port = proxy_port    # 本地程序连接的端口
        self.config = config or ProxyConfig()
        self.benchmark_writer = benchmark_writer
        self.benchmark_label = benchmark_label
        self.vci_reader: Optional[asyncio.StreamReader] = None
        self.vci_writer: Optional[asyncio.StreamWriter] = None
        self.vci_connected = asyncio.Event()
        self.vci_lock = asyncio.Lock()  # 保护 VCI 写操作
        self.request_queue = asyncio.Queue()
        self.response_futures: dict[int, asyncio.Future] = {}
        self.sequence = 0
        self._probe_task: asyncio.Task | None = None
        self._connection_counter = 0
        self._connection_epoch: str | None = None
        self._tunnel_quality = TunnelQualityTracker()
        self._last_quality_signature: tuple | None = None
        self._seen_auth_signatures: dict[tuple[int, bytes], int] = {}

        # P1-1: ReadMsgs BUFFER_EMPTY cache
        self._read_cache = ReadMsgsCache(self.config.read_msgs_cache)
        # P2-1: Filter deduplication cache
        self._filter_cache = FilterDeduplicationCache(self.config.filter_dedup)
        # Generalized read-only IOCTL cache (replaces VBATT-only cache)
        self._ioctl_cache = IoctlCache(self.config.ioctl_cache)

    def _build_tls_server_context(self) -> ssl.SSLContext | None:
        """Build optional TLS listener context for inbound reverse clients."""
        if not self.config.tls.enabled:
            return None

        certfile = self.config.tls.certfile
        keyfile = self.config.tls.keyfile
        if not certfile or not keyfile:
            raise ValueError("TLS certfile and keyfile are required")

        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(certfile=certfile, keyfile=keyfile)
        harden_tls_context(context)

        if self.config.tls.require_client_cert:
            if not self.config.tls.ca_file:
                raise ValueError("TLS CA file is required when client certificates are enforced")
            context.load_verify_locations(cafile=self.config.tls.ca_file)
            context.verify_mode = ssl.CERT_REQUIRED

        return context

    async def start(self):
        """启动服务器"""
        # 启动 VCI 监听服务
        tls_context = self._build_tls_server_context()
        vci_server = await asyncio.start_server(
            self._handle_vci_connection, '0.0.0.0', self.listen_port, ssl=tls_context
        )
        logger.info(f"等待 VCI Proxy 连接到端口 {self.listen_port}...")

        # 启动代理服务
        proxy_server = await asyncio.start_server(
            self._handle_proxy_connection, '127.0.0.1', self.proxy_port
        )
        logger.info(f"代理服务监听端口 {self.proxy_port}")

        if self.config.auth.enabled:
            logger.info("PSK authentication enabled")
        if self.config.tls.enabled:
            logger.info("Reverse tunnel TLS enabled")
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
        if self.config.ioctl_cache.enabled:
            logger.info(
                f"IOCTL cache enabled (TTL={self.config.ioctl_cache.ttl_s}s, "
                f"covers GET_CONFIG/READ_VBATT/READ_PROG_VOLTAGE)"
            )
        if self.benchmark_writer is not None:
            logger.info("Proxy benchmark logging enabled: %s", self.benchmark_writer.path)

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
        self._ioctl_cache.invalidate()
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

        try:
            body = await _read_frame_body(
                reader,
                length,
                timeout=float(self.config.auth.auth_timeout_s),
            )
        except (ValueError, TimeoutError, ConnectionError) as exc:
            logger.warning("Invalid auth frame from VCI client: %s", exc)
            return False

        if msg_type == MsgType.AUTH_REQ:
            if self.config.auth.enabled and not self.config.auth.token:
                logger.error("Auth enabled but no server token is configured")
                rsp = ProtocolEncoder.encode_auth_rsp(False, "server auth token not configured", sequence)
                writer.write(rsp)
                await writer.drain()
                return False
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
            if success and self._is_replayed_auth(signature, timestamp):
                success = False
                reason = "replay detected"
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

            try:
                await _read_frame_body(reader, length2, timeout=5.0)
            except (ValueError, TimeoutError, ConnectionError) as exc:
                logger.warning("Invalid handshake frame in phase 2: %s", exc)
                return False

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

    def _is_replayed_auth(self, signature: bytes, timestamp: int) -> bool:
        now = int(time.time())
        stale_before = now - MAX_DRIFT_S
        self._seen_auth_signatures = {
            key: seen_at
            for key, seen_at in self._seen_auth_signatures.items()
            if seen_at >= stale_before
        }
        cache_key = (timestamp, signature)
        if cache_key in self._seen_auth_signatures:
            return True
        self._seen_auth_signatures[cache_key] = now
        return False

    async def _handle_vci_connection(self, reader: asyncio.StreamReader,
                                     writer: asyncio.StreamWriter):
        """处理 VCI Proxy 的连接"""
        addr = writer.get_extra_info('peername')
        disconnect_reason = "handler_exit"
        logger.info(f"VCI Proxy 已连接: {addr}")

        # Disable Nagle algorithm for lower latency
        sock = writer.get_extra_info('socket')
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        logger.info(f"VCI Proxy 已连接: {addr}")

        # Authenticate before accepting the connection
        if not await self._authenticate_vci(reader, writer):
            logger.warning(f"VCI Proxy authentication failed, closing: {addr}")
            logger.info("[TUNNEL_CONN] auth_failed addr=%s", addr)
            writer.close()
            return

        # 关闭已有的 VCI 连接
        if self.vci_writer is not None:
            logger.warning(f"替换已有 VCI 连接，新连接: {addr}")
            logger.info(
                "[TUNNEL_CONN] replacing existing connection old_epoch=%s new_addr=%s",
                self._connection_epoch,
                addr,
            )
            old_writer = self.vci_writer
            self.vci_connected.clear()
            self._cancel_probe_task()
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
        self._connection_counter += 1
        self._connection_epoch = f"epoch-{int(time.time() * 1000)}-{self._connection_counter:03d}"
        self._tunnel_quality.mark_connected(self._connection_epoch)
        logger.info(
            "[TUNNEL_CONN] connected addr=%s epoch=%s connection_count=%s",
            addr,
            self._connection_epoch,
            self._connection_counter,
        )
        self._write_tunnel_quality_snapshot()
        self.vci_connected.set()
        self._probe_task = asyncio.create_task(self._probe_loop(self._connection_epoch))

        try:
            while True:
                # 读取消息
                header = await reader.readexactly(HEADER_SIZE)
                magic, length, msg_type, sequence = struct.unpack('>IIHI', header)

                if magic != MAGIC:
                    disconnect_reason = f"invalid_magic:{magic:#x}"
                    logger.warning(f"无效的 Magic: {magic:#x}")
                    break

                try:
                    body = await _read_frame_body(reader, length)
                except (ValueError, TimeoutError, ConnectionError) as exc:
                    disconnect_reason = f"frame_rejected:{exc}"
                    logger.warning("VCI frame rejected: %s", exc)
                    break

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
                    clean_body, hw_ms = strip_timing_trailer(body)
                    future.set_result((msg_type, clean_body, hw_ms))
                else:
                    logger.warning(f"收到未知消息: type={msg_type:#x}, seq={sequence}")

        except asyncio.IncompleteReadError as exc:
            disconnect_reason = f"eof expected={exc.expected} partial={len(exc.partial)}"
            logger.info(f"VCI Proxy 断开连接: {addr}")
        except Exception as e:
            disconnect_reason = f"exception:{type(e).__name__}:{e}"
            logger.exception(f"VCI 连接错误: {e}")
        finally:
            self.vci_connected.clear()
            self._cancel_probe_task()
            self._cancel_pending_futures()
            self._tunnel_quality.mark_disconnected(self._connection_epoch)
            logger.info(
                "[TUNNEL_CONN] disconnected addr=%s epoch=%s reason=%s",
                addr,
                self._connection_epoch,
                disconnect_reason,
            )
            self._write_tunnel_quality_snapshot()
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
            channel_id, ioctl_id, _input = ProtocolDecoder.decode_ioctl_req(body)
            cached = self._ioctl_cache.try_get_cached(channel_id, ioctl_id)
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
            self._ioctl_cache.invalidate_channel(channel_id)
        elif msg_type == MsgType.CLOSE_REQ:
            self._read_cache.clear()
            self._filter_cache.clear()
            self._ioctl_cache.invalidate()
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
            channel_id = struct.unpack('>I', body[:4])[0]
            ret, output_data = ProtocolDecoder.decode_ioctl_rsp(resp_body)
            self._ioctl_cache.record_result(channel_id, ioctl_id, ret, output_data)

    def _record_benchmark_event(
        self,
        *,
        started_at_s: float,
        duration_ms: float,
        msg_type: int,
        req_body: bytes,
        resp_type: int | None,
        resp_body: bytes,
        cache_hit: bool,
        status: str,
        hw_ms: float | None = None,
    ) -> None:
        if self.benchmark_writer is None:
            return

        event = make_proxy_benchmark_event(
            run_label=self.benchmark_label,
            source="proxy_server",
            started_at_s=started_at_s,
            duration_ms=duration_ms,
            msg_type=msg_type,
            req_body=req_body,
            resp_type=resp_type,
            resp_body=resp_body,
            cache_hit=cache_hit,
            status=status,
            hw_ms=hw_ms,
        )
        self.benchmark_writer.write_event(event)

    def _next_sequence(self) -> int:
        self.sequence = (self.sequence + 1) & 0xFFFFFFFF
        return self.sequence

    def _write_tunnel_quality_snapshot(self) -> None:
        snapshot = self._tunnel_quality.snapshot()
        write_tunnel_quality_snapshot(snapshot)
        signature = self._quality_signature(snapshot)
        if signature != self._last_quality_signature:
            self._last_quality_signature = signature
            logger.info(
                "[TUNNEL_QUALITY] epoch=%s grade=%s status=%s connected=%s fresh=%s "
                "samples=%s p95=%s reason=%s probe_failures=%s",
                snapshot.get("connection_epoch"),
                snapshot.get("grade"),
                snapshot.get("status"),
                snapshot.get("connected"),
                snapshot.get("fresh"),
                snapshot.get("sample_count"),
                self._format_ms(snapshot.get("network_ms", {}).get("p95")),
                snapshot.get("reason"),
                snapshot.get("probe_failures"),
            )

    @staticmethod
    def _quality_signature(snapshot: dict) -> tuple:
        metrics = snapshot.get("network_ms") or {}
        return (
            snapshot.get("connection_epoch"),
            snapshot.get("grade"),
            snapshot.get("status"),
            snapshot.get("connected"),
            snapshot.get("fresh"),
            snapshot.get("sample_count"),
            metrics.get("p95"),
            snapshot.get("reason"),
            snapshot.get("probe_failures"),
        )

    @staticmethod
    def _format_ms(value: float | None) -> str:
        if value is None:
            return "n/a"
        return f"{float(value):.1f}ms"

    def _cancel_probe_task(self) -> None:
        if self._probe_task is not None:
            self._probe_task.cancel()
            self._probe_task = None

    async def _probe_loop(self, connection_epoch: str) -> None:
        logger.info("[TUNNEL_PROBE] loop_started epoch=%s interval=3.0s", connection_epoch)
        try:
            while (
                self.vci_connected.is_set()
                and self.vci_writer is not None
                and self._connection_epoch == connection_epoch
            ):
                await self._run_probe(connection_epoch)
                await asyncio.sleep(3.0)
        except asyncio.CancelledError:
            logger.info("[TUNNEL_PROBE] loop_cancelled epoch=%s", connection_epoch)
            raise
        except Exception as exc:
            logger.warning("[TUNNEL_PROBE] loop_stopped epoch=%s error=%s", connection_epoch, exc)

    async def _run_probe(self, connection_epoch: str) -> None:
        if self.vci_writer is None or self._connection_epoch != connection_epoch:
            return

        started_at = time.monotonic()
        sequence = self._next_sequence()
        future = asyncio.get_running_loop().create_future()
        self.response_futures[sequence] = future

        try:
            async with self.vci_lock:
                if self.vci_writer is None:
                    raise ConnectionError("VCI disconnected")
                self.vci_writer.write(ProtocolEncoder.encode_ping_req(sequence))
                await self.vci_writer.drain()

            resp_type, _resp_body, hw_ms = await asyncio.wait_for(future, timeout=5.0)
            if resp_type != MsgType.PING_RSP:
                raise RuntimeError(f"Unexpected probe response type: {resp_type}")

            duration_ms = (time.monotonic() - started_at) * 1000.0
            network_ms = max(0.0, duration_ms - float(hw_ms or 0.0))
            self._tunnel_quality.record_probe(network_ms)
            logger.info(
                "[TUNNEL_PROBE] success epoch=%s seq=%s duration=%s hw=%s network=%s",
                connection_epoch,
                sequence,
                self._format_ms(duration_ms),
                self._format_ms(hw_ms),
                self._format_ms(network_ms),
            )
        except asyncio.CancelledError:
            self.response_futures.pop(sequence, None)
            raise
        except Exception as exc:
            self.response_futures.pop(sequence, None)
            self._tunnel_quality.record_probe_failure(reason="probe_failures")
            logger.warning(
                "[TUNNEL_PROBE] failure epoch=%s seq=%s error=%s failures=%s",
                connection_epoch,
                sequence,
                exc,
                self._tunnel_quality.probe_failures,
            )
        finally:
            self._write_tunnel_quality_snapshot()

    async def _handle_proxy_connection(self, reader: asyncio.StreamReader,
                                       writer: asyncio.StreamWriter):
        """处理本地代理连接"""
        addr = writer.get_extra_info('peername')
        logger.info(f"代理客户端连接: {addr}")

        # Disable Nagle algorithm for lower latency
        sock = writer.get_extra_info('socket')
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

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

                try:
                    body = await _read_frame_body(reader, length)
                except (ValueError, TimeoutError, ConnectionError) as exc:
                    logger.warning("代理请求帧被拒绝: %s", exc)
                    break

                msg_name = MSG_NAMES.get(msg_type, f"0x{msg_type:04x}")
                started_at_s = time.time()

                # Try serving from cache
                cached, ioctl_id = self._try_serve_cached(msg_type, body, sequence)
                if cached is not None:
                    resp_type, resp_body = decode_benchmark_response(cached)
                    self._record_benchmark_event(
                        started_at_s=started_at_s,
                        duration_ms=0.0,
                        msg_type=msg_type,
                        req_body=body,
                        resp_type=resp_type,
                        resp_body=resp_body,
                        cache_hit=True,
                        status="success",
                    )
                    writer.write(cached)
                    await writer.drain()
                    continue

                # Invalidate caches as needed
                self._invalidate_caches(msg_type, body)

                # 转发请求到 VCI Proxy
                new_seq = self._next_sequence()
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
                    resp_type, resp_body, hw_ms = await asyncio.wait_for(future, timeout=30.0)
                    fwd_ms = (time.monotonic() - fwd_start) * 1000

                    self._record_in_caches(msg_type, body, resp_type, resp_body, ioctl_id)
                    self._record_benchmark_event(
                        started_at_s=started_at_s,
                        duration_ms=fwd_ms,
                        msg_type=msg_type,
                        req_body=body,
                        resp_type=resp_type,
                        resp_body=resp_body,
                        cache_hit=False,
                        status="success",
                        hw_ms=hw_ms,
                    )

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
                    self._record_benchmark_event(
                        started_at_s=started_at_s,
                        duration_ms=fwd_ms,
                        msg_type=msg_type,
                        req_body=body,
                        resp_type=None,
                        resp_body=b"",
                        cache_hit=False,
                        status=reason.lower(),
                    )
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
    parser.add_argument('--tls', action='store_true',
                       help='Enable TLS for reverse-client connections')
    parser.add_argument('--tls-cert', type=str, default=None,
                       help='TLS certificate file for the reverse server')
    parser.add_argument('--tls-key', type=str, default=None,
                       help='TLS private key file for the reverse server')
    parser.add_argument('--tls-ca', type=str, default=None,
                       help='CA bundle for validating reverse-client certificates')
    parser.add_argument('--tls-require-client-cert', action='store_true',
                       help='Require reverse clients to present a trusted certificate')
    parser.add_argument('--no-read-cache', action='store_true',
                       help='Disable ReadMsgs BUFFER_EMPTY cache')
    parser.add_argument('--read-cache-ttl', type=int, default=150,
                       help='ReadMsgs cache TTL in ms (默认: 150)')
    parser.add_argument('--no-filter-dedup', action='store_true',
                       help='Disable StartFilter deduplication')
    parser.add_argument('--no-vbatt-cache', action='store_true',
                       help='Disable READ_VBATT response cache')
    parser.add_argument('--vbatt-ttl', type=int, default=5,
                       help='VBATT cache TTL in seconds (默认: 5)')
    parser.add_argument('--no-ioctl-cache', action='store_true',
                       help='Disable generalized read-only IOCTL cache')
    parser.add_argument('--ioctl-ttl', type=int, default=5,
                       help='IOCTL cache TTL in seconds (默认: 5)')
    parser.add_argument('--benchmark-log', type=str, default=None,
                       help='Write structured JSONL benchmark events to this file')
    parser.add_argument('--benchmark-label', type=str, default='proxy_run',
                       help='Run label stored in benchmark events')
    args = parser.parse_args()

    config = ProxyConfig.from_args(
        auth_token=args.auth_token,
        tls_enabled=args.tls,
        tls_certfile=args.tls_cert,
        tls_keyfile=args.tls_key,
        tls_ca_file=args.tls_ca,
        tls_require_client_cert=args.tls_require_client_cert,
        no_read_cache=args.no_read_cache,
        read_cache_ttl=args.read_cache_ttl,
        no_filter_dedup=args.no_filter_dedup,
        no_vbatt_cache=args.no_vbatt_cache,
        vbatt_ttl=args.vbatt_ttl,
        no_ioctl_cache=args.no_ioctl_cache,
        ioctl_ttl=args.ioctl_ttl,
    )

    print("=" * 50)
    print("VCI Proxy 反向连接服务器")
    print("=" * 50)
    print(f"VCI Proxy 连接端口: {args.listen_port}")
    print(f"本地代理端口: {args.proxy_port}")
    print(f"Auth: {'enabled' if config.auth.enabled else 'disabled'}")
    print(f"TLS: {'enabled' if config.tls.enabled else 'disabled'}")
    print(f"ReadMsgs cache: {'enabled' if config.read_msgs_cache.enabled else 'disabled'}"
          f" (TTL={config.read_msgs_cache.ttl_ms}ms)")
    print(f"Filter dedup: {'enabled' if config.filter_dedup.enabled else 'disabled'}")
    print(f"VBATT cache: {'enabled' if config.vbatt_cache.enabled else 'disabled'}"
          f" (TTL={config.vbatt_cache.ttl_s}s)")
    print("=" * 50)

    benchmark_writer = JsonlBenchmarkWriter(args.benchmark_log) if args.benchmark_log else None
    server = ReverseProxyServer(
        args.listen_port,
        args.proxy_port,
        config,
        benchmark_writer=benchmark_writer,
        benchmark_label=args.benchmark_label,
    )

    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        print("\n停止服务器...")


if __name__ == '__main__':
    main()
