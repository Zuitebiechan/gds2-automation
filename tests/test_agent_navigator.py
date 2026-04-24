from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from src.streaming.agent_navigator import AgentNavigator


def test_agent_navigator_serializes_concurrent_commands(tmp_path: Path) -> None:
    nav = AgentNavigator(data_dir=tmp_path, timeout_sec=0.8)
    start_barrier = threading.Barrier(3)
    stop_event = threading.Event()
    processed_ids: set[str] = set()
    processed_lock = threading.Lock()
    errors: list[BaseException] = []
    results: list[list[dict]] = []

    def responder() -> None:
        while not stop_event.is_set():
            command_file = tmp_path / "command.json"
            if not command_file.exists():
                time.sleep(0.01)
                continue

            try:
                command = json.loads(command_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                time.sleep(0.01)
                continue

            cmd_id = str(command.get("id") or "")
            if not cmd_id:
                time.sleep(0.01)
                continue

            with processed_lock:
                if cmd_id in processed_ids:
                    time.sleep(0.01)
                    continue
                processed_ids.add(cmd_id)

            time.sleep(0.05)
            (tmp_path / "result.json").write_text(
                json.dumps(
                    {
                        "id": cmd_id,
                        "success": True,
                        "data": {
                            "windows": [{"title": f"Window-{cmd_id}"}],
                        },
                    }
                ),
                encoding="utf-8",
            )

    def worker() -> None:
        try:
            start_barrier.wait()
            results.append(nav.get_window_info())
        except BaseException as exc:
            errors.append(exc)

    responder_thread = threading.Thread(target=responder, daemon=True)
    responder_thread.start()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()

    start_barrier.wait()

    for thread in threads:
        thread.join(timeout=2.0)

    stop_event.set()
    responder_thread.join(timeout=1.0)

    assert not errors
    assert len(results) == 2
    assert all(result and result[0]["title"].startswith("Window-") for result in results)
    assert len({result[0]["title"] for result in results}) == 2


def test_agent_navigator_retries_transient_command_file_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nav = AgentNavigator(data_dir=tmp_path, timeout_sec=0.5)
    nav._result_file.write_text(
        json.dumps(
            {
                "id": "fixed-id",
                "success": True,
                "data": {"windows": [{"title": "Window-fixed"}]},
            }
        ),
        encoding="utf-8",
    )

    attempts = {"count": 0}
    real_replace = os.replace

    def flaky_replace(src: str, dst: str) -> None:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise PermissionError("command file locked")
        real_replace(src, dst)

    monkeypatch.setattr("src.streaming.agent_navigator.uuid.uuid4", lambda: "fixed-id-uuid")
    monkeypatch.setattr("src.streaming.agent_navigator.os.replace", flaky_replace)

    result = nav.get_window_info()

    assert result == [{"title": "Window-fixed"}]
    assert attempts["count"] >= 2


def test_agent_navigator_navigation_path_commands(tmp_path: Path) -> None:
    nav = AgentNavigator(data_dir=tmp_path, timeout_sec=0.5)

    command_file = tmp_path / "command.json"
    result_file = tmp_path / "result.json"

    def write_result_for_latest_command() -> None:
        command = json.loads(command_file.read_text(encoding="utf-8"))
        result_file.write_text(
            json.dumps(
                {
                    "id": command["id"],
                    "success": True,
                    "data": {"items": ["Module Diagnostics", "Engine Control Module"]},
                }
            ),
            encoding="utf-8",
        )

    responses: list[dict] = []

    def responder() -> None:
        while len(responses) < 2:
            if not command_file.exists():
                time.sleep(0.01)
                continue
            write_result_for_latest_command()
            responses.append(json.loads(command_file.read_text(encoding="utf-8")))
            command_file.unlink(missing_ok=True)

    thread = threading.Thread(target=responder, daemon=True)
    thread.start()

    assert nav.get_navigation_path() == ["Module Diagnostics", "Engine Control Module"]
    click_result = nav.click_navigation_path_item("Module Diagnostics")
    assert click_result["success"] is True

    thread.join(timeout=1.0)

    assert responses[0]["action"] == "get_navigation_path"
    assert responses[1]["action"] == "click_navigation_path_item"
    assert responses[1]["params"] == {"text": "Module Diagnostics"}


def test_agent_navigator_clear_dtcs_selection_state_command(tmp_path: Path) -> None:
    nav = AgentNavigator(data_dir=tmp_path, timeout_sec=0.5)

    command_file = tmp_path / "command.json"
    result_file = tmp_path / "result.json"

    def responder() -> None:
        while not command_file.exists():
            time.sleep(0.01)
        command = json.loads(command_file.read_text(encoding="utf-8"))
        result_file.write_text(
            json.dumps(
                {
                    "id": command["id"],
                    "success": True,
                    "data": {
                        "selectedModules": {"rows": ["Engine Control Module"]},
                        "buttons": {"OK": {"enabled": True}},
                    },
                }
            ),
            encoding="utf-8",
        )
        command_file.unlink(missing_ok=True)

    thread = threading.Thread(target=responder, daemon=True)
    thread.start()

    result = nav.get_clear_dtcs_selection_state()

    thread.join(timeout=1.0)

    assert result["success"] is True
    assert result["data"]["selectedModules"]["rows"] == ["Engine Control Module"]


def test_agent_navigator_disables_unsupported_navigation_path_command_after_first_failure(
    tmp_path: Path,
) -> None:
    nav = AgentNavigator(data_dir=tmp_path, timeout_sec=0.5)

    command_file = tmp_path / "command.json"
    result_file = tmp_path / "result.json"
    responses: list[dict[str, object]] = []

    def responder() -> None:
        while len(responses) < 1:
            if not command_file.exists():
                time.sleep(0.01)
                continue
            command = json.loads(command_file.read_text(encoding="utf-8"))
            responses.append(command)
            result_file.write_text(
                json.dumps(
                    {
                        "id": command["id"],
                        "success": False,
                        "message": "Unknown action: get_navigation_path",
                    }
                ),
                encoding="utf-8",
            )
            command_file.unlink(missing_ok=True)

    thread = threading.Thread(target=responder, daemon=True)
    thread.start()

    assert nav.get_navigation_path() == []
    thread.join(timeout=1.0)

    assert nav.get_navigation_path() == []
    assert len(responses) == 1
