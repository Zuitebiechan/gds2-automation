"""Node allocation primitives for Local Zone worker pools."""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable
from urllib import request as urllib_request
import uuid

logger = logging.getLogger(__name__)
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
    text = str(value or "").strip().lower()
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
    if not normalized_text or normalized_text in values:
        return values
    return (normalized_text, *values)


def _prepend_time_zone_lookup(values: tuple[str, ...], value: Any) -> tuple[str, ...]:
    normalized_text = _normalize_lookup_text(value, time_zone=True)
    if not normalized_text or normalized_text in values:
        return values
    return (normalized_text, *values)


class NodeState(str, Enum):
    """Lifecycle states for one worker node in the allocation pool."""

    IDLE = "idle"
    ALLOCATING = "allocating"
    BOOTING = "booting"
    IN_USE = "in_use"
    DRAINING = "draining"
    UNHEALTHY = "unhealthy"


@dataclass
class NodeRecord:
    """One allocatable diagnostics node."""

    node_id: str
    zone: str
    metro: str
    api_base_url: str
    tunnel_host: str
    state: NodeState = NodeState.IDLE
    healthy: bool = True
    current_session_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "zone": self.zone,
            "metro": self.metro,
            "api_base_url": self.api_base_url,
            "tunnel_host": self.tunnel_host,
            "state": self.state.value,
            "healthy": self.healthy,
            "current_session_id": self.current_session_id,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "NodeRecord":
        return cls(
            node_id=str(payload.get("node_id") or "").strip(),
            zone=str(payload.get("zone") or "").strip(),
            metro=str(payload.get("metro") or "").strip(),
            api_base_url=str(payload.get("api_base_url") or "").strip(),
            tunnel_host=str(payload.get("tunnel_host") or "").strip(),
            state=NodeState(str(payload.get("state") or NodeState.IDLE.value)),
            healthy=bool(payload.get("healthy", True)),
            current_session_id=(
                str(payload.get("current_session_id") or "").strip() or None
            ),
        )


@dataclass(frozen=True)
class NodeLease:
    """Result of one successful node allocation."""

    assignment_id: str
    node: NodeRecord
    session_id: str
    selection_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "assignment_id": self.assignment_id,
            "node_id": self.node.node_id,
            "session_id": self.session_id,
            "selection_reason": self.selection_reason,
        }


class NoCapacityError(RuntimeError):
    """Raised when no healthy idle node is currently available."""


class InMemoryNodeInventory:
    """Simple in-memory inventory used by tests and early local bootstraps."""

    def __init__(self, *, nodes: Iterable[NodeRecord] | None = None) -> None:
        self._nodes = {node.node_id: node for node in (nodes or [])}

    def get(self, node_id: str) -> NodeRecord:
        return self._nodes[node_id]

    def healthy_idle_nodes(self) -> list[NodeRecord]:
        return [
            node
            for node in self._nodes.values()
            if node.state == NodeState.IDLE and node.healthy
        ]

    def pending_capacity_nodes(self) -> list[NodeRecord]:
        return [
            node
            for node in self._nodes.values()
            if node.state in {NodeState.ALLOCATING, NodeState.BOOTING}
        ]

    def claim(self, node_id: str, *, session_id: str) -> NodeRecord:
        node = self.get(node_id)
        if node.state != NodeState.IDLE or not node.healthy:
            raise NoCapacityError(f"Node '{node_id}' is not available for allocation")
        node.state = NodeState.IN_USE
        node.current_session_id = session_id
        return node

    def release(self, node_id: str) -> NodeRecord:
        node = self.get(node_id)
        node.state = NodeState.IDLE
        node.current_session_id = None
        return node

    def bind_session(self, node_id: str, *, session_id: str) -> NodeRecord:
        node = self.get(node_id)
        node.current_session_id = session_id
        return node

    def register(self, node: NodeRecord) -> NodeRecord:
        self._nodes[node.node_id] = node
        return node

    def mark_ready(self, node_id: str) -> NodeRecord:
        node = self.get(node_id)
        node.state = NodeState.IDLE
        node.healthy = True
        node.current_session_id = None
        return node


class JsonFileNodeInventory(InMemoryNodeInventory):
    """File-backed node inventory for allocator state that must survive restarts."""

    def __init__(
        self,
        path: str | Path,
        *,
        nodes: Iterable[NodeRecord] | None = None,
    ) -> None:
        self._path = Path(path)
        persisted_nodes = list(nodes or [])
        if self._path.exists():
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            persisted_nodes = [
                NodeRecord.from_dict(item)
                for item in payload.get("nodes", [])
            ]
        super().__init__(nodes=persisted_nodes)
        self._persist()

    def claim(self, node_id: str, *, session_id: str) -> NodeRecord:
        node = super().claim(node_id, session_id=session_id)
        self._persist()
        return node

    def release(self, node_id: str) -> NodeRecord:
        node = super().release(node_id)
        self._persist()
        return node

    def bind_session(self, node_id: str, *, session_id: str) -> NodeRecord:
        node = super().bind_session(node_id, session_id=session_id)
        self._persist()
        return node

    def register(self, node: NodeRecord) -> NodeRecord:
        registered = super().register(node)
        self._persist()
        return registered

    def mark_ready(self, node_id: str) -> NodeRecord:
        node = super().mark_ready(node_id)
        self._persist()
        return node

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "nodes": [node.to_dict() for node in self._nodes.values()],
        }
        self._path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )


class InMemoryLeaseStore:
    """In-memory lease store used by tests and single-process bootstraps."""

    def __init__(self) -> None:
        self._leases: dict[str, dict[str, Any]] = {}

    def put(self, lease: NodeLease) -> None:
        self._leases[lease.assignment_id] = lease.to_dict()

    def get(self, assignment_id: str) -> dict[str, Any] | None:
        payload = self._leases.get(assignment_id)
        return dict(payload) if payload is not None else None

    def delete(self, assignment_id: str) -> None:
        self._leases.pop(assignment_id, None)


class JsonFileLeaseStore(InMemoryLeaseStore):
    """File-backed lease store for assignment state that must survive restarts."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        super().__init__()
        if self._path.exists():
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            self._leases = {
                str(item.get("assignment_id") or "").strip(): dict(item)
                for item in payload.get("leases", [])
                if str(item.get("assignment_id") or "").strip()
            }
        self._persist()

    def put(self, lease: NodeLease) -> None:
        super().put(lease)
        self._persist()

    def delete(self, assignment_id: str) -> None:
        super().delete(assignment_id)
        self._persist()

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "leases": list(self._leases.values()),
        }
        self._path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )


@dataclass(frozen=True)
class LocalZoneLaunchSpec:
    """Provisioning inputs for one Local Zone worker node."""

    zone: str
    metro: str
    api_base_url: str
    tunnel_host: str
    launch_template_name: str
    instance_type: str
    subnet_id: str
    security_group_ids: tuple[str, ...] = ()
    tags: dict[str, str] | None = None


class AwsEc2LaunchTemplateProvisioner:
    """Launch Local Zone worker nodes through an injected EC2 client."""

    def __init__(self, ec2_client: Any) -> None:
        self._ec2_client = ec2_client

    def build_run_instances_kwargs(
        self,
        spec: LocalZoneLaunchSpec,
        *,
        dry_run: bool = False,
        client_token: str = "",
    ) -> dict[str, Any]:
        """Build the concrete EC2 RunInstances request for one Local Zone worker."""
        tags = [
            {"Key": key, "Value": value}
            for key, value in sorted((spec.tags or {}).items())
        ]
        tags.extend(
            [
                {"Key": "ManagedBy", "Value": "diagnostic-platform"},
                {"Key": "Metro", "Value": spec.metro},
            ]
        )
        kwargs: dict[str, Any] = {
            "MinCount": 1,
            "MaxCount": 1,
            "InstanceType": spec.instance_type,
            "SubnetId": spec.subnet_id,
            "SecurityGroupIds": list(spec.security_group_ids),
            "Placement": {"AvailabilityZone": spec.zone},
            "LaunchTemplate": {"LaunchTemplateName": spec.launch_template_name},
            "TagSpecifications": [
                {
                    "ResourceType": "instance",
                    "Tags": tags,
                }
            ],
        }
        if dry_run:
            kwargs["DryRun"] = True
        if client_token:
            kwargs["ClientToken"] = client_token
        return kwargs

    def launch_node(
        self,
        spec: LocalZoneLaunchSpec,
        *,
        dry_run: bool = False,
        client_token: str = "",
    ) -> NodeRecord:
        response = self._ec2_client.run_instances(
            **self.build_run_instances_kwargs(
                spec,
                dry_run=dry_run,
                client_token=client_token,
            )
        )
        instance = (response.get("Instances") or [{}])[0]
        return NodeRecord(
            node_id=str(instance.get("InstanceId") or "").strip(),
            zone=spec.zone,
            metro=spec.metro,
            api_base_url=spec.api_base_url,
            tunnel_host=spec.tunnel_host,
            state=NodeState.BOOTING,
            healthy=False,
        )

    def launch_into_inventory(
        self,
        spec: LocalZoneLaunchSpec,
        inventory: InMemoryNodeInventory,
    ) -> NodeRecord:
        node = self.launch_node(spec)
        return inventory.register(node)


def _normalize_zone_catalog_entries(
    *,
    zone_catalog: list[dict[str, Any]],
    default_api_base: str,
    default_tunnel_host: str,
    launch_template_name: str,
    instance_type: str,
    subnet_id: str,
    security_group_ids: tuple[str, ...],
) -> list[dict[str, Any]]:
    normalized_entries: list[dict[str, Any]] = []
    for raw_entry in zone_catalog:
        zone = str(raw_entry.get("zone") or "").strip()
        metro = str(raw_entry.get("metro") or "").strip()
        entry_subnet_id = str(raw_entry.get("subnet_id") or "").strip() or subnet_id
        entry_api_base = str(raw_entry.get("api_base_url") or "").strip() or default_api_base
        entry_tunnel_host = str(raw_entry.get("tunnel_host") or "").strip() or default_tunnel_host
        entry_launch_template = (
            str(raw_entry.get("launch_template_name") or "").strip()
            or launch_template_name
        )
        entry_instance_type = str(raw_entry.get("instance_type") or "").strip() or instance_type
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
    return normalized_entries


def build_zone_catalog_launch_spec_resolver(
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
):
    """Build one launch-spec resolver backed by the per-zone catalog."""
    normalized_entries = _normalize_zone_catalog_entries(
        zone_catalog=zone_catalog,
        default_api_base=default_api_base,
        default_tunnel_host=default_tunnel_host,
        launch_template_name=launch_template_name,
        instance_type=instance_type,
        subnet_id=subnet_id,
        security_group_ids=security_group_ids,
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
        brand = str(getattr(context, "brand", None) or "").strip()
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


def build_zone_catalog_route_resolver(
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
):
    """Build one route resolver that maps location hints to preferred metro/zone."""
    launch_spec_resolver = build_zone_catalog_launch_spec_resolver(
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

    def _resolve_default_route() -> dict[str, str]:
        selected = _match_entry(default_zone, entry_key="zone_keys")
        if selected is not None:
            return {
                "preferred_zone": str(selected["zone"]),
                "preferred_metro": str(selected["metro"]),
                "source": "default_zone",
            }

        selected = _match_entry(default_metro, entry_key="metro_keys")
        if selected is not None:
            return {
                "preferred_zone": "",
                "preferred_metro": str(selected["metro"]),
                "source": "default_metro",
            }

        selected = normalized_entries[0]
        return {
            "preferred_zone": str(selected["zone"]),
            "preferred_metro": str(selected["metro"]),
            "source": "catalog_first",
        }

    def _resolve(
        *,
        data: dict[str, Any],
        include_fallbacks: bool = True,
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
            ("client_city", "city_keys", False),
            ("client_time_zone", "time_zone_keys", True),
            ("organization_city", "city_keys", False),
            ("organization_time_zone", "time_zone_keys", True),
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

        if not include_fallbacks:
            return {}
        return _resolve_default_route()

    return _resolve


def build_http_node_ready_probe(
    *,
    path: str = "/api/session/bootstrap/ready",
    timeout_sec: float = 3.0,
    api_token: str = "",
    urlopen: Any | None = None,
):
    """Build one HTTP probe that checks whether a booting node is API-reachable."""
    opener = urlopen or urllib_request.urlopen
    normalized_path = "/" + str(path or "").lstrip("/")

    def _probe(node: NodeRecord) -> bool:
        request = urllib_request.Request(
            f"{node.api_base_url.rstrip('/')}{normalized_path}",
            headers={
                key: value
                for key, value in {
                    "X-API-Token": api_token.strip(),
                }.items()
                if value
            },
            method="GET",
        )
        with opener(request, timeout=timeout_sec) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            status = int(status)
            if status >= 400:
                return False
            payload = response.read()
        if not payload:
            return True
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            return True
        if not isinstance(decoded, dict):
            return True
        if "ready" in decoded:
            return bool(decoded.get("ready"))
        if "success" in decoded:
            return bool(decoded.get("success"))
        return True

    return _probe


class BootingNodeReadinessMonitor:
    """Poll booting nodes and promote them to healthy idle capacity once ready."""

    def __init__(
        self,
        inventory: InMemoryNodeInventory,
        *,
        probe: Any,
        poll_interval_sec: float = 15.0,
    ) -> None:
        self._inventory = inventory
        self._probe = probe
        self._poll_interval_sec = max(0.1, float(poll_interval_sec))
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def poll_interval_sec(self) -> float:
        return self._poll_interval_sec

    def poll_once(self) -> list[NodeRecord]:
        promoted: list[NodeRecord] = []
        for node in list(self._inventory.pending_capacity_nodes()):
            if node.state != NodeState.BOOTING:
                continue
            try:
                is_ready = bool(self._probe(node))
            except Exception:
                logger.warning(
                    "Node readiness probe failed for node_id=%s zone=%s",
                    node.node_id,
                    node.zone,
                    exc_info=True,
                )
                continue
            if is_ready:
                promoted.append(self._inventory.mark_ready(node.node_id))
        return promoted

    def start(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event = threading.Event()
            self._thread = threading.Thread(
                target=self._run,
                name="diagnostic-node-readiness",
                daemon=True,
            )
            self._thread.start()
            return True

    def stop(self, *, timeout_sec: float = 2.0) -> bool:
        with self._lock:
            thread = self._thread
            if thread is None:
                return False
            self._stop_event.set()
        thread.join(timeout=timeout_sec)
        with self._lock:
            if self._thread is thread:
                self._thread = None
        return True

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self.poll_once()
            self._stop_event.wait(self._poll_interval_sec)


class HotPoolAllocator:
    """Allocate one healthy idle node from a pre-started Local Zone hot pool."""

    def __init__(
        self,
        inventory: InMemoryNodeInventory,
        *,
        lease_store: InMemoryLeaseStore | None = None,
    ) -> None:
        self._inventory = inventory
        self._lease_store = lease_store or InMemoryLeaseStore()

    @property
    def inventory(self) -> InMemoryNodeInventory:
        return self._inventory

    def pending_capacity_node(
        self,
        *,
        preferred_zone: str = "",
        preferred_metro: str = "",
    ) -> NodeRecord | None:
        candidates = self._inventory.pending_capacity_nodes()
        if not candidates:
            return None

        def _priority(node: NodeRecord) -> tuple[int, str]:
            if preferred_zone and node.zone == preferred_zone:
                return (0, node.node_id)
            if preferred_metro and node.metro == preferred_metro:
                return (1, node.node_id)
            return (2, node.node_id)

        return sorted(candidates, key=_priority)[0]

    def allocate(
        self,
        *,
        preferred_zone: str = "",
        preferred_metro: str = "",
        session_id: str,
    ) -> NodeLease:
        candidates = self._inventory.healthy_idle_nodes()
        if not candidates:
            raise NoCapacityError("No healthy idle nodes are available")

        def _priority(node: NodeRecord) -> tuple[int, str]:
            if preferred_zone and node.zone == preferred_zone:
                return (0, node.node_id)
            if preferred_metro and node.metro == preferred_metro:
                return (1, node.node_id)
            return (2, node.node_id)

        selected = sorted(candidates, key=_priority)[0]
        claimed = self._inventory.claim(selected.node_id, session_id=session_id)
        priority = _priority(claimed)[0]
        selection_reason = {
            0: "preferred_zone",
            1: "preferred_metro",
        }.get(priority, "any_healthy_idle")
        lease = NodeLease(
            assignment_id=uuid.uuid4().hex[:16],
            node=claimed,
            session_id=session_id,
            selection_reason=selection_reason,
        )
        self._lease_store.put(lease)
        return lease

    def bind(self, assignment_id: str, *, session_id: str) -> NodeLease:
        lease_payload = self._lease_store.get(assignment_id)
        if lease_payload is None:
            raise KeyError(assignment_id)
        lease = NodeLease(
            assignment_id=str(lease_payload["assignment_id"]),
            node=self._inventory.get(str(lease_payload["node_id"])),
            session_id=str(lease_payload["session_id"]),
            selection_reason=str(lease_payload["selection_reason"]),
        )
        bound_node = self._inventory.bind_session(lease.node.node_id, session_id=session_id)
        bound = NodeLease(
            assignment_id=lease.assignment_id,
            node=bound_node,
            session_id=session_id,
            selection_reason=lease.selection_reason,
        )
        self._lease_store.put(bound)
        return bound

    def release(self, lease: NodeLease | str) -> NodeRecord:
        assignment_id = lease.assignment_id if isinstance(lease, NodeLease) else lease
        stored_lease_payload = self._lease_store.get(assignment_id)
        if stored_lease_payload is None:
            raise KeyError(assignment_id)
        self._lease_store.delete(assignment_id)
        return self._inventory.release(str(stored_lease_payload["node_id"]))
