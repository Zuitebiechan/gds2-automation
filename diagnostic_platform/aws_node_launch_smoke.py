"""AWS smoke-test helpers for Local Zone worker launches."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from diagnostic_platform.node_allocation import (
    AwsEc2LaunchTemplateProvisioner,
    LocalZoneLaunchSpec,
    build_zone_catalog_launch_spec_resolver,
    build_zone_catalog_route_resolver,
)


def _read_text(value: Any) -> str:
    return str(value or "").strip()


def _read_csv(value: Any) -> tuple[str, ...]:
    return tuple(part.strip() for part in _read_text(value).split(",") if part.strip())


def _aws_error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        error = response.get("Error")
        if isinstance(error, Mapping):
            return _read_text(error.get("Code"))
    return ""


def _load_zone_catalog(
    *,
    environ: Mapping[str, str],
    overrides: Mapping[str, Any],
) -> list[dict[str, Any]]:
    raw_json = _read_text(
        overrides.get("zone_catalog_json")
        if overrides.get("zone_catalog_json") is not None
        else environ.get("DIAGNOSTIC_NODE_ZONE_CATALOG_JSON")
    )
    if not raw_json:
        catalog_path = _read_text(
            overrides.get("zone_catalog_file")
            if overrides.get("zone_catalog_file") is not None
            else environ.get("DIAGNOSTIC_NODE_ZONE_CATALOG_FILE")
        )
        if catalog_path:
            raw_json = Path(catalog_path).read_text(encoding="utf-8")
    if not raw_json:
        return []
    payload = json.loads(raw_json)
    if isinstance(payload, dict):
        zones = payload.get("zones")
        if isinstance(zones, list):
            return [item for item in zones if isinstance(item, dict)]
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _apply_route_preferences(
    *,
    requested_zone: str,
    requested_metro: str,
    route: Mapping[str, Any],
) -> tuple[str, str]:
    preferred_zone = requested_zone
    preferred_metro = requested_metro
    route_source = _read_text(route.get("source"))
    route_zone = _read_text(route.get("preferred_zone"))
    route_metro = _read_text(route.get("preferred_metro"))

    if route_zone and (not preferred_zone or route_source == "preferred_zone"):
        preferred_zone = route_zone
    if route_metro and (
        not preferred_metro
        or route_source in {"preferred_zone", "preferred_metro"}
    ):
        preferred_metro = route_metro
    return preferred_zone, preferred_metro


def resolve_smoke_test_inputs(
    *,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> tuple[str, LocalZoneLaunchSpec, dict[str, str]]:
    """Resolve region + launch spec from env-backed defaults and CLI overrides."""
    env = os.environ if environ is None else environ
    params = dict(overrides or {})

    def _pick(name: str, env_key: str) -> str:
        override_value = _read_text(params.get(name))
        if override_value:
            return override_value
        return _read_text(env.get(env_key))

    region = _pick("region", "DIAGNOSTIC_AWS_REGION")
    launch_template_name = _pick("launch_template_name", "DIAGNOSTIC_NODE_LAUNCH_TEMPLATE")
    instance_type = _pick("instance_type", "DIAGNOSTIC_NODE_INSTANCE_TYPE")
    subnet_id = _pick("subnet_id", "DIAGNOSTIC_NODE_SUBNET_ID")
    zone = _pick("zone", "DIAGNOSTIC_NODE_DEFAULT_ZONE")
    metro = _pick("metro", "DIAGNOSTIC_NODE_DEFAULT_METRO")
    api_base_url = _pick("api_base_url", "DIAGNOSTIC_NODE_DEFAULT_API_BASE")
    tunnel_host = _pick("tunnel_host", "DIAGNOSTIC_NODE_DEFAULT_TUNNEL_HOST")

    sg_override = params.get("security_group_ids")
    security_group_ids = (
        tuple(str(item).strip() for item in sg_override if str(item).strip())
        if isinstance(sg_override, (list, tuple))
        else _read_csv(
            sg_override
            if sg_override is not None
            else env.get("DIAGNOSTIC_NODE_SECURITY_GROUP_IDS")
        )
    )
    zone_catalog = _load_zone_catalog(environ=env, overrides=params)
    preferred_zone = _pick("preferred_zone", "DIAGNOSTIC_NODE_PREFERRED_ZONE")
    preferred_metro = _pick("preferred_metro", "DIAGNOSTIC_NODE_PREFERRED_METRO")
    route_input = {
        "preferred_zone": preferred_zone,
        "preferred_metro": preferred_metro,
        "client_time_zone": _pick("client_time_zone", "DIAGNOSTIC_NODE_CLIENT_TIME_ZONE"),
        "organization_time_zone": _pick(
            "organization_time_zone",
            "DIAGNOSTIC_NODE_ORGANIZATION_TIME_ZONE",
        ),
        "client_city": _pick("client_city", "DIAGNOSTIC_NODE_CLIENT_CITY"),
        "organization_city": _pick("organization_city", "DIAGNOSTIC_NODE_ORGANIZATION_CITY"),
    }

    if zone_catalog:
        route_resolver = build_zone_catalog_route_resolver(
            zone_catalog=zone_catalog,
            default_zone=zone,
            default_metro=metro,
            default_api_base=api_base_url,
            default_tunnel_host=tunnel_host,
            launch_template_name=launch_template_name,
            instance_type=instance_type,
            subnet_id=subnet_id,
            security_group_ids=security_group_ids,
        )
        launch_spec_resolver = build_zone_catalog_launch_spec_resolver(
            zone_catalog=zone_catalog,
            default_zone=zone,
            default_metro=metro,
            default_api_base=api_base_url,
            default_tunnel_host=tunnel_host,
            launch_template_name=launch_template_name,
            instance_type=instance_type,
            subnet_id=subnet_id,
            security_group_ids=security_group_ids,
        )
        route = route_resolver(data=route_input) if callable(route_resolver) else {}
        preferred_zone, preferred_metro = _apply_route_preferences(
            requested_zone=preferred_zone,
            requested_metro=preferred_metro,
            route=route,
        )
        if callable(launch_spec_resolver):
            spec = launch_spec_resolver(
                context=SimpleNamespace(brand=_pick("brand", "DIAGNOSTIC_NODE_BRAND")),
                preferred_zone=preferred_zone,
                preferred_metro=preferred_metro,
            )
            spec = replace(
                spec,
                tags={
                    **({"Purpose": "smoke-test"}),
                    **(dict(spec.tags or {})),
                },
            )
            if not region:
                raise ValueError("Missing AWS smoke-test configuration: region")
            return region, spec, dict(route)

    missing = [
        key
        for key, value in (
            ("region", region),
            ("launch_template_name", launch_template_name),
            ("instance_type", instance_type),
            ("subnet_id", subnet_id),
            ("zone", zone),
            ("metro", metro),
            ("api_base_url", api_base_url),
            ("tunnel_host", tunnel_host),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            "Missing AWS smoke-test configuration: " + ", ".join(missing)
        )

    return region, LocalZoneLaunchSpec(
        zone=zone,
        metro=metro,
        api_base_url=api_base_url,
        tunnel_host=tunnel_host,
        launch_template_name=launch_template_name,
        instance_type=instance_type,
        subnet_id=subnet_id,
        security_group_ids=security_group_ids,
        tags={"Purpose": "smoke-test"},
    ), {}


def run_aws_launch_smoke_test(
    *,
    region: str,
    spec: LocalZoneLaunchSpec,
    dry_run: bool = True,
    keep_instance: bool = False,
    profile: str = "",
    session_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Validate AWS identity and try one EC2 launch request for the worker spec."""
    if session_factory is None:
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - exercised in real runtime only
            raise RuntimeError(
                "boto3 is not installed. Install requirements-cloud.txt first."
            ) from exc

        def _default_session_factory(*, profile: str = ""):
            if profile:
                return boto3.session.Session(profile_name=profile)
            return boto3.session.Session()

        session_factory = _default_session_factory

    session = session_factory(profile=profile or "")
    sts = session.client("sts", region_name=region)
    identity = dict(sts.get_caller_identity())
    ec2 = session.client("ec2", region_name=region)
    provisioner = AwsEc2LaunchTemplateProvisioner(ec2)
    launch_request = provisioner.build_run_instances_kwargs(
        spec,
        dry_run=dry_run,
    )

    if dry_run:
        try:
            provisioner.launch_node(spec, dry_run=True)
        except Exception as exc:
            if _aws_error_code(exc) != "DryRunOperation":
                raise
        return {
            "success": True,
            "dry_run": True,
            "identity": identity,
            "launch_request": launch_request,
            "termination_requested": False,
        }

    node = provisioner.launch_node(spec)
    termination_requested = False
    if not keep_instance and node.node_id:
        ec2.terminate_instances(InstanceIds=[node.node_id])
        termination_requested = True
    return {
        "success": True,
        "dry_run": False,
        "identity": identity,
        "launch_request": launch_request,
        "node": node.to_dict(),
        "termination_requested": termination_requested,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Smoke-test one AWS Local Zone worker launch request.",
    )
    parser.add_argument("--profile", default="", help="Optional AWS profile name")
    parser.add_argument("--region", default="", help="AWS region")
    parser.add_argument("--launch-template-name", default="", help="EC2 launch template name")
    parser.add_argument("--instance-type", default="", help="EC2 instance type")
    parser.add_argument("--subnet-id", default="", help="Local Zone subnet id")
    parser.add_argument("--security-group-ids", default="", help="Comma-separated security group ids")
    parser.add_argument("--zone", default="", help="Local Zone availability zone, e.g. us-west-2-lax-1a")
    parser.add_argument("--metro", default="", help="Metro label, e.g. los-angeles")
    parser.add_argument("--preferred-zone", default="", help="Requested route zone before catalog resolution")
    parser.add_argument("--preferred-metro", default="", help="Requested route metro before catalog resolution")
    parser.add_argument("--client-time-zone", default="", help="Client IANA or Windows time zone hint")
    parser.add_argument("--organization-time-zone", default="", help="Organization time zone hint")
    parser.add_argument("--client-city", default="", help="Client city hint")
    parser.add_argument("--organization-city", default="", help="Organization city hint")
    parser.add_argument("--zone-catalog-file", default="", help="Path to the multi-zone catalog JSON file")
    parser.add_argument("--zone-catalog-json", default="", help="Inline zone catalog JSON payload")
    parser.add_argument("--brand", default="", help="Optional brand tag for the launched node")
    parser.add_argument("--api-base-url", default="", help="Assigned node API base url")
    parser.add_argument("--tunnel-host", default="", help="Assigned node tunnel host")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run a real launch instead of EC2 DryRun",
    )
    parser.add_argument(
        "--keep-instance",
        action="store_true",
        help="Keep the launched instance instead of terminating it immediately",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        region, spec, route = resolve_smoke_test_inputs(
            overrides={
                "region": args.region,
                "launch_template_name": args.launch_template_name,
                "instance_type": args.instance_type,
                "subnet_id": args.subnet_id,
                "security_group_ids": args.security_group_ids,
                "zone": args.zone,
                "metro": args.metro,
                "preferred_zone": args.preferred_zone,
                "preferred_metro": args.preferred_metro,
                "client_time_zone": args.client_time_zone,
                "organization_time_zone": args.organization_time_zone,
                "client_city": args.client_city,
                "organization_city": args.organization_city,
                "zone_catalog_file": args.zone_catalog_file,
                "zone_catalog_json": args.zone_catalog_json,
                "brand": args.brand,
                "api_base_url": args.api_base_url,
                "tunnel_host": args.tunnel_host,
            }
        )
        result = run_aws_launch_smoke_test(
            region=region,
            spec=spec,
            dry_run=not args.live,
            keep_instance=args.keep_instance,
            profile=args.profile,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    result["resolved_route"] = route
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"AWS identity: {result['identity'].get('Arn', '-')}")
        print(f"Region: {region}")
        print(f"Launch template: {spec.launch_template_name}")
        print(f"Zone: {spec.zone}")
        print(f"Metro: {spec.metro}")
        if route:
            print(
                "Resolved route: "
                f"source={route.get('source', '-')}, "
                f"preferred_zone={route.get('preferred_zone', '') or '-'}, "
                f"preferred_metro={route.get('preferred_metro', '') or '-'}"
            )
        print(f"Dry run: {result['dry_run']}")
        if result.get("node"):
            print(f"Instance launched: {result['node'].get('node_id', '-')}")
            print(f"Termination requested: {result['termination_requested']}")
        else:
            print("DryRun accepted by AWS.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI wrapper
    raise SystemExit(main())
