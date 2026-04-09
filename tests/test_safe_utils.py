from __future__ import annotations

from enum import Enum

from diagnostic_platform.safe_utils import (
    display_text,
    json_dumps_safe,
    mapping_or_empty,
    sse_message_or_none,
    status_value,
    strip_optional_text,
)


class _FakeStatus(Enum):
    RUNNING = "running"


class _DisplayValue:
    def __str__(self) -> str:
        return "display-value"


def test_strip_optional_text_returns_empty_for_non_strings() -> None:
    assert strip_optional_text("  text  ") == "text"
    assert strip_optional_text(["bad"]) == ""


def test_display_text_falls_back_to_string_representation() -> None:
    assert display_text("  text  ") == "text"
    assert display_text(_DisplayValue()) == "display-value"
    assert display_text(None, default="fallback") == "fallback"


def test_mapping_or_empty_and_status_value_normalize_common_shapes() -> None:
    assert mapping_or_empty({"ok": True}) == {"ok": True}
    assert mapping_or_empty(["bad"]) == {}
    assert status_value(_FakeStatus.RUNNING) == "running"
    assert status_value("completed") == "completed"
    assert status_value(None) == "unknown"


def test_sse_message_or_none_and_json_dumps_safe_handle_unsafe_values() -> None:
    assert sse_message_or_none("event: done\n\n") == "event: done\n\n"
    assert sse_message_or_none(b"event: done\n\n") == "event: done\n\n"
    assert sse_message_or_none(["bad"]) is None
    assert json_dumps_safe({"value": _DisplayValue()}) == '{"value": "display-value"}'
