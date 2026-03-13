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
from typing import Dict, List, Callable, Optional, Any
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


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

    parameters = []
    dtcs = []
    raw_tables = []
    tables = data.get('tables', [])

    for table in tables:
        table_type = table.get('tableType', 'unknown')
        columns = table.get('columns', [])
        rows = table.get('rows', [])
        raw_tables.append(table)

        if table_type == 'data_display':
            for row in rows:
                param = {
                    'module': _get_row_value(row, columns, ['Module']),
                    'name': _get_row_value(row, columns, ['Parameter Name']),
                    'value': _get_row_value(row, columns, ['Value']),
                    'unit': _get_row_value(row, columns, ['Unit', 'Units']),
                }
                if param['name']:
                    parameters.append(param)

        elif table_type == 'dtc':
            for row in rows:
                dtc = DTCInfo(
                    control_module=_get_row_value(row, columns, ['Control Module']),
                    dtc_type=_get_row_value(row, columns, ['DTC Type']),
                    code=_get_row_value(row, columns, ['DTC']),
                    symptom_byte=_get_row_value(row, columns, ['Symptom Byte']),
                    description=_get_row_value(row, columns, ['Description']),
                    symptom_description=_get_row_value(row, columns, ['Symptom Description']),
                    status=_get_row_value(row, columns, ['Status']),
                )
                if dtc.code:
                    dtcs.append(dtc)

        else:
            # Unknown table type - skip to avoid false positives
            pass

    # Fallback: handle v1 format (windows -> controls -> rows)
    if not tables:
        windows = data.get('windows', [])
        for window in windows:
            controls = window.get('controls', [])
            for control in controls:
                if control.get('type') == 'TableView':
                    ctrl_columns = control.get('columns', [])
                    ctrl_rows = control.get('rows', [])
                    table_type = _detect_table_type_from_columns(ctrl_columns)

                    if table_type == 'data_display':
                        for row in ctrl_rows:
                            param = {
                                'module': _get_row_value(row, ctrl_columns, ['Module']),
                                'name': _get_row_value(row, ctrl_columns, ['Parameter Name']),
                                'value': _get_row_value(row, ctrl_columns, ['Value']),
                                'unit': _get_row_value(row, ctrl_columns, ['Unit', 'Units']),
                            }
                            if param['name']:
                                parameters.append(param)

                    elif table_type == 'dtc':
                        for row in ctrl_rows:
                            dtc = DTCInfo(
                                control_module=_get_row_value(row, ctrl_columns, ['Control Module']),
                                dtc_type=_get_row_value(row, ctrl_columns, ['DTC Type']),
                                code=_get_row_value(row, ctrl_columns, ['DTC']),
                                symptom_byte=_get_row_value(row, ctrl_columns, ['Symptom Byte']),
                                description=_get_row_value(row, ctrl_columns, ['Description']),
                                symptom_description=_get_row_value(row, ctrl_columns, ['Symptom Description']),
                                status=_get_row_value(row, ctrl_columns, ['Status']),
                            )
                            if dtc.code:
                                dtcs.append(dtc)

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
        self._last_mtime: float = 0
        self._last_extraction_count: int = 0
        self._last_params: Dict[str, dict] = {}
        self._last_dtc_codes: set = set()
        self._last_dtcs: List[DTCInfo] = []
        self._collection_count = 0
        self._last_snapshot: Optional[AgentSnapshot] = None
        self._fatal_error: Optional[str] = None
        self._last_guard_event_signature: Optional[str] = None

    def start(self):
        """Start polling the Agent JSON file."""
        if self._running:
            logger.warning("AgentDataCollector is already running")
            return

        if not self._json_path.parent.exists():
            logger.warning(f"Agent data directory does not exist: {self._json_path.parent}")

        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info(f"AgentDataCollector started (interval={self.interval_ms}ms, path={self._json_path})")

    def stop(self):
        """Stop polling."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("AgentDataCollector stopped")

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

    def check_agent_available(self) -> dict:
        """Check if the Agent JSON file exists and is being updated."""
        result = {
            'available': False,
            'path': str(self._json_path),
            'exists': self._json_path.exists(),
            'age_seconds': None,
            'extraction_count': None,
        }

        if not self._json_path.exists():
            return result

        try:
            mtime = self._json_path.stat().st_mtime
            age = time.time() - mtime
            result['age_seconds'] = round(age, 1)

            with open(self._json_path, 'r', encoding='gbk') as f:
                data = json.load(f)

            result['extraction_count'] = data.get('extractionCount', 0)
            result['version'] = data.get('version', '1.0')

            # Consider available if file was updated within last 10 seconds
            result['available'] = age < 10.0

        except Exception as e:
            logger.debug(f"Error checking agent availability: {e}")

        return result

    def _poll_loop(self):
        """Main polling loop."""
        logger.info("Agent poll loop started")
        interval_sec = self.interval_ms / 1000.0

        while self._running:
            try:
                guard_result = self._run_page_guard()
                if guard_result is not None and self.on_guard_event:
                    signature = json.dumps(guard_result, sort_keys=True, ensure_ascii=False)
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

            time.sleep(interval_sec)

        logger.info("Agent poll loop stopped")

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

            # Try multiple encodings (GBK for Chinese Windows, UTF-8 as fallback)
            data = None
            for encoding in ['gbk', 'utf-8', 'latin-1']:
                try:
                    with open(self._json_path, 'r', encoding=encoding) as f:
                        data = json.load(f)
                    break
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue

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

            return snapshot

        except json.JSONDecodeError:
            # File might be in the middle of being written
            return None
        except Exception as e:
            logger.debug(f"Error reading agent JSON: {e}")
            return None

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

    def _detect_dtc_changes(self, new_dtcs: List[DTCInfo]):
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
