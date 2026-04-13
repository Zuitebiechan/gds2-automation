import sys
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _sample_spec():
    from diagnostic_platform.node_allocation import LocalZoneLaunchSpec

    return LocalZoneLaunchSpec(
        zone="us-west-2-lax-1a",
        metro="los-angeles",
        api_base_url="https://lax-1.diag.example.com",
        tunnel_host="lax-1.diag.example.com",
        launch_template_name="diag-local-zone-worker",
        instance_type="m6i.xlarge",
        subnet_id="subnet-12345",
        security_group_ids=("sg-12345", "sg-67890"),
    )


class _FakeClientError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.response = {
            "Error": {
                "Code": code,
                "Message": message,
            }
        }


def test_run_aws_launch_smoke_test_treats_dry_run_operation_as_success():
    from diagnostic_platform.aws_node_launch_smoke import run_aws_launch_smoke_test

    observed: dict[str, object] = {}

    class _FakeStsClient:
        def get_caller_identity(self):
            return {
                "Account": "123456789012",
                "Arn": "arn:aws:iam::123456789012:user/tester",
                "UserId": "AIDATEST",
            }

    class _FakeEc2Client:
        def run_instances(self, **kwargs):
            observed["run_instances"] = kwargs
            raise _FakeClientError(
                "DryRunOperation",
                "Request would have succeeded, but DryRun flag is set.",
            )

    class _FakeSession:
        def client(self, name, region_name=None):
            observed.setdefault("clients", []).append((name, region_name))
            if name == "sts":
                return _FakeStsClient()
            if name == "ec2":
                return _FakeEc2Client()
            raise AssertionError(name)

    result = run_aws_launch_smoke_test(
        region="us-west-2",
        spec=_sample_spec(),
        dry_run=True,
        keep_instance=False,
        session_factory=lambda profile=None: _FakeSession(),
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["launch_request"]["DryRun"] is True
    assert result["identity"]["Account"] == "123456789012"
    assert observed["clients"] == [
        ("sts", "us-west-2"),
        ("ec2", "us-west-2"),
    ]


def test_run_aws_launch_smoke_test_terminates_live_instance_by_default():
    from diagnostic_platform.aws_node_launch_smoke import run_aws_launch_smoke_test

    observed: dict[str, object] = {}

    class _FakeStsClient:
        def get_caller_identity(self):
            return {
                "Account": "123456789012",
                "Arn": "arn:aws:iam::123456789012:user/tester",
                "UserId": "AIDATEST",
            }

    class _FakeEc2Client:
        def run_instances(self, **kwargs):
            observed["run_instances"] = kwargs
            return {
                "Instances": [
                    {
                        "InstanceId": "i-0abc123",
                    }
                ]
            }

        def terminate_instances(self, *, InstanceIds):
            observed["terminate_instances"] = list(InstanceIds)
            return {"TerminatingInstances": [{"InstanceId": InstanceIds[0]}]}

    class _FakeSession:
        def client(self, name, region_name=None):
            if name == "sts":
                return _FakeStsClient()
            if name == "ec2":
                return _FakeEc2Client()
            raise AssertionError(name)

    result = run_aws_launch_smoke_test(
        region="us-west-2",
        spec=_sample_spec(),
        dry_run=False,
        keep_instance=False,
        session_factory=lambda profile=None: _FakeSession(),
    )

    assert result["success"] is True
    assert result["dry_run"] is False
    assert result["node"]["node_id"] == "i-0abc123"
    assert result["termination_requested"] is True
    assert observed["terminate_instances"] == ["i-0abc123"]


def test_smoke_cli_main_returns_2_when_required_configuration_is_missing(capsys):
    from diagnostic_platform.aws_node_launch_smoke import main

    status = main([])

    captured = capsys.readouterr()

    assert status == 2
    assert "Missing AWS smoke-test configuration" in captured.err


def test_resolve_smoke_test_inputs_can_route_from_zone_catalog_by_client_time_zone():
    from diagnostic_platform.aws_node_launch_smoke import resolve_smoke_test_inputs

    region, spec, route = resolve_smoke_test_inputs(
        environ={
            "DIAGNOSTIC_AWS_REGION": "us-east-1",
            "DIAGNOSTIC_NODE_LAUNCH_TEMPLATE": "diag-local-zone-worker",
            "DIAGNOSTIC_NODE_INSTANCE_TYPE": "c6i.xlarge",
            "DIAGNOSTIC_NODE_DEFAULT_ZONE": "us-east-1-dfw-2a",
            "DIAGNOSTIC_NODE_ZONE_CATALOG_JSON": json.dumps(
                {
                    "zones": [
                        {
                            "zone": "us-east-1-dfw-2a",
                            "metro": "dallas",
                            "subnet_id": "subnet-dfw",
                            "api_base_url": "https://dfw.diag.example.com",
                            "tunnel_host": "dfw.diag.example.com",
                            "time_zones": ["America/Chicago"],
                            "cities": ["Dallas"],
                        },
                        {
                            "zone": "us-east-1-atl-2a",
                            "metro": "atlanta",
                            "subnet_id": "subnet-atl",
                            "api_base_url": "https://atl.diag.example.com",
                            "tunnel_host": "atl.diag.example.com",
                            "time_zones": ["America/New_York"],
                            "cities": ["Atlanta"],
                        },
                    ]
                }
            ),
        },
        overrides={
            "client_time_zone": "America/Chicago",
            "brand": "Chevrolet",
        },
    )

    assert region == "us-east-1"
    assert spec.zone == "us-east-1-dfw-2a"
    assert spec.metro == "dallas"
    assert spec.subnet_id == "subnet-dfw"
    assert spec.api_base_url == "https://dfw.diag.example.com"
    assert spec.tunnel_host == "dfw.diag.example.com"
    assert route == {
        "preferred_zone": "",
        "preferred_metro": "dallas",
        "source": "client_time_zone",
    }


def test_resolve_smoke_test_inputs_accepts_windows_time_zone_alias_for_catalog_route():
    from diagnostic_platform.aws_node_launch_smoke import resolve_smoke_test_inputs

    _region, spec, route = resolve_smoke_test_inputs(
        environ={
            "DIAGNOSTIC_AWS_REGION": "us-east-1",
            "DIAGNOSTIC_NODE_LAUNCH_TEMPLATE": "diag-local-zone-worker",
            "DIAGNOSTIC_NODE_INSTANCE_TYPE": "c6i.xlarge",
            "DIAGNOSTIC_NODE_DEFAULT_ZONE": "us-east-1-dfw-2a",
            "DIAGNOSTIC_NODE_ZONE_CATALOG_JSON": json.dumps(
                {
                    "zones": [
                        {
                            "zone": "us-east-1-dfw-2a",
                            "metro": "dallas",
                            "subnet_id": "subnet-dfw",
                            "api_base_url": "https://dfw.diag.example.com",
                            "tunnel_host": "dfw.diag.example.com",
                            "time_zones": ["America/Chicago"],
                        }
                    ]
                }
            ),
        },
        overrides={
            "client_time_zone": "Central Standard Time",
        },
    )

    assert spec.zone == "us-east-1-dfw-2a"
    assert route == {
        "preferred_zone": "",
        "preferred_metro": "dallas",
        "source": "client_time_zone",
    }
