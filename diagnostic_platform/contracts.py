"""
Standardized diagnostic backend contracts and data models.

This module defines the abstract interface that all OEM diagnostic software backends
must implement, along with standardized data classes for vehicle context, diagnostics,
live data, and backend state management.

All backends must inherit from DiagnosticBackend and implement all abstract methods
to ensure consistent behavior across different OEM software (GDS2, Honda, etc.).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# ============================================================================
# Data Models (Dataclasses)
# ============================================================================


@dataclass
class VehicleContext:
    """
    Represents the vehicle being diagnosed.
    
    Attributes:
        brand: Vehicle manufacturer (e.g., "Changan", "BMW", "Honda")
        model: Vehicle model name (e.g., "Eado", "X5", "Accord")
        year: Manufacturing year (optional)
        vin: Vehicle Identification Number (optional)
        problem_description: User-reported issue description (optional)
    """
    brand: str
    model: str
    year: Optional[int] = None
    vin: Optional[str] = None
    problem_description: Optional[str] = None


@dataclass
class DTC:
    """
    Represents a Diagnostic Trouble Code (DTC).
    
    Attributes:
        code: DTC code (e.g., "P0100", "U0122")
        module: Control module where DTC is stored (e.g., "Engine", "Transmission")
        status: DTC status (e.g., "Active", "Pending", "Historical")
        description: Human-readable description of the fault
        source_backend: Name of the backend that read this DTC (e.g., "gds2")
    """
    code: str
    module: str
    status: str
    description: str
    source_backend: str


@dataclass
class LiveDataPoint:
    """
    Represents a single live data measurement.
    
    Attributes:
        parameter: Parameter name (e.g., "Engine Speed", "Coolant Temp")
        value: Measured value as float
        unit: Unit of measurement (e.g., "rpm", "°C")
        timestamp: Unix timestamp (seconds since epoch) when measurement was taken
    """
    parameter: str
    value: float
    unit: str
    timestamp: float


class SamplingQuality(Enum):
    """
    Enum representing the quality of data samples collected.
    
    Quality levels indicate how complete and stable the data collection was:
    - EXCELLENT: Full dataset, no interruptions
    - GOOD: Minor gaps or variations, generally usable
    - FAIR: Significant gaps or noise, proceed with caution
    - POOR: Incomplete or unreliable data
    """
    EXCELLENT = "excellent"
    GOOD = "good"
    FAIR = "fair"
    POOR = "poor"


@dataclass
class DiagnosticPayload:
    """
    Complete diagnostic payload for AI analysis.
    
    Attributes:
        vehicle_context: Vehicle information being diagnosed
        dtcs: List of all DTCs read from the vehicle
        live_data: List of live data measurements collected
        sampling_quality: Quality assessment of the collected data
        source_backend: Name of the backend that collected this data
    """
    vehicle_context: VehicleContext
    dtcs: list[DTC]
    live_data: list[LiveDataPoint]
    sampling_quality: SamplingQuality
    source_backend: str


@dataclass
class BackendState:
    """
    Represents the current state of a diagnostic backend.
    
    Attributes:
        current_page: Current page/screen in the backend UI (e.g., "DataDisplay", "ModuleList")
        is_connected: Whether the backend is connected to a VCI device
        current_module: Currently selected module (optional)
        current_data_category: Currently selected data category (optional)
        extra: Additional backend-specific state information
    """
    current_page: str
    is_connected: bool
    current_module: Optional[str] = None
    current_data_category: Optional[str] = None
    extra: dict = field(default_factory=dict)


@dataclass
class ClearResult:
    """
    Result of a DTC clear operation.

    Attributes:
        success: Whether the clear operation succeeded
        cleared_count: Number of DTCs that were cleared
        message: Human-readable result message
    """
    success: bool
    cleared_count: int
    message: str


@dataclass
class ActionResult:
    """Result of executing a diagnostic action."""
    success: bool
    action: str
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class LiveDataStream:
    """
    Represents an active live data streaming session.
    
    Attributes:
        session_id: Unique identifier for this streaming session
        active: Whether the stream is currently active
    """
    session_id: str
    active: bool


# ============================================================================
# Abstract Base Class
# ============================================================================


class DiagnosticBackend(ABC):
    """
    Abstract base class for all OEM diagnostic software backends.
    
    All backends must implement this interface to provide consistent access to
    vehicle diagnostics regardless of the underlying OEM software (GDS2, Honda, etc.).
    
    The typical workflow is:
    1. start() - Initialize the backend
    2. connect_vci(device) - Connect to a VCI device
    3. get_modules() - Retrieve available modules
    4. select_module(module) - Select a module to diagnose
    5. get_data_categories() - Retrieve available data categories for the module
    6. select_data_category(category) - Select which data to view
    7. read_dtcs() - Read DTCs from the module
    8. start_live_data() - Begin collecting live data measurements
    9. stop_live_data() - Stop the live data stream
    10. clear_dtcs() - Clear DTCs from the module (if desired)
    11. stop() - Shutdown the backend
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """
        Get the backend name.
        
        Returns:
            Unique name identifier for this backend (e.g., "gds2", "honda", "toyota")
        """
        pass

    @property
    @abstractmethod
    def supported_brands(self) -> list[str]:
        """
        Get list of vehicle brands supported by this backend.
        
        Returns:
            List of supported manufacturer names (e.g., ["Changan", "BMW", "Mercedes"])
        """
        pass

    @abstractmethod
    def start(self) -> None:
        """
        Start and initialize the backend.
        
        This should perform all necessary setup:
        - Launch OEM software if needed
        - Initialize drivers and libraries
        - Set up event listeners and state management
        
        Raises:
            RuntimeError: If backend fails to start
        """
        pass

    @abstractmethod
    def stop(self) -> None:
        """
        Stop and shutdown the backend.
        
        This should perform cleanup:
        - Close OEM software gracefully
        - Release hardware resources
        - Clean up temporary state
        """
        pass

    @abstractmethod
    def connect_vci(self, device: str) -> None:
        """
        Connect to a VCI (Vehicle Communication Interface) device.
        
        Args:
            device: VCI device identifier (IP address, COM port, USB path, etc.)
            
        Raises:
            ConnectionError: If connection to VCI fails
        """
        pass

    @abstractmethod
    def get_modules(self) -> list[str]:
        """
        Retrieve list of available diagnostic modules.
        
        Returns:
            List of module names (e.g., ["Engine", "Transmission", "ABS", "Body Control"])
            
        Raises:
            RuntimeError: If unable to retrieve modules
        """
        pass

    @abstractmethod
    def select_module(self, module: str) -> None:
        """
        Select a module for diagnosis.
        
        After selection, the backend navigates to the module's diagnostic interface.
        
        Args:
            module: Name of the module to select (from get_modules())
            
        Raises:
            ValueError: If module name is invalid
            RuntimeError: If selection fails
        """
        pass

    @abstractmethod
    def get_data_categories(self) -> list[str]:
        """
        Get available data categories for the currently selected module.

        Data categories are different types of information available in the module:
        - "DTCs" for fault codes
        - "Live Data" for real-time sensor readings
        - "Freeze Frame" for snapshot data
        - etc.

        Returns:
            List of available data category names

        Raises:
            RuntimeError: If no module is selected or retrieval fails
        """
        pass

    @abstractmethod
    def go_back(self) -> None:
        """Navigate back one step in the diagnostic software UI."""
        pass

    @abstractmethod
    def detect_current_page(self) -> str:
        """Detect and return the current page/screen identifier."""
        pass

    @abstractmethod
    def execute_action(
        self,
        action: str,
        args: dict[str, Any] | None = None,
        timeout_sec: float = 30.0,
    ) -> dict[str, Any]:
        """Execute a named action in the diagnostic software.

        Args:
            action: Action identifier (e.g. 'start_diagnostics', 'select_module')
            args: Action-specific arguments
            timeout_sec: Maximum time to wait for action completion

        Returns:
            Dict with at minimum 'success' (bool) and optional 'metadata', 'error' keys
        """
        pass

    @abstractmethod
    def select_data_category(self, category: str) -> list[str]:
        """Select a data category and return available sub-items.

        Args:
            category: Name of the data category to select

        Returns:
            List of available items after selection
        """
        pass

    @abstractmethod
    def read_dtcs(self) -> list[DTC]:
        """
        Read all DTCs from the currently selected module.
        
        Must be called after selecting a module with select_module().
        
        Returns:
            List of DTC objects. May be empty if no faults are present.
            
        Raises:
            RuntimeError: If no module is selected or read fails
        """
        pass

    @abstractmethod
    def start_live_data(self) -> LiveDataStream:
        """
        Start collecting live data from the current data category.
        
        Returns a session object that can be used to track the stream.
        Live data measurements are typically made available through a separate
        mechanism (e.g., polling, SSE stream, callback).
        
        Returns:
            LiveDataStream object with session_id and active status
            
        Raises:
            RuntimeError: If no data category is selected or start fails
        """
        pass

    @abstractmethod
    def stop_live_data(self) -> None:
        """
        Stop the current live data stream.
        
        Raises:
            RuntimeError: If no stream is active or stop fails
        """
        pass

    @abstractmethod
    def clear_dtcs(self) -> ClearResult:
        """
        Clear all DTCs from the currently selected module.
        
        Note: Clearing DTCs is a privileged operation that permanently removes
        diagnostic history. Typically requires technician credentials.
        
        Returns:
            ClearResult object indicating success/failure and count of cleared DTCs
            
        Raises:
            RuntimeError: If no module is selected or clear operation fails
            PermissionError: If user lacks credentials to clear DTCs
        """
        pass

    @abstractmethod
    def get_state(self) -> BackendState:
        """
        Get the current state of the backend.
        
        Returns:
            BackendState object describing current page, connection status, and selections
        """
        pass


# ============================================================================
# Backend Registry
# ============================================================================


class BackendRegistry:
    """
    Registry for managing and discovering available diagnostic backends.
    
    Backends must be registered before they can be used. The registry maintains
    a mapping of backend names and supported brands for discovery and lookup.
    
    Example usage:
        registry = BackendRegistry()
        registry.register(gds2_backend)
        registry.register(honda_backend)
        
        # Lookup by name
        backend = registry.get_by_name("gds2")
        
        # Lookup by brand
        backend = registry.get_by_brand("Changan")
        
        # List all registered backends
        names = registry.list_backends()
    """

    def __init__(self):
        """Initialize an empty registry."""
        self._backends: dict[str, DiagnosticBackend] = {}
        self._brand_index: dict[str, str] = {}  # Maps brand -> backend name

    def register(self, backend: DiagnosticBackend) -> None:
        """
        Register a diagnostic backend.
        
        Args:
            backend: DiagnosticBackend instance to register
            
        Raises:
            ValueError: If a backend with the same name is already registered
        """
        if backend.name in self._backends:
            raise ValueError(f"Backend '{backend.name}' is already registered")
        
        self._backends[backend.name] = backend
        
        # Index all supported brands
        for brand in backend.supported_brands:
            self._brand_index[brand] = backend.name

    def get_by_name(self, name: str) -> DiagnosticBackend:
        """
        Get a backend by its name.
        
        Args:
            name: Backend name (e.g., "gds2")
            
        Returns:
            The registered DiagnosticBackend instance
            
        Raises:
            KeyError: If backend with that name is not registered
        """
        if name not in self._backends:
            raise KeyError(f"Backend '{name}' is not registered")
        return self._backends[name]

    def get_by_brand(self, brand: str) -> DiagnosticBackend:
        """
        Get a backend that supports a specific vehicle brand.
        
        Args:
            brand: Vehicle brand name (e.g., "Changan", "Honda")
            
        Returns:
            The first registered DiagnosticBackend that supports this brand
            
        Raises:
            KeyError: If no backend supports that brand
        """
        if brand not in self._brand_index:
            raise KeyError(f"No backend found for brand '{brand}'")
        backend_name = self._brand_index[brand]
        return self._backends[backend_name]

    def list_backends(self) -> list[str]:
        """
        List all registered backend names.
        
        Returns:
            List of backend names currently registered
        """
        return list(self._backends.keys())
