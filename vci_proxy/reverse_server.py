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
import signal
import os
from typing import Optional
from pathlib import Path

from diagnostic_platform.observability import (
    LogContext,
    emit_event,
    get_product_log_writer,
    install_observability_log_handler,
    read_active_session_snapshot,
)

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
from .prefetch_read_msgs import PrefetchReadMsgsBuffer
from .protocol import (
    MAGIC,
    HEADER_SIZE,
    MSG_NAMES,
    MsgType,
    ProtocolDecoder,
    ProtocolEncoder,
    strip_read_msgs_prefetch_bundle,
)
from .tls_utils import harden_tls_context
from .tunnel_quality import (
    TunnelQualityTracker,
    write_tunnel_quality_snapshot,
)

_bootstrap_logs: list[tuple[str, str]] = []

try:
    from dotenv import load_dotenv

    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
        _bootstrap_logs.append(("debug", f"Loaded environment variables from {env_path}"))
    else:
        _bootstrap_logs.append(("debug", f".env file not found at {env_path}"))
except ImportError:
    _bootstrap_logs.append(("debug", "python-dotenv not installed"))


def _resolve_reverse_server_log_path(
    file_name: str = "vci_proxy.log",
    *,
    environ: dict[str, str] | None = None,
) -> Path | None:
    env = os.environ if environ is None else environ
    configured_dir = str(env.get("LOG_DIR", "") or "").strip()
    if not configured_dir:
        return None
    log_dir = Path(configured_dir)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _bootstrap_logs.append(
            ("warning", f"Failed to create LOG_DIR {log_dir}: {exc}; console logging only")
        )
        return None
    return log_dir / file_name


_logging_handlers: list[logging.Handler] = [logging.StreamHandler()]
_reverse_server_log_path = _resolve_reverse_server_log_path()
if _reverse_server_log_path is not None:
    _logging_handlers.insert(
        0,
        logging.FileHandler(_reverse_server_log_path, encoding="utf-8"),
    )

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
    handlers=_logging_handlers,
)
logger = logging.getLogger(__name__)

for level, message in _bootstrap_logs:
    getattr(logger, level)(message)

MAX_FRAME_BODY_BYTES = 1_000_000
DEFAULT_FRAME_BODY_READ_TIMEOUT_S = 10.0


def _disable_windows_quick_edit() -> None:
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-10)
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)) == 0:
            return
        new_mode = (mode.value | 0x0080) & ~0x0040
        kernel32.SetConsoleMode(handle, new_mode)
    except Exception:
        pass


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
        self._shutting_down = False
        self._vci_server: asyncio.AbstractServer | None = None
        self._proxy_server: asyncio.AbstractServer | None = None
        self._probe_task: asyncio.Task | None = None
        self._connection_counter = 0
        self._connection_epoch: str | None = None
        self._tunnel_quality = TunnelQualityTracker()
        self._last_quality_signature: tuple | None = None
        self._last_snapshot_write_error: str | None = None
        self._seen_auth_signatures: dict[tuple[int, bytes], int] = {}
        self._observability_writer = get_product_log_writer("reverse_server")
        self._process_started_emitted = False
        self._process_shutdown_started_emitted = False
        self._process_shutdown_finished_emitted = False

        # P1-1: ReadMsgs BUFFER_EMPTY cache
        self._read_cache = ReadMsgsCache(self.config.read_msgs_cache)
        # P2-1: Filter deduplication cache
        self._filter_cache = FilterDeduplicationCache(self.config.filter_dedup)
        # Generalized read-only IOCTL cache (replaces VBATT-only cache)
        self._ioctl_cache = IoctlCache(self.config.ioctl_cache)
        # Consume-once store for local-side ReadMsgs read-ahead data.
        self._prefetch_read_msgs = PrefetchReadMsgsBuffer(
            enabled=self.config.read_ahead.enabled,
            max_messages=self.config.read_ahead.max_messages,
        )
        # channel_id -> (dll sequence, monotonic timestamp)
        self._last_write_by_channel: dict[int, tuple[int | None, float]] = {}

    def _current_observability_context(
        self,
        *,
        connection_epoch: str | None = None,
        operation_kind: str = "reverse_tunnel",
    ) -> LogContext:
        if self._shutting_down:
            snapshot: dict[str, object] = {}
        else:
            snapshot = read_active_session_snapshot() or {}
        return LogContext(
            session_id=str(snapshot.get("session_id") or "") or None,
            connection_epoch=connection_epoch or self._connection_epoch,
            operation_kind=str(snapshot.get("operation_kind") or operation_kind),
            page=str(snapshot.get("current_page") or "") or None,
            module=str(snapshot.get("selected_module") or "") or None,
            data_category=str(snapshot.get("selected_data_category") or "") or None,
        )

    def _emit_tunnel_event(
        self,
        event_type: str,
        *,
        connection_epoch: str | None = None,
        operation_kind: str = "reverse_tunnel",
        status: str = "ok",
        failure_code: str | None = None,
        failure_domain: str = "unknown",
        reason: str | None = None,
        impact_scope: str = "reverse_tunnel",
        **extra: object,
    ) -> None:
        try:
            emit_event(
                self._observability_writer,
                component="reverse_server",
                event_type=event_type,
                context=self._current_observability_context(
                    connection_epoch=connection_epoch,
                    operation_kind=operation_kind,
                ),
                status=status,
                failure_code=failure_code,
                failure_domain=failure_domain,
                reason=reason,
                impact_scope=impact_scope,
                **extra,
            )
        except Exception:
            if self._shutting_down:
                logger.debug("Skipping tunnel observability during shutdown", exc_info=True)
                return
            logger.warning("Failed to emit tunnel observability event: %s", event_type, exc_info=True)

    def _emit_proxy_request_event(
        self,
        event_type: str,
        *,
        dll_seq: int,
        msg_name: str,
        proxy_seq: int | None = None,
        status: str = "ok",
        failure_code: str | None = None,
        failure_domain: str = "unknown",
        reason: str | None = None,
        duration_ms: float | None = None,
        hw_ms: float | None = None,
        network_ms: float | None = None,
        cache_hit: bool | None = None,
        **extra: object,
    ) -> None:
        try:
            base_context = self._current_observability_context(
                connection_epoch=self._connection_epoch,
                operation_kind=f"j2534:{msg_name}",
            )
            emit_event(
                self._observability_writer,
                component="reverse_server",
                event_type=event_type,
                context=LogContext(
                    session_id=base_context.session_id,
                    connection_epoch=base_context.connection_epoch,
                    dll_seq=dll_seq,
                    proxy_seq=proxy_seq,
                    worker_request_id=base_context.worker_request_id,
                    operation_kind=base_context.operation_kind,
                    page=base_context.page,
                    module=base_context.module,
                    data_category=base_context.data_category,
                    request_id=base_context.request_id,
                ),
                status=status,
                failure_code=failure_code,
                failure_domain=failure_domain,
                reason=reason,
                duration_ms=duration_ms,
                hw_ms=hw_ms,
                network_ms=network_ms,
                impact_scope="proxy_request",
                msg_name=msg_name,
                cache_hit=cache_hit,
                **extra,
            )
        except Exception:
            if self._shutting_down:
                logger.debug("Skipping proxy observability during shutdown", exc_info=True)
                return
            logger.warning("Failed to emit proxy observability event: %s", event_type, exc_info=True)

    @staticmethod
    def _message_payload_bytes(messages: list[dict]) -> int:
        return sum(len(message.get("data", b"")) for message in messages)

    def _request_observability_fields(self, msg_type: int, body: bytes) -> dict[str, object]:
        fields: dict[str, object] = {}
        try:
            if msg_type == MsgType.READ_MSGS_REQ:
                channel_id, num_msgs, timeout = ProtocolDecoder.decode_read_msgs_req(body)
                fields.update(
                    {
                        "channel_id": channel_id,
                        "num_msgs": num_msgs,
                        "timeout": timeout,
                    }
                )
                marker = self._last_write_by_channel.get(channel_id)
                if marker is not None:
                    last_write_seq, last_write_ts = marker
                    fields["last_write_seq"] = last_write_seq
                    fields["post_write_age_ms"] = round(
                        max(0.0, (time.monotonic() - last_write_ts) * 1000.0), 3
                    )
            elif msg_type == MsgType.WRITE_MSGS_REQ:
                channel_id, messages, timeout = ProtocolDecoder.decode_write_msgs_req(body)
                fields.update(
                    {
                        "channel_id": channel_id,
                        "write_message_count": len(messages),
                        "timeout": timeout,
                        "write_payload_bytes": self._message_payload_bytes(messages),
                    }
                )
        except Exception:
            fields["decode_error"] = True
        return fields

    def _response_observability_fields(self, resp_type: int | None, resp_body: bytes) -> dict[str, object]:
        if resp_type != MsgType.READ_MSGS_RSP:
            return {}
        try:
            return_code, messages = ProtocolDecoder.decode_read_msgs_rsp(resp_body)
            message_count = len(messages)
            return {
                "return_code": return_code,
                "message_count": message_count,
                "payload_bytes": self._message_payload_bytes(messages),
                "read_result": "data" if message_count else "empty",
            }
        except Exception:
            return {"response_decode_error": True}

    def _cache_miss_reason(self, msg_type: int, request_fields: dict[str, object]) -> str:
        if msg_type != MsgType.READ_MSGS_REQ:
            return "cache_miss"
        age_ms = request_fields.get("post_write_age_ms")
        bypass_ms = self.config.read_msgs_cache.post_write_bypass_ms
        if isinstance(age_ms, (int, float)) and bypass_ms > 0 and age_ms <= bypass_ms:
            return "post_write_bypass"
        return "cache_miss"

    def _emit_process_lifecycle_event(
        self,
        event_type: str,
        *,
        status: str = "ok",
        failure_code: str | None = None,
        reason: str | None = None,
        **extra: object,
    ) -> None:
        self._emit_tunnel_event(
            event_type,
            operation_kind="reverse_server_process",
            impact_scope="reverse_server_process",
            status=status,
            failure_code=failure_code,
            failure_domain="cloud_proxy_tunnel" if status == "error" else "unknown",
            reason=reason,
            pid=os.getpid(),
            listen_port=self.listen_port,
            proxy_port=self.proxy_port,
            **extra,
        )

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

    def _auth_success_message(self) -> str:
        if self.config.read_ahead.enabled:
            return "ok;read_ahead=1"
        return "ok"

    async def start(self):
        """启动服务器"""
        # 启动 VCI 监听服务
        tls_context = self._build_tls_server_context()
        self._vci_server = await asyncio.start_server(
            self._handle_vci_connection, '0.0.0.0', self.listen_port, ssl=tls_context
        )
        logger.info(f"等待 VCI Proxy 连接到端口 {self.listen_port}...")

        # 启动代理服务
        self._proxy_server = await asyncio.start_server(
            self._handle_proxy_connection, '127.0.0.1', self.proxy_port
        )
        logger.info(f"代理服务监听端口 {self.proxy_port}")

        if self.config.auth.enabled:
            logger.info("PSK authentication enabled")
        if self.config.tls.enabled:
            logger.info("Reverse tunnel TLS enabled")
        if self.config.read_msgs_cache.enabled:
            logger.info(
                f"ReadMsgs cache enabled (idle TTL={self.config.read_msgs_cache.ttl_ms}ms, "
                f"active TTL={self.config.read_msgs_cache.active_ttl_ms}ms, "
                f"active window={self.config.read_msgs_cache.active_window_ms}ms, "
                f"post-write bypass={self.config.read_msgs_cache.post_write_bypass_ms}ms, "
                f"max timeout={self.config.read_msgs_cache.max_cacheable_timeout_ms}ms)"
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
        if self.config.read_ahead.enabled:
            logger.info(
                "Read-ahead enabled (window=%sms, max_reads=%s, timeout=%sms, max_messages=%s)",
                self.config.read_ahead.window_ms,
                self.config.read_ahead.max_reads,
                self.config.read_ahead.read_timeout_ms,
                self.config.read_ahead.max_messages,
            )
        if self.benchmark_writer is not None:
            logger.info("Proxy benchmark logging enabled: %s", self.benchmark_writer.path)
        logger.info("Waiting for local VCI Proxy connection on port %s", self.listen_port)
        logger.info("After connection, J2534 is reachable through localhost:%s", self.proxy_port)
        if not self._process_started_emitted:
            self._process_started_emitted = True
            self._emit_process_lifecycle_event(
                "process.lifecycle.started",
                reason="server_started",
                auth_enabled=self.config.auth.enabled,
                tls_enabled=self.config.tls.enabled,
            )

        try:
            await asyncio.gather(
                self._vci_server.serve_forever(),
                self._proxy_server.serve_forever()
            )
        except asyncio.CancelledError:
            self._shutting_down = True
            logger.info("ReverseProxyServer shutdown requested")
        finally:
            await self._shutdown_servers()

    async def _shutdown_servers(self) -> None:
        if self._shutting_down:
            if self._vci_server is None and self._proxy_server is None:
                return
        if not self._process_shutdown_started_emitted:
            self._process_shutdown_started_emitted = True
            self._emit_process_lifecycle_event(
                "process.lifecycle.shutdown_started",
                reason="shutdown_requested",
            )
        self._shutting_down = True
        self._cancel_pending_futures()
        await self._cancel_probe_task_async()
        self.vci_connected.clear()

        writer = self.vci_writer
        self.vci_reader = None
        self.vci_writer = None
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass
            wait_closed = getattr(writer, "wait_closed", None)
            if callable(wait_closed):
                try:
                    await wait_closed()
                except Exception:
                    pass

        for server in (self._vci_server, self._proxy_server):
            if server is None:
                continue
            server.close()
            try:
                await server.wait_closed()
            except Exception:
                pass
        self._vci_server = None
        self._proxy_server = None
        if not self._process_shutdown_finished_emitted:
            self._process_shutdown_finished_emitted = True
            self._emit_process_lifecycle_event(
                "process.lifecycle.shutdown_finished",
                reason="shutdown_finished",
            )

    def _cancel_pending_futures(self):
        """取消所有挂起的 Future（VCI 断开时调用）"""
        pending = list(self.response_futures.items())
        self.response_futures.clear()
        self._read_cache.clear()
        self._filter_cache.clear()
        self._ioctl_cache.invalidate()
        self._prefetch_read_msgs.clear()
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
        peer = writer.get_extra_info("peername")
        try:
            header = await asyncio.wait_for(
                reader.readexactly(HEADER_SIZE),
                timeout=self.config.auth.auth_timeout_s,
            )
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            logger.warning("VCI client did not send registration message in time: %s", peer)
            return False
        except (ConnectionResetError, BrokenPipeError, OSError) as exc:
            logger.warning("VCI client disconnected during auth: %s error=%s", peer, exc)
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
        except (ConnectionResetError, BrokenPipeError, OSError) as exc:
            logger.warning("VCI client disconnected while sending auth frame: %s error=%s", peer, exc)
            return False

        if msg_type == MsgType.AUTH_REQ:
            if self.config.auth.enabled and not self.config.auth.token:
                logger.error("Auth enabled but no server token is configured")
                rsp = ProtocolEncoder.encode_auth_rsp(False, "server auth token not configured", sequence)
                try:
                    writer.write(rsp)
                    await writer.drain()
                except (ConnectionResetError, BrokenPipeError, OSError) as exc:
                    logger.warning(
                        "VCI client disconnected before auth response could be sent: %s error=%s",
                        peer,
                        exc,
                    )
                    return False
                return False
            if not self.config.auth.enabled:
                # Auth not required, but client sent AUTH_REQ -- accept it
                logger.info("Auth not required, accepting AUTH_REQ")
                rsp = ProtocolEncoder.encode_auth_rsp(
                    True,
                    self._auth_success_message(),
                    sequence,
                )
                try:
                    writer.write(rsp)
                    await writer.drain()
                except (ConnectionResetError, BrokenPipeError, OSError) as exc:
                    logger.warning(
                        "VCI client disconnected before auth response could be sent: %s error=%s",
                        peer,
                        exc,
                    )
                    return False
                return True

            timestamp, signature = ProtocolDecoder.decode_auth_req(body)
            success, reason = verify_signature(
                self.config.auth.token, timestamp, signature
            )
            if success and self._is_replayed_auth(signature, timestamp):
                success = False
                reason = "replay detected"
            if success:
                reason = self._auth_success_message()
            rsp = ProtocolEncoder.encode_auth_rsp(success, reason, sequence)
            try:
                writer.write(rsp)
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError) as exc:
                logger.warning(
                    "VCI client disconnected before auth response could be sent: %s error=%s",
                    peer,
                    exc,
                )
                return False

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
            try:
                writer.write(ack_header)
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError) as exc:
                logger.warning(
                    "VCI client disconnected before handshake ACK could be sent: %s error=%s",
                    peer,
                    exc,
                )
                return False

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
            except (ConnectionResetError, BrokenPipeError, OSError) as exc:
                logger.warning(
                    "VCI client disconnected during handshake phase 2: %s error=%s",
                    peer,
                    exc,
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
            except (ConnectionResetError, BrokenPipeError, OSError) as exc:
                logger.warning(
                    "VCI client disconnected while finishing handshake phase 2: %s error=%s",
                    peer,
                    exc,
                )
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
            try:
                writer.write(ack2)
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError) as exc:
                logger.warning(
                    "VCI client disconnected before handshake completion ACK could be sent: %s error=%s",
                    peer,
                    exc,
                )
                return False

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

    def _should_accept_new_vci_connection(
        self,
        new_addr: tuple[str, int] | None,
    ) -> tuple[bool, str, tuple[str, int] | None]:
        existing_writer = self.vci_writer
        if existing_writer is None:
            return True, "no_existing_tunnel", None

        existing_addr = existing_writer.get_extra_info("peername")
        is_closing = getattr(existing_writer, "is_closing", None)
        if callable(is_closing) and is_closing():
            return True, "existing_writer_closing", existing_addr
        if not self.vci_connected.is_set():
            return True, "existing_tunnel_inactive", existing_addr

        snapshot = self._tunnel_quality.snapshot()
        try:
            probe_failures = int(snapshot.get("probe_failures") or 0)
        except (TypeError, ValueError):
            probe_failures = 0
        has_probe_failures = probe_failures > 0 or snapshot.get("reason") == "probe_failures"
        if snapshot.get("connected") and snapshot.get("fresh") and not has_probe_failures:
            return False, "existing_tunnel_healthy", existing_addr
        if has_probe_failures:
            return True, "existing_tunnel_probe_failed", existing_addr
        return True, "existing_tunnel_stale", existing_addr

    async def _handle_vci_connection(self, reader: asyncio.StreamReader,
                                     writer: asyncio.StreamWriter):
        """处理 VCI Proxy 的连接"""
        addr = writer.get_extra_info('peername')
        disconnect_reason = "handler_exit"
        local_epoch: str | None = None
        logger.info("VCI tunnel connected: %s", addr)

        # Disable Nagle algorithm for lower latency
        sock = writer.get_extra_info('socket')
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        # Authenticate before accepting the connection
        if not await self._authenticate_vci(reader, writer):
            logger.warning("VCI tunnel authentication failed: %s", addr)
            self._emit_tunnel_event(
                "tunnel.auth.failed",
                status="error",
                failure_code="auth_failed",
                failure_domain="cloud_proxy_tunnel",
                reason="authentication_failed",
                remote_addr=str(addr),
            )
            writer.close()
            return

        accept_new, decision_reason, existing_addr = self._should_accept_new_vci_connection(addr)
        if not accept_new:
            logger.warning(
                "Rejecting additional VCI tunnel: existing_epoch=%s existing_addr=%s new_addr=%s reason=%s",
                self._connection_epoch,
                existing_addr,
                addr,
                decision_reason,
            )
            self._emit_tunnel_event(
                "tunnel.lifecycle.rejected",
                connection_epoch=self._connection_epoch,
                status="error",
                failure_code="tunnel_rejected",
                failure_domain="cloud_proxy_tunnel",
                reason=decision_reason,
                remote_addr=str(addr),
                existing_addr=str(existing_addr),
            )
            writer.close()
            return

        # 关闭已有的 VCI 连接
        if self.vci_writer is not None:
            logger.warning(
                "Replacing inactive VCI tunnel: old_epoch=%s old_addr=%s new_addr=%s reason=%s",
                self._connection_epoch,
                existing_addr,
                addr,
                decision_reason,
            )
            self._emit_tunnel_event(
                "tunnel.lifecycle.replaced",
                connection_epoch=self._connection_epoch,
                status="error",
                failure_code="tunnel_replaced",
                failure_domain="cloud_proxy_tunnel",
                reason=decision_reason,
                remote_addr=str(addr),
                existing_addr=str(existing_addr),
            )
            old_writer = self.vci_writer
            self.vci_connected.clear()
            await self._cancel_probe_task_async()
            self._cancel_pending_futures()
            self.vci_reader = None
            self.vci_writer = None
            try:
                old_writer.close()
            except Exception:
                pass

        self.vci_reader = reader
        self.vci_writer = writer
        logger.info("VCI Proxy connected: %s", addr)
        self._connection_counter += 1
        local_epoch = f"epoch-{int(time.time() * 1000)}-{self._connection_counter:03d}"
        self._connection_epoch = local_epoch
        self._tunnel_quality.mark_connected(local_epoch)
        self._write_tunnel_quality_snapshot()
        self._emit_tunnel_event(
            "tunnel.lifecycle.connected",
            connection_epoch=local_epoch,
            reason="tunnel_connected",
            remote_addr=str(addr),
        )
        self.vci_connected.set()
        self._probe_task = asyncio.create_task(self._probe_loop(local_epoch))

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
                    if msg_type == MsgType.WRITE_MSGS_RSP:
                        try:
                            clean_body, prefetch_bundle = strip_read_msgs_prefetch_bundle(
                                clean_body
                            )
                        except ValueError:
                            logger.warning(
                                "Discarding malformed ReadMsgs prefetch bundle seq=%s",
                                sequence,
                                exc_info=True,
                            )
                            clean_body = clean_body[:8]
                        else:
                            if (
                                prefetch_bundle is not None
                                and self.config.read_ahead.enabled
                            ):
                                recorded = self._prefetch_read_msgs.record_read_rsp_bodies(
                                    prefetch_bundle.channel_id,
                                    prefetch_bundle.read_rsp_bodies,
                                )
                                if recorded:
                                    logger.debug(
                                        "Recorded %s prefetched ReadMsgs message(s) for channel_id=%s",
                                        recorded,
                                        prefetch_bundle.channel_id,
                                    )
                    future.set_result((msg_type, clean_body, hw_ms))
                else:
                    logger.warning(f"收到未知消息: type={msg_type:#x}, seq={sequence}")

        except asyncio.CancelledError:
            self._shutting_down = True
            disconnect_reason = "shutdown_cancelled"
        except asyncio.IncompleteReadError as exc:
            disconnect_reason = f"eof expected={exc.expected} partial={len(exc.partial)}"
        except (ConnectionResetError, BrokenPipeError, OSError) as exc:
            disconnect_reason = f"connection_lost:{type(exc).__name__}:{exc}"
            logger.warning("VCI tunnel lost connection: addr=%s error=%s", addr, exc)
        except Exception as e:
            disconnect_reason = f"exception:{type(e).__name__}:{e}"
            logger.exception(f"VCI 连接错误: {e}")
        finally:
            owns_current_tunnel = self.vci_writer is writer
            if owns_current_tunnel:
                self.vci_connected.clear()
                self._cancel_pending_futures()
                await self._cancel_probe_task_async()
                self._tunnel_quality.mark_disconnected(local_epoch)
                self._write_tunnel_quality_snapshot()
                self.vci_reader = None
                self.vci_writer = None
            else:
                logger.info(
                    "VCI tunnel handler exited after ownership changed: addr=%s epoch=%s current_epoch=%s",
                    addr,
                    local_epoch,
                    self._connection_epoch,
                )
            shutdown_disconnect = self._shutting_down and disconnect_reason in {
                "handler_exit",
                "shutdown_cancelled",
                "cancelled",
            }
            if shutdown_disconnect:
                logger.info(
                    "VCI tunnel handler stopping during server shutdown: addr=%s epoch=%s reason=%s",
                    addr,
                    local_epoch,
                    disconnect_reason,
                )
            else:
                logger.log(
                    logging.ERROR if disconnect_reason.startswith("exception:") else logging.WARNING,
                    "VCI tunnel disconnected: addr=%s epoch=%s reason=%s",
                    addr,
                    local_epoch,
                    disconnect_reason,
                )
                self._emit_tunnel_event(
                    "tunnel.lifecycle.disconnected",
                    connection_epoch=local_epoch,
                    status="error" if disconnect_reason != "handler_exit" else "ok",
                    failure_code=disconnect_reason.split(":", 1)[0] if disconnect_reason else None,
                    failure_domain="cloud_proxy_tunnel",
                    reason=disconnect_reason,
                    remote_addr=str(addr),
                )
            writer.close()

    def _try_serve_cached(self, msg_type: int, body: bytes,
                          sequence: int) -> tuple[Optional[bytes], Optional[int], str | None]:
        """Try to serve the request from cache.

        Returns (cached_response, ioctl_id, cache_reason).
        cached_response is None if cache miss.
        ioctl_id is set when msg_type is IOCTL_REQ (needed for recording later).
        """
        ioctl_id = None

        if msg_type == MsgType.READ_MSGS_REQ:
            channel_id, num_msgs, timeout = ProtocolDecoder.decode_read_msgs_req(body)
            prefetched = self._prefetch_read_msgs.try_serve(channel_id, num_msgs, sequence)
            if prefetched is not None:
                return prefetched, ioctl_id, "prefetch_hit"

            cached = self._read_cache.try_serve_from_cache(
                channel_id, num_msgs, timeout, sequence
            )
            if cached is not None:
                return cached, ioctl_id, "cache_hit"

        elif msg_type == MsgType.START_FILTER_REQ:
            cached = self._filter_cache.try_dedup(body, sequence)
            if cached is not None:
                return cached, ioctl_id, "cache_hit"

        elif msg_type == MsgType.IOCTL_REQ:
            channel_id, ioctl_id, _input = ProtocolDecoder.decode_ioctl_req(body)
            cached = self._ioctl_cache.try_get_cached(channel_id, ioctl_id)
            if cached is not None:
                ret, output_data = cached
                resp = ProtocolEncoder.encode_ioctl_rsp(ret, output_data, sequence)
                return resp, ioctl_id, "cache_hit"

        return None, ioctl_id, None

    def _invalidate_caches(self, msg_type: int, body: bytes, sequence: int | None = None):
        """Invalidate caches based on the request type."""
        if msg_type == MsgType.DISCONNECT_REQ:
            channel_id = ProtocolDecoder.decode_disconnect_req(body)
            self._read_cache.invalidate_channel(channel_id)
            self._filter_cache.invalidate_channel(channel_id)
            self._ioctl_cache.invalidate_channel(channel_id)
            self._prefetch_read_msgs.clear_channel(channel_id)
            self._last_write_by_channel.pop(channel_id, None)
        elif msg_type == MsgType.CLOSE_REQ:
            self._read_cache.clear()
            self._filter_cache.clear()
            self._ioctl_cache.invalidate()
            self._prefetch_read_msgs.clear()
            self._last_write_by_channel.clear()
        elif msg_type == MsgType.WRITE_MSGS_REQ:
            channel_id, _messages, _timeout = ProtocolDecoder.decode_write_msgs_req(body)
            now = time.monotonic()
            self._read_cache.record_write(channel_id, now=now)
            self._last_write_by_channel[channel_id] = (sequence, now)
        elif msg_type == MsgType.START_FILTER_REQ:
            channel_id, _filter_type, _mask, _pattern, _flow = (
                ProtocolDecoder.decode_start_filter_req(body)
            )
            self._read_cache.mark_channel_active(channel_id, invalidate_empty=True)
            self._prefetch_read_msgs.clear_channel(channel_id)
        elif msg_type == MsgType.STOP_FILTER_REQ:
            channel_id, filter_id = ProtocolDecoder.decode_stop_filter_req(body)
            self._read_cache.mark_channel_active(channel_id, invalidate_empty=True)
            self._prefetch_read_msgs.clear_channel(channel_id)
            self._filter_cache.on_stop_filter(filter_id)
        elif msg_type == MsgType.IOCTL_REQ:
            channel_id, ioctl_id, _input = ProtocolDecoder.decode_ioctl_req(body)
            if not self._ioctl_cache.is_cacheable(ioctl_id):
                self._read_cache.mark_channel_active(channel_id, invalidate_empty=True)
                self._prefetch_read_msgs.clear_channel(channel_id)

    def _clear_prefetch_after_failed_write(self, msg_type: int, body: bytes) -> None:
        if msg_type != MsgType.WRITE_MSGS_REQ:
            return
        try:
            channel_id, _messages, _timeout = ProtocolDecoder.decode_write_msgs_req(body)
        except Exception:
            return
        self._prefetch_read_msgs.clear_channel(channel_id)

    def _record_in_caches(self, msg_type: int, body: bytes,
                          resp_type: int, resp_body: bytes,
                          ioctl_id: Optional[int]):
        """Record response in caches for future lookups."""
        if msg_type == MsgType.READ_MSGS_REQ:
            channel_id = struct.unpack('>I', body[:4])[0]
            return_code, messages = ProtocolDecoder.decode_read_msgs_rsp(resp_body)
            self._read_cache.record_result(
                channel_id,
                return_code,
                message_count=len(messages),
            )

        elif msg_type == MsgType.START_FILTER_REQ and resp_type == MsgType.START_FILTER_RSP:
            return_code, filter_id = ProtocolDecoder.decode_start_filter_rsp(resp_body)
            self._filter_cache.record_result(body, filter_id, return_code, resp_body)

        elif msg_type == MsgType.WRITE_MSGS_REQ:
            if resp_type != MsgType.WRITE_MSGS_RSP:
                self._clear_prefetch_after_failed_write(msg_type, body)
                return
            return_code, _num_written = ProtocolDecoder.decode_write_msgs_rsp(resp_body)
            if return_code != 0:
                self._clear_prefetch_after_failed_write(msg_type, body)

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
        try:
            write_tunnel_quality_snapshot(snapshot)
            self._last_snapshot_write_error = None
        except PermissionError as exc:
            self._log_tunnel_quality_snapshot_error(exc)
        except OSError as exc:
            self._log_tunnel_quality_snapshot_error(exc)
        signature = self._quality_signature(snapshot)
        if signature == self._last_quality_signature:
            return
        self._last_quality_signature = signature
        self._emit_tunnel_event(
            "tunnel.quality.changed",
            connection_epoch=snapshot.get("connection_epoch"),
            status="error" if not snapshot.get("connected") else "ok",
            failure_code="quality_blocked" if snapshot.get("status") == "blocked" else None,
            failure_domain="cloud_proxy_tunnel" if snapshot.get("status") == "blocked" else "unknown",
            reason=str(snapshot.get("reason") or ""),
            network_grade=snapshot.get("grade"),
            tunnel_status=snapshot.get("status"),
            tunnel_connected=bool(snapshot.get("connected")),
            tunnel_fresh=bool(snapshot.get("fresh")),
            probe_failures=int(snapshot.get("probe_failures") or 0),
        )
        if not self._should_log_tunnel_quality(snapshot):
            return

        level = logging.INFO
        if (
            not snapshot.get("connected")
            or snapshot.get("probe_failures")
            or snapshot.get("reason") in {"snapshot_stale", "tunnel_disconnected"}
        ):
            level = logging.WARNING
        logger.log(
            level,
            "[TUNNEL_QUALITY] status=%s connected=%s reason=%s",
            snapshot.get("status"),
            snapshot.get("connected"),
            snapshot.get("reason"),
        )

    @staticmethod
    def _should_log_tunnel_quality(snapshot: dict) -> bool:
        if not snapshot.get("connected"):
            return True
        if snapshot.get("probe_failures"):
            return True
        return snapshot.get("status") != "healthy"

    def _log_tunnel_quality_snapshot_error(self, exc: Exception) -> None:
        message = str(exc)
        if self._last_snapshot_write_error == message:
            return
        self._last_snapshot_write_error = message
        logger.warning("Failed to persist tunnel quality snapshot: %s", exc)

    @staticmethod
    def _quality_signature(snapshot: dict) -> tuple:
        return (
            snapshot.get("connection_epoch"),
            snapshot.get("grade"),
            snapshot.get("status"),
            snapshot.get("connected"),
            snapshot.get("fresh"),
            snapshot.get("reason"),
            snapshot.get("probe_failures"),
        )

    @staticmethod
    def _format_ms(value: float | None) -> str:
        if value is None:
            return "n/a"
        return f"{float(value):.1f}ms"

    def _cancel_probe_task(self) -> asyncio.Task | None:
        task = self._probe_task
        self._probe_task = None
        if task is not None:
            task.cancel()
        return task

    async def _cancel_probe_task_async(self) -> None:
        task = self._cancel_probe_task()
        if task is None:
            return
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("probe task cleanup failed", exc_info=True)

    async def _probe_loop(self, connection_epoch: str) -> None:
        try:
            while (
                self.vci_connected.is_set()
                and self.vci_writer is not None
                and self._connection_epoch == connection_epoch
            ):
                await self._run_probe(connection_epoch)
                await asyncio.sleep(3.0)
        except asyncio.CancelledError:
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
            self._emit_tunnel_event(
                "tunnel.probe.success",
                connection_epoch=connection_epoch,
                operation_kind="tunnel_probe",
                duration_ms=duration_ms,
                hw_ms=hw_ms,
                network_ms=network_ms,
                reason="probe_ok",
                probe_sequence=sequence,
            )
            logger.debug(
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
            self._emit_tunnel_event(
                "tunnel.probe.failure",
                connection_epoch=connection_epoch,
                operation_kind="tunnel_probe",
                status="error",
                failure_code="probe_failure",
                failure_domain="cloud_proxy_tunnel",
                reason=str(exc) or type(exc).__name__.lower(),
                probe_sequence=sequence,
                probe_failures=self._tunnel_quality.probe_failures,
            )
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
        logger.debug("Proxy client connected: %s", addr)

        # Disable Nagle algorithm for lower latency
        sock = writer.get_extra_info('socket')
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        try:
            while True:
                # 等待 VCI 连接
                if not self.vci_connected.is_set():
                    logger.debug("Waiting for VCI tunnel before serving proxy client")
                    await self.vci_connected.wait()

                if self.vci_writer is None:
                    logger.error("VCI tunnel unavailable while serving proxy client")
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
                request_fields = self._request_observability_fields(msg_type, body)
                self._emit_proxy_request_event(
                    "proxy.request.received_from_dll",
                    dll_seq=sequence,
                    msg_name=msg_name,
                    reason="received_from_dll",
                    **request_fields,
                )

                # Try serving from cache
                cached, ioctl_id, cache_reason = self._try_serve_cached(
                    msg_type, body, sequence
                )
                if cached is not None:
                    self._emit_proxy_request_event(
                        "proxy.request.cache_decision",
                        dll_seq=sequence,
                        msg_name=msg_name,
                        cache_hit=True,
                        reason=cache_reason or "cache_hit",
                        **request_fields,
                    )
                    resp_type, resp_body = decode_benchmark_response(cached)
                    response_fields = self._response_observability_fields(resp_type, resp_body)
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
                    self._emit_proxy_request_event(
                        "proxy.request.replied_to_dll",
                        dll_seq=sequence,
                        msg_name=msg_name,
                        cache_hit=True,
                        reason="cache_reply",
                        **request_fields,
                        **response_fields,
                    )
                    continue
                self._emit_proxy_request_event(
                    "proxy.request.cache_decision",
                    dll_seq=sequence,
                    msg_name=msg_name,
                    cache_hit=False,
                    reason=self._cache_miss_reason(msg_type, request_fields),
                    **request_fields,
                )

                # Invalidate caches as needed
                self._invalidate_caches(msg_type, body, sequence)

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
                    self._emit_proxy_request_event(
                        "proxy.request.forwarded_to_tunnel",
                        dll_seq=sequence,
                        proxy_seq=new_seq,
                        msg_name=msg_name,
                        reason="forwarded_to_tunnel",
                        **request_fields,
                    )
                except Exception as e:
                    logger.error(f"转发请求失败: {e}")
                    self.response_futures.pop(new_seq, None)
                    self._clear_prefetch_after_failed_write(msg_type, body)
                    self._emit_proxy_request_event(
                        "proxy.request.failed",
                        dll_seq=sequence,
                        proxy_seq=new_seq,
                        msg_name=msg_name,
                        status="error",
                        failure_code="forward_failed",
                        failure_domain="cloud_proxy_tunnel",
                        reason=str(e),
                        **request_fields,
                    )
                    break

                # 等待响应
                try:
                    resp_type, resp_body, hw_ms = await asyncio.wait_for(future, timeout=30.0)
                    fwd_ms = (time.monotonic() - fwd_start) * 1000
                    network_ms = max(0.0, fwd_ms - float(hw_ms or 0.0))

                    self._record_in_caches(msg_type, body, resp_type, resp_body, ioctl_id)
                    response_fields = self._response_observability_fields(resp_type, resp_body)
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
                    self._emit_proxy_request_event(
                        "proxy.request.response_received",
                        dll_seq=sequence,
                        proxy_seq=new_seq,
                        msg_name=msg_name,
                        duration_ms=fwd_ms,
                        hw_ms=hw_ms,
                        network_ms=network_ms,
                        reason="response_received",
                        **request_fields,
                        **response_fields,
                    )

                    # 发送响应给客户端（使用原始 sequence）
                    resp_header = struct.pack('>IIHI', MAGIC,
                                             HEADER_SIZE + len(resp_body),
                                             resp_type, sequence)
                    writer.write(resp_header + resp_body)
                    await writer.drain()
                    self._emit_proxy_request_event(
                        "proxy.request.replied_to_dll",
                        dll_seq=sequence,
                        proxy_seq=new_seq,
                        msg_name=msg_name,
                        duration_ms=fwd_ms,
                        hw_ms=hw_ms,
                        network_ms=network_ms,
                        reason="reply_sent",
                        **request_fields,
                        **response_fields,
                    )

                    if fwd_ms > 1000:
                        logger.warning(
                            "Slow proxy request: %s seq=%s duration=%.1fms",
                            msg_name,
                            sequence,
                            fwd_ms,
                        )

                except (asyncio.TimeoutError, ConnectionError) as e:
                    self.response_futures.pop(new_seq, None)
                    fwd_ms = (time.monotonic() - fwd_start) * 1000
                    reason = "VCI_DISCONNECTED" if isinstance(e, ConnectionError) else "TIMEOUT"
                    self._clear_prefetch_after_failed_write(msg_type, body)
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
                    self._emit_proxy_request_event(
                        "proxy.request.timeout" if isinstance(e, asyncio.TimeoutError) else "proxy.request.failed",
                        dll_seq=sequence,
                        proxy_seq=new_seq,
                        msg_name=msg_name,
                        status="error",
                        failure_code="timeout" if isinstance(e, asyncio.TimeoutError) else "vci_disconnected",
                        failure_domain="cloud_proxy_tunnel",
                        reason="wait_response_timeout" if isinstance(e, asyncio.TimeoutError) else "wait_response_connection_lost",
                        duration_ms=fwd_ms,
                        **request_fields,
                    )
                    logger.error(f"[PROXY] {msg_name} seq={sequence} {reason} after {fwd_ms:.0f}ms")
                    break

        except asyncio.CancelledError:
            self._shutting_down = True
            logger.info("Proxy client handler stopping during server shutdown: %s", addr)
        except asyncio.IncompleteReadError:
            logger.debug("Proxy client disconnected: %s", addr)
        except Exception as e:
            logger.error(f"代理连接错误: {e}")
        finally:
            writer.close()
def _install_runtime_log_observability(server: ReverseProxyServer) -> None:
    install_observability_log_handler(
        logging.getLogger(),
        component="reverse_server.runtime",
        writer=get_product_log_writer("reverse_server.runtime"),
        context_provider=lambda _record: server._current_observability_context(
            operation_kind="reverse_server_runtime"
        ),
    )


def _run_server_until_stopped(server: ReverseProxyServer) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    main_task = loop.create_task(server.start())
    previous_sigint_handler = signal.getsignal(signal.SIGINT)
    stop_requested = False

    def _handle_sigint(signum, frame):
        nonlocal stop_requested
        if stop_requested:
            logger.warning("强制停止服务器...")
            os._exit(130)
        stop_requested = True
        server._shutting_down = True
        logger.info("停止服务器...")
        if not main_task.done():
            loop.call_soon_threadsafe(main_task.cancel)

    signal.signal(signal.SIGINT, _handle_sigint)
    try:
        try:
            loop.run_until_complete(main_task)
        except asyncio.CancelledError:
            pass

        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        if pending:
            for task in pending:
                task.cancel()
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))

        loop.run_until_complete(loop.shutdown_asyncgens())
        if hasattr(loop, "shutdown_default_executor"):
            loop.run_until_complete(loop.shutdown_default_executor())
    finally:
        signal.signal(signal.SIGINT, previous_sigint_handler)
        asyncio.set_event_loop(None)
        loop.close()


def main():
    _disable_windows_quick_edit()
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
    parser.add_argument('--read-cache-post-write-bypass-ms', type=int, default=150,
                       help='Bypass ReadMsgs empty cache after same-channel writes in ms (default: 150; 0 disables)')
    parser.add_argument('--read-cache-active-ttl-ms', type=int, default=25,
                       help='ReadMsgs empty cache TTL while a channel is active in ms (default: 25)')
    parser.add_argument('--read-cache-active-window-ms', type=int, default=500,
                       help='Window after writes/data/filter mutations that uses active TTL in ms (default: 500)')
    parser.add_argument('--read-cache-max-timeout-ms', type=int, default=25,
                       help='Only cache ReadMsgs polls whose timeout is at or below this value in ms (default: 25; -1 allows all)')
    parser.add_argument('--read-ahead', dest='read_ahead', action='store_true', default=None,
                       help='Enable consume-once ReadMsgs prefetch serving after successful writes (or VCI_PROXY_READ_AHEAD=1)')
    parser.add_argument('--no-read-ahead', dest='read_ahead', action='store_false',
                       help='Disable read-ahead even if VCI_PROXY_READ_AHEAD is set')
    parser.add_argument('--read-ahead-window-ms', type=int, default=None,
                       help='Read-ahead collection window in ms (default: 200 or VCI_PROXY_READ_AHEAD_WINDOW_MS)')
    parser.add_argument('--read-ahead-max-reads', type=int, default=None,
                       help='Maximum local ReadMsgs calls after one successful write (default: 3 or VCI_PROXY_READ_AHEAD_MAX_READS)')
    parser.add_argument('--read-ahead-read-timeout-ms', type=int, default=None,
                       help='Timeout passed to local read-ahead ReadMsgs calls in ms (default: 0 or VCI_PROXY_READ_AHEAD_READ_TIMEOUT_MS)')
    parser.add_argument('--read-ahead-max-messages', type=int, default=None,
                       help='Maximum prefetched messages retained per channel (default: 16 or VCI_PROXY_READ_AHEAD_MAX_MESSAGES)')
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
        read_cache_post_write_bypass_ms=args.read_cache_post_write_bypass_ms,
        read_cache_active_ttl_ms=args.read_cache_active_ttl_ms,
        read_cache_active_window_ms=args.read_cache_active_window_ms,
        read_cache_max_timeout_ms=args.read_cache_max_timeout_ms,
        read_ahead_enabled=args.read_ahead,
        read_ahead_window_ms=args.read_ahead_window_ms,
        read_ahead_max_reads=args.read_ahead_max_reads,
        read_ahead_read_timeout_ms=args.read_ahead_read_timeout_ms,
        read_ahead_max_messages=args.read_ahead_max_messages,
        no_filter_dedup=args.no_filter_dedup,
        no_vbatt_cache=args.no_vbatt_cache,
        vbatt_ttl=args.vbatt_ttl,
        no_ioctl_cache=args.no_ioctl_cache,
        ioctl_ttl=args.ioctl_ttl,
    )

    benchmark_writer = JsonlBenchmarkWriter(args.benchmark_log) if args.benchmark_log else None
    server = ReverseProxyServer(
        args.listen_port,
        args.proxy_port,
        config,
        benchmark_writer=benchmark_writer,
        benchmark_label=args.benchmark_label,
    )
    _install_runtime_log_observability(server)
    logger.info("=" * 50)
    logger.info("VCI Proxy reverse connection server")
    logger.info("=" * 50)
    logger.info("VCI Proxy listen port: %s", args.listen_port)
    logger.info("Local proxy port: %s", args.proxy_port)
    logger.info("Auth: %s", "enabled" if config.auth.enabled else "disabled")
    logger.info("TLS: %s", "enabled" if config.tls.enabled else "disabled")
    logger.info(
        "ReadMsgs cache: %s (idle TTL=%sms, active TTL=%sms, active window=%sms, post-write bypass=%sms, max timeout=%sms)",
        "enabled" if config.read_msgs_cache.enabled else "disabled",
        config.read_msgs_cache.ttl_ms,
        config.read_msgs_cache.active_ttl_ms,
        config.read_msgs_cache.active_window_ms,
        config.read_msgs_cache.post_write_bypass_ms,
        config.read_msgs_cache.max_cacheable_timeout_ms,
    )
    logger.info(
        "Read-ahead: %s (window=%sms, max_reads=%s, timeout=%sms, max_messages=%s)",
        "enabled" if config.read_ahead.enabled else "disabled",
        config.read_ahead.window_ms,
        config.read_ahead.max_reads,
        config.read_ahead.read_timeout_ms,
        config.read_ahead.max_messages,
    )
    logger.info(
        "Filter dedup: %s",
        "enabled" if config.filter_dedup.enabled else "disabled",
    )
    logger.info(
        "VBATT cache: %s (TTL=%ss)",
        "enabled" if config.vbatt_cache.enabled else "disabled",
        config.vbatt_cache.ttl_s,
    )
    logger.info("=" * 50)

    try:
        _run_server_until_stopped(server)
    except KeyboardInterrupt:
        logger.warning("强制停止服务器...")
    except Exception as exc:
        server._emit_process_lifecycle_event(
            "process.lifecycle.failed",
            status="error",
            failure_code=type(exc).__name__,
            reason=str(exc),
        )
        raise


if __name__ == '__main__':
    main()
