from pathlib import Path
import importlib


ROOT = Path(__file__).resolve().parent.parent


def test_readme_setup_references_existing_requirements_files():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "requirements-cloud.txt" in readme
    assert "requirements-client.txt" in readme
    assert "requirements.txt" not in readme


def test_setup_windows_script_references_existing_entrypoints():
    script = (ROOT / "setup_windows.bat").read_text(encoding="utf-8")

    assert "requirements-cloud.txt" in script
    assert "requirements-client.txt" in script
    assert "python app.py --port 8080" in script
    assert "python -m vci_proxy.client_gui" in script
    assert "requirements-minimal.txt" not in script
    assert "python main.py inspect" not in script


def test_requirements_cloud_excludes_langgraph_stack():
    requirements = (ROOT / "requirements-cloud.txt").read_text(encoding="utf-8")

    assert "langgraph" not in requirements
    assert "langchain-core" not in requirements
    assert "langchain-openai" not in requirements
    assert "langchain-google-genai" not in requirements


def test_gds2_orchestration_package_replaces_agentic_namespace():
    assert (ROOT / "src" / "gds2_orchestration").is_dir()
    assert not (ROOT / "src" / "agentic").exists()


def test_runtime_layers_stop_importing_src_agentic_namespace():
    source_roots = [
        ROOT / "backends",
        ROOT / "diagnostic_platform",
        ROOT / "server",
        ROOT / "src",
    ]

    for source_root in source_roots:
        for path in source_root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            assert "from src.agentic" not in text, f"{path} should stop importing src.agentic"
            assert "import src.agentic" not in text, f"{path} should stop importing src.agentic"


def test_session_runtime_uses_backend_name_not_session_workflow_property():
    targets = [
        ROOT / "diagnostic_platform" / "runtime" / "session_decisions.py",
        ROOT / "diagnostic_platform" / "runtime" / "session_lifecycle.py",
        ROOT / "diagnostic_platform" / "runtime" / "session_preflight.py",
        ROOT / "src" / "gds2_orchestration" / "session_orchestrator.py",
    ]

    for path in targets:
        text = path.read_text(encoding="utf-8")
        assert "session.workflow" not in text, f"{path} should use backend_name semantics"
        assert "route_workflow" not in text, f"{path} should stop exposing route_workflow"


def test_public_session_docs_mark_workflow_as_deprecated_alias():
    session_api = (ROOT / "server" / "api" / "session.py").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    api_architecture = (ROOT / "agent_docs" / "api_architecture.md").read_text(encoding="utf-8")

    assert '"workflow": "gds2",        # deprecated alias' in session_api
    assert "deprecated alias for `backend_name`" in readme
    assert "deprecated alias of `backend_name`" in api_architecture


def test_session_dependencies_module_exposes_runtime_accessors():
    module = importlib.import_module("server.api.session_dependencies")

    assert hasattr(module, "get_orchestrator")
    assert hasattr(module, "set_orchestrator")
    assert hasattr(module, "set_data_viewer_getter")
    assert hasattr(module, "get_executor")
    assert hasattr(module, "get_adapter")
    assert hasattr(module, "reset_executor")


def test_session_domain_handler_modules_exist():
    ai_module = importlib.import_module("server.api.session_ai_handlers")
    live_data_module = importlib.import_module("server.api.session_live_data_handlers")
    navigation_module = importlib.import_module("server.api.session_navigation_handlers")

    assert hasattr(ai_module, "start_ai_diagnose")
    assert hasattr(ai_module, "stream_ai_diagnose_events")
    assert hasattr(ai_module, "retry_ai_diagnose")
    assert hasattr(live_data_module, "start_live_data_session")
    assert hasattr(live_data_module, "stream_live_data_events")
    assert hasattr(live_data_module, "stop_live_data_session")
    assert hasattr(live_data_module, "read_session_dtcs")
    assert hasattr(navigation_module, "start_navigation_session_for_business")
    assert hasattr(navigation_module, "stream_navigation_events")
    assert hasattr(navigation_module, "submit_navigation_decision_for_business")
    assert hasattr(navigation_module, "abort_navigation_session_for_business")
    assert hasattr(navigation_module, "build_navigation_status_for_business")
