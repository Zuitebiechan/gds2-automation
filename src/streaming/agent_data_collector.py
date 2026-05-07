"""
Agent-based Data Collector for GDS2.

Reads JSON data written by the GDS2 Java Agent instead of
clicking Create Report and parsing HTML.

Benefits over HTML approach:
- 100ms collection interval (vs 3000ms)
- ~50ms latency (vs ~1500ms)
- No UI interaction needed
- DTC data included automatically
- Lower CPU usage (JSON parse vs template matching)

Usage:
    collector = AgentDataCollector(
        on_snapshot=my_callback,
        on_dtc_change=my_dtc_callback,
        interval_ms=100
    )
    collector.start()
    # ... later
    collector.stop()
"""

import json
import time
import threading
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Callable, Optional, Any, Tuple
from dataclasses import dataclass, field

from diagnostic_platform.session_observability import emit_collector_event
from diagnostic_platform.safe_utils import (
    json_dumps_safe as _json_dumps_safe,
    mapping_or_empty as _mapping_or_empty,
)

logger = logging.getLogger(__name__)

_AGENT_JSON_ENCODINGS: Tuple[str, ...] = ("gbk", "utf-8", "latin-1")
_AGENT_AVAILABILITY_MAX_AGE_SECONDS = 10.0
_AGENT_AVAILABILITY_READ_ATTEMPTS = 3
_AGENT_AVAILABILITY_RETRY_DELAY_SECONDS = 0.05
_FOCUS_PARAMETER_ALIASES: dict[str, tuple[str, ...]] = {
    "engine_speed": ("engine speed", "rpm"),
    "accelerator_pedal_position": ("accelerator pedal position",),
    "battery_voltage": (
        "battery voltage",
        "engine controls ignition relay feedback 2 signal",
        "ignition 1 signal",
    ),
}
_FOCUS_PARAMETER_KEYS: tuple[str, ...] = (
    "engine_speed",
    "accelerator_pedal_position",
    "battery_voltage",
)


def _guard_event_signature(payload: Any) -> str:
    """Build one stable guard-event signature without crashing on odd values."""
    return _json_dumps_safe(_mapping_or_empty(payload), sort_keys=True)


def _clean_text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _normalize_parameter_name(value: Any) -> str:
    return " ".join(_clean_text(value).casefold().split())


def _focus_parameter_key(parameter_name: Any) -> str | None:
    normalized_name = _normalize_parameter_name(parameter_name)
    if not normalized_name:
        return None

    for key, aliases in _FOCUS_PARAMETER_ALIASES.items():
        for alias in aliases:
            normalized_alias = _normalize_parameter_name(alias)
            if normalized_name == normalized_alias:
                return key
            if key == "accelerator_pedal_position" and normalized_name.startswith(
                f"{normalized_alias} "
            ):
                return key
    return None


def _is_numeric_text(value: Any) -> bool:
    text = _clean_text(value)
    if not text:
        return False
    try:
        float(text)
    except ValueError:
        return False
    return True


def _focus_parameter_alias_rank(key: str, parameter_name: Any) -> int:
    normalized_name = _normalize_parameter_name(parameter_name)
    aliases = _FOCUS_PARAMETER_ALIASES.get(key, ())
    for index, alias in enumerate(aliases):
        if normalized_name == _normalize_parameter_name(alias):
            return index
    return len(aliases) + 1


def _focus_parameter_identity(
    *,
    key: str,
    name: str,
    module: str,
    unit: str,
) -> str:
    return "|".join(
        (
            key,
            _normalize_parameter_name(module),
            _normalize_parameter_name(name),
            _normalize_parameter_name(unit),
        )
    )


def _build_focus_parameter_sample(
    *,
    key: str,
    parameter: dict[str, str],
    previous_values: dict[str, str],
) -> tuple[dict[str, Any], str]:
    name = _clean_text(parameter.get("name"))
    module = _clean_text(parameter.get("module"))
    value = _clean_text(parameter.get("value"))
    unit = _clean_text(parameter.get("unit"))
    identity = _focus_parameter_identity(
        key=key,
        name=name,
        module=module,
        unit=unit,
    )
    previous_value = previous_values.get(identity)

    sample: dict[str, Any] = {
        "key": key,
        "name": name,
        "value": value,
        "unit": unit,
        "module": module,
        "changed": previous_value is not None and previous_value != value,
    }
    if previous_value is not None:
        sample["previous_value"] = previous_value
    return sample, identity


def _is_same_value_battery_voltage_alias(
    parameter: dict[str, str],
    reference_points: set[tuple[str, str, str]],
) -> bool:
    if not reference_points:
        return False

    normalized_name = _normalize_parameter_name(parameter.get("name"))
    if "voltage" not in normalized_name.split():
        return False

    module = _clean_text(parameter.get("module"))
    unit = _clean_text(parameter.get("unit"))
    value = _clean_text(parameter.get("value"))
    return (module, unit, value) in reference_points


def _extract_focus_parameter_samples(
    parameters: list[dict[str, str]],
    previous_values: dict[str, str],
) -> tuple[list[dict[str, Any]], list[str], dict[str, str]]:
    """Extract value-level samples for parameters that diagnose visible lag."""
    samples: list[dict[str, Any]] = []
    current_values: dict[str, str] = {}
    seen_keys: set[str] = set()
    matched_indexes: set[int] = set()
    battery_voltage_reference_points: set[tuple[str, str, str]] = set()

    for index, parameter in enumerate(parameters):
        key = _focus_parameter_key(parameter.get("name"))
        if key is None:
            continue

        sample, identity = _build_focus_parameter_sample(
            key=key,
            parameter=parameter,
            previous_values=previous_values,
        )
        current_values[identity] = sample["value"]
        seen_keys.add(key)
        samples.append(sample)
        matched_indexes.add(index)
        if key == "battery_voltage":
            battery_voltage_reference_points.add(
                (
                    _clean_text(parameter.get("module")),
                    _clean_text(parameter.get("unit")),
                    _clean_text(parameter.get("value")),
                )
            )

    for index, parameter in enumerate(parameters):
        if index in matched_indexes:
            continue
        if not _is_same_value_battery_voltage_alias(
            parameter,
            battery_voltage_reference_points,
        ):
            continue

        sample, identity = _build_focus_parameter_sample(
            key="battery_voltage",
            parameter=parameter,
            previous_values=previous_values,
        )
        current_values[identity] = sample["value"]
        seen_keys.add("battery_voltage")
        samples.append(sample)

    order = {key: index for index, key in enumerate(_FOCUS_PARAMETER_KEYS)}
    samples.sort(key=lambda item: (order.get(str(item.get("key")), 99), str(item.get("name"))))
    missing_keys = [key for key in _FOCUS_PARAMETER_KEYS if key not in seen_keys]
    return samples, missing_keys, current_values


def _prefer_primary_focus_sample(
    current: dict[str, Any] | None,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    if current is None:
        return candidate

    current_key = str(current.get("key", ""))
    candidate_key = str(candidate.get("key", ""))
    if current_key != candidate_key:
        return current

    current_numeric = _is_numeric_text(current.get("value"))
    candidate_numeric = _is_numeric_text(candidate.get("value"))
    if candidate_numeric != current_numeric:
        return candidate if candidate_numeric else current

    current_rank = _focus_parameter_alias_rank(current_key, current.get("name"))
    candidate_rank = _focus_parameter_alias_rank(candidate_key, candidate.get("name"))
    if candidate_rank != current_rank:
        return candidate if candidate_rank < current_rank else current

    current_name = str(current.get("name", ""))
    candidate_name = str(candidate.get("name", ""))
    if candidate_name < current_name:
        return candidate
    return current


@dataclass
class DTCInfo:
    """Represents a single Diagnostic Trouble Code."""
    control_module: str
    dtc_type: str
    code: str
    symptom_byte: str
    description: str
    symptom_description: str
    status: str

    def to_dict(self) -> dict:
        return {
            'control_module': self.control_module,
            'dtc_type': self.dtc_type,
            'code': self.code,
            'symptom_byte': self.symptom_byte,
            'description': self.description,
            'symptom_description': self.symptom_description,
            'status': self.status,
        }


@dataclass
class AgentSnapshot:
    """A snapshot of all data from the Agent JSON."""
    timestamp: int
    extraction_count: int
    extraction_duration_ms: int
    page_context: Dict[str, Any]
    parameters: List[Dict[str, str]]
    dtcs: List[DTCInfo]
    table_count: int
    raw_tables: List[Dict[str, Any]] = field(default_factory=list)
    agent_timestamp_s: Optional[float] = None
    collected_at_s: Optional[float] = None
    collector_lag_ms: Optional[float] = None

    @property
    def has_parameters(self) -> bool:
        return len(self.parameters) > 0

    @property
    def has_dtcs(self) -> bool:
        return len(self.dtcs) > 0

    def to_dict(self) -> dict:
        return {
            'timestamp': self.timestamp,
            'extraction_count': self.extraction_count,
            'extraction_duration_ms': self.extraction_duration_ms,
            'page_context': self.page_context,
            'parameters': self.parameters,
            'dtcs': [d.to_dict() for d in self.dtcs],
            'table_count': self.table_count,
            'agent_timestamp_s': self.agent_timestamp_s,
            'collected_at_s': self.collected_at_s,
            'collector_lag_ms': self.collector_lag_ms,
        }


def _normalize_timestamp_seconds(timestamp: Any) -> Optional[float]:
    """Normalize agent timestamp to unix seconds when possible."""
    try:
        value = float(timestamp)
    except (TypeError, ValueError):
        return None

    if value <= 0:
        return None

    # Java-side timestamps are typically currentTimeMillis().
    if value >= 1e11:
        return value / 1000.0

    return value


def _parse_agent_json(data: dict) -> AgentSnapshot:
    """Parse Agent JSON into an AgentSnapshot."""
    timestamp = data.get('timestamp', 0)
    extraction_count = data.get('extractionCount', 0)
    extraction_duration_ms = data.get('extractionDurationMs', 0)
    page_context = data.get('pageContext', {})

    tables = data.get('tables', [])
    if tables:
        parameters, dtcs, raw_tables = _parse_agent_tables(tables)
    else:
        parameters, dtcs = _parse_agent_windows(data.get('windows', []))
        raw_tables = []

    return AgentSnapshot(
        timestamp=timestamp,
        extraction_count=extraction_count,
        extraction_duration_ms=extraction_duration_ms,
        page_context=page_context,
        parameters=parameters,
        dtcs=dtcs,
        table_count=len(tables) if tables else 0,
        raw_tables=raw_tables,
        agent_timestamp_s=_normalize_timestamp_seconds(timestamp),
    )


def _build_parameter(row: dict, columns: list) -> dict[str, str]:
    """Build one normalized parameter row."""
    return {
        'module': _get_row_value(row, columns, ['Module', 'Control Module']),
        'name': _get_row_value(row, columns, ['Parameter Name']),
        'value': _get_row_value(row, columns, ['Value']),
        'unit': _get_row_value(row, columns, ['Unit', 'Units']),
    }


def _build_dtc(row: dict, columns: list) -> DTCInfo:
    """Build one normalized DTC row."""
    return DTCInfo(
        control_module=_get_row_value(row, columns, ['Control Module']),
        dtc_type=_get_row_value(row, columns, ['DTC Type']),
        code=_get_row_value(row, columns, ['DTC']),
        symptom_byte=_get_row_value(row, columns, ['Symptom Byte']),
        description=_get_row_value(row, columns, ['Description']),
        symptom_description=_get_row_value(row, columns, ['Symptom Description']),
        status=_get_row_value(row, columns, ['Status']),
    )


def _append_table_rows(
    table_type: str,
    *,
    columns: list,
    rows: list,
    parameters: list[dict[str, str]],
    dtcs: list[DTCInfo],
) -> None:
    """Append parsed rows for one known agent table type."""
    if table_type == 'data_display':
        for row in rows:
            param = _build_parameter(row, columns)
            if param['name']:
                parameters.append(param)
        return

    if table_type == 'dtc':
        for row in rows:
            dtc = _build_dtc(row, columns)
            if dtc.code:
                dtcs.append(dtc)


def _parse_agent_tables(tables: list) -> tuple[list[dict[str, str]], list[DTCInfo], list[dict[str, Any]]]:
    """Parse v2 agent tables payload."""
    parameters: list[dict[str, str]] = []
    dtcs: list[DTCInfo] = []
    raw_tables: list[dict[str, Any]] = []

    for table in tables:
        raw_tables.append(table)
        _append_table_rows(
            str(table.get('tableType', 'unknown')),
            columns=table.get('columns', []),
            rows=table.get('rows', []),
            parameters=parameters,
            dtcs=dtcs,
        )

    return parameters, dtcs, raw_tables


def _parse_agent_windows(windows: list) -> tuple[list[dict[str, str]], list[DTCInfo]]:
    """Parse v1 agent windows/controls fallback payload."""
    parameters: list[dict[str, str]] = []
    dtcs: list[DTCInfo] = []

    for window in windows:
        for control in window.get('controls', []):
            if control.get('type') != 'TableView':
                continue
            ctrl_columns = control.get('columns', [])
            _append_table_rows(
                _detect_table_type_from_columns(ctrl_columns),
                columns=ctrl_columns,
                rows=control.get('rows', []),
                parameters=parameters,
                dtcs=dtcs,
            )

    return parameters, dtcs


def _get_row_value(row: dict, columns: list, possible_keys: list) -> str:
    """Get a value from a row dict, trying multiple possible column names."""
    for key in possible_keys:
        if key in row:
            val = row[key]
            return str(val) if val is not None else ''
    return ''


def _detect_table_type_from_columns(columns: list) -> str:
    """Detect table type from column names (v1 fallback)."""
    col_set = set(columns)
    dtc_cols = {'DTC', 'Description', 'Status', 'Control Module', 'DTC Type',
                'Symptom Byte', 'Symptom Description'}
    data_cols = {'Parameter Name', 'Value', 'Module', 'Unit', 'Units'}

    dtc_matches = len(col_set & dtc_cols)
    data_matches = len(col_set & data_cols)

    if dtc_matches >= 2:
        return 'dtc'
    if data_matches >= 2:
        return 'data_display'
    return 'unknown'


def _try_extract_unknown_table(row_list: list, columns: list,
                                parameters: list) -> None:
    """Try to extract data from unknown table type as parameters."""
    # Only attempt if the table has at least value-like columns
    if len(columns) < 2:
        return
    for row in row_list:
        values = list(row.values())
        if len(values) >= 2:
            param = {
                'module': '',
                'name': str(values[0]) if values[0] else '',
                'value': str(values[1]) if len(values) > 1 and values[1] else '',
                'unit': str(values[2]) if len(values) > 2 and values[2] else '',
            }
            if param['name']:
                parameters.append(param)


class AgentDataCollector:
    """
    Collects data from GDS2 by reading the Java Agent's JSON output.

    The Java Agent runs inside the GDS2 JVM and writes table data to
    %USERPROFILE%/gds2-data/latest.json at configurable intervals
    (default 100ms).

    This collector polls the JSON file and provides callbacks for:
    - Full snapshots (all parameters + DTCs)
    - Parameter changes
    - DTC changes
    """

    def __init__(
        self,
        on_snapshot: Optional[Callable[[AgentSnapshot, List[dict]], None]] = None,
        on_param_change: Optional[Callable[[List[dict]], None]] = None,
        on_dtc_change: Optional[Callable[[List[DTCInfo], List[DTCInfo]], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        page_guard: Optional[Callable[[], Optional[dict[str, Any]]]] = None,
        on_guard_event: Optional[Callable[[dict[str, Any]], None]] = None,
        interval_ms: int = 100,
        json_path: Optional[Path] = None,
    ):
        self.on_snapshot = on_snapshot
        self.on_param_change = on_param_change
        self.on_dtc_change = on_dtc_change
        self.on_error = on_error
        self.page_guard = page_guard
        self.on_guard_event = on_guard_event
        self.interval_ms = max(50, interval_ms)

        if json_path is None:
            self._json_path = Path.home() / 'gds2-data' / 'latest.json'
        else:
            self._json_path = Path(json_path)

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._guard_thread: Optional[threading.Thread] = None
        self._guard_interval_sec: float = 5.0
        self._last_guard_result: Optional[dict] = None
        self._guard_result_lock = threading.Lock()
        self._last_mtime: float = 0
        self._last_extraction_count: int = 0
        self._last_params: Dict[str, dict] = {}
        self._last_dtc_codes: set = set()
        self._last_dtcs: List[DTCInfo] = []
        self._collection_count = 0
        self._last_snapshot: Optional[AgentSnapshot] = None
        self._fatal_error: Optional[str] = None
        self._last_guard_event_signature: Optional[str] = None
        self._last_focus_parameter_values: Dict[str, str] = {}

    def start(self):
        """Start polling the Agent JSON file."""
        if self._running:
            logger.warning("AgentDataCollector is already running")
            return

        if not self._json_path.parent.exists():
            logger.warning(f"Agent data directory does not exist: {self._json_path.parent}")

        self._running = True
        with self._guard_result_lock:
            self._last_guard_result = None
        if self.page_guard is not None:
            self._guard_thread = threading.Thread(target=self._guard_loop, daemon=True)
            self._guard_thread.start()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info("Agent collector started interval=%sms", self.interval_ms)
        logger.debug("Agent collector JSON path: %s", self._json_path)
        emit_collector_event(
            "agent.collector.started",
            reason="collector_started",
            interval_ms=self.interval_ms,
            json_path=str(self._json_path),
        )

    def stop(self):
        """Stop polling."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        if self._guard_thread is not None:
            self._guard_thread.join(timeout=5)
            self._guard_thread = None
        logger.info("Agent collector stopped")
        emit_collector_event(
            "agent.collector.stopped",
            reason="collector_stopped",
            json_path=str(self._json_path),
            collection_count=self._collection_count,
        )

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def collection_count(self) -> int:
        return self._collection_count

    @property
    def last_snapshot(self) -> Optional[AgentSnapshot]:
        return self._last_snapshot

    @property
    def last_params(self) -> Dict[str, dict]:
        return dict(self._last_params)

    @property
    def last_dtcs(self) -> List[DTCInfo]:
        return list(self._last_dtcs)

    @property
    def fatal_error(self) -> Optional[str]:
        return self._fatal_error

    @property
    def json_path(self) -> Path:
        return self._json_path

    def _read_agent_json(
        self,
        *,
        attempts: int = 1,
        retry_delay_sec: float = 0.0,
    ) -> Tuple[Optional[dict], int, Optional[str], Optional[str]]:
        """Read Agent JSON with encoding fallback and optional retry."""
        last_error: Optional[Exception] = None

        for attempt in range(1, max(1, attempts) + 1):
            for encoding in _AGENT_JSON_ENCODINGS:
                try:
                    with open(self._json_path, 'r', encoding=encoding) as f:
                        data = json.load(f)
                    return data, attempt, None, encoding
                except Exception as exc:
                    last_error = exc

            if attempt < max(1, attempts) and retry_delay_sec > 0:
                time.sleep(retry_delay_sec)

        error = None
        if last_error is not None:
            error = f"{type(last_error).__name__}: {last_error}"
        return None, max(1, attempts), error, None

    def check_agent_available(self) -> dict:
        """Check if the Agent JSON file exists and is being updated."""
        result = {
            'available': False,
            'path': str(self._json_path),
            'exists': self._json_path.exists(),
            'age_seconds': None,
            'extraction_count': None,
            'version': None,
            'encoding': None,
            'attempts': 0,
            'error': None,
        }

        if not self._json_path.exists():
            emit_collector_event(
                "agent.collector.availability",
                status="error",
                failure_code="json_missing",
                reason="latest_json_missing",
                json_path=str(self._json_path),
                available=False,
            )
            return result

        try:
            mtime = self._json_path.stat().st_mtime
            age = time.time() - mtime
            result['age_seconds'] = round(age, 1)
            data, attempts, error, encoding = self._read_agent_json(
                attempts=_AGENT_AVAILABILITY_READ_ATTEMPTS,
                retry_delay_sec=_AGENT_AVAILABILITY_RETRY_DELAY_SECONDS,
            )
            result['attempts'] = attempts
            result['encoding'] = encoding
            result['error'] = error

            if data is None:
                logger.debug(
                    "Agent availability read failed path=%s attempts=%s age=%s error=%s",
                    self._json_path,
                    attempts,
                    result['age_seconds'],
                    error,
                )
                return result

            result['extraction_count'] = data.get('extractionCount', 0)
            result['version'] = data.get('version', '1.0')
            result['available'] = age < _AGENT_AVAILABILITY_MAX_AGE_SECONDS
            emit_collector_event(
                "agent.collector.availability",
                status="ok" if result["available"] else "error",
                failure_code=None if result["available"] else "stale_or_unreadable",
                reason=result.get("error") or ("available" if result["available"] else "stale"),
                json_path=str(self._json_path),
                available=bool(result["available"]),
                age_seconds=result.get("age_seconds"),
                extraction_count=result.get("extraction_count"),
                version=result.get("version"),
                attempts=result.get("attempts"),
            )

        except Exception as e:
            result['error'] = f"{type(e).__name__}: {e}"
            logger.debug(
                "Error checking agent availability path=%s error=%s",
                self._json_path,
                result['error'],
            )
            emit_collector_event(
                "agent.collector.availability",
                status="error",
                failure_code=type(e).__name__,
                reason=result["error"],
                json_path=str(self._json_path),
                available=False,
            )

        return result

    def _guard_loop(self):
        """Run page guard checks in the background."""
        while self._running:
            try:
                result = self._run_page_guard()
                with self._guard_result_lock:
                    self._last_guard_result = result
            except Exception as e:
                logger.debug(f"Guard loop error: {e}")
            time.sleep(self._guard_interval_sec)

    def _poll_loop(self):
        """Main polling loop."""
        logger.debug("Agent poll loop started")
        interval_sec = self.interval_ms / 1000.0

        while self._running:
            loop_start = time.monotonic()
            try:
                with self._guard_result_lock:
                    guard_result = self._last_guard_result
                    self._last_guard_result = None
                if guard_result is not None and self.on_guard_event:
                    signature = _guard_event_signature(guard_result)
                    if signature != self._last_guard_event_signature:
                        self._last_guard_event_signature = signature
                        self.on_guard_event(guard_result)

                snapshot = self._read_and_parse()

                if snapshot is not None:
                    self._collection_count += 1
                    self._last_snapshot = snapshot

                    # Detect parameter changes
                    param_changes = self._detect_param_changes(snapshot.parameters)

                    # Detect DTC changes
                    dtc_added, dtc_removed = self._detect_dtc_changes(snapshot.dtcs)

                    # Update stored state
                    self._update_stored_state(snapshot)

                    # Fire callbacks
                    if self.on_snapshot:
                        self.on_snapshot(snapshot, param_changes)

                    if param_changes and self.on_param_change:
                        self.on_param_change(param_changes)

                    if (dtc_added or dtc_removed) and self.on_dtc_change:
                        self.on_dtc_change(dtc_added, dtc_removed)

            except Exception as e:
                logger.exception(f"Error in agent poll loop: {e}")
                if self.on_error:
                    self.on_error(str(e))

            elapsed = time.monotonic() - loop_start
            remaining = max(0.01, interval_sec - elapsed)
            time.sleep(remaining)

        logger.debug("Agent poll loop stopped")

    def _run_page_guard(self) -> Optional[dict[str, Any]]:
        """Run optional page consistency guard before reading Agent output."""
        if self.page_guard is None:
            return None

        result = self.page_guard()
        if result is None:
            return None
        if not isinstance(result, dict):
            raise RuntimeError("page_guard must return a dict or None")

        if not result.get('ok', False):
            self._fatal_error = str(result.get('error') or 'Data Display guard failed.')
            logger.warning("Agent collector guard failed: %s", self._fatal_error)
            emit_collector_event(
                "agent.collector.guard_failed",
                status="error",
                failure_code="guard_failed",
                reason=self._fatal_error,
                guard_payload=dict(result),
            )
            self._running = False
            if self.on_error:
                self.on_error(self._fatal_error)
        elif not result.get('message'):
            self._last_guard_event_signature = None

        return result

    def _read_and_parse(self) -> Optional[AgentSnapshot]:
        """Read and parse the Agent JSON file if it has been updated."""
        if not self._json_path.exists():
            return None

        try:
            mtime = self._json_path.stat().st_mtime

            # Skip if file hasn't been modified
            if mtime <= self._last_mtime:
                return None

            self._last_mtime = mtime

            data, _attempts, _error, _encoding = self._read_agent_json()

            if data is None:
                return None

            # Skip if extraction count hasn't changed
            extraction_count = data.get('extractionCount', 0)
            if extraction_count <= self._last_extraction_count:
                return None

            self._last_extraction_count = extraction_count

            snapshot = _parse_agent_json(data)
            collected_at_s = time.time()
            snapshot.collected_at_s = collected_at_s

            if snapshot.agent_timestamp_s is not None:
                snapshot.collector_lag_ms = round(
                    max(0.0, collected_at_s - snapshot.agent_timestamp_s) * 1000.0,
                    1,
                )

            emit_collector_event(
                "agent.collector.snapshot",
                reason="snapshot_read",
                extraction_count=snapshot.extraction_count,
                collector_lag_ms=snapshot.collector_lag_ms,
                page_context=snapshot.page_context,
                dtc_count=len(snapshot.dtcs),
                parameter_count=len(snapshot.parameters),
            )
            self._emit_focus_parameter_sample(snapshot)

            return snapshot

        except json.JSONDecodeError:
            # File might be in the middle of being written
            return None
        except Exception as e:
            logger.debug(f"Error reading agent JSON: {e}")
            emit_collector_event(
                "agent.collector.error",
                status="error",
                failure_code=type(e).__name__,
                reason=str(e),
                json_path=str(self._json_path),
            )
            return None

    def _emit_focus_parameter_sample(self, snapshot: AgentSnapshot) -> None:
        """Emit value-level samples for lag-sensitive Data Display parameters."""
        samples, missing_keys, current_values = _extract_focus_parameter_samples(
            snapshot.parameters,
            self._last_focus_parameter_values,
        )
        if not samples:
            return

        self._last_focus_parameter_values = current_values
        emit_collector_event(
            "agent.collector.focus_parameters_sampled",
            operation_kind="agent_data_value_sample",
            reason="focus_parameters_sampled",
            page="data_display",
            extraction_count=snapshot.extraction_count,
            extraction_duration_ms=snapshot.extraction_duration_ms,
            agent_timestamp_s=snapshot.agent_timestamp_s,
            collected_at_s=snapshot.collected_at_s,
            collector_lag_ms=snapshot.collector_lag_ms,
            page_context=snapshot.page_context,
            target_keys=list(_FOCUS_PARAMETER_KEYS),
            missing_target_keys=missing_keys,
            parameters=samples,
            parameter_values=self._parameter_values_by_key(samples),
            parameter_value_sources=self._parameter_value_sources(samples),
        )

    @staticmethod
    def _parameter_values_by_key(samples: list[dict[str, Any]]) -> dict[str, str]:
        preferred_samples: dict[str, dict[str, Any]] = {}
        for sample in samples:
            key = str(sample["key"])
            preferred_samples[key] = _prefer_primary_focus_sample(
                preferred_samples.get(key),
                sample,
            )
        return {
            key: str(sample["value"])
            for key, sample in preferred_samples.items()
        }

    @staticmethod
    def _parameter_value_sources(samples: list[dict[str, Any]]) -> dict[str, str]:
        preferred_samples: dict[str, dict[str, Any]] = {}
        for sample in samples:
            key = str(sample["key"])
            preferred_samples[key] = _prefer_primary_focus_sample(
                preferred_samples.get(key),
                sample,
            )
        return {
            key: str(sample["name"])
            for key, sample in preferred_samples.items()
        }

    def _detect_param_changes(self, new_params: List[dict]) -> List[dict]:
        """Detect parameter value changes."""
        changes = []

        for param in new_params:
            key = f"{param['name']}|{param['unit']}"
            if key in self._last_params:
                old = self._last_params[key]
                if old['value'] != param['value']:
                    changes.append({
                        'parameter': param['name'],
                        'old_value': old['value'],
                        'new_value': param['value'],
                        'unit': param['unit'],
                        'timestamp': datetime.now().isoformat(),
                    })

        return changes

    def _detect_dtc_changes(self, new_dtcs: List[DTCInfo]) -> Tuple[List[DTCInfo], List[DTCInfo]]:
        """Detect DTC additions and removals."""
        new_codes = {d.code for d in new_dtcs}
        old_codes = self._last_dtc_codes

        added_codes = new_codes - old_codes
        removed_codes = old_codes - new_codes

        added = [d for d in new_dtcs if d.code in added_codes]
        removed = [d for d in self._last_dtcs if d.code in removed_codes]

        return added, removed

    def _update_stored_state(self, snapshot: AgentSnapshot):
        """Update stored state for next comparison."""
        self._last_params = {}
        for param in snapshot.parameters:
            key = f"{param['name']}|{param['unit']}"
            self._last_params[key] = param

        self._last_dtc_codes = {d.code for d in snapshot.dtcs}
        self._last_dtcs = list(snapshot.dtcs)


# Simple test
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    def on_snapshot(snap, _changes=None):
        print(f"\n[SNAPSHOT] #{snap.extraction_count} "
              f"({snap.extraction_duration_ms}ms) "
              f"params={len(snap.parameters)} dtcs={len(snap.dtcs)}")
        for p in snap.parameters[:3]:
            print(f"  {p['name']}: {p['value']} {p['unit']}")
        if len(snap.parameters) > 3:
            print(f"  ... and {len(snap.parameters) - 3} more")

    def on_param_change(changes):
        print(f"\n[CHANGES] {len(changes)} parameter(s) changed:")
        for c in changes:
            print(f"  * {c['parameter']}: {c['old_value']} -> {c['new_value']} {c['unit']}")
        print()

    def on_dtc_change(added, removed):
        if added:
            print(f"\n[DTC ADDED] {len(added)} new DTC(s):")
            for d in added:
                print(f"  + {d.code}: {d.description} ({d.status})")
        if removed:
            print(f"\n[DTC REMOVED] {len(removed)} DTC(s) cleared:")
            for d in removed:
                print(f"  - {d.code}: {d.description}")

    def on_error(error):
        print(f"\n[ERROR] {error}")

    collector = AgentDataCollector(
        on_snapshot=on_snapshot,
        on_param_change=on_param_change,
        on_dtc_change=on_dtc_change,
        on_error=on_error,
        interval_ms=200,
    )

    # Check availability first
    status = collector.check_agent_available()
    print("=" * 70)
    print("Agent Data Collector Test")
    print("=" * 70)
    print(f"Agent JSON: {status['path']}")
    print(f"Exists: {status['exists']}")
    print(f"Available: {status['available']}")
    if status['age_seconds'] is not None:
        print(f"Last update: {status['age_seconds']}s ago")
    print()

    if not status['available']:
        print("Agent not available. Start GDS2 with the agent:")
        print("  C:\\tools\\gds2-agent\\launch-gds2-with-agent.bat")
        print()

    print("Starting collector (press Ctrl+C to stop)...")
    print()

    collector.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")
        collector.stop()
        print("Done.")
