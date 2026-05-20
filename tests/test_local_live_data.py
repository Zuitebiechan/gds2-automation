import json
import time
from pathlib import Path

import pytest

from diagnostic_platform.observability import LogContext
from vci_proxy.config import LocalLiveDataConfig
from vci_proxy.local_live_data import (
    LocalLiveDataChannel,
    LocalLiveDataCollector,
    LocalLiveDataMonitor,
    decode_engine_speed_frame,
    engine_speed_uds_request_message,
    get_latest_snapshot_path,
)


def _local_live_data_context(_msg_name: str) -> LogContext:
    return LogContext(operation_kind="j2534:LOCAL_LIVE_DATA")


def test_decode_engine_speed_obd_mode01_pid_0c_response() -> None:
    decoded = decode_engine_speed_frame(b"\x41\x0c\x1f\x40")

    assert decoded is not None
    assert decoded.value == 2000.0
    assert decoded.source == "proxy_local_obd"
    assert decoded.decoder_id == "obd_mode01_pid_0c"
    assert decoded.raw_value == 0x1F40


def test_decode_engine_speed_can_id_prefixed_uds_did_000c_response() -> None:
    decoded = decode_engine_speed_frame(bytes.fromhex("000007e862000c1234"))

    assert decoded is not None
    assert decoded.value == pytest.approx(1165.0)
    assert decoded.source == "proxy_local_known_uds"
    assert decoded.decoder_id == "uds_did_000c_engine_speed"
    assert decoded.raw_prefix_hex == "000007e862000c1234"


def test_decoder_ignores_engine_speed_marker_inside_gm_a9_payload() -> None:
    decoded = decode_engine_speed_frame(bytes.fromhex("000007e0a98122000c1234"))

    assert decoded is None


def test_latest_snapshot_path_uses_vci_proxy_live_data_dir(tmp_path: Path) -> None:
    assert get_latest_snapshot_path(tmp_path) == (
        tmp_path / "VCI_Proxy" / "live_data" / "latest.json"
    )


def test_monitor_writes_latest_snapshot_for_decoded_engine_speed(tmp_path: Path) -> None:
    monitor = LocalLiveDataMonitor(latest_path=tmp_path / "latest.json")
    now = time.time()

    events = monitor.record_sweep_item_finished(
        write_messages=[{"protocol_id": 6, "data": b"\x22\x00\x0c"}],
        read_messages=[{"protocol_id": 6, "data": b"\x62\x00\x0c\x1f\x40"}],
        return_code=0,
        started_at_s=now - 0.02,
        finished_at_s=now,
        channel_id=44,
        sweep_plan_id="plan-1",
        sweep_item_index=0,
        sweep_signature_digest="sig-000c",
        read_observability={"read_timeout_ms": 15},
    )

    event_types = [event_type for event_type, _fields in events]
    assert event_types == [
        "proxy.local_live_data.sample",
        "proxy.local_live_data.summary",
    ]
    sample = events[0][1]
    assert sample["signal_key"] == "engine_speed"
    assert sample["value"] == 2000.0
    assert sample["source"] == "proxy_local_known_uds"
    assert sample["decoder_id"] == "uds_did_000c_engine_speed"
    assert sample["return_code"] == 0

    payload = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    latest = payload["latest_sample"]
    assert latest["signal_key"] == "engine_speed"
    assert latest["value"] == 2000.0
    assert payload["signals"]["engine_speed"]["decoder_id"] == "uds_did_000c_engine_speed"
    assert payload["summary"]["sample_count"] == 1


def test_monitor_decodes_engine_speed_payload_with_timeout_return_code(
    tmp_path: Path,
) -> None:
    monitor = LocalLiveDataMonitor(latest_path=tmp_path / "latest.json")
    now = time.time()

    events = monitor.record_sweep_item_finished(
        write_messages=[{"protocol_id": 6, "data": b"\x22\x00\x0c"}],
        read_messages=[
            {"protocol_id": 6, "data": bytes.fromhex("000007e0")},
            {"protocol_id": 6, "data": bytes.fromhex("000007e862000c0d96")},
        ],
        return_code=9,
        started_at_s=now - 0.024,
        finished_at_s=now,
        channel_id=1,
        sweep_plan_id="plan-000c",
        sweep_item_index=0,
        sweep_signature_digest="sig-000c",
        read_observability={
            "read_timeout_ms": 15,
            "tail_read_triggered": False,
            "tail_read_attempts": 0,
            "tail_read_data_reads": 0,
        },
    )

    event_types = [event_type for event_type, _fields in events]
    assert event_types == [
        "proxy.local_live_data.sample",
        "proxy.local_live_data.summary",
    ]
    sample = events[0][1]
    assert sample["value"] == pytest.approx(869.5)
    assert sample["source"] == "proxy_local_known_uds"
    assert sample["return_code"] == 9
    assert sample["j2534_return_code_warning"] is True
    assert sample["j2534_return_code_warning_reason"] == "timeout_return_code_with_data"
    assert sample["raw_prefix_hex"] == "000007e862000c0d96"
    assert sample["read_timeout_ms"] == 15
    assert sample["message_index"] == 1

    summary = events[1][1]
    assert summary["sample_count"] == 1
    assert summary["backoff_count"] == 0
    assert summary["timeout_count"] == 0
    assert summary["return_code_warning_count"] == 1

    payload = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    latest = payload["latest_sample"]
    assert latest["value"] == pytest.approx(869.5)
    assert latest["return_code"] == 9
    assert latest["j2534_return_code_warning"] is True


def test_monitor_reports_unsupported_engine_speed_response(tmp_path: Path) -> None:
    monitor = LocalLiveDataMonitor(latest_path=tmp_path / "latest.json")
    now = time.time()

    events = monitor.record_sweep_item_finished(
        write_messages=[{"protocol_id": 6, "data": b"\x22\x00\x0c"}],
        read_messages=[{"protocol_id": 6, "data": b"\x62\x00\x31\x12\x34"}],
        return_code=0,
        started_at_s=now - 0.01,
        finished_at_s=now,
        channel_id=44,
        sweep_plan_id="plan-1",
        sweep_item_index=0,
        sweep_signature_digest="sig-000c",
    )

    assert events[0][0] == "proxy.local_live_data.unsupported"
    assert events[0][1]["reason"] == "no_supported_engine_speed_shape"
    assert events[1][0] == "proxy.local_live_data.summary"
    assert events[1][1]["unsupported_count"] == 1
    assert not (tmp_path / "latest.json").exists()


def test_monitor_reports_backoff_for_failed_engine_speed_read(tmp_path: Path) -> None:
    monitor = LocalLiveDataMonitor(latest_path=tmp_path / "latest.json")
    now = time.time()

    events = monitor.record_sweep_item_finished(
        write_messages=[{"protocol_id": 6, "data": b"\x01\x0c"}],
        read_messages=[],
        return_code=1,
        started_at_s=now - 0.01,
        finished_at_s=now,
        channel_id=44,
        sweep_plan_id="plan-1",
        sweep_item_index=0,
        sweep_signature_digest="sig-obd",
    )

    assert events[0][0] == "proxy.local_live_data.backoff"
    assert events[0][1]["backoff_reason"] == "read_return_code"
    assert events[1][0] == "proxy.local_live_data.summary"
    assert events[1][1]["backoff_count"] == 1


def test_engine_speed_uds_request_message_uses_known_can_id_prefixed_shape() -> None:
    message = engine_speed_uds_request_message(protocol_id=6)

    assert message["protocol_id"] == 6
    assert message["tx_flags"] == 64
    assert message["data"] == bytes.fromhex("000007e022000c")


def test_collector_poll_once_emits_sample_and_snapshot(tmp_path: Path) -> None:
    async def _run() -> None:
        monitor = LocalLiveDataMonitor(latest_path=tmp_path / "latest.json")
        emitted: list[tuple[str, dict[str, object]]] = []
        calls: list[tuple[str, int, int]] = []

        async def _run_driver_call(method_name: str, *args, **_kwargs):
            if method_name == "write_msgs":
                channel_id, messages, timeout = args
                calls.append((method_name, channel_id, timeout))
                assert messages[0]["data"] == bytes.fromhex("000007e022000c")
                return 0, 1
            if method_name == "read_msgs":
                channel_id, num_msgs, timeout = args
                calls.append((method_name, channel_id, timeout))
                assert num_msgs == 8
                return 9, [{"protocol_id": 6, "data": bytes.fromhex("000007e862000c0d96")}]
            raise AssertionError(method_name)

        collector = LocalLiveDataCollector(
            config=LocalLiveDataConfig(enabled=True, read_timeout_ms=15),
            monitor=monitor,
            run_driver_call=_run_driver_call,
            context_factory=_local_live_data_context,
            emit_event=lambda event_type, **fields: emitted.append((event_type, fields)),
            foreground_idle=lambda: True,
            channel_provider=lambda: LocalLiveDataChannel(channel_id=44, protocol_id=6),
        )

        events = await collector.poll_once()

        assert [name for name, *_ in calls] == ["write_msgs", "read_msgs"]
        event_types = [event_type for event_type, _fields in events]
        assert event_types == [
            "proxy.local_live_data.sample",
            "proxy.local_live_data.summary",
        ]
        sample = events[0][1]
        assert sample["request_origin"] == "local_live_data_collector"
        assert sample["value"] == pytest.approx(869.5)
        assert sample["return_code"] == 9
        assert sample["j2534_return_code_warning"] is True
        assert sample["sample_gap_ms"] is None
        assert emitted[0][0] == "proxy.local_live_data.sample"
        assert emitted[0][1]["collector_source"] == "uds_did_000c"
        assert emitted[0][1]["collector_interval_ms"] == 500
        assert emitted[0][1]["read_timeout_ms"] == 15

        latest = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
        assert latest["latest_sample"]["value"] == pytest.approx(869.5)

    import asyncio

    asyncio.run(_run())


def test_collector_poll_once_pauses_when_foreground_is_busy(tmp_path: Path) -> None:
    async def _run() -> None:
        monitor = LocalLiveDataMonitor(latest_path=tmp_path / "latest.json")
        emitted: list[tuple[str, dict[str, object]]] = []

        async def _run_driver_call(*_args, **_kwargs):
            raise AssertionError("collector should not call driver while foreground is busy")

        collector = LocalLiveDataCollector(
            config=LocalLiveDataConfig(enabled=True),
            monitor=monitor,
            run_driver_call=_run_driver_call,
            context_factory=_local_live_data_context,
            emit_event=lambda event_type, **fields: emitted.append((event_type, fields)),
            foreground_idle=lambda: False,
            channel_provider=lambda: LocalLiveDataChannel(channel_id=44, protocol_id=6),
        )

        events = await collector.poll_once()

        assert events[0][0] == "proxy.local_live_data.backoff"
        assert events[0][1]["reason"] == "foreground_busy"
        assert events[1][1]["foreground_priority_pause_count"] == 1
        assert emitted[0][0] == "proxy.local_live_data.backoff"
        assert not (tmp_path / "latest.json").exists()

    import asyncio

    asyncio.run(_run())


def test_collector_poll_once_reports_no_channel_without_driver_call(tmp_path: Path) -> None:
    async def _run() -> None:
        monitor = LocalLiveDataMonitor(latest_path=tmp_path / "latest.json")

        async def _run_driver_call(*_args, **_kwargs):
            raise AssertionError("collector should not call driver without a channel")

        collector = LocalLiveDataCollector(
            config=LocalLiveDataConfig(enabled=True),
            monitor=monitor,
            run_driver_call=_run_driver_call,
            context_factory=_local_live_data_context,
            emit_event=lambda *_args, **_kwargs: None,
            foreground_idle=lambda: True,
            channel_provider=lambda: None,
        )

        events = await collector.poll_once()

        assert events[0][0] == "proxy.local_live_data.backoff"
        assert events[0][1]["reason"] == "no_channel"
        assert events[1][1]["backoff_count"] == 1

    import asyncio

    asyncio.run(_run())


def test_collector_poll_once_rejects_non_iso15765_channel_without_driver_call(tmp_path: Path) -> None:
    async def _run() -> None:
        monitor = LocalLiveDataMonitor(latest_path=tmp_path / "latest.json")

        async def _run_driver_call(*_args, **_kwargs):
            raise AssertionError("collector should not call driver for unsupported protocol")

        collector = LocalLiveDataCollector(
            config=LocalLiveDataConfig(enabled=True),
            monitor=monitor,
            run_driver_call=_run_driver_call,
            context_factory=_local_live_data_context,
            emit_event=lambda *_args, **_kwargs: None,
            foreground_idle=lambda: True,
            channel_provider=lambda: LocalLiveDataChannel(channel_id=44, protocol_id=5),
        )

        events = await collector.poll_once()

        assert events[0][0] == "proxy.local_live_data.backoff"
        assert events[0][1]["reason"] == "unsupported_protocol"
        assert events[0][1]["channel_id"] == 44
        assert events[1][1]["backoff_count"] == 1

    import asyncio

    asyncio.run(_run())


def test_collector_stop_waits_for_in_flight_driver_call(tmp_path: Path) -> None:
    async def _run() -> None:
        monitor = LocalLiveDataMonitor(latest_path=tmp_path / "latest.json")
        write_started = asyncio.Event()
        release_write = asyncio.Event()
        write_finished = False
        read_called = False

        async def _run_driver_call(method_name: str, *args, **_kwargs):
            nonlocal read_called, write_finished
            if method_name == "write_msgs":
                write_started.set()
                await release_write.wait()
                write_finished = True
                return 0, 1
            if method_name == "read_msgs":
                read_called = True
                return 0, [{"protocol_id": 6, "data": bytes.fromhex("000007e862000c1234")}]
            raise AssertionError(method_name)

        collector = LocalLiveDataCollector(
            config=LocalLiveDataConfig(enabled=True, interval_ms=250),
            monitor=monitor,
            run_driver_call=_run_driver_call,
            context_factory=_local_live_data_context,
            emit_event=lambda *_args, **_kwargs: None,
            foreground_idle=lambda: True,
            channel_provider=lambda: LocalLiveDataChannel(channel_id=44, protocol_id=6),
        )

        assert collector.start()
        await write_started.wait()
        stop_task = asyncio.create_task(collector.stop("test_stop"))
        await asyncio.sleep(0)
        assert write_finished is False
        assert collector.running is True
        release_write.set()
        await stop_task
        assert write_finished is True
        assert read_called is False
        assert collector.running is False

    import asyncio

    asyncio.run(_run())
