from __future__ import annotations

import json
import math
import os
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from diagnostic_platform.observability import utc_now_iso

DEFAULT_WINDOW_SIZE = 5
DEFAULT_FRESHNESS_SECONDS = 10.0
DEFAULT_HYSTERESIS_WINDOWS = 2


def _utc_iso(ts: float | None = None) -> str:
    return utc_now_iso(ts)


def _parse_utc_iso(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * ratio) - 1))
    return float(ordered[index])


def get_tunnel_quality_snapshot_path(programdata: str | Path | None = None) -> Path:
    base = Path(programdata) if programdata is not None else Path(
        os.environ.get("PROGRAMDATA", "C:/ProgramData")
    )
    return base / "VCI_Proxy" / "tunnel_quality.json"


def default_tunnel_quality_snapshot(
    *,
    reason: str = "snapshot_missing",
    connection_epoch: str | None = None,
    connected: bool = False,
) -> dict[str, Any]:
    return {
        "connection_epoch": connection_epoch,
        "connected": connected,
        "fresh": False,
        "updated_at": _utc_iso(),
        "source": "probe",
        "sample_count": 0,
        "network_ms": {
            "last": None,
            "p50": None,
            "p95": None,
        },
        "grade": "block",
        "status": "blocked",
        "reason": reason,
        "probe_failures": 0,
    }


def normalize_tunnel_quality_snapshot(
    snapshot: dict[str, Any] | None,
    *,
    now: float | None = None,
    freshness_seconds: float = DEFAULT_FRESHNESS_SECONDS,
    recompute_freshness: bool = False,
) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return default_tunnel_quality_snapshot()

    normalized = default_tunnel_quality_snapshot(
        reason=str(snapshot.get("reason") or "snapshot_missing"),
        connection_epoch=snapshot.get("connection_epoch"),
        connected=bool(snapshot.get("connected", False)),
    )
    normalized.update(snapshot)

    metrics = normalized.get("network_ms")
    if not isinstance(metrics, dict):
        metrics = {}
    normalized["network_ms"] = {
        "last": metrics.get("last"),
        "p50": metrics.get("p50"),
        "p95": metrics.get("p95"),
    }

    normalized["connected"] = bool(normalized.get("connected", False))
    normalized["fresh"] = bool(normalized.get("fresh", False))
    normalized["sample_count"] = int(normalized.get("sample_count", 0) or 0)
    normalized["probe_failures"] = int(normalized.get("probe_failures", 0) or 0)
    normalized["updated_at"] = str(normalized.get("updated_at") or _utc_iso())
    normalized["grade"] = str(normalized.get("grade") or "block")
    normalized["status"] = str(normalized.get("status") or "blocked")
    normalized["reason"] = str(normalized.get("reason") or "unknown")

    if recompute_freshness:
        updated_at_s = _parse_utc_iso(normalized["updated_at"])
        now_s = time.time() if now is None else now
        fresh = (
            normalized["connected"]
            and updated_at_s is not None
            and (now_s - updated_at_s) <= freshness_seconds
        )
        normalized["fresh"] = fresh
        if normalized["connected"] and not fresh:
            normalized["grade"] = "block"
            normalized["status"] = "blocked"
            normalized["reason"] = "snapshot_stale"
    return normalized


def read_tunnel_quality_snapshot(
    path: str | Path | None = None,
    *,
    now: float | None = None,
    freshness_seconds: float = DEFAULT_FRESHNESS_SECONDS,
) -> dict[str, Any]:
    snapshot_path = Path(path) if path is not None else get_tunnel_quality_snapshot_path()
    try:
        raw = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default_tunnel_quality_snapshot(reason="snapshot_missing")
    except json.JSONDecodeError:
        return default_tunnel_quality_snapshot(reason="snapshot_invalid")
    except OSError:
        return default_tunnel_quality_snapshot(reason="snapshot_unreadable")
    return normalize_tunnel_quality_snapshot(
        raw,
        now=now,
        freshness_seconds=freshness_seconds,
        recompute_freshness=True,
    )


def write_tunnel_quality_snapshot(snapshot: dict[str, Any], path: str | Path | None = None) -> Path:
    snapshot_path = Path(path) if path is not None else get_tunnel_quality_snapshot_path()
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    normalized = normalize_tunnel_quality_snapshot(snapshot)
    temp_path = snapshot_path.with_suffix(snapshot_path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temp_path, snapshot_path)
    return snapshot_path


class TunnelQualityTracker:
    def __init__(
        self,
        *,
        window_size: int = DEFAULT_WINDOW_SIZE,
        freshness_seconds: float = DEFAULT_FRESHNESS_SECONDS,
        hysteresis_windows: int = DEFAULT_HYSTERESIS_WINDOWS,
    ) -> None:
        self.window_size = window_size
        self.freshness_seconds = freshness_seconds
        self.hysteresis_windows = hysteresis_windows
        self.connection_epoch: str | None = None
        self.connected = False
        self.probe_failures = 0
        self._samples: deque[float] = deque(maxlen=window_size)
        self._updated_at_s: float | None = None
        self._soft_grade = "block"
        self._soft_reason = "awaiting_hysteresis"
        self._candidate_grade: str | None = None
        self._candidate_count = 0

    def mark_connected(self, connection_epoch: str, *, measured_at: float | None = None) -> None:
        ts = time.time() if measured_at is None else measured_at
        if self.connection_epoch != connection_epoch:
            self._samples.clear()
            self._soft_grade = "block"
            self._soft_reason = "awaiting_hysteresis"
            self._candidate_grade = None
            self._candidate_count = 0
        self.connection_epoch = connection_epoch
        self.connected = True
        self.probe_failures = 0
        self._updated_at_s = ts

    def mark_disconnected(self, connection_epoch: str | None = None, *, measured_at: float | None = None) -> None:
        self.connection_epoch = connection_epoch or self.connection_epoch
        self.connected = False
        self.probe_failures = 0
        self._samples.clear()
        self._updated_at_s = time.time() if measured_at is None else measured_at
        self._soft_grade = "block"
        self._soft_reason = "tunnel_disconnected"
        self._candidate_grade = None
        self._candidate_count = 0

    def record_probe(self, network_ms: float, *, measured_at: float | None = None) -> None:
        ts = time.time() if measured_at is None else measured_at
        self.connected = True
        self.probe_failures = 0
        self._updated_at_s = ts
        self._samples.append(float(network_ms))
        if len(self._samples) >= self.window_size:
            self._apply_soft_grade(self._desired_grade())

    def record_probe_failure(self, *, measured_at: float | None = None, reason: str = "probe_failures") -> None:
        self.probe_failures += 1
        self._updated_at_s = time.time() if measured_at is None else measured_at
        self._soft_reason = reason

    def snapshot(self, *, now: float | None = None) -> dict[str, Any]:
        now_s = time.time() if now is None else now
        fresh = (
            self.connected
            and self._updated_at_s is not None
            and (now_s - self._updated_at_s) <= self.freshness_seconds
        )
        metrics = self._metrics()

        if not self.connected:
            reason = "tunnel_disconnected"
            grade = "block"
            status = "blocked"
        elif self._updated_at_s is None:
            reason = "no_probe_samples"
            grade = "block"
            status = "blocked"
        elif not fresh:
            reason = "snapshot_stale"
            grade = "block"
            status = "blocked"
        elif len(self._samples) < self.window_size:
            reason = "insufficient_samples"
            grade = "block"
            status = "blocked"
        elif self.probe_failures > 0:
            reason = "probe_failures"
            grade = "block"
            status = "blocked"
        else:
            grade = self._soft_grade
            status = _status_for_grade(grade)
            reason = self._soft_reason

        return {
            "connection_epoch": self.connection_epoch,
            "connected": self.connected,
            "fresh": fresh,
            "updated_at": _utc_iso(self._updated_at_s),
            "source": "probe",
            "sample_count": len(self._samples),
            "network_ms": metrics,
            "grade": grade,
            "status": status,
            "reason": reason,
            "probe_failures": self.probe_failures,
        }

    def _metrics(self) -> dict[str, float | None]:
        values = list(self._samples)
        return {
            "last": values[-1] if values else None,
            "p50": _percentile(values, 0.50),
            "p95": _percentile(values, 0.95),
        }

    def _desired_grade(self) -> str:
        p95 = self._metrics()["p95"]
        if p95 is None:
            return "block"
        if p95 <= 80.0:
            return "good"
        if p95 <= 150.0:
            return "warn"
        return "block"

    def _apply_soft_grade(self, desired_grade: str) -> None:
        if desired_grade == self._soft_grade:
            self._candidate_grade = None
            self._candidate_count = 0
            self._soft_reason = _reason_for_grade(desired_grade)
            return

        if desired_grade == self._candidate_grade:
            self._candidate_count += 1
        else:
            self._candidate_grade = desired_grade
            self._candidate_count = 1

        if self._candidate_count >= self.hysteresis_windows:
            self._soft_grade = desired_grade
            self._soft_reason = _reason_for_grade(desired_grade)
            self._candidate_grade = None
            self._candidate_count = 0
            return

        if self._soft_grade == "block":
            self._soft_reason = "awaiting_hysteresis"


def _status_for_grade(grade: str) -> str:
    return {
        "good": "healthy",
        "warn": "degraded",
        "block": "blocked",
    }.get(grade, "blocked")


def _reason_for_grade(grade: str) -> str:
    return {
        "good": "p95 within good threshold",
        "warn": "p95 above good threshold",
        "block": "p95 above warn threshold",
    }.get(grade, "unknown")


__all__ = [
    "TunnelQualityTracker",
    "default_tunnel_quality_snapshot",
    "get_tunnel_quality_snapshot_path",
    "normalize_tunnel_quality_snapshot",
    "read_tunnel_quality_snapshot",
    "write_tunnel_quality_snapshot",
]
