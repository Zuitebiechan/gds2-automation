from __future__ import annotations

from server.app import create_app


def test_removed_diagnostics_routes_return_404() -> None:
    app = create_app()
    client = app.test_client()

    assert client.post("/api/diagnose/start").status_code == 404
    assert client.get("/api/diagnose/dtcs").status_code == 404
    assert client.post("/api/diagnose/live_data/start").status_code == 404
    assert client.post("/api/diagnose/ai_diagnose").status_code == 404
