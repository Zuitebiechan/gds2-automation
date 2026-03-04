"""Unit tests for capability registry."""

from src.agentic.capability_registry import CapabilityRegistry
from src.agentic.contracts.action_schema import GDS2Action


def test_registry_has_g1_actions():
    registry = CapabilityRegistry()
    all_actions = registry.all_actions()
    assert GDS2Action.START_DIAGNOSTICS in all_actions
    assert GDS2Action.SELECT_DEVICE in all_actions
    assert GDS2Action.READ_DTCS in all_actions
    assert GDS2Action.ABORT_SESSION in all_actions


def test_action_allowed_for_page():
    registry = CapabilityRegistry()
    assert registry.is_action_allowed("main_menu", GDS2Action.START_DIAGNOSTICS)
    assert not registry.is_action_allowed("main_menu", GDS2Action.READ_DTCS)


def test_get_allowed_actions():
    registry = CapabilityRegistry()
    actions = registry.get_allowed_actions("data_display")
    assert "read_dtcs" in actions
    assert "start_live_stream" in actions


def test_register_page_capability():
    registry = CapabilityRegistry()
    assert not registry.is_action_allowed("custom_page", GDS2Action.GO_HOME)
    registry.register_page_capability("custom_page", GDS2Action.GO_HOME)
    assert registry.is_action_allowed("custom_page", GDS2Action.GO_HOME)
