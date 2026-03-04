from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class GDS2Action(str, Enum):
    START_DIAGNOSTICS = "start_diagnostics"
    SELECT_DEVICE = "select_device"
    READ_DTCS = "read_dtcs"
    ABORT_SESSION = "abort_session"
    SELECT_MODULE = "select_module"
    GO_HOME = "go_home"
    START_LIVE_STREAM = "start_live_stream"
    CONNECT_DEVICE = "connect_device"
    SELECT_DATA_CATEGORY = "select_data_category"
    SELECT_SUB_CATEGORY = "select_sub_category"
    GO_BACK = "go_back"
    STOP_LIVE_STREAM = "stop_live_stream"


class RiskLevel(str, Enum):
    NORMAL = "normal"
    HIGH = "high"


@dataclass
class RetryPolicy:
    max_attempts: int = 1
    backoff_sec: float = 1.0

    def __post_init__(self):
        if self.max_attempts <= 0:
            raise ValueError("max_attempts must be > 0")
        if self.backoff_sec < 0:
            raise ValueError("backoff_sec must be >= 0")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_attempts": self.max_attempts,
            "backoff_sec": self.backoff_sec,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RetryPolicy":
        return cls(**data)


@dataclass
class ActionStep:
    action: GDS2Action
    args: Dict[str, Any] = field(default_factory=dict)
    preconditions: List[str] = field(default_factory=list)
    postconditions: List[str] = field(default_factory=list)
    timeout_sec: float = 30.0
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    risk_level: RiskLevel = RiskLevel.NORMAL
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.timeout_sec <= 0:
            raise ValueError("timeout_sec must be > 0")
        if isinstance(self.action, str) and not isinstance(self.action, GDS2Action):
            self.action = GDS2Action(self.action)
        if isinstance(self.retry_policy, dict):
            self.retry_policy = RetryPolicy.from_dict(self.retry_policy)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action.value,
            "args": self.args,
            "preconditions": self.preconditions,
            "postconditions": self.postconditions,
            "timeout_sec": self.timeout_sec,
            "retry_policy": self.retry_policy.to_dict(),
            "risk_level": self.risk_level.value,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ActionStep":
        data = data.copy()
        data["action"] = GDS2Action(data["action"])
        if "risk_level" in data:
            data["risk_level"] = RiskLevel(data["risk_level"])
        if "retry_policy" in data:
            data["retry_policy"] = RetryPolicy.from_dict(data["retry_policy"])
        return cls(**data)
