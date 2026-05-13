"""Local serial executor for shadow-only sweep validation."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Awaitable, Callable

from diagnostic_platform.observability import LogContext

from .cache_read_msgs import BUFFER_EMPTY
from .config import LocalSweepConfig
from .protocol import HEADER_SIZE, ProtocolDecoder, ProtocolEncoder
from .sweep_protocol import (
    SweepPlanStartRequest,
    SweepResultRecord,
    SweepStatus,
)


RunDriverCall = Callable[..., Awaitable[object]]
EventEmitter = Callable[..., None]
ContextFactory = Callable[[str], LogContext]


class LocalSweepExecutor:
    """Execute one shadow plan at a time through the normal driver call path."""

    def __init__(
        self,
        *,
        config: LocalSweepConfig,
        run_driver_call: RunDriverCall,
        context_factory: ContextFactory,
        emit_event: EventEmitter,
        foreground_idle: Callable[[], bool],
    ):
        self._config = config
        self._run_driver_call = run_driver_call
        self._context_factory = context_factory
        self._emit_event = emit_event
        self._foreground_idle = foreground_idle
        self._active_plan: SweepPlanStartRequest | None = None
        self._task: asyncio.Task | None = None
        self._queue: deque[SweepResultRecord] = deque()
        self._stop_requested = False
        self._state = "idle"
        self._error_count = 0

    @property
    def active_plan_id(self) -> str | None:
        return self._active_plan.plan_id if self._active_plan is not None else None

    def start(self, plan: SweepPlanStartRequest) -> tuple[bool, str]:
        if not self._config.shadow_transport_enabled:
            return False, "local_sweep_shadow_disabled"
        if not plan.requests:
            return False, "empty_plan"
        if self._active_plan is not None:
            if self._active_plan.plan_id == plan.plan_id:
                return True, "already_running"
            self.stop("superseded_by_new_plan")
        if self._task is not None and not self._task.done():
            return False, "executor_busy_stopping"

        self._active_plan = plan
        self._stop_requested = False
        self._state = "running"
        self._error_count = 0
        self._queue.clear()
        self._emit_event(
            "sweep.executor.started",
            reason="plan_started",
            sweep_plan_id=plan.plan_id,
            channel_id=plan.channel_id,
            sweep_item_count=len(plan.requests),
            sweep_plan_min_item_interval_ms=plan.min_item_interval_ms,
            sweep_effective_min_item_interval_ms=self._effective_min_item_interval_ms(plan),
        )
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_loop())
        return True, "started"

    def stop(self, reason: str) -> None:
        self._stop_requested = True
        plan_id = self.active_plan_id
        self._active_plan = None
        self._state = "stopped"
        self._queue.clear()
        if plan_id is not None:
            self._emit_event(
                "sweep.executor.stopped",
                reason=reason,
                sweep_plan_id=plan_id,
                sweep_error_count=self._error_count,
            )

    def status(self) -> SweepStatus:
        return SweepStatus(
            active_plan_id=self.active_plan_id,
            state=self._state,
            queued_results=len(self._queue),
            error_count=self._error_count,
        )

    def drain(self, max_results: int = 128) -> tuple[SweepResultRecord, ...]:
        drained: list[SweepResultRecord] = []
        limit = max(0, int(max_results))
        while self._queue and len(drained) < limit:
            drained.append(self._queue.popleft())
        return tuple(drained)

    def _effective_min_item_interval_ms(self, plan: SweepPlanStartRequest) -> int:
        return max(
            0,
            int(self._config.min_item_interval_ms),
            int(plan.min_item_interval_ms),
        )

    async def _wait_for_foreground_idle(self, plan: SweepPlanStartRequest) -> bool:
        while self._active_plan is plan and not self._stop_requested:
            if self._foreground_idle():
                return True
            await asyncio.sleep(0.001)
        return False

    def _plan_is_active(self, plan: SweepPlanStartRequest) -> bool:
        return self._active_plan is plan and not self._stop_requested

    async def _run_loop(self) -> None:
        plan = self._active_plan
        if plan is None:
            return
        deadline = time.monotonic() + max(1, plan.shadow_max_seconds)
        min_item_interval_ms = self._effective_min_item_interval_ms(plan)
        try:
            while (
                self._active_plan is plan
                and not self._stop_requested
                and time.monotonic() <= deadline
            ):
                for index, request in enumerate(plan.requests):
                    if (
                        self._active_plan is not plan
                        or self._stop_requested
                        or time.monotonic() > deadline
                    ):
                        break
                    await self._execute_item(plan, index, request)
                    if min_item_interval_ms > 0:
                        await asyncio.sleep(min_item_interval_ms / 1000.0)
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            pass
        finally:
            if self._active_plan is plan:
                self._active_plan = None
                self._state = "expired" if time.monotonic() > deadline else "stopped"
                self._emit_event(
                    "sweep.executor.stopped",
                    reason=self._state,
                    sweep_plan_id=plan.plan_id,
                    sweep_error_count=self._error_count,
                    sweep_effective_min_item_interval_ms=min_item_interval_ms,
                )

    async def _execute_item(self, plan: SweepPlanStartRequest, index: int, request) -> None:
        started_at = time.time()
        context = self._context_factory("SWEEP_ITEM")
        self._emit_event(
            "sweep.item.started",
            context=context,
            reason="shadow_item_started",
            sweep_plan_id=plan.plan_id,
            sweep_item_index=index,
            sweep_signature_digest=request.signature_digest,
            channel_id=plan.channel_id,
        )
        try:
            channel_id, messages, write_timeout = ProtocolDecoder.decode_write_msgs_req(
                request.write_req_body
            )
            if not await self._wait_for_foreground_idle(plan):
                return
            write_ret, _num_written = await self._run_driver_call(
                "write_msgs",
                channel_id,
                messages,
                write_timeout,
                request_context=context,
                result_metadata={
                    "channel_id": channel_id,
                    "local_sweep_shadow": True,
                    "sweep_signature_digest": request.signature_digest,
                },
            )
            if not self._plan_is_active(plan):
                return
            if int(write_ret) != 0:
                self._record_error_result(
                    plan,
                    request.signature_digest,
                    started_at,
                    f"write_return_code:{write_ret}",
                )
                return

            if not await self._wait_for_foreground_idle(plan):
                return
            read_ret, read_messages = await self._run_driver_call(
                "read_msgs",
                channel_id,
                request.read_num_msgs,
                request.read_timeout_ms,
                request_context=context,
                ok_codes=(0, BUFFER_EMPTY),
                result_metadata={
                    "channel_id": channel_id,
                    "local_sweep_shadow": True,
                    "sweep_signature_digest": request.signature_digest,
                },
            )
            if not self._plan_is_active(plan):
                return
            read_rsp_body = ProtocolEncoder.encode_read_msgs_rsp(
                int(read_ret),
                list(read_messages),
                0,
            )[HEADER_SIZE:]
            self._queue.append(
                SweepResultRecord(
                    plan_id=plan.plan_id,
                    signature_digest=request.signature_digest,
                    return_code=int(read_ret),
                    read_rsp_body=read_rsp_body,
                    started_at_s=started_at,
                    finished_at_s=time.time(),
                )
            )
            self._emit_event(
                "sweep.item.finished",
                context=context,
                reason="shadow_item_finished",
                sweep_plan_id=plan.plan_id,
                sweep_item_index=index,
                sweep_signature_digest=request.signature_digest,
                return_code=int(read_ret),
                message_count=len(read_messages),
                message_lengths=[
                    len(bytes(message.get("data", b"") or b""))
                    for message in read_messages
                ],
                message_prefixes=[
                    bytes(message.get("data", b"") or b"")[:16].hex()
                    for message in read_messages
                ],
            )
        except Exception as exc:
            if not self._plan_is_active(plan):
                return
            self._record_error_result(
                plan,
                request.signature_digest,
                started_at,
                f"{type(exc).__name__}:{exc}",
            )

    def _record_error_result(
        self,
        plan: SweepPlanStartRequest,
        signature_digest: str,
        started_at: float,
        error_name: str,
    ) -> None:
        self._error_count += 1
        self._queue.append(
            SweepResultRecord(
                plan_id=plan.plan_id,
                signature_digest=signature_digest,
                return_code=1,
                read_rsp_body=b"",
                started_at_s=started_at,
                finished_at_s=time.time(),
                error_name=error_name,
            )
        )
        self._emit_event(
            "sweep.executor.error",
            status="error",
            failure_code="shadow_item_error",
            failure_domain="local_j2534_driver",
            reason=error_name,
            sweep_plan_id=plan.plan_id,
            sweep_signature_digest=signature_digest,
            sweep_error_count=self._error_count,
        )
        if self._error_count >= self._config.error_threshold:
            self.stop("error_threshold_reached")
