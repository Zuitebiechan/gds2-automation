"""Reverse-tunnel client for the local VCI proxy."""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import functools
import logging
import os
import socket
import ssl
import time
from typing import Any, Callable, Mapping, Optional

from diagnostic_platform.observability import (
    LogContext,
    emit_event,
    generate_request_id,
    get_local_observability_root,
    get_product_log_writer,
    utc_now_iso,
)
from vci_proxy.auth import compute_signature
from vci_proxy.benchmark import attach_timing_trailer
from vci_proxy.cache_read_msgs import BUFFER_EMPTY
from vci_proxy.cache_ioctl import IoctlCache
from vci_proxy.config import ProxyConfig
from vci_proxy.j2534_driver import J2534Driver
from vci_proxy.j2534_worker import create_driver_runtime
from vci_proxy.local_live_data import (
    LocalLiveDataChannel,
    LocalLiveDataCollector,
    LocalLiveDataMonitor,
)
from vci_proxy.protocol import (
    HEADER_SIZE,
    LOCAL_LIVE_DATA_SAMPLE_SCHEMA_VERSION,
    MAGIC,
    Message,
    MsgType,
    ProtocolDecoder,
    ProtocolEncoder,
    attach_read_msgs_prefetch_bundle,
)
from vci_proxy.sweep_executor import LocalSweepExecutor
from vci_proxy.sweep_protocol import (
    SweepPlanResponse,
    decode_sweep_plan_start_req,
    decode_sweep_plan_stop_req,
    encode_sweep_drain_results_rsp,
    encode_sweep_plan_start_rsp,
    encode_sweep_plan_stop_rsp,
    encode_sweep_status_rsp,
    is_sweep_message_type,
)
from vci_proxy.tls_utils import harden_tls_context


logger = logging.getLogger(__name__)

READ_COLLECT_MIN_DRAIN_CAP_MS = 8
READ_COLLECT_DATA_AT_MAX_EXTRA_READS = 2
READ_COLLECT_EMPTY_GRACE_DATA_EXTRA_READS = 1
ISO15765_PROTOCOL_ID = 6


class ReverseProxyClient:
    """Local-side reverse tunnel client that executes J2534 requests."""

    def __init__(
        self,
        server_host: str,
        server_port: int,
        dll_path: Optional[str] = None,
        config: Optional[ProxyConfig] = None,
        on_status_change: Optional[Callable[[str, str], None]] = None,
        driver_loader: Optional[Callable[[Optional[str]], tuple[Any, Optional[Callable[[], None]]]]] = None,
    ):
        self.server_host = server_host
        self.server_port = server_port
        self.dll_path = dll_path
        self.config = config or ProxyConfig()
        self.driver: Optional[Any] = None
        self._driver_cleanup: Optional[Callable[[], None]] = None
        self._driver_loader = driver_loader or create_driver_runtime
        self.running = False
        self._on_status_change = on_status_change
        self._prewarm_device_id: Optional[int] = None
        self._prewarm_ret: Optional[int] = None
        self._prewarm_task: Optional[asyncio.Task] = None
        self._ioctl_cache = IoctlCache(self.config.ioctl_cache)
        self._active_writer: Optional[asyncio.StreamWriter] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._backoff_sleep_task: Optional[asyncio.Task] = None
        self._instance_id = f"pid={os.getpid()}-obj={id(self):x}"
        self._attempt_counter = 0
        self._observability_writer = get_product_log_writer(
            "reverse_client",
            root=get_local_observability_root() / "raw",
        )
        self._server_read_ahead_enabled = False
        self._server_read_collect_enabled = False
        self._server_write_collect_enabled = False
        self._server_sweep_shadow_enabled = False
        self._server_local_live_data_enabled = False
        self._server_connection_epoch: str | None = None
        self._driver_call_lock = asyncio.Lock()
        self._tunnel_write_lock = asyncio.Lock()
        self._client_sample_seq = 0
        self._foreground_request_depth = 0
        self._local_live_data_channel: LocalLiveDataChannel | None = None
        self._local_live_data = LocalLiveDataMonitor()
        self._local_live_data_collector = LocalLiveDataCollector(
            config=self.config.local_live_data,
            monitor=self._local_live_data,
            run_driver_call=self._run_driver_call,
            context_factory=self._sweep_log_context,
            emit_event=self._emit_client_event,
            foreground_idle=self._foreground_idle,
            channel_provider=self._get_local_live_data_channel,
            on_sample=self._handle_local_live_data_sample,
        )
        self._sweep_executor = LocalSweepExecutor(
            config=self.config.local_sweep,
            run_driver_call=self._run_driver_call,
            context_factory=self._sweep_log_context,
            emit_event=self._emit_client_event,
            foreground_idle=self._foreground_idle,
            local_live_data=self._local_live_data,
        )

    @staticmethod
    def _describe_task_state(task: Optional[asyncio.Task]) -> str:
        if task is None:
            return "none"
        cancelled_attr = getattr(task, "cancelled", None)
        if callable(cancelled_attr):
            if cancelled_attr():
                return "cancelled"
        elif cancelled_attr:
            return "cancelled"
        done_attr = getattr(task, "done", None)
        if callable(done_attr):
            if done_attr():
                return "done"
        elif done_attr:
            return "done"
        return "pending"

    def _build_tls_connection_options(self) -> tuple[ssl.SSLContext | None, str | None]:
        """Build optional TLS connection settings for the reverse tunnel."""
        if not self.config.tls.enabled:
            return None, None

        ssl_context = ssl.create_default_context(
            ssl.Purpose.SERVER_AUTH,
            cafile=self.config.tls.ca_file,
        )
        harden_tls_context(ssl_context)
        server_hostname = (
            self.config.tls.server_name
            or self.server_host
        )
        return ssl_context, server_hostname

    def _notify_status(self, status: str, detail: str = "") -> None:
        if self._on_status_change:
            try:
                self._on_status_change(status, detail)
            except Exception:
                logger.debug("status callback failed", exc_info=True)

    def _get_error_name(self, code: int) -> str:
        error_name_getter = getattr(self.driver, "get_error_name", None)
        if callable(error_name_getter):
            try:
                return str(error_name_getter(code))
            except Exception:
                pass
        return f"ERROR_{code:#x}"

    def _log_j2534_result(
        self,
        operation: str,
        ret: int,
        *,
        detail: str = "",
        ok_codes: tuple[int, ...] = (0,),
    ) -> None:
        if ret in ok_codes:
            logger.debug("%s succeeded%s", operation, detail)
            return
        logger.warning(
            "%s returned %s (%s)%s",
            operation,
            ret,
            self._get_error_name(ret),
            detail,
        )

    def _emit_client_event(
        self,
        event_type: str,
        *,
        context: LogContext | None = None,
        status: str = "ok",
        failure_code: str | None = None,
        failure_domain: str = "unknown",
        reason: str | None = None,
        impact_scope: str = "reverse_client",
        **extra: object,
    ) -> None:
        emit_event(
            self._observability_writer,
            component="reverse_client",
            event_type=event_type,
            context=context,
            status=status,
            failure_code=failure_code,
            failure_domain=failure_domain,
            reason=reason,
            impact_scope=impact_scope,
            **extra,
        )

    def _request_log_context(self, *, sequence: int, msg_name: str, worker_request_id: str) -> LogContext:
        return LogContext(
            connection_epoch=self._server_connection_epoch,
            proxy_seq=sequence,
            worker_request_id=worker_request_id,
            operation_kind=f"j2534:{msg_name}",
        )

    def _sweep_log_context(self, msg_name: str) -> LogContext:
        return LogContext(
            connection_epoch=self._server_connection_epoch,
            worker_request_id=generate_request_id(),
            operation_kind=f"j2534:{msg_name}",
        )

    def _foreground_idle(self) -> bool:
        return self._foreground_request_depth <= 0 and not self._driver_call_lock.locked()

    def _get_local_live_data_channel(self) -> LocalLiveDataChannel | None:
        return self._local_live_data_channel

    def _handle_local_live_data_sample(self, sample: Mapping[str, object]) -> None:
        try:
            loop = self._loop or asyncio.get_running_loop()
        except RuntimeError:
            return
        if not loop.is_running():
            return
        loop.create_task(self._send_local_live_data_sample(sample))

    async def _write_tunnel_frame(self, writer: asyncio.StreamWriter, frame: bytes) -> None:
        async with self._tunnel_write_lock:
            writer.write(frame)
            await writer.drain()

    async def _send_local_live_data_sample(self, sample: Mapping[str, object]) -> None:
        if not self._server_local_live_data_enabled:
            return
        writer = self._active_writer
        if writer is None:
            return
        self._client_sample_seq += 1
        client_sample_seq = self._client_sample_seq
        payload = self._local_live_data_tunnel_payload(
            sample,
            client_sample_seq=client_sample_seq,
        )
        frame = ProtocolEncoder.encode_local_live_data_sample(
            payload,
            sequence=client_sample_seq,
        )
        try:
            if not self._server_local_live_data_enabled:
                return
            writer = self._active_writer
            if writer is None:
                return
            await self._write_tunnel_frame(writer, frame)
        except Exception as exc:
            self._emit_client_event(
                "proxy.local_live_data.tunnel_send_failed",
                context=self._sweep_log_context("LOCAL_LIVE_DATA"),
                status="warning",
                failure_code=type(exc).__name__,
                failure_domain="cloud_proxy_tunnel",
                reason="tunnel_send_failed",
                impact_scope="proxy_local_live_data",
                client_sample_seq=client_sample_seq,
                error=str(exc),
            )

    @staticmethod
    def _local_live_data_tunnel_payload(
        sample: Mapping[str, object],
        *,
        client_sample_seq: int,
    ) -> dict[str, object]:
        fields = {
            "schema_version": LOCAL_LIVE_DATA_SAMPLE_SCHEMA_VERSION,
            "signal_key": sample.get("signal_key"),
            "display_name": sample.get("display_name"),
            "unit": sample.get("unit"),
            "value": sample.get("value"),
            "source": sample.get("source"),
            "decoder_id": sample.get("decoder_id"),
            "sample_ts": sample.get("sample_ts"),
            "local_send_ts": utc_now_iso(),
            "local_reported_sample_age_ms": sample.get("sample_age_ms"),
            "poll_id": sample.get("poll_id"),
            "channel_id": sample.get("channel_id"),
            "request_kind": sample.get("request_kind"),
            "request_origin": sample.get("request_origin"),
            "return_code": sample.get("return_code"),
            "j2534_return_code_warning": sample.get("j2534_return_code_warning"),
            "raw_prefix_hex": sample.get("raw_prefix_hex"),
            "collector_interval_ms": sample.get("collector_interval_ms"),
            "read_timeout_ms": sample.get("read_timeout_ms"),
            "client_sample_seq": client_sample_seq,
        }
        return {key: value for key, value in fields.items() if value is not None}

    def _stop_local_live_data_collector(self, reason: str) -> None:
        self._local_live_data_channel = None
        self._local_live_data_collector.request_stop(reason)

    async def _stop_local_live_data_collector_async(self, reason: str) -> None:
        self._local_live_data_channel = None
        await self._local_live_data_collector.stop(reason)

    def _cancel_shadow_for_foreground_if_needed(self, msg_type: int, body: bytes) -> None:
        if msg_type in {
            MsgType.DISCONNECT_REQ,
            MsgType.CLOSE_REQ,
            MsgType.START_FILTER_REQ,
            MsgType.STOP_FILTER_REQ,
        }:
            self._sweep_executor.stop("foreground_invalidation")
            if msg_type in {MsgType.DISCONNECT_REQ, MsgType.CLOSE_REQ}:
                self._stop_local_live_data_collector("foreground_disconnect_or_close")
            return
        if msg_type != MsgType.IOCTL_REQ:
            return
        try:
            _channel_id, ioctl_id, _input_data = ProtocolDecoder.decode_ioctl_req(body)
        except Exception:
            self._sweep_executor.stop("foreground_ioctl_decode_failed")
            return
        if not self._ioctl_cache.is_cacheable(ioctl_id):
            self._sweep_executor.stop("foreground_mutating_or_non_cacheable_ioctl")

    def _ensure_request_context(
        self,
        request_context: LogContext | None,
        *,
        sequence: int,
        msg_name: str,
    ) -> LogContext:
        return request_context or self._request_log_context(
            sequence=sequence,
            msg_name=msg_name,
            worker_request_id=generate_request_id(),
        )

    def _invoke_driver_call(
        self,
        method_name: str,
        *args: Any,
        log_context: dict[str, Any] | None = None,
    ) -> Any:
        if self.driver is None:
            raise RuntimeError("J2534 driver is not loaded")
        call_with_context = getattr(self.driver, "call_with_context", None)
        if callable(call_with_context):
            return call_with_context(method_name, args, log_context=log_context)
        return getattr(self.driver, method_name)(*args)

    @staticmethod
    def _auth_message_enables_read_ahead(message: str) -> bool:
        return any(
            token.strip().lower() == "read_ahead=1"
            for token in message.replace(",", ";").split(";")
        )

    @staticmethod
    def _auth_message_enables_write_collect(message: str) -> bool:
        return any(
            token.strip().lower() == "write_collect=1"
            for token in message.replace(",", ";").split(";")
        )

    @staticmethod
    def _auth_message_enables_read_collect(message: str) -> bool:
        return any(
            token.strip().lower() == "read_collect=1"
            for token in message.replace(",", ";").split(";")
        )

    @staticmethod
    def _auth_message_enables_sweep_shadow(message: str) -> bool:
        return any(
            token.strip().lower() == "sweep_shadow=1"
            for token in message.replace(",", ";").split(";")
        )

    @staticmethod
    def _auth_message_enables_local_live_data(message: str) -> bool:
        return any(
            token.strip().lower() == "local_live_data=1"
            for token in message.replace(",", ";").split(";")
        )

    @staticmethod
    def _auth_message_connection_epoch(message: str) -> str | None:
        for token in str(message or "").replace(",", ";").split(";"):
            stripped = token.strip()
            if not stripped:
                continue
            if not stripped.lower().startswith("connection_epoch="):
                continue
            value = stripped.split("=", 1)[1].strip()
            return value or None
        return None

    def _auth_capability_message(self) -> str:
        capabilities: list[str] = []
        read_ahead = self.config.read_ahead
        if read_ahead.enabled:
            capabilities.append("read_ahead=1")
            if read_ahead.transaction_enabled:
                capabilities.append("read_collect=1")
                capabilities.append("write_collect=1")
        if self.config.local_sweep.shadow_transport_enabled:
            capabilities.append("sweep_shadow=1")
        if self.config.local_live_data.enabled:
            capabilities.append("local_live_data=1")
        return ";".join(capabilities)

    async def _run_driver_call(
        self,
        j2534_method: str,
        *args: Any,
        request_context: LogContext,
        ok_codes: tuple[int, ...] = (0,),
        warning_codes: tuple[int, ...] = (),
        warning_requires_payload: bool = False,
        result_metadata: dict[str, object] | None = None,
    ) -> Any:
        loop = asyncio.get_running_loop()
        msg_name = str(request_context.operation_kind or j2534_method).split(":", 1)[-1]
        self._emit_client_event(
            "j2534.call.started",
            context=request_context,
            reason="call_started",
            j2534_method=j2534_method,
        )
        started_at = time.monotonic()
        try:
            async with self._driver_call_lock:
                result = await loop.run_in_executor(
                    None,
                    functools.partial(
                        self._invoke_driver_call,
                        j2534_method,
                        *args,
                        log_context={
                            "connection_epoch": request_context.connection_epoch,
                            "proxy_seq": request_context.proxy_seq,
                            "worker_request_id": request_context.worker_request_id,
                            "msg_name": msg_name,
                        },
                    ),
                )
        except Exception as exc:
            duration_ms = (time.monotonic() - started_at) * 1000.0
            self._emit_client_event(
                "j2534.call.failed",
                context=request_context,
                status="error",
                failure_code=type(exc).__name__,
                failure_domain="local_worker_rpc",
                reason=str(exc),
                duration_ms=duration_ms,
                j2534_method=j2534_method,
            )
            raise

        duration_ms = (time.monotonic() - started_at) * 1000.0
        return_code = None
        if isinstance(result, int):
            return_code = result
        elif isinstance(result, tuple) and result:
            first = result[0]
            if isinstance(first, int):
                return_code = first
        payload_present = False
        if isinstance(result, tuple) and len(result) > 1:
            payload = result[1]
            if isinstance(payload, (list, tuple)):
                payload_present = len(payload) > 0
        status = "ok"
        event_type = "j2534.call.finished"
        failure_domain = "unknown"
        failure_code = None
        error_name = None
        reason = "call_finished"
        if return_code is not None and return_code not in ok_codes:
            if return_code in warning_codes and (
                not warning_requires_payload or payload_present
            ):
                status = "warning"
                event_type = "j2534.call.warning"
                failure_domain = "vehicle_or_vci"
                reason = "driver_return_code_with_data"
            else:
                status = "error"
                event_type = "j2534.call.failed"
                failure_domain = "local_j2534_driver"
                failure_code = str(return_code)
                reason = "driver_return_code"
                error_name = self._get_error_name(int(return_code))
        self._emit_client_event(
            event_type,
            context=request_context,
            status=status,
            failure_code=failure_code,
            failure_domain=failure_domain,
            reason=reason,
            duration_ms=duration_ms,
            j2534_method=j2534_method,
            return_code=return_code,
            error_name=error_name,
            **(result_metadata or {}),
        )
        return result

    def _ensure_driver(self) -> bool:
        if self.driver is None:
            try:
                self.driver, self._driver_cleanup = self._driver_loader(self.dll_path)
                logger.info(
                    "J2534 driver loaded: %s",
                    getattr(self.driver, "dll_path", self.dll_path or "<runtime>"),
                )
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
        peer = writer.get_extra_info("peername")
        try:
            logger.debug(
                "[CLIENT_CONN] instance=%s closing active writer peer=%s",
                self._instance_id,
                peer,
            )
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

    async def _release_prewarmed_device(self) -> None:
        """Close any device handle opened only for pre-warm."""
        device_id = self._prewarm_device_id
        self._prewarm_device_id = None
        self._prewarm_ret = None
        if device_id is None or self.driver is None:
            return

        loop = asyncio.get_running_loop()
        try:
            ret = await loop.run_in_executor(None, self.driver.close, device_id)
        except Exception as exc:
            logger.warning(
                "Failed to release pre-warmed device_id=%s: %s",
                device_id,
                exc,
            )
            return

        if ret == 0:
            logger.debug("Released pre-warmed device_id=%s", device_id)
            return

        error_name_getter = getattr(self.driver, "get_error_name", None)
        error_name = error_name_getter(ret) if callable(error_name_getter) else f"ERROR_{ret:#x}"
        logger.warning(
            "Failed to release pre-warmed device_id=%s, ret=%s (%s)",
            device_id,
            ret,
            error_name,
        )

    async def shutdown(self) -> None:
        """Gracefully stop background work and close the active tunnel."""
        self._emit_client_event(
            "reverse_client.lifecycle.shutdown_started",
            reason="shutdown_started",
            instance_id=self._instance_id,
        )
        logger.debug(
            "[CLIENT_CTRL] shutdown begin instance=%s prewarm_device_id=%s prewarm_task=%s",
            self._instance_id,
            self._prewarm_device_id,
            self._describe_task_state(self._prewarm_task),
        )
        self.running = False

        backoff_sleep_task = self._backoff_sleep_task
        self._backoff_sleep_task = None
        if backoff_sleep_task is not None:
            backoff_sleep_task.cancel()

        await self._cancel_prewarm_task()
        await self._stop_local_live_data_collector_async("shutdown")
        await self._release_prewarmed_device()
        await self._close_writer()
        cleanup = self._driver_cleanup
        self._driver_cleanup = None
        self.driver = None
        if cleanup is not None:
            try:
                cleanup()
            except Exception:
                logger.warning("J2534 driver cleanup failed", exc_info=True)

        self._ioctl_cache.invalidate()
        logger.debug("[CLIENT_CTRL] shutdown finished instance=%s", self._instance_id)
        self._emit_client_event(
            "reverse_client.lifecycle.shutdown_finished",
            reason="shutdown_finished",
            instance_id=self._instance_id,
        )

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
                self._attempt_counter += 1
                attempt_label = f"attempt={self._attempt_counter}"
                try:
                    self._notify_status("connecting", f"{self.server_host}:{self.server_port}")
                    self._emit_client_event(
                        "reverse_client.lifecycle.connecting",
                        reason="connecting",
                        instance_id=self._instance_id,
                        attempt_label=attempt_label,
                        target_host=self.server_host,
                        target_port=self.server_port,
                    )
                    logger.info(
                        "[CLIENT_CONN] instance=%s %s connecting target=%s:%s",
                        self._instance_id,
                        attempt_label,
                        self.server_host,
                        self.server_port,
                    )
                    ssl_context, server_hostname = self._build_tls_connection_options()

                    reader, writer = await asyncio.open_connection(
                        self.server_host,
                        self.server_port,
                        ssl=ssl_context,
                        server_hostname=server_hostname,
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

                    logger.info(
                        "[CLIENT_CONN] instance=%s %s connected local=%s remote=%s",
                        self._instance_id,
                        attempt_label,
                        writer.get_extra_info("sockname"),
                        writer.get_extra_info("peername"),
                    )
                    self._emit_client_event(
                        "reverse_client.lifecycle.connected",
                        reason="connected",
                        instance_id=self._instance_id,
                        attempt_label=attempt_label,
                        local_addr=str(writer.get_extra_info("sockname")),
                        remote_addr=str(writer.get_extra_info("peername")),
                    )
                    if ssl_context is not None:
                        logger.info(
                            "[CLIENT_CONN] instance=%s %s reverse tunnel TLS enabled server_name=%s",
                            self._instance_id,
                            attempt_label,
                            server_hostname,
                        )
                    backoff_seconds = 5.0

                    if await self._send_registration(reader, writer, attempt_label=attempt_label):
                        logger.info(
                            "[CLIENT_CONN] instance=%s %s reverse tunnel ready",
                            self._instance_id,
                            attempt_label,
                        )
                        self._notify_status("connected", f"{self.server_host}:{self.server_port}")
                        self._emit_client_event(
                            "reverse_client.lifecycle.registration_succeeded",
                            reason="registration_succeeded",
                            instance_id=self._instance_id,
                            attempt_label=attempt_label,
                        )
                        self._prewarm_task = asyncio.create_task(self._prewarm_open())
                        await self._handle_requests(reader, writer, attempt_label=attempt_label)
                    else:
                        logger.warning(
                            "[CLIENT_CONN] instance=%s %s registration/auth failed, reconnecting",
                            self._instance_id,
                            attempt_label,
                        )
                        self._emit_client_event(
                            "reverse_client.lifecycle.registration_failed",
                            status="error",
                            failure_code="registration_failed",
                            failure_domain="local_reverse_client",
                            reason="registration_failed",
                            instance_id=self._instance_id,
                            attempt_label=attempt_label,
                        )

                except ConnectionRefusedError:
                    self._notify_status(
                        "disconnected",
                        f"Connection refused, retrying in {backoff_seconds:.0f}s",
                    )
                    self._emit_client_event(
                        "reverse_client.lifecycle.connect_failed",
                        status="error",
                        failure_code="connection_refused",
                        failure_domain="cloud_proxy_tunnel",
                        reason="connection_refused",
                        instance_id=self._instance_id,
                        attempt_label=attempt_label,
                    )
                    logger.warning(
                        "[CLIENT_CONN] instance=%s %s connection refused, retrying in %.0fs",
                        self._instance_id,
                        attempt_label,
                        backoff_seconds,
                    )
                except ConnectionError as exc:
                    reason = str(exc) or "reverse server disconnected"
                    normalized_reason = reason[0].upper() + reason[1:] if reason else "Reverse server disconnected"
                    if self.running:
                        self._notify_status(
                            "disconnected",
                            f"{normalized_reason}, retrying in {backoff_seconds:.0f}s",
                        )
                    self._emit_client_event(
                        "reverse_client.lifecycle.disconnected",
                        status="error",
                        failure_code="connection_error",
                        failure_domain="cloud_proxy_tunnel",
                        reason=reason,
                        instance_id=self._instance_id,
                        attempt_label=attempt_label,
                    )
                    logger.warning(
                        "[CLIENT_CONN] instance=%s %s %s, retrying in %.0fs",
                        self._instance_id,
                        attempt_label,
                        reason,
                        backoff_seconds,
                    )
                except Exception as exc:
                    if self.running:
                        self._notify_status(
                            "disconnected",
                            f"Error: {exc}, retrying in {backoff_seconds:.0f}s",
                        )
                    self._emit_client_event(
                        "reverse_client.lifecycle.connect_failed",
                        status="error",
                        failure_code=type(exc).__name__,
                        failure_domain="local_reverse_client",
                        reason=str(exc),
                        instance_id=self._instance_id,
                        attempt_label=attempt_label,
                    )
                    logger.exception(
                        "[CLIENT_CONN] instance=%s %s connection error, retrying in %.0fs",
                        self._instance_id,
                        attempt_label,
                        backoff_seconds,
                    )
                finally:
                    logger.debug(
                        "[CLIENT_CONN] instance=%s %s cleanup begin active_writer=%s prewarm_device_id=%s prewarm_task=%s",
                        self._instance_id,
                        attempt_label,
                        self._active_writer is not None,
                        self._prewarm_device_id,
                        self._describe_task_state(self._prewarm_task),
                    )
                    await self._cancel_prewarm_task()
                    await self._stop_local_live_data_collector_async("connection_cleanup")
                    await self._release_prewarmed_device()
                    await self._close_writer()
                    self._ioctl_cache.invalidate()
                    logger.debug(
                        "[CLIENT_CONN] instance=%s %s cleanup finished running=%s",
                        self._instance_id,
                        attempt_label,
                        self.running,
                    )
                    self._emit_client_event(
                        "reverse_client.lifecycle.cleanup",
                        reason="connection_cleanup",
                        instance_id=self._instance_id,
                        attempt_label=attempt_label,
                    )

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
            logger.info("[CLIENT_CTRL] client stopped instance=%s", self._instance_id)

    async def _send_registration(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        attempt_label: str = "attempt=unknown",
    ) -> bool:
        """Register with the server using auth or the legacy heartbeat path."""
        self._server_read_ahead_enabled = False
        self._server_read_collect_enabled = False
        self._server_write_collect_enabled = False
        self._server_sweep_shadow_enabled = False
        self._server_local_live_data_enabled = False
        self._server_connection_epoch = None
        self._stop_local_live_data_collector("registration_reset")
        if self.config.auth.enabled and self.config.auth.token:
            timestamp = int(time.time())
            signature = compute_signature(self.config.auth.token, timestamp)
            msg = ProtocolEncoder.encode_auth_req(
                timestamp,
                signature,
                0,
                capabilities=self._auth_capability_message(),
            )
            await self._write_tunnel_frame(writer, msg)
            logger.debug(
                "[CLIENT_CONN] instance=%s %s sent auth request ts=%s",
                self._instance_id,
                attempt_label,
                timestamp,
            )

            try:
                header = await asyncio.wait_for(
                    reader.readexactly(HEADER_SIZE),
                    timeout=self.config.auth.auth_timeout_s,
                )
                magic, length, msg_type, _sequence = Message.decode_header(header)
                if magic != MAGIC:
                    self._server_read_ahead_enabled = False
                    self._server_read_collect_enabled = False
                    self._server_write_collect_enabled = False
                    self._server_sweep_shadow_enabled = False
                    self._server_local_live_data_enabled = False
                    self._server_connection_epoch = None
                    logger.error("Invalid magic in auth response: %#x", magic)
                    return False

                body_len = length - HEADER_SIZE
                body = await reader.readexactly(body_len) if body_len > 0 else b""

                if msg_type == MsgType.AUTH_RSP:
                    success, message = ProtocolDecoder.decode_auth_rsp(body)
                    self._server_read_ahead_enabled = (
                        success and self._auth_message_enables_read_ahead(message)
                    )
                    self._server_read_collect_enabled = (
                        success and self._auth_message_enables_read_collect(message)
                    )
                    self._server_write_collect_enabled = (
                        success and self._auth_message_enables_write_collect(message)
                    )
                    self._server_sweep_shadow_enabled = (
                        success and self._auth_message_enables_sweep_shadow(message)
                    )
                    self._server_local_live_data_enabled = (
                        success and self._auth_message_enables_local_live_data(message)
                    )
                    self._server_connection_epoch = (
                        self._auth_message_connection_epoch(message)
                        if success
                        else None
                    )
                    if success:
                        logger.info("Reverse server authentication succeeded")
                        self._emit_client_event(
                            "reverse_client.lifecycle.auth_succeeded",
                            reason=message or "auth_ok",
                            connection_epoch=self._server_connection_epoch,
                            instance_id=self._instance_id,
                            attempt_label=attempt_label,
                        )
                    else:
                        logger.error(
                            "[CLIENT_CONN] instance=%s %s authentication failed: %s",
                            self._instance_id,
                            attempt_label,
                            message,
                        )
                        self._emit_client_event(
                            "reverse_client.lifecycle.auth_failed",
                            status="error",
                            failure_code="auth_failed",
                            failure_domain="cloud_proxy_tunnel",
                            reason=message,
                            instance_id=self._instance_id,
                            attempt_label=attempt_label,
                        )
                    return success

                if msg_type == MsgType.HEARTBEAT_ACK:
                    self._server_read_ahead_enabled = False
                    self._server_read_collect_enabled = False
                    self._server_write_collect_enabled = False
                    self._server_sweep_shadow_enabled = False
                    self._server_local_live_data_enabled = False
                    self._server_connection_epoch = None
                    logger.warning(
                        "[CLIENT_CONN] instance=%s %s server accepted auth as legacy heartbeat",
                        self._instance_id,
                        attempt_label,
                    )
                    self._emit_client_event(
                        "reverse_client.lifecycle.auth_legacy_ack",
                        reason="legacy_heartbeat_ack",
                        instance_id=self._instance_id,
                        attempt_label=attempt_label,
                    )
                    return True

                logger.warning(
                    "[CLIENT_CONN] instance=%s %s unexpected auth response type: %#x",
                    self._instance_id,
                    attempt_label,
                    msg_type,
                )
                self._server_read_ahead_enabled = False
                self._server_read_collect_enabled = False
                self._server_write_collect_enabled = False
                self._server_sweep_shadow_enabled = False
                self._server_local_live_data_enabled = False
                self._emit_client_event(
                    "reverse_client.lifecycle.auth_failed",
                    status="error",
                    failure_code="unexpected_auth_response",
                    failure_domain="cloud_proxy_tunnel",
                    reason=f"unexpected_auth_response:{msg_type:#x}",
                    instance_id=self._instance_id,
                    attempt_label=attempt_label,
                )
                return False
            except asyncio.TimeoutError:
                self._server_read_ahead_enabled = False
                self._server_read_collect_enabled = False
                self._server_write_collect_enabled = False
                self._server_sweep_shadow_enabled = False
                self._server_local_live_data_enabled = False
                self._server_connection_epoch = None
                logger.error(
                    "[CLIENT_CONN] instance=%s %s auth response timeout after %ss",
                    self._instance_id,
                    attempt_label,
                    self.config.auth.auth_timeout_s,
                )
                self._emit_client_event(
                    "reverse_client.lifecycle.auth_failed",
                    status="error",
                    failure_code="auth_timeout",
                    failure_domain="cloud_proxy_tunnel",
                    reason="auth_timeout",
                    instance_id=self._instance_id,
                    attempt_label=attempt_label,
                )
                return False

        msg = ProtocolEncoder.encode_heartbeat(0)
        self._server_local_live_data_enabled = False
        await self._write_tunnel_frame(writer, msg)
        logger.debug(
            "[CLIENT_CONN] instance=%s %s sent registration heartbeat phase=1",
            self._instance_id,
            attempt_label,
        )

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
            self._emit_client_event(
                "reverse_client.lifecycle.registration_failed",
                status="error",
                failure_code="registration_timeout",
                failure_domain="cloud_proxy_tunnel",
                reason="registration_timeout",
                instance_id=self._instance_id,
                attempt_label=attempt_label,
            )
            return False

        msg2 = ProtocolEncoder.encode_heartbeat(1)
        await self._write_tunnel_frame(writer, msg2)
        logger.debug(
            "[CLIENT_CONN] instance=%s %s sent registration heartbeat phase=2",
            self._instance_id,
            attempt_label,
        )
        return True

    async def _handle_requests(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        attempt_label: str = "attempt=unknown",
    ) -> None:
        while self.running:
            try:
                header = await asyncio.wait_for(reader.readexactly(HEADER_SIZE), timeout=25.0)
                magic, length, msg_type, sequence = Message.decode_header(header)

                if magic != MAGIC:
                    raise ConnectionError(
                        f"reverse server sent invalid frame magic {magic:#x}"
                    )

                body_len = length - HEADER_SIZE
                body = await reader.readexactly(body_len) if body_len > 0 else b""
                msg_name = MsgType(msg_type).name if msg_type in MsgType._value2member_map_ else f"0x{msg_type:04x}"
                worker_request_id = generate_request_id()
                request_context = self._request_log_context(
                    sequence=sequence,
                    msg_name=msg_name,
                    worker_request_id=worker_request_id,
                )
                self._emit_client_event(
                    "proxy.request.client_received",
                    context=request_context,
                    reason="client_received",
                    msg_type=msg_type,
                    msg_name=msg_name,
                )

                t0 = time.monotonic()
                response = await self._handle_message(
                    msg_type,
                    body,
                    sequence,
                    request_context=request_context,
                )
                hw_ms = (time.monotonic() - t0) * 1000

                if response:
                    response = attach_timing_trailer(response, hw_ms)
                    await self._write_tunnel_frame(writer, response)

            except asyncio.TimeoutError:
                logger.debug(
                    "[CLIENT_CONN] instance=%s %s idle for 25s, sending heartbeat",
                    self._instance_id,
                    attempt_label,
                )
                self._emit_client_event(
                    "reverse_client.lifecycle.idle_heartbeat",
                    reason="idle_heartbeat",
                    instance_id=self._instance_id,
                    attempt_label=attempt_label,
                )
                await self._write_tunnel_frame(writer, ProtocolEncoder.encode_heartbeat(0))
            except asyncio.IncompleteReadError as exc:
                raise ConnectionError(
                    "reverse server disconnected: EOF while waiting for messages "
                    f"(expected {exc.expected} bytes, got {len(exc.partial)})"
                )
            except ConnectionError:
                raise
            except Exception as exc:
                logger.exception(
                    "[CLIENT_CONN] instance=%s %s request handling error",
                    self._instance_id,
                    attempt_label,
                )
                raise

    async def _prewarm_open(self) -> None:
        if self.driver is None:
            return

        logger.debug("Pre-warm: calling PassThruOpen in background")
        loop = asyncio.get_running_loop()
        try:
            ret, device_id = await loop.run_in_executor(None, self.driver.open, None)
            if ret == 0:
                self._prewarm_device_id = device_id
                self._prewarm_ret = ret
                logger.debug("Pre-warm: PassThruOpen OK, device_id=%s", device_id)
            else:
                logger.debug("Pre-warm skipped: ret=%s (%s)", ret, self._get_error_name(ret))
                self._prewarm_device_id = None
                self._prewarm_ret = None
        except Exception as exc:
            logger.warning("Pre-warm open failed: %s", exc)
            self._prewarm_device_id = None
            self._prewarm_ret = None

    async def _handle_open(self, body: bytes, sequence: int, request_context: LogContext | None = None) -> bytes:
        device_name = ProtocolDecoder.decode_open_req(body)

        if self._prewarm_task is not None and not self._prewarm_task.done():
            logger.debug("OPEN_REQ arrived while pre-warm was in progress")
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
            self._log_j2534_result("PassThruOpen", ret, detail=f" device_id={device_id} [pre-warmed]")
            return ProtocolEncoder.encode_open_rsp(ret, device_id, sequence)

        request_context = self._ensure_request_context(
            request_context,
            sequence=sequence,
            msg_name="OPEN_REQ",
        )
        ret, device_id = await self._run_driver_call(
            "open",
            device_name,
            request_context=request_context,
            result_metadata={"device_name": device_name or ""},
        )
        self._log_j2534_result("PassThruOpen", ret, detail=f" device_id={device_id}")
        return ProtocolEncoder.encode_open_rsp(ret, device_id, sequence)

    async def _handle_close(self, body: bytes, sequence: int, request_context: LogContext | None = None) -> bytes:
        device_id = ProtocolDecoder.decode_close_req(body)
        request_context = self._ensure_request_context(
            request_context,
            sequence=sequence,
            msg_name="CLOSE_REQ",
        )
        ret = await self._run_driver_call(
            "close",
            device_id,
            request_context=request_context,
            result_metadata={"device_id": device_id},
        )
        self._log_j2534_result("PassThruClose", ret, detail=f" device_id={device_id}")
        self._ioctl_cache.invalidate()
        self._stop_local_live_data_collector("device_closed")
        self._prewarm_device_id = None
        self._prewarm_ret = None
        return ProtocolEncoder.encode_close_rsp(ret, sequence)

    async def _handle_connect(self, body: bytes, sequence: int, request_context: LogContext | None = None) -> bytes:
        device_id, protocol_id, flags, baudrate = ProtocolDecoder.decode_connect_req(body)
        request_context = self._ensure_request_context(
            request_context,
            sequence=sequence,
            msg_name="CONNECT_REQ",
        )
        ret, channel_id = await self._run_driver_call(
            "connect",
            device_id,
            protocol_id,
            flags,
            baudrate,
            request_context=request_context,
            result_metadata={
                "device_id": device_id,
                "protocol_id": protocol_id,
                "baudrate": baudrate,
            },
        )
        self._log_j2534_result(
            "PassThruConnect",
            ret,
            detail=f" device_id={device_id} protocol={protocol_id} baud={baudrate} channel_id={channel_id}",
        )
        if int(ret) == 0:
            if int(protocol_id) == ISO15765_PROTOCOL_ID:
                self._local_live_data_channel = LocalLiveDataChannel(
                    channel_id=int(channel_id),
                    protocol_id=int(protocol_id),
                )
                self._local_live_data_collector.start(reason="channel_connected")
            elif self.config.local_live_data.enabled:
                self._emit_client_event(
                    "proxy.local_live_data.unsupported",
                    context=request_context,
                    impact_scope="proxy_local_live_data",
                    reason="unsupported_protocol",
                    signal_key="engine_speed",
                    request_kind="uds_did_000c",
                    protocol_id=int(protocol_id),
                    channel_id=int(channel_id),
                    supported_protocol_id=ISO15765_PROTOCOL_ID,
                )
        return ProtocolEncoder.encode_connect_rsp(ret, channel_id, sequence)

    async def _handle_disconnect(self, body: bytes, sequence: int, request_context: LogContext | None = None) -> bytes:
        channel_id = ProtocolDecoder.decode_disconnect_req(body)
        request_context = self._ensure_request_context(
            request_context,
            sequence=sequence,
            msg_name="DISCONNECT_REQ",
        )
        ret = await self._run_driver_call(
            "disconnect",
            channel_id,
            request_context=request_context,
            result_metadata={"channel_id": channel_id},
        )
        self._log_j2534_result("PassThruDisconnect", ret, detail=f" channel_id={channel_id}")
        self._ioctl_cache.invalidate()
        if (
            self._local_live_data_channel is None
            or self._local_live_data_channel.channel_id == int(channel_id)
        ):
            self._stop_local_live_data_collector("channel_disconnected")
        return ProtocolEncoder.encode_disconnect_rsp(ret, sequence)

    async def _handle_read_msgs_common(
        self,
        body: bytes,
        sequence: int,
        request_context: LogContext | None = None,
        *,
        collect_window_ms: int | None = None,
        max_reads: int | None = None,
        read_timeout_ms: int | None = None,
        max_messages: int | None = None,
        min_drain_ms: int | None = None,
        local_max_reads: int | None = None,
        stop_after_empty_once_min_drain_elapsed: bool = False,
        extra_read_after_data_at_max: bool = False,
    ) -> bytes:
        channel_id, num_msgs, timeout = ProtocolDecoder.decode_read_msgs_req(body)
        request_context = self._ensure_request_context(
            request_context,
            sequence=sequence,
            msg_name="READ_MSGS_REQ",
        )
        ret, messages = await self._run_driver_call(
            "read_msgs",
            channel_id,
            num_msgs,
            timeout,
            request_context=request_context,
            ok_codes=(0, BUFFER_EMPTY),
            result_metadata={"channel_id": channel_id, "num_msgs": num_msgs},
        )
        self._log_j2534_result(
            "ReadMsgs",
            ret,
            detail=f" channel_id={channel_id} count={len(messages)}",
            ok_codes=(0, BUFFER_EMPTY),
        )
        response = ProtocolEncoder.encode_read_msgs_rsp(ret, messages, sequence)
        if ret not in (0, BUFFER_EMPTY):
            return response

        collect_requested = any(
            value is not None
            for value in (
                collect_window_ms,
                max_reads,
                read_timeout_ms,
                max_messages,
                min_drain_ms,
            )
        )
        if not collect_requested:
            return response

        read_rsp_bodies = await self._collect_read_ahead_bodies(
            channel_id,
            request_context,
            collect_window_ms=collect_window_ms,
            max_reads=max_reads,
            read_timeout_ms=read_timeout_ms,
            max_messages=max_messages,
            min_drain_ms=min_drain_ms,
            local_max_reads=local_max_reads,
            stop_after_empty_once_min_drain_elapsed=(
                stop_after_empty_once_min_drain_elapsed
            ),
            extra_read_after_data_at_max=extra_read_after_data_at_max,
            include_empty_confirmations=True,
            foreground_had_data=(ret == 0 and bool(messages)),
        )
        return attach_read_msgs_prefetch_bundle(
            response,
            channel_id=channel_id,
            read_rsp_bodies=read_rsp_bodies,
        )

    async def _handle_read_msgs(self, body: bytes, sequence: int, request_context: LogContext | None = None) -> bytes:
        return await self._handle_read_msgs_common(body, sequence, request_context)

    def _read_collect_min_drain_ms(self, collect_window_ms: int) -> int:
        configured_ms = max(0, int(self.config.read_ahead.min_drain_ms))
        if configured_ms <= 0:
            return 0
        return min(
            configured_ms,
            READ_COLLECT_MIN_DRAIN_CAP_MS,
            max(0, int(collect_window_ms)),
        )

    async def _collect_read_ahead_bodies(
        self,
        channel_id: int,
        request_context: LogContext,
        *,
        collect_window_ms: int | None = None,
        max_reads: int | None = None,
        read_timeout_ms: int | None = None,
        max_messages: int | None = None,
        min_drain_ms: int | None = None,
        local_max_reads: int | None = None,
        stop_after_empty_once_min_drain_elapsed: bool = False,
        extra_read_after_data_at_max: bool = False,
        soft_max_reads_after_data: int | None = None,
        include_empty_confirmations: bool = False,
        foreground_had_data: bool = False,
    ) -> list[bytes]:
        read_ahead = self.config.read_ahead
        if (
            not read_ahead.enabled
            or not self._server_read_ahead_enabled
        ):
            return []

        effective_window_ms = min(
            read_ahead.window_ms,
            read_ahead.window_ms if collect_window_ms is None else int(collect_window_ms),
        )
        local_max_reads_limit = (
            read_ahead.max_reads if local_max_reads is None else int(local_max_reads)
        )
        effective_max_reads = min(
            local_max_reads_limit,
            read_ahead.max_reads if max_reads is None else int(max_reads),
        )
        effective_timeout_ms = min(
            read_ahead.read_timeout_ms,
            read_ahead.read_timeout_ms if read_timeout_ms is None else int(read_timeout_ms),
        )
        effective_max_messages = min(
            read_ahead.max_messages,
            read_ahead.max_messages if max_messages is None else int(max_messages),
        )
        effective_max_empty_reads = int(read_ahead.max_empty_reads)
        effective_max_consecutive_empty_reads = int(
            read_ahead.max_consecutive_empty_reads
        )
        effective_min_drain_ms = min(
            effective_window_ms,
            max(
                0,
                int(read_ahead.min_drain_ms if min_drain_ms is None else min_drain_ms),
            ),
        )
        effective_soft_max_reads_after_data = 0
        if soft_max_reads_after_data is not None:
            effective_soft_max_reads_after_data = min(
                effective_max_reads,
                max(0, int(soft_max_reads_after_data)),
            )
        if (
            effective_window_ms <= 0
            or effective_max_reads <= 0
            or effective_max_messages <= 0
        ):
            return []

        started_at_mono = time.monotonic()
        deadline = started_at_mono + max(0, effective_window_ms) / 1000.0
        min_drain_until = started_at_mono + effective_min_drain_ms / 1000.0
        collected: list[bytes] = []
        empty_confirmation_body: bytes | None = None
        empty_confirmation_after_drain = False
        collected_messages = 0
        attempted_reads = 0
        data_reads = 0
        empty_reads = 0
        consecutive_empty_reads = 0
        empty_after_data_grace_used = False
        empty_after_data_grace_extra_read_used = False
        empty_after_data_grace_extra_read_pending = False
        empty_after_data_grace_skipped_reason: str | None = None
        empty_after_data_grace_sleep_ms = 0.0
        empty_after_data_grace_data_extra_read_used = False
        empty_after_data_grace_data_extra_read_attempts = 0
        empty_after_data_grace_data_extra_read_limit = (
            READ_COLLECT_EMPTY_GRACE_DATA_EXTRA_READS
            if extra_read_after_data_at_max
            else 0
        )
        empty_after_data_grace_data_extra_read_pending = False
        extra_read_after_data_at_max_used = False
        extra_read_after_data_at_max_attempts = 0
        extra_read_after_data_at_max_limit = (
            READ_COLLECT_DATA_AT_MAX_EXTRA_READS
            if extra_read_after_data_at_max
            else 0
        )
        extra_read_after_data_at_max_pending = False
        stop_reason = "max_reads"
        read_index = 0
        while True:
            if time.monotonic() > deadline:
                stop_reason = "window_elapsed"
                break

            current_read_is_extra_grace = False
            current_read_is_extra_grace_data = False
            current_read_is_extra_data_at_max = False
            if read_index >= effective_max_reads:
                if empty_after_data_grace_extra_read_pending:
                    empty_after_data_grace_extra_read_pending = False
                    empty_after_data_grace_extra_read_used = True
                    current_read_is_extra_grace = True
                elif empty_after_data_grace_data_extra_read_pending:
                    empty_after_data_grace_data_extra_read_pending = False
                    empty_after_data_grace_data_extra_read_used = True
                    empty_after_data_grace_data_extra_read_attempts += 1
                    current_read_is_extra_grace_data = True
                elif extra_read_after_data_at_max_pending:
                    extra_read_after_data_at_max_pending = False
                    extra_read_after_data_at_max_used = True
                    extra_read_after_data_at_max_attempts += 1
                    current_read_is_extra_data_at_max = True
                else:
                    if empty_after_data_grace_data_extra_read_attempts > 0:
                        stop_reason = "empty_after_data_grace_data_extra_limit"
                    elif extra_read_after_data_at_max_attempts > 0:
                        stop_reason = "extra_read_after_data_at_max_limit"
                    else:
                        stop_reason = "max_reads"
                    break

            remaining = effective_max_messages - collected_messages
            if remaining <= 0:
                stop_reason = "max_messages"
                break

            current_read_index = read_index
            read_index += 1
            attempted_reads += 1
            try:
                ret, messages = await self._run_driver_call(
                    "read_msgs",
                    channel_id,
                    remaining,
                    effective_timeout_ms,
                    request_context=request_context,
                    ok_codes=(0, BUFFER_EMPTY),
                    result_metadata={
                        "channel_id": channel_id,
                        "num_msgs": remaining,
                        "read_ahead": True,
                        "read_ahead_index": current_read_index,
                        "read_ahead_extra_after_data_grace": (
                            current_read_is_extra_grace
                        ),
                        "read_ahead_extra_after_data_grace_data": (
                            current_read_is_extra_grace_data
                        ),
                        "read_ahead_extra_after_data_at_max": (
                            current_read_is_extra_data_at_max
                        ),
                    },
                )
            except Exception:
                logger.warning("Read-ahead failed on channel_id=%s", channel_id, exc_info=True)
                stop_reason = "driver_exception"
                break

            if ret not in (0, BUFFER_EMPTY):
                stop_reason = f"return_code_{ret}"
                break

            if ret == BUFFER_EMPTY or not messages:
                empty_confirmation_body = ProtocolEncoder.encode_read_msgs_rsp(
                    BUFFER_EMPTY,
                    [],
                    0,
                )[HEADER_SIZE:]
                empty_confirmation_after_drain = False
                empty_reads += 1
                consecutive_empty_reads += 1
                now = time.monotonic()
                if (
                    collected_messages > 0
                    and effective_soft_max_reads_after_data > 0
                    and effective_soft_max_reads_after_data < effective_max_reads
                    and attempted_reads >= effective_soft_max_reads_after_data
                ):
                    stop_reason = "soft_max_reads_after_data"
                    break
                if now < min_drain_until and read_index < effective_max_reads:
                    sleep_s = min(
                        0.005,
                        max(0.0, min(min_drain_until, deadline) - now),
                    )
                    if sleep_s > 0:
                        await asyncio.sleep(sleep_s)
                    continue
                empty_confirmation_after_drain = (
                    effective_min_drain_ms > 0
                    and now >= min_drain_until
                )
                if stop_after_empty_once_min_drain_elapsed:
                    if collected_messages <= 0:
                        stop_reason = "empty_after_min_drain"
                        break
                    if not empty_after_data_grace_used:
                        if read_index >= effective_max_reads:
                            if extra_read_after_data_at_max_used:
                                stop_reason = "extra_read_after_data_at_max_empty"
                                break
                            if now >= deadline:
                                empty_after_data_grace_skipped_reason = "window_elapsed"
                                stop_reason = "window_elapsed"
                                break
                            empty_after_data_grace_extra_read_pending = True
                        sleep_s = min(
                            READ_COLLECT_MIN_DRAIN_CAP_MS / 1000.0,
                            max(0.0, min(min_drain_until, deadline) - now),
                        )
                        if sleep_s > 0:
                            await asyncio.sleep(sleep_s)
                            empty_after_data_grace_sleep_ms += sleep_s * 1000.0
                        empty_after_data_grace_used = True
                        continue
                    if empty_after_data_grace_data_extra_read_used:
                        stop_reason = "empty_after_data_grace_data_extra_empty"
                        break
                    stop_reason = "empty_after_data_grace_empty"
                    break
                if (
                    effective_max_empty_reads > 0
                    and empty_reads >= effective_max_empty_reads
                ):
                    stop_reason = "max_empty_reads"
                    break
                if (
                    effective_max_consecutive_empty_reads > 0
                    and consecutive_empty_reads >= effective_max_consecutive_empty_reads
                ):
                    stop_reason = "max_consecutive_empty_reads"
                    break
                continue

            limited_messages = messages[:remaining]
            empty_confirmation_body = None
            empty_confirmation_after_drain = False
            data_reads += 1
            consecutive_empty_reads = 0
            collected_messages += len(limited_messages)
            collected.append(
                ProtocolEncoder.encode_read_msgs_rsp(0, limited_messages, 0)[HEADER_SIZE:]
            )
            if collected_messages >= effective_max_messages:
                stop_reason = "max_messages"
                break
            if (
                effective_soft_max_reads_after_data > 0
                and effective_soft_max_reads_after_data < effective_max_reads
                and attempted_reads >= effective_soft_max_reads_after_data
            ):
                stop_reason = "soft_max_reads_after_data"
                break
            if (
                extra_read_after_data_at_max
                and stop_after_empty_once_min_drain_elapsed
                and not empty_after_data_grace_used
                and not empty_after_data_grace_extra_read_used
                and extra_read_after_data_at_max_attempts
                < extra_read_after_data_at_max_limit
                and read_index >= effective_max_reads
                and time.monotonic() < deadline
            ):
                extra_read_after_data_at_max_pending = True
                continue
            if (
                extra_read_after_data_at_max
                and stop_after_empty_once_min_drain_elapsed
                and empty_after_data_grace_used
                and empty_after_data_grace_extra_read_used
                and empty_after_data_grace_data_extra_read_attempts
                < empty_after_data_grace_data_extra_read_limit
                and read_index >= effective_max_reads
                and time.monotonic() < deadline
            ):
                empty_after_data_grace_data_extra_read_pending = True
                continue

        should_attach_empty_confirmation = (
            include_empty_confirmations
            and empty_confirmation_body is not None
            and (
                foreground_had_data
                or collected_messages > 0
                or empty_confirmation_after_drain
            )
        )
        if should_attach_empty_confirmation:
            collected.append(empty_confirmation_body)

        if collected:
            logger.debug(
                "Collected %s read-ahead ReadMsgs response(s) on channel_id=%s",
                len(collected),
                channel_id,
            )
        self._emit_client_event(
            "read_ahead.collection_finished",
            context=request_context,
            reason=stop_reason,
            channel_id=channel_id,
            attempted_reads=attempted_reads,
            data_reads=data_reads,
            empty_reads=empty_reads,
            consecutive_empty_reads=consecutive_empty_reads,
            collected_responses=len(collected),
            collected_messages=collected_messages,
            collect_window_ms=effective_window_ms,
            max_reads=effective_max_reads,
            local_max_reads=local_max_reads_limit,
            read_timeout_ms=effective_timeout_ms,
            max_messages=effective_max_messages,
            max_empty_reads=effective_max_empty_reads,
            max_consecutive_empty_reads=effective_max_consecutive_empty_reads,
            min_drain_ms=effective_min_drain_ms,
            soft_max_reads_after_data=effective_soft_max_reads_after_data,
            stop_after_empty_once_min_drain_elapsed=(
                stop_after_empty_once_min_drain_elapsed
            ),
            empty_after_data_grace_used=empty_after_data_grace_used,
            empty_after_data_grace_extra_read_used=(
                empty_after_data_grace_extra_read_used
            ),
            empty_after_data_grace_skipped_reason=(
                empty_after_data_grace_skipped_reason
            ),
            empty_after_data_grace_sleep_ms=round(
                empty_after_data_grace_sleep_ms,
                3,
            ),
            empty_after_data_grace_data_extra_read_used=(
                empty_after_data_grace_data_extra_read_used
            ),
            empty_after_data_grace_data_extra_read_attempts=(
                empty_after_data_grace_data_extra_read_attempts
            ),
            empty_after_data_grace_data_extra_read_limit=(
                empty_after_data_grace_data_extra_read_limit
            ),
            extra_read_after_data_at_max_enabled=extra_read_after_data_at_max,
            extra_read_after_data_at_max_used=extra_read_after_data_at_max_used,
            extra_read_after_data_at_max_attempts=(
                extra_read_after_data_at_max_attempts
            ),
            extra_read_after_data_at_max_limit=extra_read_after_data_at_max_limit,
        )
        return collected

    async def _handle_write_msgs_common(
        self,
        body: bytes,
        sequence: int,
        request_context: LogContext | None = None,
        *,
        collect_window_ms: int | None = None,
        max_reads: int | None = None,
        read_timeout_ms: int | None = None,
        max_messages: int | None = None,
        stop_after_empty_once_min_drain_elapsed: bool | None = None,
    ) -> bytes:
        channel_id, messages, timeout = ProtocolDecoder.decode_write_msgs_req(body)
        request_context = self._ensure_request_context(
            request_context,
            sequence=sequence,
            msg_name="WRITE_MSGS_REQ",
        )
        ret, num_written = await self._run_driver_call(
            "write_msgs",
            channel_id,
            messages,
            timeout,
            request_context=request_context,
            result_metadata={"channel_id": channel_id, "requested_count": len(messages)},
        )
        self._log_j2534_result(
            "WriteMsgs",
            ret,
            detail=f" channel_id={channel_id} written={num_written} requested={len(messages)}",
        )
        response = ProtocolEncoder.encode_write_msgs_rsp(ret, num_written, sequence)
        if ret != 0:
            return response

        soft_max_reads_after_data = None
        if (
            self.config.read_ahead.write_collect_max_reads
            > self.config.read_ahead.max_reads
        ):
            soft_max_reads_after_data = self.config.read_ahead.max_reads

        read_rsp_bodies = await self._collect_read_ahead_bodies(
            channel_id,
            request_context,
            collect_window_ms=collect_window_ms,
            max_reads=max_reads,
            read_timeout_ms=read_timeout_ms,
            max_messages=max_messages,
            local_max_reads=self.config.read_ahead.write_collect_max_reads,
            soft_max_reads_after_data=soft_max_reads_after_data,
            stop_after_empty_once_min_drain_elapsed=(
                self.config.read_ahead.min_drain_ms > 0
                if stop_after_empty_once_min_drain_elapsed is None
                else stop_after_empty_once_min_drain_elapsed
            ),
            include_empty_confirmations=True,
        )
        return attach_read_msgs_prefetch_bundle(
            response,
            channel_id=channel_id,
            read_rsp_bodies=read_rsp_bodies,
        )

    async def _handle_write_msgs(self, body: bytes, sequence: int, request_context: LogContext | None = None) -> bytes:
        return await self._handle_write_msgs_common(body, sequence, request_context)

    async def _handle_write_and_collect_reads(
        self,
        body: bytes,
        sequence: int,
        request_context: LogContext | None = None,
    ) -> bytes:
        request = ProtocolDecoder.decode_write_and_collect_reads_req(body)
        if not (
            self.config.read_ahead.enabled
            and self.config.read_ahead.transaction_enabled
            and self._server_read_ahead_enabled
            and self._server_write_collect_enabled
        ):
            return await self._handle_write_msgs_common(
                request.write_req_body,
                sequence,
                request_context,
            )
        return await self._handle_write_msgs_common(
            request.write_req_body,
            sequence,
            request_context,
            collect_window_ms=request.collect_window_ms,
            max_reads=request.max_reads,
            read_timeout_ms=request.read_timeout_ms,
            max_messages=request.max_messages,
        )

    async def _handle_read_and_collect_reads(
        self,
        body: bytes,
        sequence: int,
        request_context: LogContext | None = None,
    ) -> bytes:
        request = ProtocolDecoder.decode_read_and_collect_reads_req(body)
        if not (
            self.config.read_ahead.enabled
            and self.config.read_ahead.transaction_enabled
            and self._server_read_ahead_enabled
            and self._server_read_collect_enabled
        ):
            return await self._handle_read_msgs_common(
                request.read_req_body,
                sequence,
                request_context,
            )
        return await self._handle_read_msgs_common(
            request.read_req_body,
            sequence,
            request_context,
            collect_window_ms=request.collect_window_ms,
            max_reads=request.max_reads,
            read_timeout_ms=request.read_timeout_ms,
            max_messages=request.max_messages,
            local_max_reads=self.config.read_ahead.write_collect_max_reads,
            min_drain_ms=self._read_collect_min_drain_ms(
                request.collect_window_ms,
            ),
            stop_after_empty_once_min_drain_elapsed=True,
            extra_read_after_data_at_max=True,
        )

    async def _handle_read_version(self, body: bytes, sequence: int, request_context: LogContext | None = None) -> bytes:
        device_id = ProtocolDecoder.decode_read_version_req(body)
        request_context = self._ensure_request_context(
            request_context,
            sequence=sequence,
            msg_name="READ_VERSION_REQ",
        )
        ret, fw, dll, api = await self._run_driver_call(
            "read_version",
            device_id,
            request_context=request_context,
            result_metadata={"device_id": device_id},
        )
        self._log_j2534_result("PassThruReadVersion", ret, detail=f" device_id={device_id}")
        return ProtocolEncoder.encode_read_version_rsp(ret, fw, dll, api, sequence)

    async def _handle_start_filter(self, body: bytes, sequence: int, request_context: LogContext | None = None) -> bytes:
        channel_id, filter_type, mask_msg, pattern_msg, flow_msg = (
            ProtocolDecoder.decode_start_filter_req(body)
        )
        request_context = self._ensure_request_context(
            request_context,
            sequence=sequence,
            msg_name="START_FILTER_REQ",
        )
        ret, filter_id = await self._run_driver_call(
            "start_msg_filter",
            channel_id,
            filter_type,
            mask_msg,
            pattern_msg,
            flow_msg,
            request_context=request_context,
            result_metadata={"channel_id": channel_id, "filter_type": filter_type},
        )
        self._log_j2534_result(
            "StartMsgFilter",
            ret,
            detail=f" channel_id={channel_id} filter_type={filter_type} filter_id={filter_id}",
        )
        return ProtocolEncoder.encode_start_filter_rsp(ret, filter_id, sequence)

    async def _handle_stop_filter(self, body: bytes, sequence: int, request_context: LogContext | None = None) -> bytes:
        channel_id, filter_id = ProtocolDecoder.decode_stop_filter_req(body)
        request_context = self._ensure_request_context(
            request_context,
            sequence=sequence,
            msg_name="STOP_FILTER_REQ",
        )
        ret = await self._run_driver_call(
            "stop_msg_filter",
            channel_id,
            filter_id,
            request_context=request_context,
            result_metadata={"channel_id": channel_id, "filter_id": filter_id},
        )
        self._log_j2534_result(
            "StopMsgFilter",
            ret,
            detail=f" channel_id={channel_id} filter_id={filter_id}",
        )
        return ProtocolEncoder.encode_stop_filter_rsp(ret, sequence)

    async def _handle_ioctl(self, body: bytes, sequence: int, request_context: LogContext | None = None) -> bytes:
        channel_id, ioctl_id, input_data = ProtocolDecoder.decode_ioctl_req(body)

        cached = self._ioctl_cache.try_get_cached(channel_id, ioctl_id)
        if cached is not None:
            ret, output_data = cached
            logger.debug("<< Ioctl(%#x) -> [cached] ret=%s", ioctl_id, ret)
            return ProtocolEncoder.encode_ioctl_rsp(ret, output_data, sequence)

        request_context = self._ensure_request_context(
            request_context,
            sequence=sequence,
            msg_name="IOCTL_REQ",
        )
        ret, output_data = await self._run_driver_call(
            "ioctl",
            channel_id,
            ioctl_id,
            input_data,
            request_context=request_context,
            result_metadata={"channel_id": channel_id, "ioctl_id": ioctl_id},
        )
        self._log_j2534_result(
            "PassThruIoctl",
            ret,
            detail=f" channel_id={channel_id} ioctl={ioctl_id:#x}",
        )
        self._ioctl_cache.record_result(channel_id, ioctl_id, ret, output_data)
        return ProtocolEncoder.encode_ioctl_rsp(ret, output_data, sequence)

    async def _handle_sweep_plan_start(
        self,
        body: bytes,
        sequence: int,
        request_context: LogContext | None = None,
    ) -> bytes:
        request = decode_sweep_plan_start_req(body)
        if not (self.config.local_sweep.shadow_transport_enabled and self._server_sweep_shadow_enabled):
            return encode_sweep_plan_start_rsp(
                SweepPlanResponse(
                    success=False,
                    plan_id=request.plan_id,
                    reason="shadow_not_enabled_or_not_negotiated",
                ),
                sequence=sequence,
            )
        success, reason = self._sweep_executor.start(request)
        return encode_sweep_plan_start_rsp(
            SweepPlanResponse(success=success, plan_id=request.plan_id, reason=reason),
            sequence=sequence,
        )

    async def _handle_sweep_plan_stop(
        self,
        body: bytes,
        sequence: int,
        request_context: LogContext | None = None,
    ) -> bytes:
        request = decode_sweep_plan_stop_req(body)
        self._sweep_executor.stop(request.reason or "server_stop")
        return encode_sweep_plan_stop_rsp(
            SweepPlanResponse(success=True, plan_id=request.plan_id, reason="stopped"),
            sequence=sequence,
        )

    async def _handle_sweep_status(
        self,
        body: bytes,
        sequence: int,
        request_context: LogContext | None = None,
    ) -> bytes:
        return encode_sweep_status_rsp(self._sweep_executor.status(), sequence=sequence)

    async def _handle_sweep_drain_results(
        self,
        body: bytes,
        sequence: int,
        request_context: LogContext | None = None,
    ) -> bytes:
        return encode_sweep_drain_results_rsp(
            self._sweep_executor.drain(),
            sequence=sequence,
        )

    _DISPATCH = {
        MsgType.OPEN_REQ: _handle_open,
        MsgType.CLOSE_REQ: _handle_close,
        MsgType.CONNECT_REQ: _handle_connect,
        MsgType.DISCONNECT_REQ: _handle_disconnect,
        MsgType.READ_MSGS_REQ: _handle_read_msgs,
        MsgType.READ_AND_COLLECT_READS_REQ: _handle_read_and_collect_reads,
        MsgType.WRITE_MSGS_REQ: _handle_write_msgs,
        MsgType.WRITE_AND_COLLECT_READS_REQ: _handle_write_and_collect_reads,
        MsgType.READ_VERSION_REQ: _handle_read_version,
        MsgType.START_FILTER_REQ: _handle_start_filter,
        MsgType.STOP_FILTER_REQ: _handle_stop_filter,
        MsgType.IOCTL_REQ: _handle_ioctl,
        MsgType.SWEEP_PLAN_START_REQ: _handle_sweep_plan_start,
        MsgType.SWEEP_PLAN_STOP_REQ: _handle_sweep_plan_stop,
        MsgType.SWEEP_STATUS_REQ: _handle_sweep_status,
        MsgType.SWEEP_DRAIN_RESULTS_REQ: _handle_sweep_drain_results,
    }

    async def _handle_message(
        self,
        msg_type: int,
        body: bytes,
        sequence: int,
        request_context: LogContext | None = None,
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
        foreground_request = not is_sweep_message_type(msg_type)
        if foreground_request:
            self._foreground_request_depth += 1
            self._cancel_shadow_for_foreground_if_needed(msg_type, body)
        try:
            return await handler(self, body, sequence, request_context=request_context)
        finally:
            if foreground_request:
                self._foreground_request_depth = max(0, self._foreground_request_depth - 1)

    def stop(self) -> concurrent.futures.Future[None]:
        """Request a graceful shutdown and return a future for completion."""
        loop = self._loop
        logger.info(
            "[CLIENT_CTRL] stop requested instance=%s loop_running=%s",
            self._instance_id,
            bool(loop is not None and loop.is_running()),
        )
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
        "--tls",
        action="store_true",
        help="Enable TLS for the reverse tunnel connection",
    )
    parser.add_argument(
        "--tls-ca",
        default=None,
        help="CA bundle for validating the reverse server certificate",
    )
    parser.add_argument(
        "--tls-server-name",
        default=None,
        help="Override TLS server name for certificate validation",
    )
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
    parser.add_argument(
        "--read-ahead",
        dest="read_ahead",
        action="store_true",
        default=None,
        help="Enable local ReadMsgs read-ahead after successful writes (or VCI_PROXY_READ_AHEAD=1)",
    )
    parser.add_argument(
        "--no-read-ahead",
        dest="read_ahead",
        action="store_false",
        help="Disable local ReadMsgs read-ahead even if VCI_PROXY_READ_AHEAD is set",
    )
    parser.add_argument(
        "--read-ahead-window-ms",
        type=int,
        default=None,
        help="Read-ahead collection window in ms (default: 200 or VCI_PROXY_READ_AHEAD_WINDOW_MS)",
    )
    parser.add_argument(
        "--read-ahead-max-reads",
        type=int,
        default=None,
        help="Maximum local ReadMsgs calls for generic/read-tail collection (default: 3 or VCI_PROXY_READ_AHEAD_MAX_READS)",
    )
    parser.add_argument(
        "--read-ahead-write-collect-max-reads",
        type=int,
        default=None,
        help="Maximum local ReadMsgs calls after write-collect transactions (default: 6 or VCI_PROXY_READ_AHEAD_WRITE_COLLECT_MAX_READS)",
    )
    parser.add_argument(
        "--read-ahead-read-timeout-ms",
        type=int,
        default=None,
        help="Timeout passed to local ReadMsgs read-ahead calls (default: 0 or VCI_PROXY_READ_AHEAD_READ_TIMEOUT_MS)",
    )
    parser.add_argument(
        "--read-ahead-max-messages",
        type=int,
        default=None,
        help="Maximum prefetched messages retained per channel (default: 16 or VCI_PROXY_READ_AHEAD_MAX_MESSAGES)",
    )
    parser.add_argument(
        "--read-ahead-max-empty-reads",
        type=int,
        default=None,
        help="Stop local read-ahead after this many empty reads (default: 0 or VCI_PROXY_READ_AHEAD_MAX_EMPTY_READS; 0 disables)",
    )
    parser.add_argument(
        "--read-ahead-max-consecutive-empty-reads",
        type=int,
        default=None,
        help="Stop local read-ahead after this many consecutive empty reads (default: 0 or VCI_PROXY_READ_AHEAD_MAX_CONSECUTIVE_EMPTY_READS; 0 disables)",
    )
    parser.add_argument(
        "--read-ahead-min-drain-ms",
        type=int,
        default=None,
        help="Keep local read-ahead draining through early empty reads for at least this many ms (default: 0 or VCI_PROXY_READ_AHEAD_MIN_DRAIN_MS; 0 disables)",
    )
    parser.add_argument(
        "--read-ahead-transaction",
        dest="read_ahead_transaction",
        action="store_true",
        default=None,
        help="Advertise support for internal WRITE_AND_COLLECT_READS transaction RPC (or VCI_PROXY_READ_AHEAD_TRANSACTION=1)",
    )
    parser.add_argument(
        "--no-read-ahead-transaction",
        dest="read_ahead_transaction",
        action="store_false",
        help="Disable internal read-ahead transaction support even if VCI_PROXY_READ_AHEAD_TRANSACTION is set",
    )
    parser.add_argument(
        "--local-sweep",
        dest="local_sweep",
        action="store_true",
        default=None,
        help="Enable guarded local sweep shadow support (or VCI_PROXY_LOCAL_SWEEP=1)",
    )
    parser.add_argument(
        "--no-local-sweep",
        dest="local_sweep",
        action="store_false",
        help="Disable local sweep even if VCI_PROXY_LOCAL_SWEEP is set",
    )
    parser.add_argument(
        "--local-sweep-mode",
        choices=["observe_only", "shadow_local", "active_replay"],
        default=None,
        help="Local sweep mode (default: observe_only or VCI_PROXY_LOCAL_SWEEP_MODE)",
    )
    parser.add_argument(
        "--local-sweep-min-cycles",
        type=int,
        default=None,
        help="Minimum complete observed cycles before sweep candidacy",
    )
    parser.add_argument(
        "--local-sweep-max-items",
        type=int,
        default=None,
        help="Maximum learned signatures in one shadow plan",
    )
    parser.add_argument(
        "--local-sweep-min-item-interval-ms",
        type=int,
        default=None,
        help="Minimum delay between local shadow sweep items in ms",
    )
    parser.add_argument(
        "--local-sweep-read-timeout-ms",
        type=int,
        default=None,
        help="Minimum timeout for local shadow ReadMsgs calls in ms",
    )
    parser.add_argument(
        "--local-sweep-shadow-allow-gm-a9-packet",
        dest="local_sweep_shadow_allow_gm_a9_packet",
        action="store_true",
        default=None,
        help="Allow GM A9 packet signatures to run in shadow_local plans",
    )
    parser.add_argument(
        "--no-local-sweep-shadow-allow-gm-a9-packet",
        dest="local_sweep_shadow_allow_gm_a9_packet",
        action="store_false",
        help="Keep GM A9 packet signatures observe-only even in shadow_local mode",
    )
    parser.add_argument(
        "--local-sweep-shadow-max-seconds",
        type=int,
        default=None,
        help="Maximum duration for one shadow plan",
    )
    parser.add_argument(
        "--local-sweep-plan-delay-ms",
        type=int,
        default=None,
        help="Delay before cloud starts a shadow plan; accepted for shared env/CLI symmetry",
    )
    parser.add_argument(
        "--local-sweep-include-uds-dids",
        type=str,
        default=None,
        help="Comma-separated UDS DID allowlist for shadow plans, for example 0x000c,0x0031",
    )
    parser.add_argument(
        "--local-sweep-exclude-uds-dids",
        type=str,
        default=None,
        help="Comma-separated UDS DID blocklist for shadow plans, for example 0x0031",
    )
    args = parser.parse_args()

    config = ProxyConfig.from_args(
        auth_token=args.auth_token,
        tls_enabled=args.tls,
        tls_ca_file=args.tls_ca,
        tls_server_name=args.tls_server_name,
        no_vbatt_cache=args.no_vbatt_cache,
        vbatt_ttl=args.vbatt_ttl,
        no_ioctl_cache=args.no_ioctl_cache,
        ioctl_ttl=args.ioctl_ttl,
        read_ahead_enabled=args.read_ahead,
        read_ahead_window_ms=args.read_ahead_window_ms,
        read_ahead_max_reads=args.read_ahead_max_reads,
        read_ahead_write_collect_max_reads=(
            args.read_ahead_write_collect_max_reads
        ),
        read_ahead_read_timeout_ms=args.read_ahead_read_timeout_ms,
        read_ahead_max_messages=args.read_ahead_max_messages,
        read_ahead_max_empty_reads=args.read_ahead_max_empty_reads,
        read_ahead_max_consecutive_empty_reads=(
            args.read_ahead_max_consecutive_empty_reads
        ),
        read_ahead_min_drain_ms=args.read_ahead_min_drain_ms,
        read_ahead_transaction_enabled=args.read_ahead_transaction,
        local_sweep_enabled=getattr(args, "local_sweep", None),
        local_sweep_mode=getattr(args, "local_sweep_mode", None),
        local_sweep_min_cycles=getattr(args, "local_sweep_min_cycles", None),
        local_sweep_max_items=getattr(args, "local_sweep_max_items", None),
        local_sweep_min_item_interval_ms=getattr(args, "local_sweep_min_item_interval_ms", None),
        local_sweep_read_timeout_ms=getattr(args, "local_sweep_read_timeout_ms", None),
        local_sweep_shadow_allow_gm_a9_packet=getattr(
            args,
            "local_sweep_shadow_allow_gm_a9_packet",
            None,
        ),
        local_sweep_shadow_max_seconds=getattr(args, "local_sweep_shadow_max_seconds", None),
        local_sweep_plan_delay_ms=getattr(args, "local_sweep_plan_delay_ms", None),
        local_sweep_include_uds_dids=(
            tuple(
                int(token.strip(), 16 if token.strip().lower().startswith("0x") else 10)
                for token in str(args.local_sweep_include_uds_dids or "").split(",")
                if token.strip()
            )
            if args.local_sweep_include_uds_dids is not None
            else None
        ),
        local_sweep_exclude_uds_dids=(
            tuple(
                int(token.strip(), 16 if token.strip().lower().startswith("0x") else 10)
                for token in str(args.local_sweep_exclude_uds_dids or "").split(",")
                if token.strip()
            )
            if args.local_sweep_exclude_uds_dids is not None
            else None
        ),
    )

    print("=" * 50)
    print("VCI Proxy Reverse Client")
    print("=" * 50)
    print(f"Target server: {args.host}:{args.port}")
    print(f"Auth: {'enabled' if config.auth.enabled else 'disabled'}")
    print(f"TLS: {'enabled' if config.tls.enabled else 'disabled'}")
    print(
        f"IOCTL cache: {'enabled' if config.ioctl_cache.enabled else 'disabled'} "
        f"(TTL={config.ioctl_cache.ttl_s}s)"
    )
    print(
        f"Read-ahead: {'enabled' if config.read_ahead.enabled else 'disabled'} "
        f"(window={config.read_ahead.window_ms}ms, max_reads={config.read_ahead.max_reads}, "
        f"write_collect_max_reads={config.read_ahead.write_collect_max_reads}, "
        f"timeout={config.read_ahead.read_timeout_ms}ms, max_messages={config.read_ahead.max_messages}, "
        f"max_empty_reads={config.read_ahead.max_empty_reads}, "
        f"max_consecutive_empty_reads={config.read_ahead.max_consecutive_empty_reads}, "
        f"min_drain={config.read_ahead.min_drain_ms}ms, "
        f"transaction={'enabled' if config.read_ahead.transaction_enabled else 'disabled'})"
    )
    print(
        f"Local sweep: {'enabled' if config.local_sweep.enabled else 'disabled'} "
        f"(mode={config.local_sweep.mode}, min_cycles={config.local_sweep.min_cycles}, "
        f"max_items={config.local_sweep.max_items}, shadow_max_seconds={config.local_sweep.shadow_max_seconds}, "
        f"plan_delay_ms={config.local_sweep.plan_delay_ms}, "
        f"allow_gm_a9_packet={config.local_sweep.allow_gm_a9_packet}, "
        f"shadow_allow_gm_a9_packet={config.local_sweep.shadow_allow_gm_a9_packet}, "
        f"min_item_interval_ms={config.local_sweep.min_item_interval_ms}, "
        f"read_timeout_ms={config.local_sweep.read_timeout_ms}, "
        f"include_uds_dids={list(config.local_sweep.include_uds_dids)}, "
        f"exclude_uds_dids={list(config.local_sweep.exclude_uds_dids)})"
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
