from pathlib import Path

from src.streaming.agent_data_collector import AgentDataCollector


def test_page_guard_emits_recovery_event() -> None:
    events: list[dict[str, object]] = []

    collector = AgentDataCollector(
        json_path=Path('missing.json'),
        page_guard=lambda: {'ok': True, 'message': 'Recovered Data Display'},
        on_guard_event=events.append,
    )

    result = collector._run_page_guard()

    assert result == {'ok': True, 'message': 'Recovered Data Display'}
    assert collector.fatal_error is None


def test_page_guard_failure_stops_collector_and_reports_error() -> None:
    errors: list[str] = []

    collector = AgentDataCollector(
        json_path=Path('missing.json'),
        on_error=errors.append,
        page_guard=lambda: {'ok': False, 'error': 'Data Display guard failed'},
    )
    collector._running = True

    result = collector._run_page_guard()

    assert result == {'ok': False, 'error': 'Data Display guard failed'}
    assert collector.fatal_error == 'Data Display guard failed'
    assert collector.is_running is False
    assert errors == ['Data Display guard failed']
