from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path

from diagnostic_platform.observability import (
    ActiveSessionSnapshotStore,
    JsonlWriter,
    LogContext,
    ObservabilityLogHandler,
    RotatingGzipWriter,
    build_snapshot_log_context,
    emit_event,
    flush_product_log_writers,
    get_active_session_snapshot_path,
    get_cloud_observability_root,
    get_product_log_writer,
    install_observability_log_handler,
    read_active_session_snapshot,
    redact_payload,
)


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_emit_event_writes_required_observability_fields(tmp_path: Path) -> None:
    writer = JsonlWriter(tmp_path / "events.jsonl")

    record = emit_event(
        writer,
        component="server.api",
        event_type="api.request.completed",
        context=LogContext(
            session_id="session-1",
            operation_kind="session.start",
            page="vehicle_selection",
            module="Engine Control Module",
            data_category="Engine Data",
        ),
        status="ok",
    )
    writer.close()

    lines = _read_jsonl(tmp_path / "events.jsonl")
    assert len(lines) == 1
    written = lines[0]

    assert written["schema_version"] == "observability.v1"
    assert written["component"] == "server.api"
    assert written["event_type"] == "api.request.completed"
    assert written["session_id"] == "session-1"
    assert written["operation_kind"] == "session.start"
    assert written["page"] == "vehicle_selection"
    assert written["module"] == "Engine Control Module"
    assert written["data_category"] == "Engine Data"
    assert written["failure_domain"] == "unknown"
    assert isinstance(written["redaction_applied"], list)
    assert record["component_instance_id"] == written["component_instance_id"]


def test_rotating_gzip_writer_rotates_previous_file(tmp_path: Path) -> None:
    writer = RotatingGzipWriter(
        tmp_path / "rotating.jsonl",
        max_bytes=80,
        gzip_rotated=True,
    )

    writer.write_line(json.dumps({"event": 1, "payload": "a" * 32}))
    writer.write_line(json.dumps({"event": 2, "payload": "b" * 64}))
    writer.close()

    rotated = sorted(tmp_path.glob("rotating*.gz"))
    assert rotated, "expected at least one rotated gzip artifact"
    with gzip.open(rotated[0], "rt", encoding="utf-8") as handle:
        rotated_text = handle.read()
    assert '"event": 1' in rotated_text
    assert (tmp_path / "rotating.jsonl").exists()


def test_redact_payload_masks_tokens_vin_and_binary_payload() -> None:
    redacted = redact_payload(
        {
            "Authorization": "Bearer top-secret-token",
            "vin": "1GCHK23D57F123456",
            "payload": bytes(range(20)),
        }
    )

    assert redacted["Authorization"] == "<redacted>"
    assert "vin" not in redacted
    assert redacted["vin_masked"] == "1GC***********456"
    assert len(redacted["vin_hash"]) == 64
    assert redacted["payload"] == {
        "type": "bytes",
        "length": 20,
        "digest": redacted["payload"]["digest"],
        "prefix_hex": "000102030405060708090a0b0c0d0e0f",
    }


def test_active_session_snapshot_store_round_trips_and_recovers_from_corruption(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    store = ActiveSessionSnapshotStore()

    store.write(
        {
            "session_id": "session-1",
            "backend_name": "gds2",
            "operation_kind": "start_diagnostics",
            "selected_module": "Engine Control Module",
            "selected_data_category": "Engine Data",
            "current_page": "data_display",
            "navigation_session_id": "nav-1",
            "ai_session_id": "ai-1",
            "live_data_active": True,
            "connection_epoch": "epoch-1",
        }
    )

    snapshot = read_active_session_snapshot()
    assert snapshot is not None
    assert snapshot["session_id"] == "session-1"
    assert snapshot["backend_name"] == "gds2"
    assert snapshot["current_page"] == "data_display"
    assert snapshot["connection_epoch"] == "epoch-1"

    snapshot_path = get_active_session_snapshot_path()
    snapshot_path.write_text("{bad json", encoding="utf-8")

    assert read_active_session_snapshot() is None

    store.clear()
    assert not snapshot_path.exists()
    flush_product_log_writers()


def test_get_cloud_observability_root_prefers_product_log_cloud_root_env(
    tmp_path: Path,
    monkeypatch,
) -> None:
    configured = tmp_path / "D-drive-like" / "RPA_Diagnostic" / "observability" / "cloud"
    monkeypatch.setenv("PRODUCT_LOG_CLOUD_ROOT", str(configured))
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "ProgramData"))

    assert get_cloud_observability_root() == configured


def test_install_observability_log_handler_writes_runtime_log_event(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    logger = logging.getLogger("tests.runtime-log")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers = []

    handler = install_observability_log_handler(
        logger,
        component="server.runtime",
        writer=get_product_log_writer("server.runtime"),
        context_provider=lambda _record: build_snapshot_log_context(
            operation_kind="server_runtime"
        ),
    )
    assert isinstance(handler, ObservabilityLogHandler)

    logger.info("runtime mirror works")
    flush_product_log_writers()

    raw_dir = tmp_path / "RPA_Diagnostic" / "observability" / "cloud" / "raw"
    event_files = sorted(raw_dir.glob("*.jsonl"))
    assert event_files
    records = []
    for path in event_files:
        records.extend(_read_jsonl(path))
    mirrored = [record for record in records if record["event_type"] == "runtime.log"]
    assert mirrored
    assert mirrored[-1]["component"] == "server.runtime"
    assert mirrored[-1]["log_level"] == "INFO"
    assert mirrored[-1]["log_message"] == "runtime mirror works"
