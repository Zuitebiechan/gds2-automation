"""Bootstrap helpers for assigning a session to a remote worker node."""

from __future__ import annotations

from typing import Any

from diagnostic_platform.node_allocation import NoCapacityError, NodeState
from diagnostic_platform.session_models import SessionContext


def _capacity_pending_payload(node: Any, *, context: SessionContext) -> dict[str, Any]:
    return {
        "success": True,
        "pending_capacity": True,
        "status": "capacity_pending",
        "retry_after_sec": 30,
        "next_action": "retry_session_bootstrap",
        "provisioning": {
            "node_id": node.node_id,
            "zone": node.zone,
            "metro": node.metro,
            "state": node.state.value,
        },
        "session_context": context.to_dict(),
    }


def bootstrap_session_node_assignment(
    *,
    allocator: Any,
    context: SessionContext,
    preferred_zone: str = "",
    preferred_metro: str = "",
    provisioner: Any | None = None,
    launch_spec_resolver: Any | None = None,
) -> dict[str, Any]:
    """Allocate one node and return the public bootstrap payload."""
    try:
        lease = allocator.allocate(
            preferred_zone=preferred_zone,
            preferred_metro=preferred_metro,
            session_id="bootstrap-pending",
        )
        return {
            "success": True,
            "assignment": {
                "assignment_id": lease.assignment_id,
                "node_id": lease.node.node_id,
                "zone": lease.node.zone,
                "metro": lease.node.metro,
                "api_base_url": lease.node.api_base_url,
                "tunnel_host": lease.node.tunnel_host,
                "selection_reason": lease.selection_reason,
            },
            "session_context": context.to_dict(),
            "next_action": "start_session_on_assigned_node",
        }
    except NoCapacityError:
        pending_node = allocator.pending_capacity_node(
            preferred_zone=preferred_zone,
            preferred_metro=preferred_metro,
        )
        if pending_node is not None:
            return _capacity_pending_payload(pending_node, context=context)
        if provisioner is None or launch_spec_resolver is None:
            raise
        launch_spec = launch_spec_resolver(
            context=context,
            preferred_zone=preferred_zone,
            preferred_metro=preferred_metro,
        )
        node = provisioner.launch_into_inventory(launch_spec, allocator.inventory)
        return _capacity_pending_payload(node, context=context)


def bind_session_node_assignment(
    *,
    allocator: Any,
    assignment_id: str,
    session_id: str,
) -> dict[str, Any]:
    """Bind one existing assignment to the concrete business session id."""

    lease = allocator.bind(assignment_id, session_id=session_id)
    return {
        "success": True,
        "assignment_id": lease.assignment_id,
        "session_id": lease.session_id,
        "node_id": lease.node.node_id,
    }


def release_session_node_assignment(
    *,
    allocator: Any,
    assignment_id: str,
    recovery_action: str = "idle",
) -> dict[str, Any]:
    """Release one existing node assignment back into the hot pool."""
    normalized_action = str(recovery_action or "idle").strip().lower()
    if normalized_action == "reprobe":
        next_state = NodeState.BOOTING
        healthy = False
    else:
        normalized_action = "idle"
        next_state = NodeState.IDLE
        healthy = True

    node = allocator.release(
        assignment_id,
        next_state=next_state,
        healthy=healthy,
    )
    return {
        "success": True,
        "assignment_id": assignment_id,
        "released": True,
        "node_id": node.node_id,
        "recovery_action": normalized_action,
        "node_state": node.state.value,
    }
