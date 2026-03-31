import json
import os

from src.streaming import agent_data_collector as collector_module
from src.streaming.agent_data_collector import AgentDataCollector


def _write_latest_json(path, *, extraction_count=1, version="2.0"):
    path.write_text(
        json.dumps(
            {
                "extractionCount": extraction_count,
                "version": version,
            },
            ensure_ascii=False,
        ),
        encoding="gbk",
    )


def test_check_agent_available_retries_transient_json_error(tmp_path, monkeypatch):
    json_path = tmp_path / "latest.json"
    _write_latest_json(json_path, extraction_count=42, version="2.0")

    now = 1_000.0
    mtime = now - 2.0
    os.utime(json_path, (mtime, mtime))

    collector = AgentDataCollector(json_path=json_path)
    real_json_load = collector_module.json.load
    attempts = {"count": 0}

    def flaky_json_load(handle):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise json.JSONDecodeError("partial write", "{}", 0)
        return real_json_load(handle)

    monkeypatch.setattr(collector_module.time, "time", lambda: now)
    monkeypatch.setattr(collector_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(collector_module.json, "load", flaky_json_load)

    result = collector.check_agent_available()

    assert result["available"] is True
    assert result["extraction_count"] == 42
    assert attempts["count"] >= 2
    assert result["attempts"] >= 1
    assert result["error"] is None


def test_check_agent_available_reports_error_after_retry_exhaustion(tmp_path, monkeypatch):
    json_path = tmp_path / "latest.json"
    _write_latest_json(json_path, extraction_count=7)

    now = 1_000.0
    mtime = now - 1.0
    os.utime(json_path, (mtime, mtime))

    collector = AgentDataCollector(json_path=json_path)
    attempts = {"count": 0}

    def always_bad_json(_handle):
        attempts["count"] += 1
        raise json.JSONDecodeError("partial write", "{}", 0)

    monkeypatch.setattr(collector_module.time, "time", lambda: now)
    monkeypatch.setattr(collector_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(collector_module.json, "load", always_bad_json)

    result = collector.check_agent_available()

    assert result["available"] is False
    assert result["exists"] is True
    assert result["attempts"] >= 2
    assert "partial write" in result["error"]


def test_check_agent_available_still_rejects_stale_file(tmp_path, monkeypatch):
    json_path = tmp_path / "latest.json"
    _write_latest_json(json_path, extraction_count=9)

    now = 1_000.0
    mtime = now - 12.0
    os.utime(json_path, (mtime, mtime))

    collector = AgentDataCollector(json_path=json_path)
    monkeypatch.setattr(collector_module.time, "time", lambda: now)

    result = collector.check_agent_available()

    assert result["available"] is False
    assert result["age_seconds"] == 12.0
    assert result["extraction_count"] == 9
