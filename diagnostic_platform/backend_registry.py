"""Platform backend registry bootstrap and singleton access."""

from __future__ import annotations

from diagnostic_platform.contracts import BackendDescriptor, BackendRegistry

_BACKEND_REGISTRY: BackendRegistry | None = None


def create_backend_registry() -> BackendRegistry:
    from backends.gds2 import GDS2DiagnosticBackend

    registry = BackendRegistry()
    registry.register(GDS2DiagnosticBackend())
    return registry


def get_backend_registry() -> BackendRegistry:
    global _BACKEND_REGISTRY
    if _BACKEND_REGISTRY is None:
        _BACKEND_REGISTRY = create_backend_registry()
    return _BACKEND_REGISTRY


def set_backend_registry(registry: BackendRegistry | None) -> BackendRegistry:
    global _BACKEND_REGISTRY
    _BACKEND_REGISTRY = create_backend_registry() if registry is None else registry
    return _BACKEND_REGISTRY


def resolve_backend_descriptors_for_brand(brand: str) -> list[BackendDescriptor]:
    registry = get_backend_registry()
    return [backend.descriptor for backend in registry.find_by_brand(brand)]
