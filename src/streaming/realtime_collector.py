"""
Real-time Data Streaming using Create Report + HTML Parsing

This module provides real-time data streaming from GDS2 by:
1. Periodically clicking Create Report button
2. Parsing the generated HTML to extract all 73 parameters
3. Detecting changes and streaming them via callback

Usage:
    collector = RealtimeDataCollector(on_data_change=my_callback)
    collector.start()
    # ... later
    collector.stop()
"""

import time
import threading
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Callable, Optional, Any
from dataclasses import dataclass, field
from bs4 import BeautifulSoup
import cv2
import numpy as np
import pyautogui

logger = logging.getLogger(__name__)


@dataclass
class ParameterValue:
    """Represents a parameter's current value."""
    module: str
    name: str
    value: str
    unit: str
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def unique_key(self) -> str:
        """
        Generate unique key for this parameter.

        Some parameters have the same name but different units:
        - Turbocharger Bypass Solenoid Valve Command (On/Off state)
        - Turbocharger Bypass Solenoid Valve Command (0-100% value)

        Using name + unit ensures these are tracked separately.
        """
        return f"{self.name}|{self.unit}"

    def __str__(self):
        return f"{self.name}: {self.value} {self.unit}"

    def to_dict(self):
        return {
            'module': self.module,
            'name': self.name,
            'value': self.value,
            'unit': self.unit,
            'timestamp': self.timestamp.isoformat()
        }


@dataclass
class DataChange:
    """Represents a change in parameter value."""
    parameter: str
    old_value: str
    new_value: str
    old_unit: str
    new_unit: str
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self):
        return {
            'parameter': self.parameter,
            'old_value': self.old_value,
            'new_value': self.new_value,
            'unit': self.new_unit,
            'timestamp': self.timestamp.isoformat()
        }


class RealtimeDataCollector:
    """
    Collects real-time data from GDS2 using Create Report + HTML parsing.

    This approach ensures all 73 parameters are captured simultaneously,
    providing consistent snapshots at each interval.
    """

    def __init__(
        self,
        on_data_change: Optional[Callable[[List[DataChange]], None]] = None,
        on_full_data: Optional[Callable[[List[ParameterValue]], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        interval_seconds: float = 3.0,
        template_dir: Path = Path(r"C:\Users\shsww\projects\RPA_demo\images\buttons"),
    ):
        """
        Initialize the collector.

        Args:
            on_data_change: Callback when parameter values change
            on_full_data: Callback with all parameters on each collection
            on_error: Callback when an error occurs
            interval_seconds: Time between Create Report clicks
            template_dir: Directory containing button templates
        """
        self.on_data_change = on_data_change
        self.on_full_data = on_full_data
        self.on_error = on_error
        self.interval_seconds = interval_seconds
        self.template_dir = template_dir

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_values: Dict[str, ParameterValue] = {}
        self._last_report_mtime: float = 0
        self._collection_count = 0

        # Report directory
        self._report_dir = Path.home() / 'AppData' / 'Local' / 'Temp' / 'GDS 2'

        # Load Create Report button template
        self._create_report_template = None
        template_path = self.template_dir / "create_report.png"
        if template_path.exists():
            self._create_report_template = cv2.imread(str(template_path), cv2.IMREAD_GRAYSCALE)
        else:
            logger.warning(f"Create Report template not found: {template_path}")

    def start(self):
        """Start the data collection loop."""
        if self._running:
            logger.warning("Collector is already running")
            return

        self._running = True
        self._thread = threading.Thread(target=self._collection_loop, daemon=True)
        self._thread.start()
        logger.info("Realtime data collector started")

    def stop(self):
        """Stop the data collection loop."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("Realtime data collector stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def collection_count(self) -> int:
        return self._collection_count

    @property
    def last_values(self) -> Dict[str, ParameterValue]:
        return self._last_values.copy()

    def _collection_loop(self):
        """Main collection loop."""
        logger.info("Collection loop started")

        while self._running:
            try:
                # Step 1: Click Create Report
                if not self._click_create_report():
                    if self.on_error:
                        self.on_error("Could not click Create Report button")
                    time.sleep(self.interval_seconds)
                    continue

                # Step 2: Wait for HTML file to be created
                time.sleep(1.5)  # Wait for file to be written

                # Step 3: Parse the new HTML report
                params = self._parse_latest_report()
                if not params:
                    if self.on_error:
                        self.on_error("Could not parse HTML report")
                    time.sleep(self.interval_seconds)
                    continue

                self._collection_count += 1

                # Step 4: Clean up old reports (keep latest 50)
                self._cleanup_old_reports(keep_latest=50)

                # Step 5: Detect changes
                changes = self._detect_changes(params)

                # Step 6: Update stored values (use unique_key to handle duplicate names)
                for p in params:
                    self._last_values[p.unique_key] = p

                # Step 7: Call callbacks
                if self.on_full_data:
                    self.on_full_data(params)

                if changes and self.on_data_change:
                    self.on_data_change(changes)

                # Step 8: Wait for next interval
                # Subtract time already spent (~1.5s for file wait)
                remaining_wait = max(0, self.interval_seconds - 1.5)
                time.sleep(remaining_wait)

            except Exception as e:
                logger.exception(f"Error in collection loop: {e}")
                if self.on_error:
                    self.on_error(str(e))
                time.sleep(self.interval_seconds)

        logger.info("Collection loop stopped")

    def _click_create_report(self) -> bool:
        """Click the Create Report button using template matching."""
        if self._create_report_template is None:
            logger.error("Create Report template not loaded")
            return False

        try:
            # Take screenshot
            screenshot = pyautogui.screenshot()
            screenshot_np = np.array(screenshot)
            screenshot_gray = cv2.cvtColor(screenshot_np, cv2.COLOR_RGB2GRAY)

            # Template matching
            result = cv2.matchTemplate(
                screenshot_gray,
                self._create_report_template,
                cv2.TM_CCOEFF_NORMED
            )
            min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)

            if max_val >= 0.8:
                h, w = self._create_report_template.shape
                center_x = max_loc[0] + w // 2
                center_y = max_loc[1] + h // 2

                pyautogui.click(center_x, center_y)
                logger.debug(f"Clicked Create Report at ({center_x}, {center_y})")
                return True
            else:
                logger.warning(f"Create Report button not found (confidence: {max_val:.3f})")
                return False

        except Exception as e:
            logger.error(f"Error clicking Create Report: {e}")
            return False

    def _parse_latest_report(self) -> List[ParameterValue]:
        """Parse the latest HTML report."""
        try:
            reports = list(self._report_dir.glob('Data Display_*.html'))
            if not reports:
                logger.warning("No HTML reports found")
                return []

            latest = max(reports, key=lambda p: p.stat().st_mtime)
            current_mtime = latest.stat().st_mtime

            # Check if this is a new report
            if current_mtime <= self._last_report_mtime:
                logger.debug("No new report since last check")
                return []

            self._last_report_mtime = current_mtime

            # Parse HTML (use iso-8859-1 encoding like other report parsers)
            with open(latest, 'r', encoding='iso-8859-1') as f:
                soup = BeautifulSoup(f.read(), 'html.parser')

            params = []
            rows = soup.find_all('tr')

            for row in rows:
                cells = row.find_all('td')
                if cells and len(cells) >= 4:
                    module = cells[0].get_text().strip()
                    param_name = cells[1].get_text().strip()
                    value = cells[2].get_text().strip()
                    unit = cells[3].get_text().strip() if len(cells) > 3 else ''

                    if param_name and module and not param_name.startswith('Data Display'):
                        params.append(ParameterValue(
                            module=module,
                            name=param_name,
                            value=value,
                            unit=unit,
                            timestamp=datetime.fromtimestamp(current_mtime)
                        ))

            logger.debug(f"Parsed {len(params)} parameters from {latest.name}")
            return params

        except Exception as e:
            logger.error(f"Error parsing HTML report: {e}")
            return []

    def _cleanup_old_reports(self, keep_latest: int = 50):
        """
        Clean up old HTML reports to prevent disk space issues.

        Keeps only the most recent N report files and deletes older ones.
        This prevents disk space from filling up during long monitoring sessions.

        Args:
            keep_latest: Number of recent reports to keep (default: 50)
        """
        try:
            # Get all HTML report files
            reports = list(self._report_dir.glob('Data Display_*.html'))

            if len(reports) <= keep_latest:
                # No cleanup needed
                return

            # Sort by modification time (newest first)
            reports.sort(key=lambda p: p.stat().st_mtime, reverse=True)

            # Delete old reports beyond keep_latest
            deleted_count = 0
            for old_report in reports[keep_latest:]:
                try:
                    old_report.unlink()
                    deleted_count += 1
                except Exception as e:
                    # File might be locked by browser or other process
                    logger.debug(f"Could not delete {old_report.name}: {e}")

            if deleted_count > 0:
                logger.info(f"Cleaned up {deleted_count} old report(s), kept latest {keep_latest}")

        except Exception as e:
            logger.error(f"Error cleaning up old reports: {e}")

    def _detect_changes(self, new_params: List[ParameterValue]) -> List[DataChange]:
        """Detect changes between new and previous parameter values."""
        changes = []

        for p in new_params:
            # Use unique_key (name + unit) to handle duplicate parameter names
            key = p.unique_key
            if key in self._last_values:
                old = self._last_values[key]
                if old.value != p.value:
                    changes.append(DataChange(
                        parameter=p.name,
                        old_value=old.value,
                        new_value=p.value,
                        old_unit=old.unit,
                        new_unit=p.unit,
                        timestamp=p.timestamp
                    ))

        return changes


# Simple test
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    def on_change(changes):
        print(f"\n[CHANGES] {len(changes)} parameter(s) changed:")
        for c in changes:
            print(f"  * {c.parameter}: {c.old_value} -> {c.new_value} {c.new_unit}")

    def on_data(params):
        print(f"\n[DATA] Collected {len(params)} parameters")
        # Show Battery Voltage
        for p in params:
            if 'Battery' in p.name:
                print(f"  >> {p.name}: {p.value} {p.unit}")

    def on_error(error):
        print(f"\n[ERROR] {error}")

    collector = RealtimeDataCollector(
        on_data_change=on_change,
        on_full_data=on_data,
        on_error=on_error,
        interval_seconds=3.0
    )

    print("=" * 70)
    print("Realtime Data Collector Test")
    print("=" * 70)
    print()
    print("Make sure GDS2 is at Data Display page.")
    print("Press Ctrl+C to stop.")
    print()

    collector.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")
        collector.stop()
        print("Done.")
