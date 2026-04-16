import importlib
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_fake_flask_stack(monkeypatch):
    class FakeBlueprint:
        def __init__(self, name, import_name, url_prefix=""):
            self.name = name
            self.import_name = import_name
            self.url_prefix = url_prefix
            self._registered_routes = []

        def route(self, path, methods=None):
            def decorator(fn):
                self._registered_routes.append(path)
                return fn

            return decorator

    fake_request = types.SimpleNamespace(args={}, json=None, headers={})
    fake_flask = types.ModuleType("flask")
    fake_flask.Blueprint = FakeBlueprint
    fake_flask.Response = object
    fake_flask.jsonify = lambda payload=None, **kwargs: payload if payload is not None else kwargs
    fake_flask.request = fake_request
    monkeypatch.setitem(sys.modules, "flask", fake_flask)
    return fake_request


def _import_session_api(monkeypatch):
    fake_request = _install_fake_flask_stack(monkeypatch)
    for module_name in [
        "server.api.session",
        "server.api.session_ai_handlers",
        "server.api.session_live_data_handlers",
        "server.api.session_navigation_handlers",
        "server.api.session_dependencies",
    ]:
        sys.modules.pop(module_name, None)
    session_api = importlib.import_module("server.api.session")
    session_dependencies = importlib.import_module("server.api.session_dependencies")
    return session_api, session_dependencies, fake_request


def _unwrap_response(result):
    if isinstance(result, tuple):
        payload, status = result
        return payload, status
    return result, 200


def test_session_bootstrap_returns_assigned_node(monkeypatch):
    from diagnostic_platform.node_allocation import (
        HotPoolAllocator,
        InMemoryNodeInventory,
        NodeRecord,
        NodeState,
    )

    session_api, session_dependencies, fake_request = _import_session_api(monkeypatch)
    allocator = HotPoolAllocator(
        InMemoryNodeInventory(
            nodes=[
                NodeRecord(
                    node_id="node-lax-1",
                    zone="us-west-2-lax-1a",
                    metro="los-angeles",
                    api_base_url="https://lax-1.example.com",
                    tunnel_host="lax-1.example.com",
                    state=NodeState.IDLE,
                    healthy=True,
                )
            ]
        )
    )
    session_dependencies.set_node_allocator(allocator)
    fake_request.json = {
        "brand": "Chevrolet",
        "model": "Malibu",
        "vin": "VIN123",
        "preferred_zone": "us-west-2-lax-1a",
        "preferred_metro": "los-angeles",
    }

    payload, status = _unwrap_response(session_api.session_bootstrap())

    assert status == 200
    assert payload["success"] is True
    assert payload["assignment"]["assignment_id"]
    assert payload["assignment"]["node_id"] == "node-lax-1"
    assert payload["assignment"]["api_base_url"] == "https://lax-1.example.com"
    assert payload["assignment"]["tunnel_host"] == "lax-1.example.com"
    assert payload["assignment"]["selection_reason"] == "preferred_zone"
    assert payload["session_context"]["brand"] == "Chevrolet"
    assert payload["next_action"] == "start_session_on_assigned_node"


def test_session_bootstrap_can_infer_preferred_metro_from_location_hints(monkeypatch):
    from diagnostic_platform.node_allocation import (
        HotPoolAllocator,
        InMemoryNodeInventory,
        NodeRecord,
        NodeState,
    )

    session_api, session_dependencies, fake_request = _import_session_api(monkeypatch)
    allocator = HotPoolAllocator(
        InMemoryNodeInventory(
            nodes=[
                NodeRecord(
                    node_id="node-dfw-1",
                    zone="us-east-1-dfw-2a",
                    metro="dallas",
                    api_base_url="https://dfw-1.example.com",
                    tunnel_host="dfw-1.example.com",
                    state=NodeState.IDLE,
                    healthy=True,
                ),
                NodeRecord(
                    node_id="node-atl-1",
                    zone="us-east-1-atl-2a",
                    metro="atlanta",
                    api_base_url="https://atl-1.example.com",
                    tunnel_host="atl-1.example.com",
                    state=NodeState.IDLE,
                    healthy=True,
                ),
            ]
        )
    )
    session_dependencies.set_node_allocator(allocator)
    session_dependencies.set_node_route_resolver(
        lambda *, data: {
            "preferred_zone": "",
            "preferred_metro": "dallas"
            if str(data.get("client_time_zone") or "").strip() == "America/Chicago"
            else "",
            "source": "client_time_zone",
        }
    )
    fake_request.json = {
        "brand": "Chevrolet",
        "client_time_zone": "America/Chicago",
    }

    payload, status = _unwrap_response(session_api.session_bootstrap())

    assert status == 200
    assert payload["assignment"]["node_id"] == "node-dfw-1"
    assert payload["assignment"]["selection_reason"] == "preferred_metro"


def test_session_bootstrap_can_use_trusted_cloudfront_city_when_client_hint_is_missing(
    monkeypatch,
):
    from diagnostic_platform.node_allocation import (
        HotPoolAllocator,
        InMemoryNodeInventory,
        NodeRecord,
        NodeState,
    )

    session_api, session_dependencies, fake_request = _import_session_api(monkeypatch)
    allocator = HotPoolAllocator(
        InMemoryNodeInventory(
            nodes=[
                NodeRecord(
                    node_id="node-atl-1",
                    zone="us-east-1-atl-2a",
                    metro="atlanta",
                    api_base_url="https://atl-1.example.com",
                    tunnel_host="atl-1.example.com",
                    state=NodeState.IDLE,
                    healthy=True,
                ),
                NodeRecord(
                    node_id="node-dfw-1",
                    zone="us-east-1-dfw-2a",
                    metro="dallas",
                    api_base_url="https://dfw-1.example.com",
                    tunnel_host="dfw-1.example.com",
                    state=NodeState.IDLE,
                    healthy=True,
                ),
            ]
        )
    )
    session_dependencies.set_node_allocator(allocator)
    session_dependencies.set_node_geo_routing_enabled(True)
    session_dependencies.set_trust_cloudfront_headers(True)
    session_dependencies.set_node_route_resolver(
        lambda *, data: {
            "preferred_zone": "",
            "preferred_metro": "dallas"
            if str(data.get("client_city") or "").strip() == "Dallas"
            else "",
            "source": "client_city"
            if str(data.get("client_city") or "").strip() == "Dallas"
            else "",
        }
    )
    fake_request.json = {
        "brand": "Chevrolet",
    }
    fake_request.headers = {
        "CloudFront-Viewer-City": "Dallas",
    }

    payload, status = _unwrap_response(session_api.session_bootstrap())

    assert status == 200
    assert payload["assignment"]["node_id"] == "node-dfw-1"
    assert payload["assignment"]["selection_reason"] == "preferred_metro"


def test_session_bootstrap_can_use_trusted_cloudfront_time_zone_when_city_is_missing(
    monkeypatch,
):
    from diagnostic_platform.node_allocation import (
        HotPoolAllocator,
        InMemoryNodeInventory,
        NodeRecord,
        NodeState,
    )

    session_api, session_dependencies, fake_request = _import_session_api(monkeypatch)
    allocator = HotPoolAllocator(
        InMemoryNodeInventory(
            nodes=[
                NodeRecord(
                    node_id="node-atl-1",
                    zone="us-east-1-atl-2a",
                    metro="atlanta",
                    api_base_url="https://atl-1.example.com",
                    tunnel_host="atl-1.example.com",
                    state=NodeState.IDLE,
                    healthy=True,
                ),
                NodeRecord(
                    node_id="node-dfw-1",
                    zone="us-east-1-dfw-2a",
                    metro="dallas",
                    api_base_url="https://dfw-1.example.com",
                    tunnel_host="dfw-1.example.com",
                    state=NodeState.IDLE,
                    healthy=True,
                ),
            ]
        )
    )
    session_dependencies.set_node_allocator(allocator)
    session_dependencies.set_node_geo_routing_enabled(True)
    session_dependencies.set_trust_cloudfront_headers(True)
    session_dependencies.set_node_route_resolver(
        lambda *, data: {
            "preferred_zone": "",
            "preferred_metro": "dallas"
            if str(data.get("client_time_zone") or "").strip() == "America/Chicago"
            else "",
            "source": "client_time_zone"
            if str(data.get("client_time_zone") or "").strip() == "America/Chicago"
            else "",
        }
    )
    fake_request.json = {
        "brand": "Chevrolet",
    }
    fake_request.headers = {
        "CloudFront-Viewer-Time-Zone": "America/Chicago",
    }

    payload, status = _unwrap_response(session_api.session_bootstrap())

    assert status == 200
    assert payload["assignment"]["node_id"] == "node-dfw-1"
    assert payload["assignment"]["selection_reason"] == "preferred_metro"


def test_session_bootstrap_logs_route_decision_metadata(
    monkeypatch,
    caplog,
):
    from diagnostic_platform.node_allocation import (
        HotPoolAllocator,
        InMemoryNodeInventory,
        NodeRecord,
        NodeState,
    )

    session_api, session_dependencies, fake_request = _import_session_api(monkeypatch)
    allocator = HotPoolAllocator(
        InMemoryNodeInventory(
            nodes=[
                NodeRecord(
                    node_id="node-dfw-1",
                    zone="us-east-1-dfw-2a",
                    metro="dallas",
                    api_base_url="https://dfw-1.example.com",
                    tunnel_host="dfw-1.example.com",
                    state=NodeState.IDLE,
                    healthy=True,
                ),
            ]
        )
    )
    session_dependencies.set_node_allocator(allocator)
    session_dependencies.set_node_geo_routing_enabled(True)
    session_dependencies.set_trust_cloudfront_headers(True)
    session_dependencies.set_node_route_resolver(
        lambda *, data: {
            "preferred_zone": "",
            "preferred_metro": "dallas"
            if str(data.get("client_city") or "").strip() == "Dallas"
            else "",
            "source": "client_city"
            if str(data.get("client_city") or "").strip() == "Dallas"
            else "",
        }
    )
    fake_request.json = {
        "brand": "Chevrolet",
    }
    fake_request.headers = {
        "CloudFront-Viewer-City": "Dallas",
    }

    with caplog.at_level("INFO"):
        payload, status = _unwrap_response(session_api.session_bootstrap())

    assert status == 200
    assert payload["assignment"]["node_id"] == "node-dfw-1"
    assert "selected_zone=us-east-1-dfw-2a" in caplog.text
    assert "selected_metro=dallas" in caplog.text
    assert "route_source=cloudfront_viewer_city" in caplog.text
    assert "fallback_used=true" in caplog.text
    assert "signal_conflict=false" in caplog.text


def test_session_bootstrap_ignores_cloudfront_headers_when_trust_is_disabled(
    monkeypatch,
):
    from diagnostic_platform.node_allocation import (
        HotPoolAllocator,
        InMemoryNodeInventory,
        NodeRecord,
        NodeState,
    )

    session_api, session_dependencies, fake_request = _import_session_api(monkeypatch)
    allocator = HotPoolAllocator(
        InMemoryNodeInventory(
            nodes=[
                NodeRecord(
                    node_id="node-atl-1",
                    zone="us-east-1-atl-2a",
                    metro="atlanta",
                    api_base_url="https://atl-1.example.com",
                    tunnel_host="atl-1.example.com",
                    state=NodeState.IDLE,
                    healthy=True,
                ),
                NodeRecord(
                    node_id="node-dfw-1",
                    zone="us-east-1-dfw-2a",
                    metro="dallas",
                    api_base_url="https://dfw-1.example.com",
                    tunnel_host="dfw-1.example.com",
                    state=NodeState.IDLE,
                    healthy=True,
                ),
            ]
        )
    )
    session_dependencies.set_node_allocator(allocator)
    session_dependencies.set_node_geo_routing_enabled(True)
    session_dependencies.set_trust_cloudfront_headers(False)
    session_dependencies.set_node_route_resolver(
        lambda *, data: {
            "preferred_zone": "",
            "preferred_metro": "dallas"
            if str(data.get("client_city") or "").strip() == "Dallas"
            else "",
            "source": "client_city"
            if str(data.get("client_city") or "").strip() == "Dallas"
            else "",
        }
    )
    fake_request.json = {
        "brand": "Chevrolet",
    }
    fake_request.headers = {
        "CloudFront-Viewer-City": "Dallas",
    }

    payload, status = _unwrap_response(session_api.session_bootstrap())

    assert status == 200
    assert payload["assignment"]["node_id"] == "node-atl-1"
    assert payload["assignment"]["selection_reason"] == "any_healthy_idle"


def test_session_bootstrap_prefers_client_hint_over_conflicting_cloudfront_signal(
    monkeypatch,
    caplog,
):
    from diagnostic_platform.node_allocation import (
        HotPoolAllocator,
        InMemoryNodeInventory,
        NodeRecord,
        NodeState,
    )

    session_api, session_dependencies, fake_request = _import_session_api(monkeypatch)
    allocator = HotPoolAllocator(
        InMemoryNodeInventory(
            nodes=[
                NodeRecord(
                    node_id="node-atl-1",
                    zone="us-east-1-atl-2a",
                    metro="atlanta",
                    api_base_url="https://atl-1.example.com",
                    tunnel_host="atl-1.example.com",
                    state=NodeState.IDLE,
                    healthy=True,
                ),
                NodeRecord(
                    node_id="node-dfw-1",
                    zone="us-east-1-dfw-2a",
                    metro="dallas",
                    api_base_url="https://dfw-1.example.com",
                    tunnel_host="dfw-1.example.com",
                    state=NodeState.IDLE,
                    healthy=True,
                ),
            ]
        )
    )
    session_dependencies.set_node_allocator(allocator)
    session_dependencies.set_node_geo_routing_enabled(True)
    session_dependencies.set_trust_cloudfront_headers(True)
    session_dependencies.set_node_route_resolver(
        lambda *, data: {
            "preferred_zone": "",
            "preferred_metro": (
                "atlanta"
                if str(data.get("client_time_zone") or "").strip() == "America/New_York"
                else "dallas"
                if str(data.get("client_time_zone") or "").strip() == "America/Chicago"
                else ""
            ),
            "source": "client_time_zone"
            if str(data.get("client_time_zone") or "").strip() in {
                "America/New_York",
                "America/Chicago",
            }
            else "",
        }
    )
    fake_request.json = {
        "brand": "Chevrolet",
        "client_time_zone": "America/New_York",
    }
    fake_request.headers = {
        "CloudFront-Viewer-Time-Zone": "America/Chicago",
    }

    with caplog.at_level("WARNING"):
        payload, status = _unwrap_response(session_api.session_bootstrap())

    assert status == 200
    assert payload["assignment"]["node_id"] == "node-atl-1"
    assert payload["assignment"]["selection_reason"] == "preferred_metro"
    assert "source_client=client_time_zone" in caplog.text
    assert "source_cloudfront=cloudfront_viewer_time_zone" in caplog.text


def test_session_bootstrap_ready_reports_worker_is_reachable(monkeypatch):
    session_api, _session_dependencies, _fake_request = _import_session_api(monkeypatch)

    payload, status = _unwrap_response(session_api.session_bootstrap_ready())

    assert status == 200
    assert payload == {
        "success": True,
        "ready": True,
        "status": "worker_ready",
    }


def test_session_bootstrap_returns_503_when_allocator_is_not_configured(monkeypatch):
    session_api, session_dependencies, fake_request = _import_session_api(monkeypatch)
    session_dependencies.set_node_allocator(None)
    fake_request.json = {"brand": "Chevrolet"}

    payload, status = _unwrap_response(session_api.session_bootstrap())

    assert status == 503
    assert payload == {
        "success": False,
        "error": "Node allocator is not configured",
    }


def test_session_bootstrap_bind_and_release_manage_assignment_lifecycle(monkeypatch):
    from diagnostic_platform.node_allocation import (
        HotPoolAllocator,
        InMemoryNodeInventory,
        NodeRecord,
        NodeState,
    )

    session_api, session_dependencies, fake_request = _import_session_api(monkeypatch)
    allocator = HotPoolAllocator(
        InMemoryNodeInventory(
            nodes=[
                NodeRecord(
                    node_id="node-lax-1",
                    zone="us-west-2-lax-1a",
                    metro="los-angeles",
                    api_base_url="https://lax-1.example.com",
                    tunnel_host="lax-1.example.com",
                    state=NodeState.IDLE,
                    healthy=True,
                )
            ]
        )
    )
    session_dependencies.set_node_allocator(allocator)

    fake_request.json = {
        "brand": "Chevrolet",
        "preferred_zone": "us-west-2-lax-1a",
        "preferred_metro": "los-angeles",
    }
    bootstrap_payload, bootstrap_status = _unwrap_response(session_api.session_bootstrap())
    assignment_id = bootstrap_payload["assignment"]["assignment_id"]

    fake_request.json = {
        "assignment_id": assignment_id,
        "session_id": "session-123",
    }
    bind_payload, bind_status = _unwrap_response(session_api.session_bootstrap_bind())

    assert bootstrap_status == 200
    assert bind_status == 200
    assert bind_payload == {
        "success": True,
        "assignment_id": assignment_id,
        "session_id": "session-123",
        "node_id": "node-lax-1",
    }

    fake_request.json = {"assignment_id": assignment_id}
    release_payload, release_status = _unwrap_response(session_api.session_bootstrap_release())

    assert release_status == 200
    assert release_payload == {
        "success": True,
        "assignment_id": assignment_id,
        "released": True,
        "node_id": "node-lax-1",
        "recovery_action": "idle",
        "node_state": "idle",
    }


def test_session_bootstrap_triggers_provisioning_when_hot_pool_is_empty(monkeypatch):
    from diagnostic_platform.node_allocation import (
        AwsEc2LaunchTemplateProvisioner,
        HotPoolAllocator,
        InMemoryNodeInventory,
        LocalZoneLaunchSpec,
    )

    class _FakeEc2Client:
        def run_instances(self, **kwargs):
            return {
                "Instances": [
                    {
                        "InstanceId": "i-0abc123",
                    }
                ]
            }

    session_api, session_dependencies, fake_request = _import_session_api(monkeypatch)
    session_dependencies.set_node_allocator(HotPoolAllocator(InMemoryNodeInventory()))
    session_dependencies.set_node_provisioner(
        AwsEc2LaunchTemplateProvisioner(_FakeEc2Client())
    )
    session_dependencies.set_launch_spec_resolver(
        lambda **kwargs: LocalZoneLaunchSpec(
            zone="us-west-2-lax-1a",
            metro="los-angeles",
            api_base_url="https://lax-1.diag.example.com",
            tunnel_host="lax-1.diag.example.com",
            launch_template_name="diag-local-zone-worker",
            instance_type="m6i.xlarge",
            subnet_id="subnet-12345",
        )
    )
    fake_request.json = {
        "brand": "Chevrolet",
        "preferred_zone": "us-west-2-lax-1a",
        "preferred_metro": "los-angeles",
    }

    payload, status = _unwrap_response(session_api.session_bootstrap())

    assert status == 202
    assert payload == {
        "success": True,
        "pending_capacity": True,
        "status": "capacity_pending",
        "retry_after_sec": 30,
        "next_action": "retry_session_bootstrap",
        "provisioning": {
            "node_id": "i-0abc123",
            "zone": "us-west-2-lax-1a",
            "metro": "los-angeles",
            "state": "booting",
        },
        "session_context": {
            "brand": "Chevrolet",
            "model": "",
            "vin": "",
            "backend_name": "",
            "extra": {},
        },
    }


def test_session_bootstrap_release_can_mark_node_for_reprobe(monkeypatch):
    from diagnostic_platform.node_allocation import (
        HotPoolAllocator,
        InMemoryNodeInventory,
        NodeRecord,
        NodeState,
    )

    session_api, session_dependencies, fake_request = _import_session_api(monkeypatch)
    allocator = HotPoolAllocator(
        InMemoryNodeInventory(
            nodes=[
                NodeRecord(
                    node_id="node-lax-1",
                    zone="us-west-2-lax-1a",
                    metro="los-angeles",
                    api_base_url="https://lax-1.example.com",
                    tunnel_host="lax-1.example.com",
                    state=NodeState.IDLE,
                    healthy=True,
                )
            ]
        )
    )
    session_dependencies.set_node_allocator(allocator)

    fake_request.json = {
        "brand": "Chevrolet",
        "preferred_zone": "us-west-2-lax-1a",
        "preferred_metro": "los-angeles",
    }
    bootstrap_payload, bootstrap_status = _unwrap_response(session_api.session_bootstrap())
    assignment_id = bootstrap_payload["assignment"]["assignment_id"]

    fake_request.json = {
        "assignment_id": assignment_id,
        "recovery_action": "reprobe",
    }
    release_payload, release_status = _unwrap_response(session_api.session_bootstrap_release())

    assert bootstrap_status == 200
    assert release_status == 200
    assert release_payload == {
        "success": True,
        "assignment_id": assignment_id,
        "released": True,
        "node_id": "node-lax-1",
        "recovery_action": "reprobe",
        "node_state": "booting",
    }
    released = allocator.inventory.get("node-lax-1")
    assert released.state == NodeState.BOOTING
    assert released.healthy is False


def test_session_bootstrap_reuses_existing_booting_capacity_before_launching_another_node(monkeypatch):
    from diagnostic_platform.node_allocation import (
        AwsEc2LaunchTemplateProvisioner,
        HotPoolAllocator,
        InMemoryNodeInventory,
        LocalZoneLaunchSpec,
        NodeRecord,
        NodeState,
    )

    launched: list[bool] = []

    class _FakeEc2Client:
        def run_instances(self, **kwargs):
            launched.append(True)
            return {"Instances": [{"InstanceId": "i-new"}]}

    session_api, session_dependencies, fake_request = _import_session_api(monkeypatch)
    session_dependencies.set_node_allocator(
        HotPoolAllocator(
            InMemoryNodeInventory(
                nodes=[
                    NodeRecord(
                        node_id="i-booting-1",
                        zone="us-west-2-lax-1a",
                        metro="los-angeles",
                        api_base_url="https://lax-1.diag.example.com",
                        tunnel_host="lax-1.diag.example.com",
                        state=NodeState.BOOTING,
                        healthy=False,
                    )
                ]
            )
        )
    )
    session_dependencies.set_node_provisioner(
        AwsEc2LaunchTemplateProvisioner(_FakeEc2Client())
    )
    session_dependencies.set_launch_spec_resolver(
        lambda **kwargs: LocalZoneLaunchSpec(
            zone="us-west-2-lax-1a",
            metro="los-angeles",
            api_base_url="https://lax-1.diag.example.com",
            tunnel_host="lax-1.diag.example.com",
            launch_template_name="diag-local-zone-worker",
            instance_type="m6i.xlarge",
            subnet_id="subnet-12345",
        )
    )
    fake_request.json = {
        "brand": "Chevrolet",
        "preferred_zone": "us-west-2-lax-1a",
        "preferred_metro": "los-angeles",
    }

    payload, status = _unwrap_response(session_api.session_bootstrap())

    assert status == 202
    assert payload["pending_capacity"] is True
    assert payload["provisioning"] == {
        "node_id": "i-booting-1",
        "zone": "us-west-2-lax-1a",
        "metro": "los-angeles",
        "state": "booting",
    }
    assert launched == []
