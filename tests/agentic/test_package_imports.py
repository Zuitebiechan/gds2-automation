"""Regression tests for lightweight src.agentic package imports."""

import importlib
import sys


def test_src_agentic_root_import_is_lazy():
    for module_name in (
        "src.agentic",
        "src.agentic.graph",
        "src.agentic.llm_factory",
        "src.agentic.state",
        "src.agentic.tools",
    ):
        sys.modules.pop(module_name, None)

    package = importlib.import_module("src.agentic")

    assert package.DeterministicExecutor is not None
    assert "src.agentic.graph" not in sys.modules
    assert "src.agentic.llm_factory" not in sys.modules
