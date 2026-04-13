"""Shared runtime accessors for the session API layer."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from diagnostic_platform.backend_registry import get_backend_registry
from diagnostic_platform.node_allocation import (
    AwsEc2LaunchTemplateProvisioner,
    BootingNodeReadinessMonitor,
    HotPoolAllocator,
    JsonFileLeaseStore,
    JsonFileNodeInventory,
    LocalZoneLaunchSpec,
    build_http_node_ready_probe,
)
from diagnostic_platform.safe_utils import strip_optional_text as _strip_optional_text
from diagnostic_platform.session_orchestrator import BusinessSessionOrchestrator
from diagnostic_platform.runtime.worker_runtime import get_worker_runtime

logger = logging.getLogger(__name__)
_NODE_ALLOCATOR: Any | None = None
_NODE_PROVISIONER: Any | None = None
_NODE_LAUNCH_SPEC_RESOLVER: Any | None = None
_NODE_ROUTE_RESOLVER: Any | None = None
_NODE_READINESS_MONITOR: Any | None = None
_TRUTHY_VALUES = {"1", "true", "yes", "on"}
_WINDOWS_TIME_ZONE_ALIASES = {
    "alaskan standard time": "america/anchorage",
    "central standard time": "america/chicago",
    "china standard time": "asia/shanghai",
    "eastern standard time": "america/new_york",
    "hawaiian standard time": "pacific/honolulu",
    "mountain standard time": "america/denver",
    "pacific standard time": "america/los_angeles",
    "tokyo standard time": "asia/tokyo",
    "us mountain standard time": "america/phoenix",
}


def _runtime():
    return get_worker_runtime()


def get_orchestrator() -> BusinessSessionOrchestrator:
    """Return the shared worker-scoped orchestrator."""
    return _runtime().orchestrator


def set_orchestrator(orch: BusinessSessionOrchestrator) -> None:
    """Replace the shared worker-scoped orchestrator."""
    _runtime().set_orchestrator(orch)


def set_data_viewer_getter(getter: Callable[[], Any] | None) -> None:
    """Inject a lightweight viewer object for tests that bypass GDS2 startup."""
    _runtime().set_data_viewer_getter(getter)


def _get_bound_session():
    runtime = _runtime()
    binding = runtime.get_business_session_binding()
    if binding.session_id:
        return runtime.orchestrator.get_session(binding.session_id)

    session = runtime.orchestrator.get_active_session()
    if session is not None:
        runtime.bind_business_session(session.session_id)
    return session


def _get_session(session_id: str | None = None):
    if session_id:
        session = _runtime().orchestrator.get_session(session_id)
        _runtime().bind_business_session(session.session_id)
        return session
    return _get_bound_session()


def _resolve_active_backend_descriptor(
    session_id: str | None = None,
    *,
    required: bool = True,
):
    session = _get_session(session_id)
    if session is None:
        raise RuntimeError("No active session bound to the worker")

    backend_name = _strip_optional_text(getattr(session, "backend_name", None))
    if not backend_name or backend_name == "manual":
        if required:
            raise RuntimeError("Active session does not have a runnable backend")
        return session, None

    return session, get_backend_registry().get_descriptor(backend_name)


def get_data_viewer() -> Any:
    """Return the injected viewer when present, otherwise the backend guided runtime."""
    return _runtime().get_data_viewer(get_backend)


def get_backend(
    session_id: str | None = None,
    *,
    required: bool = True,
) -> Any | None:
    session, descriptor = _resolve_active_backend_descriptor(
        session_id,
        required=required,
    )
    if descriptor is None:
        return None
    bundle = _runtime().ensure_backend_bundle(
        session.session_id,
        descriptor=descriptor,
        backend_factory=lambda: get_backend_registry().get_by_name(descriptor.backend_name),
    )
    return bundle.backend


def get_ai_engine():
    from . import diagnostics as diagnostics_api

    return diagnostics_api._get_ai_engine()


def get_executor() -> Any:
    """Return the worker-scoped executor, lazily wired by the active backend."""
    if _runtime().get_adapter() is None:
        logger.debug("Session API executor wiring with backend action runtime")
    return _runtime().get_executor(lambda: get_backend().build_action_runtime())


def get_adapter() -> Any | None:
    """Return the current backend action adapter (available after get_executor())."""
    return _runtime().get_adapter()


def reset_executor() -> None:
    """Reset backend-owned executor/adapter state."""
    _runtime().reset_executor()


def get_node_allocator() -> Any | None:
    """Return the shared node allocator used by bootstrap entrypoints."""
    return _NODE_ALLOCATOR


def set_node_allocator(allocator: Any | None) -> None:
    """Replace the shared node allocator used by bootstrap entrypoints."""
    global _NODE_ALLOCATOR
    _NODE_ALLOCATOR = allocator


def get_node_provisioner() -> Any | None:
    """Return the shared node provisioner used when hot-pool capacity is empty."""
    return _NODE_PROVISIONER


def set_node_provisioner(provisioner: Any | None) -> None:
    """Replace the shared node provisioner used for cold-start capacity."""
    global _NODE_PROVISIONER
    _NODE_PROVISIONER = provisioner


def get_launch_spec_resolver() -> Any | None:
    """Return the shared launch-spec resolver for cold-start capacity."""
    return _NODE_LAUNCH_SPEC_RESOLVER


def set_launch_spec_resolver(resolver: Any | None) -> None:
    """Replace the shared launch-spec resolver used for cold-start capacity."""
    global _NODE_LAUNCH_SPEC_RESOLVER
    _NODE_LAUNCH_SPEC_RESOLVER = resolver


def get_node_readiness_monitor() -> Any | None:
    """Return the shared readiness monitor for booting nodes."""
    return _NODE_READINESS_MONITOR


def get_node_route_resolver() -> Any | None:
    """Return the shared route resolver used to infer zone preferences."""
    return _NODE_ROUTE_RESOLVER


def set_node_route_resolver(resolver: Any | None) -> None:
    """Replace the shared route resolver used to infer zone preferences."""
    global _NODE_ROUTE_RESOLVER
    _NODE_ROUTE_RESOLVER = resolver


def set_node_readiness_monitor(monitor: Any | None) -> None:
    """Replace the shared readiness monitor for booting nodes."""
    global _NODE_READINESS_MONITOR
    _NODE_READINESS_MONITOR = monitor


def configure_node_allocator_from_env(
    environ: dict[str, str] | None = None,
) -> Any | None:
    """Configure a file-backed node allocator when inventory env vars are set."""
    env = os.environ if environ is None else environ
    inventory_path = str(env.get("DIAGNOSTIC_NODE_INVENTORY_FILE") or "").strip()
    lease_path = str(env.get("DIAGNOSTIC_NODE_LEASE_FILE") or "").strip()
    if not inventory_path or not lease_path:
        return get_node_allocator()

    allocator = HotPoolAllocator(
        JsonFileNodeInventory(inventory_path),
        lease_store=JsonFileLeaseStore(lease_path),
    )
    set_node_allocator(allocator)
    return allocator


def _read_env_text(
    env: dict[str, str],
    key: str,
) -> str:
    return str(env.get(key) or "").strip()


def _read_env_csv(
    env: dict[str, str],
    key: str,
) -> tuple[str, ...]:
    return tuple(
        item.strip()
        for item in _read_env_text(env, key).split(",")
        if item.strip()
    )


def _read_csv_text(value: Any) -> tuple[str, ...]:
    return tuple(
        item.strip()
        for item in str(value or "").split(",")
        if item.strip()
    )


def _coerce_string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return _read_csv_text(value)


def _normalize_lookup_text(
    value: Any,
    *,
    time_zone: bool = False,
) -> str:
    text = _strip_optional_text(value).lower()
    if not text:
        return ""
    if time_zone:
        return _WINDOWS_TIME_ZONE_ALIASES.get(text, text)
    return text


def _normalize_lookup_tuple(
    value: Any,
    *,
    time_zone: bool = False,
) -> tuple[str, ...]:
    normalized: list[str] = []
    for item in _coerce_string_tuple(value):
        normalized_text = _normalize_lookup_text(item, time_zone=time_zone)
        if normalized_text and normalized_text not in normalized:
            normalized.append(normalized_text)
    return tuple(normalized)


def _prepend_lookup_text(values: tuple[str, ...], value: Any) -> tuple[str, ...]:
    normalized_text = _normalize_lookup_text(value)
    if not normalized_text:
        return values
    if normalized_text in values:
        return values
    return (normalized_text, *values)


def _prepend_time_zone_lookup(values: tuple[str, ...], value: Any) -> tuple[str, ...]:
    normalized_text = _normalize_lookup_text(value, time_zone=True)
    if not normalized_text:
        return values
    if normalized_text in values:
        return values
    return (normalized_text, *values)


def _read_env_float(
    env: dict[str, str],
    key: str,
    *,
    default: float,
) -> float:
    raw_value = _read_env_text(env, key)
    if not raw_value:
        return default
    try:
        return float(raw_value)
    except ValueError:
        logger.warning("Ignoring invalid float env %s=%r", key, raw_value)
        return default


def _load_zone_catalog_from_env(
    env: dict[str, str],
) -> list[dict[str, Any]]:
    raw_json = _read_env_text(env, "DIAGNOSTIC_NODE_ZONE_CATALOG_JSON")
    if not raw_json:
        catalog_path = _read_env_text(env, "DIAGNOSTIC_NODE_ZONE_CATALOG_FILE")
        if catalog_path:
            try:
                raw_json = Path(catalog_path).read_text(encoding="utf-8")
            except OSError:
                logger.warning(
                    "Ignoring unreadable DIAGNOSTIC_NODE_ZONE_CATALOG_FILE=%r",
                    catalog_path,
                )
                return []
    if not raw_json:
        return []
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError:
        logger.warning("Ignoring invalid DIAGNOSTIC_NODE_ZONE_CATALOG_JSON payload")
        return []
    if isinstance(payload, dict):
        zones = payload.get("zones")
        if isinstance(zones, list):
            return [item for item in zones if isinstance(item, dict)]
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _build_zone_catalog_launch_spec_resolver(
    *,
    zone_catalog: list[dict[str, Any]],
    default_zone: str,
    default_metro: str,
    default_api_base: str,
    default_tunnel_host: str,
    launch_template_name: str,
    instance_type: str,
    subnet_id: str,
    security_group_ids: tuple[str, ...],
) -> Callable[..., LocalZoneLaunchSpec] | None:
    normalized_entries: list[dict[str, Any]] = []
    for raw_entry in zone_catalog:
        zone = _strip_optional_text(raw_entry.get("zone"))
        metro = _strip_optional_text(raw_entry.get("metro"))
        entry_subnet_id = _strip_optional_text(raw_entry.get("subnet_id")) or subnet_id
        entry_api_base = _strip_optional_text(raw_entry.get("api_base_url")) or default_api_base
        entry_tunnel_host = _strip_optional_text(raw_entry.get("tunnel_host")) or default_tunnel_host
        entry_launch_template = (
            _strip_optional_text(raw_entry.get("launch_template_name"))
            or launch_template_name
        )
        entry_instance_type = _strip_optional_text(raw_entry.get("instance_type")) or instance_type
        entry_security_group_ids = _coerce_string_tuple(
            raw_entry.get("security_group_ids")
        ) or security_group_ids
        if not all(
            (
                zone,
                metro,
                entry_subnet_id,
                entry_api_base,
                entry_tunnel_host,
                entry_launch_template,
                entry_instance_type,
            )
        ):
            logger.warning(
                "Skipping incomplete node zone catalog entry zone=%r metro=%r",
                zone,
                metro,
            )
            continue
        city_keys = _normalize_lookup_tuple(raw_entry.get("cities"))
        metro_keys = _normalize_lookup_tuple(raw_entry.get("metro_aliases"))
        for city_key in city_keys:
            if city_key not in metro_keys:
                metro_keys = (*metro_keys, city_key)
        metro_keys = _prepend_lookup_text(metro_keys, metro)
        zone_keys = _prepend_lookup_text(
            _normalize_lookup_tuple(raw_entry.get("zone_aliases")),
            zone,
        )
        city_keys = _prepend_lookup_text(city_keys, metro)
        time_zone_keys = _prepend_time_zone_lookup(
            _normalize_lookup_tuple(raw_entry.get("time_zones"), time_zone=True),
            raw_entry.get("time_zone"),
        )
        normalized_entries.append(
            {
                "zone": zone,
                "metro": metro,
                "subnet_id": entry_subnet_id,
                "api_base_url": entry_api_base,
                "tunnel_host": entry_tunnel_host,
                "launch_template_name": entry_launch_template,
                "instance_type": entry_instance_type,
                "security_group_ids": entry_security_group_ids,
                "zone_keys": zone_keys,
                "metro_keys": metro_keys,
                "city_keys": city_keys,
                "time_zone_keys": time_zone_keys,
            }
        )

    if not normalized_entries:
        return None

    def _match_entry(
        value: Any,
        *,
        entry_key: str,
        time_zone: bool = False,
    ) -> dict[str, Any] | None:
        normalized_value = _normalize_lookup_text(value, time_zone=time_zone)
        if not normalized_value:
            return None
        for entry in normalized_entries:
            if normalized_value in entry.get(entry_key, ()):
                return entry
        return None

    def _select_entry(
        *,
        preferred_zone: str,
        preferred_metro: str,
    ) -> dict[str, Any]:
        selected = _match_entry(preferred_zone, entry_key="zone_keys")
        if selected is not None:
            return selected
        selected = _match_entry(preferred_metro, entry_key="metro_keys")
        if selected is not None:
            return selected
        selected = _match_entry(default_zone, entry_key="zone_keys")
        if selected is not None:
            return selected
        selected = _match_entry(default_metro, entry_key="metro_keys")
        if selected is not None:
            return selected
        return normalized_entries[0]

    def _resolve(
        *,
        context: Any,
        preferred_zone: str = "",
        preferred_metro: str = "",
    ) -> LocalZoneLaunchSpec:
        selected = _select_entry(
            preferred_zone=preferred_zone,
            preferred_metro=preferred_metro,
        )
        brand = _strip_optional_text(getattr(context, "brand", None))
        tags = {"Brand": brand} if brand else None
        return LocalZoneLaunchSpec(
            zone=str(selected["zone"]),
            metro=str(selected["metro"]),
            api_base_url=str(selected["api_base_url"]),
            tunnel_host=str(selected["tunnel_host"]),
            launch_template_name=str(selected["launch_template_name"]),
            instance_type=str(selected["instance_type"]),
            subnet_id=str(selected["subnet_id"]),
            security_group_ids=tuple(selected["security_group_ids"]),
            tags=tags,
        )

    setattr(_resolve, "_normalized_entries", normalized_entries)
    return _resolve


def _build_zone_catalog_route_resolver(
    *,
    zone_catalog: list[dict[str, Any]],
    default_zone: str,
    default_metro: str,
    default_api_base: str,
    default_tunnel_host: str,
    launch_template_name: str,
    instance_type: str,
    subnet_id: str,
    security_group_ids: tuple[str, ...],
) -> Callable[..., dict[str, str]] | None:
    normalized_entries = []
    launch_spec_resolver = _build_zone_catalog_launch_spec_resolver(
        zone_catalog=zone_catalog,
        default_zone=default_zone,
        default_metro=default_metro,
        default_api_base=default_api_base,
        default_tunnel_host=default_tunnel_host,
        launch_template_name=launch_template_name,
        instance_type=instance_type,
        subnet_id=subnet_id,
        security_group_ids=security_group_ids,
    )
    if launch_spec_resolver is None:
        return None
    normalized_entries = getattr(launch_spec_resolver, "_normalized_entries", None) or []
    if not normalized_entries:
        return None

    def _match_entry(
        value: Any,
        *,
        entry_key: str,
        time_zone: bool = False,
    ) -> dict[str, Any] | None:
        normalized_value = _normalize_lookup_text(value, time_zone=time_zone)
        if not normalized_value:
            return None
        for entry in normalized_entries:
            if normalized_value in entry.get(entry_key, ()):
                return entry
        return None

    def _resolve(
        *,
        data: dict[str, Any],
    ) -> dict[str, str]:
        if not isinstance(data, dict):
            return {}

        explicit_zone = _match_entry(data.get("preferred_zone"), entry_key="zone_keys")
        if explicit_zone is not None:
            return {
                "preferred_zone": str(explicit_zone["zone"]),
                "preferred_metro": str(explicit_zone["metro"]),
                "source": "preferred_zone",
            }

        explicit_metro = _match_entry(data.get("preferred_metro"), entry_key="metro_keys")
        if explicit_metro is not None:
            return {
                "preferred_zone": "",
                "preferred_metro": str(explicit_metro["metro"]),
                "source": "preferred_metro",
            }

        for field_name, entry_key, is_time_zone in (
            ("client_time_zone", "time_zone_keys", True),
            ("organization_time_zone", "time_zone_keys", True),
            ("client_city", "city_keys", False),
            ("organization_city", "city_keys", False),
        ):
            entry = _match_entry(
                data.get(field_name),
                entry_key=entry_key,
                time_zone=is_time_zone,
            )
            if entry is not None:
                return {
                    "preferred_zone": "",
                    "preferred_metro": str(entry["metro"]),
                    "source": field_name,
                }

        return {}

    return _resolve


def _build_env_launch_spec_resolver(
    *,
    default_zone: str,
    default_metro: str,
    default_api_base: str,
    default_tunnel_host: str,
    launch_template_name: str,
    instance_type: str,
    subnet_id: str,
    security_group_ids: tuple[str, ...],
) -> Callable[..., LocalZoneLaunchSpec]:
    def _resolve(
        *,
        context: Any,
        preferred_zone: str = "",
        preferred_metro: str = "",
    ) -> LocalZoneLaunchSpec:
        zone = _strip_optional_text(preferred_zone) or default_zone
        metro = _strip_optional_text(preferred_metro) or default_metro
        brand = _strip_optional_text(getattr(context, "brand", None))
        tags = {"Brand": brand} if brand else None
        return LocalZoneLaunchSpec(
            zone=zone,
            metro=metro,
            api_base_url=default_api_base,
            tunnel_host=default_tunnel_host,
            launch_template_name=launch_template_name,
            instance_type=instance_type,
            subnet_id=subnet_id,
            security_group_ids=security_group_ids,
            tags=tags,
        )

    return _resolve


def configure_node_provisioning_from_env(
    environ: dict[str, str] | None = None,
) -> tuple[Any | None, Any | None]:
    """Configure AWS-based node provisioning and launch-spec resolution from env."""
    env = os.environ if environ is None else environ
    region = _read_env_text(env, "DIAGNOSTIC_AWS_REGION")
    launch_template_name = _read_env_text(env, "DIAGNOSTIC_NODE_LAUNCH_TEMPLATE")
    instance_type = _read_env_text(env, "DIAGNOSTIC_NODE_INSTANCE_TYPE")
    subnet_id = _read_env_text(env, "DIAGNOSTIC_NODE_SUBNET_ID")
    security_group_ids = _read_env_csv(env, "DIAGNOSTIC_NODE_SECURITY_GROUP_IDS")
    default_zone = _read_env_text(env, "DIAGNOSTIC_NODE_DEFAULT_ZONE")
    default_metro = _read_env_text(env, "DIAGNOSTIC_NODE_DEFAULT_METRO")
    default_api_base = _read_env_text(env, "DIAGNOSTIC_NODE_DEFAULT_API_BASE")
    default_tunnel_host = _read_env_text(env, "DIAGNOSTIC_NODE_DEFAULT_TUNNEL_HOST")
    zone_catalog = _load_zone_catalog_from_env(env)

    provisioner = get_node_provisioner()
    resolver = get_launch_spec_resolver()
    set_node_route_resolver(None)

    if region:
        try:
            import boto3
        except ImportError:
            logger.warning(
                "AWS node provisioning env is set, but boto3 is not installed; "
                "cold-start provisioning remains disabled."
            )
        else:
            provisioner = AwsEc2LaunchTemplateProvisioner(
                boto3.client("ec2", region_name=region)
            )
            set_node_provisioner(provisioner)

    catalog_resolver = _build_zone_catalog_launch_spec_resolver(
        zone_catalog=zone_catalog,
        default_zone=default_zone,
        default_metro=default_metro,
        default_api_base=default_api_base,
        default_tunnel_host=default_tunnel_host,
        launch_template_name=launch_template_name,
        instance_type=instance_type,
        subnet_id=subnet_id,
        security_group_ids=security_group_ids,
    )
    catalog_route_resolver = _build_zone_catalog_route_resolver(
        zone_catalog=zone_catalog,
        default_zone=default_zone,
        default_metro=default_metro,
        default_api_base=default_api_base,
        default_tunnel_host=default_tunnel_host,
        launch_template_name=launch_template_name,
        instance_type=instance_type,
        subnet_id=subnet_id,
        security_group_ids=security_group_ids,
    )
    if catalog_route_resolver is not None:
        set_node_route_resolver(catalog_route_resolver)
    if catalog_resolver is not None:
        resolver = catalog_resolver
        set_launch_spec_resolver(resolver)
        return provisioner, resolver

    required_resolver = {
        "DIAGNOSTIC_NODE_LAUNCH_TEMPLATE": launch_template_name,
        "DIAGNOSTIC_NODE_INSTANCE_TYPE": instance_type,
        "DIAGNOSTIC_NODE_SUBNET_ID": subnet_id,
        "DIAGNOSTIC_NODE_DEFAULT_ZONE": default_zone,
        "DIAGNOSTIC_NODE_DEFAULT_METRO": default_metro,
        "DIAGNOSTIC_NODE_DEFAULT_API_BASE": default_api_base,
        "DIAGNOSTIC_NODE_DEFAULT_TUNNEL_HOST": default_tunnel_host,
    }
    if all(required_resolver.values()):
        resolver = _build_env_launch_spec_resolver(
            default_zone=default_zone,
            default_metro=default_metro,
            default_api_base=default_api_base,
            default_tunnel_host=default_tunnel_host,
            launch_template_name=launch_template_name,
            instance_type=instance_type,
            subnet_id=subnet_id,
            security_group_ids=security_group_ids,
        )
        set_launch_spec_resolver(resolver)

    return provisioner, resolver


def configure_node_readiness_from_env(
    environ: dict[str, str] | None = None,
) -> Any | None:
    """Configure a booting-node readiness monitor when env opts it in."""
    env = os.environ if environ is None else environ
    enabled = _read_env_text(env, "DIAGNOSTIC_NODE_READINESS_ENABLED").lower()
    if enabled not in _TRUTHY_VALUES:
        return get_node_readiness_monitor()

    allocator = get_node_allocator()
    if allocator is None:
        logger.warning(
            "Node readiness monitoring was enabled, but no node allocator is configured."
        )
        return get_node_readiness_monitor()

    monitor = BootingNodeReadinessMonitor(
        allocator.inventory,
        probe=build_http_node_ready_probe(
            path=_read_env_text(env, "DIAGNOSTIC_NODE_READINESS_PATH")
            or "/api/session/bootstrap/ready",
            timeout_sec=_read_env_float(
                env,
                "DIAGNOSTIC_NODE_READINESS_TIMEOUT_SEC",
                default=3.0,
            ),
            api_token=(
                _read_env_text(env, "DIAGNOSTIC_NODE_READINESS_API_TOKEN")
                or _read_env_text(env, "DIAGNOSTIC_API_TOKEN")
            ),
        ),
        poll_interval_sec=_read_env_float(
            env,
            "DIAGNOSTIC_NODE_READINESS_INTERVAL_SEC",
            default=15.0,
        ),
    )
    set_node_readiness_monitor(monitor)
    return monitor


def start_node_readiness_monitor() -> bool:
    """Start the configured background readiness monitor when present."""
    monitor = get_node_readiness_monitor()
    starter = getattr(monitor, "start", None)
    if not callable(starter):
        return False
    return bool(starter())


def stop_node_readiness_monitor() -> bool:
    """Stop the configured background readiness monitor when present."""
    monitor = get_node_readiness_monitor()
    stopper = getattr(monitor, "stop", None)
    if not callable(stopper):
        return False
    return bool(stopper())
