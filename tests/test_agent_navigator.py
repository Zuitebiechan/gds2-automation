from __future__ import annotations

import json
import threading
import time
from pathlib import Path

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
