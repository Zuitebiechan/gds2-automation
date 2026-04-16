def _load_node_allocation_module():
    try:
        import diagnostic_platform.node_allocation as node_allocation_module
    except ModuleNotFoundError:
        node_allocation_module = None

    assert node_allocation_module is not None, (
        "diagnostic_platform.node_allocation module should exist"
    )
    return node_allocation_module


def test_allocator_prefers_healthy_idle_node_in_preferred_zone():
    module = _load_node_allocation_module()

    InMemoryNodeInventory = getattr(module, "InMemoryNodeInventory", None)
    HotPoolAllocator = getattr(module, "HotPoolAllocator", None)
    NodeRecord = getattr(module, "NodeRecord", None)
    NodeState = getattr(module, "NodeState", None)

    assert InMemoryNodeInventory is not None
    assert HotPoolAllocator is not None
    assert NodeRecord is not None
    assert NodeState is not None

    inventory = InMemoryNodeInventory(
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
                node_id="node-lax-1",
                zone="us-west-2-lax-1a",
                metro="los-angeles",
                api_base_url="https://lax-1.example.com",
                tunnel_host="lax-1.example.com",
                state=NodeState.IDLE,
                healthy=True,
            ),
            NodeRecord(
                node_id="node-lax-2",
                zone="us-west-2-lax-1a",
                metro="los-angeles",
                api_base_url="https://lax-2.example.com",
                tunnel_host="lax-2.example.com",
                state=NodeState.UNHEALTHY,
                healthy=False,
            ),
        ]
    )
    allocator = HotPoolAllocator(inventory)

    lease = allocator.allocate(
        preferred_zone="us-west-2-lax-1a",
        preferred_metro="los-angeles",
        session_id="session-001",
    )

    assert lease.node.node_id == "node-lax-1"
    assert lease.node.zone == "us-west-2-lax-1a"
    assert lease.node.metro == "los-angeles"
    assert inventory.get("node-lax-1").state == NodeState.IN_USE


def test_allocator_release_returns_node_to_idle_pool():
    module = _load_node_allocation_module()

    inventory = module.InMemoryNodeInventory(
        nodes=[
            module.NodeRecord(
                node_id="node-lax-1",
                zone="us-west-2-lax-1a",
                metro="los-angeles",
                api_base_url="https://lax-1.example.com",
                tunnel_host="lax-1.example.com",
                state=module.NodeState.IDLE,
                healthy=True,
            )
        ]
    )
    allocator = module.HotPoolAllocator(inventory)

    lease = allocator.allocate(
        preferred_zone="us-west-2-lax-1a",
        preferred_metro="los-angeles",
        session_id="session-001",
    )

    allocator.release(lease)

    released = inventory.get("node-lax-1")
    assert released.state == module.NodeState.IDLE
    assert released.current_session_id is None

    second_lease = allocator.allocate(
        preferred_zone="us-west-2-lax-1a",
        preferred_metro="los-angeles",
        session_id="session-002",
    )
    assert second_lease.node.node_id == "node-lax-1"


def test_allocator_release_can_move_node_back_to_booting_for_reprobe():
    module = _load_node_allocation_module()

    inventory = module.InMemoryNodeInventory(
        nodes=[
            module.NodeRecord(
                node_id="node-lax-1",
                zone="us-west-2-lax-1a",
                metro="los-angeles",
                api_base_url="https://lax-1.example.com",
                tunnel_host="lax-1.example.com",
                state=module.NodeState.IDLE,
                healthy=True,
            )
        ]
    )
    allocator = module.HotPoolAllocator(inventory)

    lease = allocator.allocate(
        preferred_zone="us-west-2-lax-1a",
        preferred_metro="los-angeles",
        session_id="bootstrap-pending",
    )

    allocator.release(
        lease.assignment_id,
        next_state=module.NodeState.BOOTING,
        healthy=False,
    )

    released = inventory.get("node-lax-1")
    assert released.state == module.NodeState.BOOTING
    assert released.healthy is False
    assert released.current_session_id is None


def test_allocator_reports_fallback_reason_when_zone_is_unavailable():
    module = _load_node_allocation_module()

    inventory = module.InMemoryNodeInventory(
        nodes=[
            module.NodeRecord(
                node_id="node-lax-2",
                zone="us-west-2-lax-2a",
                metro="los-angeles",
                api_base_url="https://lax-2.example.com",
                tunnel_host="lax-2.example.com",
                state=module.NodeState.IDLE,
                healthy=True,
            ),
            module.NodeRecord(
                node_id="node-atl-1",
                zone="us-east-1-atl-2a",
                metro="atlanta",
                api_base_url="https://atl-1.example.com",
                tunnel_host="atl-1.example.com",
                state=module.NodeState.IDLE,
                healthy=True,
            ),
        ]
    )
    allocator = module.HotPoolAllocator(inventory)

    lease = allocator.allocate(
        preferred_zone="us-west-2-lax-1a",
        preferred_metro="los-angeles",
        session_id="session-003",
    )

    assert lease.node.node_id == "node-lax-2"
    assert lease.selection_reason == "preferred_metro"


def test_allocator_bind_and_release_track_assignment_lifecycle():
    module = _load_node_allocation_module()

    inventory = module.InMemoryNodeInventory(
        nodes=[
            module.NodeRecord(
                node_id="node-lax-1",
                zone="us-west-2-lax-1a",
                metro="los-angeles",
                api_base_url="https://lax-1.example.com",
                tunnel_host="lax-1.example.com",
                state=module.NodeState.IDLE,
                healthy=True,
            )
        ]
    )
    allocator = module.HotPoolAllocator(inventory)

    lease = allocator.allocate(
        preferred_zone="us-west-2-lax-1a",
        preferred_metro="los-angeles",
        session_id="bootstrap-pending",
    )

    assert getattr(lease, "assignment_id", "")
    assert inventory.get("node-lax-1").current_session_id == "bootstrap-pending"

    bound = allocator.bind(lease.assignment_id, session_id="session-123")

    assert bound.assignment_id == lease.assignment_id
    assert bound.session_id == "session-123"
    assert inventory.get("node-lax-1").current_session_id == "session-123"

    allocator.release(lease.assignment_id)

    released = inventory.get("node-lax-1")
    assert released.state == module.NodeState.IDLE
    assert released.current_session_id is None


def test_json_file_inventory_and_lease_store_persist_assignment_lifecycle(tmp_path):
    module = _load_node_allocation_module()

    inventory_path = tmp_path / "node_inventory.json"
    lease_path = tmp_path / "node_leases.json"

    inventory = module.JsonFileNodeInventory(
        inventory_path,
        nodes=[
            module.NodeRecord(
                node_id="node-lax-1",
                zone="us-west-2-lax-1a",
                metro="los-angeles",
                api_base_url="https://lax-1.example.com",
                tunnel_host="lax-1.example.com",
                state=module.NodeState.IDLE,
                healthy=True,
            )
        ],
    )
    lease_store = module.JsonFileLeaseStore(lease_path)
    allocator = module.HotPoolAllocator(inventory, lease_store=lease_store)

    lease = allocator.allocate(
        preferred_zone="us-west-2-lax-1a",
        preferred_metro="los-angeles",
        session_id="bootstrap-pending",
    )

    reloaded_inventory = module.JsonFileNodeInventory(inventory_path)
    reloaded_lease_store = module.JsonFileLeaseStore(lease_path)
    reloaded_allocator = module.HotPoolAllocator(
        reloaded_inventory,
        lease_store=reloaded_lease_store,
    )

    bound = reloaded_allocator.bind(lease.assignment_id, session_id="session-123")

    assert bound.assignment_id == lease.assignment_id
    assert reloaded_inventory.get("node-lax-1").current_session_id == "session-123"

    reloaded_allocator.release(lease.assignment_id)

    final_inventory = module.JsonFileNodeInventory(inventory_path)
    final_lease_store = module.JsonFileLeaseStore(lease_path)
    assert final_inventory.get("node-lax-1").state == module.NodeState.IDLE
    assert final_inventory.get("node-lax-1").current_session_id is None
    assert final_lease_store.get(lease.assignment_id) is None


def test_aws_ec2_launch_template_provisioner_registers_booting_node():
    module = _load_node_allocation_module()
    captured: dict[str, object] = {}

    class _FakeEc2Client:
        def run_instances(self, **kwargs):
            captured["kwargs"] = kwargs
            return {
                "Instances": [
                    {
                        "InstanceId": "i-0abc123",
                        "PrivateIpAddress": "10.0.0.15",
                    }
                ]
            }

    provisioner = module.AwsEc2LaunchTemplateProvisioner(_FakeEc2Client())
    spec = module.LocalZoneLaunchSpec(
        zone="us-west-2-lax-1a",
        metro="los-angeles",
        api_base_url="https://lax-1.diag.example.com",
        tunnel_host="lax-1.diag.example.com",
        launch_template_name="diag-local-zone-worker",
        instance_type="m6i.xlarge",
        subnet_id="subnet-12345",
        security_group_ids=("sg-12345",),
        tags={"Environment": "test"},
    )

    node = provisioner.launch_node(spec)

    assert node.node_id == "i-0abc123"
    assert node.state == module.NodeState.BOOTING
    assert node.healthy is False
    assert node.zone == "us-west-2-lax-1a"
    assert captured["kwargs"] == {
        "InstanceType": "m6i.xlarge",
        "LaunchTemplate": {"LaunchTemplateName": "diag-local-zone-worker"},
        "MaxCount": 1,
        "MinCount": 1,
        "Placement": {"AvailabilityZone": "us-west-2-lax-1a"},
        "SecurityGroupIds": ["sg-12345"],
        "SubnetId": "subnet-12345",
        "TagSpecifications": [
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Environment", "Value": "test"},
                    {"Key": "ManagedBy", "Value": "diagnostic-platform"},
                    {"Key": "Metro", "Value": "los-angeles"},
                ],
            }
        ],
    }


def test_provisioner_launch_into_inventory_persists_booting_node(tmp_path):
    module = _load_node_allocation_module()

    class _FakeEc2Client:
        def run_instances(self, **kwargs):
            return {
                "Instances": [
                    {
                        "InstanceId": "i-0abc123",
                    }
                ]
            }

    inventory_path = tmp_path / "node_inventory.json"
    inventory = module.JsonFileNodeInventory(inventory_path)
    provisioner = module.AwsEc2LaunchTemplateProvisioner(_FakeEc2Client())
    spec = module.LocalZoneLaunchSpec(
        zone="us-west-2-lax-1a",
        metro="los-angeles",
        api_base_url="https://lax-1.diag.example.com",
        tunnel_host="lax-1.diag.example.com",
        launch_template_name="diag-local-zone-worker",
        instance_type="m6i.xlarge",
        subnet_id="subnet-12345",
    )

    launched = provisioner.launch_into_inventory(spec, inventory)
    reloaded = module.JsonFileNodeInventory(inventory_path)

    assert launched.node_id == "i-0abc123"
    assert launched.state == module.NodeState.BOOTING
    assert reloaded.get("i-0abc123").state == module.NodeState.BOOTING
    assert reloaded.get("i-0abc123").healthy is False


def test_mark_ready_promotes_booting_node_to_healthy_idle_capacity():
    module = _load_node_allocation_module()

    inventory = module.InMemoryNodeInventory(
        nodes=[
            module.NodeRecord(
                node_id="i-booting-1",
                zone="us-west-2-lax-1a",
                metro="los-angeles",
                api_base_url="https://lax-1.diag.example.com",
                tunnel_host="lax-1.diag.example.com",
                state=module.NodeState.BOOTING,
                healthy=False,
            )
        ]
    )
    allocator = module.HotPoolAllocator(inventory)

    inventory.mark_ready("i-booting-1")
    lease = allocator.allocate(
        preferred_zone="us-west-2-lax-1a",
        preferred_metro="los-angeles",
        session_id="session-123",
    )

    assert inventory.get("i-booting-1").state == module.NodeState.IN_USE
    assert inventory.get("i-booting-1").healthy is True
    assert lease.node.node_id == "i-booting-1"


def test_booting_node_readiness_monitor_marks_ready_nodes_after_successful_probe():
    module = _load_node_allocation_module()

    inventory = module.InMemoryNodeInventory(
        nodes=[
            module.NodeRecord(
                node_id="i-booting-1",
                zone="us-west-2-lax-1a",
                metro="los-angeles",
                api_base_url="https://lax-1.diag.example.com",
                tunnel_host="lax-1.diag.example.com",
                state=module.NodeState.BOOTING,
                healthy=False,
            )
        ]
    )
    observed: list[str] = []
    monitor = module.BootingNodeReadinessMonitor(
        inventory,
        probe=lambda node: observed.append(node.node_id) or True,
        poll_interval_sec=5.0,
    )

    promoted = monitor.poll_once()

    assert observed == ["i-booting-1"]
    assert [node.node_id for node in promoted] == ["i-booting-1"]
    assert inventory.get("i-booting-1").state == module.NodeState.IDLE
    assert inventory.get("i-booting-1").healthy is True


def test_http_node_ready_probe_calls_bootstrap_ready_endpoint():
    module = _load_node_allocation_module()
    observed: dict[str, object] = {}

    class _FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"success": true, "ready": true}'

    def _urlopen(request, timeout=0):
        observed["url"] = request.full_url
        observed["headers"] = dict(request.header_items())
        observed["timeout"] = timeout
        return _FakeResponse()

    probe = module.build_http_node_ready_probe(
        path="/api/session/bootstrap/ready",
        timeout_sec=4.5,
        api_token="secret-token",
        urlopen=_urlopen,
    )

    ready = probe(
        module.NodeRecord(
            node_id="i-booting-1",
            zone="us-west-2-lax-1a",
            metro="los-angeles",
            api_base_url="https://lax-1.diag.example.com",
            tunnel_host="lax-1.diag.example.com",
            state=module.NodeState.BOOTING,
            healthy=False,
        )
    )

    assert ready is True
    assert observed == {
        "url": "https://lax-1.diag.example.com/api/session/bootstrap/ready",
        "headers": {
            "X-api-token": "secret-token",
        },
        "timeout": 4.5,
    }
