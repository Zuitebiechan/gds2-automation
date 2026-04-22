from __future__ import annotations

from pathlib import Path


def test_virtual_j2534_source_contains_observability_jsonl_writer() -> None:
    source = Path("vci_proxy/virtual_dll/virtual_j2534.c").read_text(encoding="utf-8", errors="replace")

    assert "observability.v1" in source
    assert "static void jsonl_init(void)" in source
    assert "static void jsonl_emit_event(" in source
    assert "static void emit_j2534_call_started(" in source
    assert "static void emit_j2534_call_finished(" in source
    assert "static void emit_j2534_call_failed(" in source


def test_virtual_j2534_source_contains_phase3_event_types() -> None:
    source = Path("vci_proxy/virtual_dll/virtual_j2534.c").read_text(encoding="utf-8", errors="replace")

    for event_name in [
        "j2534.call.started",
        "j2534.call.finished",
        "j2534.call.failed",
        "dll.socket.connect_failed",
        "dll.socket.connected",
        "dll.socket.send_failed",
        "dll.socket.recv_failed",
        "dll.request.retry",
        "dll.request.retry_succeeded",
        "dll.request.retry_exhausted",
        "j2534.read_msgs.buffer_empty_aggregate",
    ]:
        assert event_name in source


def test_virtual_j2534_source_tracks_actual_dll_sequence_from_send_recv() -> None:
    source = Path("vci_proxy/virtual_dll/virtual_j2534.c").read_text(encoding="utf-8", errors="replace")

    assert "unsigned long* used_sequence" in source
    assert "*used_sequence = sequence;" in source
    assert source.count("&dll_seq") >= 10
