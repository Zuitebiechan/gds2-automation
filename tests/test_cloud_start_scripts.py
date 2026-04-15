from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_cloud_start_script_loads_local_config_and_requires_auth_token():
    script = (ROOT / "scripts" / "cloud_start_services.bat").read_text(encoding="utf-8")

    assert "cloud_service_config.cmd" in script
    assert "VCI_PROXY_AUTH_TOKEN" in script
    assert "if not defined vci_proxy_auth_token" in script.lower()


def test_cloud_start_script_passes_auth_token_and_optional_tls_flags_to_reverse_server():
    script = (ROOT / "scripts" / "cloud_start_services.bat").read_text(encoding="utf-8")

    assert "-m vci_proxy.reverse_server" in script
    assert "--auth-token \"%VCI_PROXY_AUTH_TOKEN%\"" in script
    assert "VCI_PROXY_TLS_ENABLED" in script
    assert "VCI_PROXY_TLS_CERT" in script
    assert "VCI_PROXY_TLS_KEY" in script


def test_cloud_autostart_assets_cover_config_driven_startup():
    register_script = (ROOT / "scripts" / "cloud_register_autostart.bat").read_text(
        encoding="utf-8"
    )
    deployment_doc = (ROOT / "agent_docs" / "ops" / "deployment_and_operations.md").read_text(
        encoding="utf-8"
    )

    assert (ROOT / "scripts" / "cloud_service_config.example.cmd").exists()
    assert "cloud_start_services.bat" in register_script
    assert "cloud_service_config.cmd" in deployment_doc
    assert "cloud_register_autostart.bat" in deployment_doc


def test_gitignore_excludes_local_cloud_service_config():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "scripts/cloud_service_config.cmd" in gitignore
