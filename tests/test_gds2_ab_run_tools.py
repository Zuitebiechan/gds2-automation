from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
COLLECT_SCRIPT = ROOT / "scripts" / "collect_gds2_ab_run.py"
COMPARE_SCRIPT = ROOT / "scripts" / "compare_gds2_ab_runs.py"


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=cwd or ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_collect_script_captures_delta_and_full_copy(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    programdata = tmp_path / "ProgramData"
    appdata = tmp_path / "AppData"
    home = tmp_path / "Home"
    output_root = tmp_path / "runs"
    project_root.mkdir()
    appdata.mkdir()
    home.mkdir()

    cloud_raw = programdata / "RPA_Diagnostic" / "observability" / "cloud" / "raw"
    cloud_raw.mkdir(parents=True)
    raw_file = cloud_raw / "reverse_server-test.jsonl"
    raw_file.write_text('{"event_type":"before"}\n', encoding="utf-8")

    gds2_data = home / "gds2-data"
    gds2_data.mkdir(parents=True)
    latest_json = gds2_data / "latest.json"
    latest_json.write_text('{"pageContext":{"windowTitle":"GDS 2"}}', encoding="utf-8")

    summary_dir = programdata / "GDS 2" / "PersistentData" / "SummaryFiles"
    summary_dir.mkdir(parents=True)
    summary_file = summary_dir / "TESTVIN.vsf"
    summary_file.write_text("<Entries></Entries>", encoding="utf-8")

    start = _run(
        str(COLLECT_SCRIPT),
        "start",
        "--mode",
        "cloud",
        "--label",
        "unit",
        "--scenario",
        "unit",
        "--output-root",
        str(output_root),
        "--project-root",
        str(project_root),
        "--programdata",
        str(programdata),
        "--appdata",
        str(appdata),
        "--home",
        str(home),
    )
    run_dir = Path(start.stdout.strip())
    assert (run_dir / "run_manifest.json").exists()

    raw_file.write_text('{"event_type":"before"}\n{"event_type":"after"}\n', encoding="utf-8")
    summary_file.write_text("<Entries><Entry /></Entries>", encoding="utf-8")

    session_logs = programdata / "GDS 2" / "PersistentData" / "SessionLogs" / "2026001"
    session_logs.mkdir(parents=True)
    session_log = session_logs / "20260423101010_TESTVIN_Engine Control Module_Data Display_Data Display.bin"
    session_log.write_bytes(b"session-payload")

    finish = _run(
        str(COLLECT_SCRIPT),
        "finish",
        "--run-dir",
        str(run_dir),
        "--project-root",
        str(project_root),
        "--programdata",
        str(programdata),
        "--appdata",
        str(appdata),
        "--home",
        str(home),
    )
    assert "Captured" in finish.stdout

    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    captured = {item["logical_path"]: item for item in manifest["captured_artifacts"]}
    assert "observability/cloud/raw/reverse_server-test.jsonl" in captured
    assert "gds2-data/latest.json" in captured
    assert "gds2/PersistentData/SummaryFiles/TESTVIN.vsf" in captured
    assert (
        "gds2/PersistentData/SessionLogs/2026001/"
        "20260423101010_TESTVIN_Engine Control Module_Data Display_Data Display.bin"
    ) in captured

    delta_artifact = run_dir / captured["observability/cloud/raw/reverse_server-test.jsonl"]["artifact_path"]
    assert delta_artifact.read_text(encoding="utf-8") == '{"event_type":"after"}\n'


def test_compare_script_reports_network_and_session_match(tmp_path: Path) -> None:
    baseline_dir = tmp_path / "baseline"
    candidate_dir = tmp_path / "candidate"
    baseline_dir.mkdir()
    candidate_dir.mkdir()

    baseline_manifest = {
        "run_id": "baseline",
        "mode": "local",
        "label": "baseline",
        "scenario": "engine-data",
        "started_at_local": "2026-04-23T10:00:00",
        "finished_at_local": "2026-04-23T10:30:00",
        "captured_artifacts": [
            {"artifact_path": "artifacts/gds2/PersistentData/SummaryFiles/TEST.vsf"},
        ],
    }
    candidate_manifest = {
        "run_id": "candidate",
        "mode": "cloud",
        "label": "candidate",
        "scenario": "engine-data",
        "started_at_local": "2026-04-23T10:00:00",
        "finished_at_local": "2026-04-23T10:30:00",
        "captured_artifacts": [
            {"artifact_path": "artifacts/gds2/PersistentData/SummaryFiles/TEST.vsf"},
            {"artifact_path": "artifacts/observability/cloud/raw/reverse_server.jsonl"},
        ],
    }
    _write_json(baseline_dir / "run_manifest.json", baseline_manifest)
    _write_json(candidate_dir / "run_manifest.json", candidate_manifest)

    vsf_payload = (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>'
        '<Entries><SessionsList>'
        '<Session Area="China" FileName="20260423100523_TESTVIN_Engine Control Module_Data Display_Data Display.bin" '
        'Path="C:\\ProgramData\\GDS 2\\PersistentData\\SessionLogs\\2026113" Release="GDS 2 22.7.01500" '
        'Timestamp="20260423100523" Version="18">'
        "<Header Language=\"ENGLISH\">"
        "<Year>2017</Year><EngYear>2017</EngYear><Make>Buick</Make><Model>Envision</Model>"
        "<Module>Engine Control Module</Module><Application>Data Display</Application><Machine>Data Display</Machine>"
        "</Header></Session></SessionsList></Entries>"
    )
    for run_dir in (baseline_dir, candidate_dir):
        path = run_dir / "artifacts" / "gds2" / "PersistentData" / "SummaryFiles" / "TEST.vsf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(vsf_payload, encoding="utf-8")

    raw_event = {
        "schema_version": "observability.v1",
        "ts": "2026-04-23T02:00:00Z",
        "component": "reverse_server",
        "component_instance_id": "reverse_server:test",
        "event_type": "proxy.request.response_received",
        "session_id": "s1",
        "connection_epoch": "epoch-1",
        "dll_seq": 1,
        "proxy_seq": 2,
        "worker_request_id": "w1",
        "operation_kind": "j2534:READ_MSGS_REQ",
        "status": "ok",
        "failure_code": None,
        "failure_domain": "unknown",
        "reason": "response_received",
        "duration_ms": 30.0,
        "hw_ms": 10.0,
        "network_ms": 20.0,
        "page": "data_display",
        "module": "Engine Control Module",
        "data_category": "Engine Data",
        "symptom": None,
        "impact_scope": "proxy_request",
        "next_checks": [],
        "redaction_applied": [],
        "msg_name": "READ_MSGS_REQ",
        "cache_hit": False,
    }
    raw_path = candidate_dir / "artifacts" / "observability" / "cloud" / "raw" / "reverse_server.jsonl"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(json.dumps(raw_event, ensure_ascii=False) + "\n", encoding="utf-8")

    report_path = tmp_path / "comparison.md"
    json_path = tmp_path / "comparison.json"
    _run(
        str(COMPARE_SCRIPT),
        "--baseline",
        str(baseline_dir),
        "--candidate",
        str(candidate_dir),
        "--report",
        str(report_path),
        "--json",
        str(json_path),
    )

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["comparison"]["candidate_network_p95_ms"] == 20.0
    assert payload["comparison"]["common_gds2_sessions"]
    assert "# GDS2 Local vs Cloud Comparison" in report_path.read_text(encoding="utf-8")
