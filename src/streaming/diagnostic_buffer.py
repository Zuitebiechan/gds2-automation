"""
Diagnostic Buffer for AI-powered diagnosis.

Collects timestamped parameter snapshots into a 30-second sliding window.
Exports delta-compressed payloads optimized for LLM consumption:
- Full initial state (all parameters at t=0)
- Timestamped changes only (parameter, old_value, new_value, time)

Dual-rate sampling:
- Fast parameters (RPM, MAF, TPS, O2, STFT): every 100ms (10Hz)
- Slow parameters (ECT, IAT, LTFT, ambient): every 1000ms (1Hz)
"""

import threading
import time
import logging
from datetime import datetime
from typing import Any, Optional

from .agent_data_collector import AgentSnapshot

logger = logging.getLogger(__name__)

# Parameters that change rapidly and need 10Hz sampling
FAST_PARAMETERS: set[str] = {
    'Engine Speed', 'RPM',
    'Throttle Position', 'TPS', 'Accelerator Pedal Position',
    'Mass Air Flow', 'MAF', 'Mass Air Flow Sensor',
    'Manifold Absolute Pressure', 'MAP',
    'O2 Sensor', 'O2S', 'HO2S',
    'Short Term Fuel Trim', 'STFT',
    'Intake Air Flow',
    'Vehicle Speed',
    'Ignition Timing', 'Spark Advance',
    'Injector Pulse Width',
    'Fuel Rail Pressure',
    'Boost Pressure', 'Turbo Boost',
}


def _is_fast_parameter(name: str) -> bool:
    """Check if a parameter name matches a fast-sampling parameter."""
    name_upper = name.upper()
    for fast in FAST_PARAMETERS:
        if fast.upper() in name_upper:
            return True
    return False


class DiagnosticBuffer:
    """
    Sliding window buffer that accumulates timestamped parameter data.

    Feeds from AgentDataCollector snapshots and produces delta-compressed
    payloads suitable for LLM analysis.
    """

    def __init__(self, window_seconds: int = 30):
        self._window_seconds = window_seconds
        self._lock = threading.Lock()

        # Timestamped snapshots: [{ts, parameters, dtcs}]
        self._snapshots: list[dict[str, Any]] = []

        # For dual-rate sampling: track last slow-param write time per param
        self._last_slow_write: dict[str, float] = {}

        # For delta compression: track per-parameter value history
        # key = "param_name|unit", value = [(relative_time, value)]
        self._param_history: dict[str, list[tuple[float, str]]] = {}

        # Start time (set on first snapshot)
        self._start_time: Optional[float] = None

        # Latest DTCs (overwritten each snapshot)
        self._latest_dtcs: list[dict[str, Any]] = []

        self._snapshot_count = 0

    @property
    def snapshot_count(self) -> int:
        return self._snapshot_count

    @property
    def duration_seconds(self) -> float:
        """How many seconds of data are in the buffer."""
        with self._lock:
            if not self._snapshots:
                return 0.0
            return self._snapshots[-1]['ts'] - self._snapshots[0]['ts']

    def is_ready(self, min_seconds: float = 25.0) -> bool:
        """Check if we have enough data for analysis."""
        return self.duration_seconds >= min_seconds

    def append_snapshot(self, snapshot: AgentSnapshot) -> None:
        """
        Ingest an AgentSnapshot from AgentDataCollector.

        Applies dual-rate sampling and tracks per-parameter value changes.
        """
        now = time.time()

        with self._lock:
            if self._start_time is None:
                self._start_time = now

            relative_time = round(now - self._start_time, 3)

            # Store DTCs (always latest)
            self._latest_dtcs = [d.to_dict() for d in snapshot.dtcs]

            # Process parameters with dual-rate sampling
            sampled_params: list[dict[str, str]] = []

            for param in snapshot.parameters:
                name = param.get('name', '')
                if not name:
                    continue

                key = f"{name}|{param.get('unit', '')}"
                is_fast = _is_fast_parameter(name)

                if not is_fast:
                    # Slow parameter: only sample at 1Hz
                    last_write = self._last_slow_write.get(key, 0.0)
                    if now - last_write < 0.9:  # ~1Hz with tolerance
                        continue
                    self._last_slow_write[key] = now

                sampled_params.append(param)

                # Track value history for delta compression
                value = param.get('value', '')
                history = self._param_history.setdefault(key, [])

                if not history or history[-1][1] != value:
                    # Value changed — record it
                    history.append((relative_time, value))
                elif not history:
                    # First entry
                    history.append((relative_time, value))

            # Store snapshot
            self._snapshots.append({
                'ts': now,
                'relative_time': relative_time,
                'parameters': sampled_params,
            })

            # Trim buffer to window
            cutoff = now - self._window_seconds
            self._snapshots = [s for s in self._snapshots if s['ts'] >= cutoff]

            # Trim param history to window
            min_relative = relative_time - self._window_seconds
            for key in list(self._param_history.keys()):
                entries = self._param_history[key]
                # Keep entries within window, but always keep at least one
                trimmed = [e for e in entries if e[0] >= min_relative]
                if not trimmed and entries:
                    trimmed = [entries[-1]]
                self._param_history[key] = trimmed

            self._snapshot_count += 1

    def get_delta_payload(self) -> dict[str, Any]:
        """
        Export buffer as delta-compressed payload for LLM.

        Returns:
            {
                "window_seconds": 30,
                "actual_duration": 29.8,
                "snapshot_count": 285,
                "initial_state": [
                    {"name": "Engine Speed", "value": "840", "unit": "RPM"}
                ],
                "timeline": [
                    {"t": "0.2s", "param": "Engine Speed", "from": "840", "to": "850", "unit": "RPM"},
                    ...
                ],
                "dtcs": [...],
                "significant_changes": [...]
            }
        """
        with self._lock:
            if not self._snapshots:
                return {
                    'window_seconds': self._window_seconds,
                    'actual_duration': 0,
                    'snapshot_count': 0,
                    'initial_state': [],
                    'timeline': [],
                    'dtcs': [],
                    'significant_changes': [],
                }

            duration = self._snapshots[-1]['ts'] - self._snapshots[0]['ts']

            # Build initial state from first entries
            initial_state: list[dict[str, str]] = []
            timeline: list[dict[str, str]] = []

            for key, history in self._param_history.items():
                if not history:
                    continue

                parts = key.split('|', 1)
                name = parts[0]
                unit = parts[1] if len(parts) > 1 else ''

                # Initial state = first recorded value
                initial_state.append({
                    'name': name,
                    'value': history[0][1],
                    'unit': unit,
                })

                # Timeline = all subsequent changes
                for i in range(1, len(history)):
                    prev_value = history[i - 1][1]
                    curr_time, curr_value = history[i]
                    timeline.append({
                        't': f"{curr_time:.1f}s",
                        'param': name,
                        'from': prev_value,
                        'to': curr_value,
                        'unit': unit,
                    })

            # Sort timeline by time
            timeline.sort(key=lambda x: float(x['t'].rstrip('s')))

            # Detect significant changes (large jumps)
            significant = self._detect_significant_changes()

            return {
                'window_seconds': self._window_seconds,
                'actual_duration': round(duration, 1),
                'snapshot_count': self._snapshot_count,
                'initial_state': initial_state,
                'timeline': timeline,
                'dtcs': list(self._latest_dtcs),
                'significant_changes': significant,
            }

    def get_raw_payload(self) -> dict[str, Any]:
        """
        Export buffer as raw cached payload (for retry without re-collecting).
        Includes everything needed to reconstruct the LLM prompt.
        """
        with self._lock:
            return {
                'delta_payload': self.get_delta_payload(),
                'cached_at': datetime.now().isoformat(),
                'snapshot_count': self._snapshot_count,
            }

    def clear(self) -> None:
        """Reset the buffer."""
        with self._lock:
            self._snapshots.clear()
            self._param_history.clear()
            self._last_slow_write.clear()
            self._latest_dtcs.clear()
            self._start_time = None
            self._snapshot_count = 0

    def _detect_significant_changes(self) -> list[dict[str, Any]]:
        """
        Identify large/sudden parameter changes within the buffer.

        Returns pre-flagged changes that the LLM should pay attention to.
        """
        significant: list[dict[str, Any]] = []

        for key, history in self._param_history.items():
            if len(history) < 2:
                continue

            parts = key.split('|', 1)
            name = parts[0]
            unit = parts[1] if len(parts) > 1 else ''

            # Check for large value jumps
            for i in range(1, len(history)):
                prev_time, prev_val = history[i - 1]
                curr_time, curr_val = history[i]

                try:
                    prev_num = float(prev_val)
                    curr_num = float(curr_val)
                except (ValueError, TypeError):
                    continue

                if prev_num == 0:
                    continue

                change_pct = abs(curr_num - prev_num) / abs(prev_num) * 100
                time_delta = curr_time - prev_time

                # Flag if >30% change in <2 seconds
                if change_pct > 30 and time_delta < 2.0:
                    significant.append({
                        'parameter': name,
                        'from': prev_val,
                        'to': curr_val,
                        'unit': unit,
                        'change_percent': round(change_pct, 1),
                        'at': f"{curr_time:.1f}s",
                        'duration': f"{time_delta:.1f}s",
                    })

        # Sort by change magnitude
        significant.sort(key=lambda x: x.get('change_percent', 0), reverse=True)
        return significant[:10]  # Top 10 most significant
