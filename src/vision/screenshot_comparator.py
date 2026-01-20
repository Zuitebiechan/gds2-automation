"""
OpenCV Screenshot Comparator

Compares screenshots using OpenCV histogram comparison and structural analysis.
"""

import logging
import time
from pathlib import Path
from typing import Optional, Tuple, Union, List

from ..core.interfaces.screenshot import ScreenshotComparator, ComparisonResult
from ..core.exceptions import ScreenshotComparisonError

logger = logging.getLogger(__name__)


class OpenCVScreenshotComparator(ScreenshotComparator):
    """
    Screenshot comparator using OpenCV.

    Uses histogram comparison for fast similarity detection.
    Optionally uses structural similarity (SSIM) for more accurate comparison.
    """

    def __init__(
        self,
        screenshot_dir: Optional[Union[str, Path]] = None,
        use_ssim: bool = False,
    ):
        """
        Initialize the comparator.

        Args:
            screenshot_dir: Default directory for screenshots
            use_ssim: Whether to use SSIM for comparison (slower but more accurate)
        """
        self.screenshot_dir = Path(screenshot_dir) if screenshot_dir else Path("screenshots")
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self.use_ssim = use_ssim

        self._cv2 = None
        self._np = None
        self._pyautogui = None

    def _ensure_imports(self):
        """Lazy import of heavy dependencies"""
        if self._cv2 is None:
            try:
                import cv2
                import numpy as np
                self._cv2 = cv2
                self._np = np
            except ImportError:
                raise ImportError(
                    "OpenCV is required for screenshot comparison. "
                    "Install with: pip install opencv-python"
                )

    def _ensure_pyautogui(self):
        """Lazy import of pyautogui for screenshots"""
        if self._pyautogui is None:
            try:
                import pyautogui
                self._pyautogui = pyautogui
            except ImportError:
                raise ImportError(
                    "pyautogui is required for screenshots. "
                    "Install with: pip install pyautogui"
                )

    def compare(
        self,
        before_path: Union[str, Path],
        after_path: Union[str, Path],
        threshold: float = 0.95,
        pixel_change_threshold: float = 0.99,
    ) -> ComparisonResult:
        """
        Compare two screenshots using HYBRID approach (histogram + pixel).

        Uses both histogram comparison and pixel-level comparison.
        Detects a change if EITHER method detects it:
        - Histogram similarity < threshold, OR
        - Pixel similarity < pixel_change_threshold (more than 1% of pixels changed)

        This catches changes that histogram alone misses (same colors, different positions).

        Args:
            before_path: Path to the "before" screenshot
            after_path: Path to the "after" screenshot
            threshold: Histogram similarity threshold (0.0-1.0)
            pixel_change_threshold: Pixel similarity threshold (default 0.99 = 1% change triggers detection)

        Returns:
            ComparisonResult with comparison details
        """
        self._ensure_imports()
        cv2 = self._cv2
        np = self._np

        before_path = Path(before_path)
        after_path = Path(after_path)

        # Validate paths
        if not before_path.exists():
            raise ScreenshotComparisonError(f"Before image not found: {before_path}")
        if not after_path.exists():
            raise ScreenshotComparisonError(f"After image not found: {after_path}")

        # Load images
        before_img = cv2.imread(str(before_path))
        after_img = cv2.imread(str(after_path))

        if before_img is None:
            raise ScreenshotComparisonError(f"Failed to load image: {before_path}")
        if after_img is None:
            raise ScreenshotComparisonError(f"Failed to load image: {after_path}")

        # Convert to grayscale
        before_gray = cv2.cvtColor(before_img, cv2.COLOR_BGR2GRAY)
        after_gray = cv2.cvtColor(after_img, cv2.COLOR_BGR2GRAY)

        # Calculate BOTH similarities
        if self.use_ssim:
            histogram_similarity = self._calculate_ssim(before_gray, after_gray)
        else:
            histogram_similarity = self._calculate_histogram_similarity(before_gray, after_gray)

        pixel_similarity = self._calculate_pixel_similarity(before_gray, after_gray)

        # HYBRID detection: change detected if EITHER method detects it
        histogram_different = histogram_similarity < threshold
        pixel_different = pixel_similarity < pixel_change_threshold

        is_different = histogram_different or pixel_different

        # Use the lower similarity for reporting (more conservative)
        similarity = min(histogram_similarity, pixel_similarity)

        # Find diff regions if different
        diff_regions = []
        if is_different:
            diff_regions = self._find_diff_regions(before_gray, after_gray)

        logger.debug(
            f"Screenshot comparison: hist={histogram_similarity:.4f}, pixel={pixel_similarity:.4f}, "
            f"threshold={threshold}, different={is_different} "
            f"(hist_diff={histogram_different}, pixel_diff={pixel_different})"
        )

        return ComparisonResult(
            is_different=is_different,
            similarity_score=similarity,
            diff_regions=diff_regions,
            before_path=str(before_path),
            after_path=str(after_path),
        )

    def _calculate_histogram_similarity(self, img1, img2) -> float:
        """Calculate similarity using histogram comparison"""
        cv2 = self._cv2

        # Calculate histograms
        hist1 = cv2.calcHist([img1], [0], None, [256], [0, 256])
        hist2 = cv2.calcHist([img2], [0], None, [256], [0, 256])

        # Normalize histograms
        cv2.normalize(hist1, hist1, 0, 1, cv2.NORM_MINMAX)
        cv2.normalize(hist2, hist2, 0, 1, cv2.NORM_MINMAX)

        # Compare using correlation
        correlation = cv2.compareHist(hist1, hist2, cv2.HISTCMP_CORREL)

        # Correlation ranges from -1 to 1, normalize to 0-1
        return (correlation + 1) / 2

    def _calculate_pixel_similarity(self, img1, img2, pixel_threshold: int = 30) -> float:
        """
        Calculate similarity based on actual pixel differences.

        This catches changes that histogram comparison misses (same colors, different positions).

        Args:
            img1: First grayscale image
            img2: Second grayscale image
            pixel_threshold: Minimum pixel value difference to count as "changed"

        Returns:
            Similarity score (0.0-1.0) based on percentage of unchanged pixels
        """
        cv2 = self._cv2
        np = self._np

        # Ensure same size
        if img1.shape != img2.shape:
            h = min(img1.shape[0], img2.shape[0])
            w = min(img1.shape[1], img2.shape[1])
            img1 = cv2.resize(img1, (w, h))
            img2 = cv2.resize(img2, (w, h))

        # Calculate absolute difference
        diff = cv2.absdiff(img1, img2)

        # Count pixels that changed more than threshold
        changed_pixels = np.sum(diff > pixel_threshold)
        total_pixels = img1.shape[0] * img1.shape[1]

        # Return similarity (percentage of unchanged pixels)
        unchanged_ratio = 1.0 - (changed_pixels / total_pixels)
        return unchanged_ratio

    def _calculate_ssim(self, img1, img2) -> float:
        """Calculate structural similarity index (SSIM)"""
        cv2 = self._cv2
        np = self._np

        # Ensure same size
        if img1.shape != img2.shape:
            # Resize to smaller
            h = min(img1.shape[0], img2.shape[0])
            w = min(img1.shape[1], img2.shape[1])
            img1 = cv2.resize(img1, (w, h))
            img2 = cv2.resize(img2, (w, h))

        # Parameters for SSIM
        C1 = (0.01 * 255) ** 2
        C2 = (0.03 * 255) ** 2

        img1 = img1.astype(np.float64)
        img2 = img2.astype(np.float64)

        # Mean
        mu1 = cv2.GaussianBlur(img1, (11, 11), 1.5)
        mu2 = cv2.GaussianBlur(img2, (11, 11), 1.5)

        mu1_sq = mu1 ** 2
        mu2_sq = mu2 ** 2
        mu1_mu2 = mu1 * mu2

        # Variance
        sigma1_sq = cv2.GaussianBlur(img1 ** 2, (11, 11), 1.5) - mu1_sq
        sigma2_sq = cv2.GaussianBlur(img2 ** 2, (11, 11), 1.5) - mu2_sq
        sigma12 = cv2.GaussianBlur(img1 * img2, (11, 11), 1.5) - mu1_mu2

        # SSIM
        ssim_map = ((2 * mu1_mu2 + C1) * (2 * sigma12 + C2)) / \
                   ((mu1_sq + mu2_sq + C1) * (sigma1_sq + sigma2_sq + C2))

        return float(ssim_map.mean())

    def _find_diff_regions(
        self,
        before_gray,
        after_gray,
        min_area: int = 100,
    ) -> List[Tuple[int, int, int, int]]:
        """Find regions that differ between images"""
        cv2 = self._cv2
        np = self._np

        # Ensure same size
        if before_gray.shape != after_gray.shape:
            h = min(before_gray.shape[0], after_gray.shape[0])
            w = min(before_gray.shape[1], after_gray.shape[1])
            before_gray = cv2.resize(before_gray, (w, h))
            after_gray = cv2.resize(after_gray, (w, h))

        # Calculate absolute difference
        diff = cv2.absdiff(before_gray, after_gray)

        # Threshold to binary
        _, thresh = cv2.threshold(diff, 30, 255, cv2.THRESH_BINARY)

        # Find contours
        contours, _ = cv2.findContours(
            thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )

        # Get bounding rectangles for significant contours
        regions = []
        for contour in contours:
            area = cv2.contourArea(contour)
            if area >= min_area:
                x, y, w, h = cv2.boundingRect(contour)
                regions.append((x, y, w, h))

        return regions

    def take_screenshot(
        self,
        output_path: Union[str, Path],
        region: Optional[Tuple[int, int, int, int]] = None,
    ) -> str:
        """
        Take a screenshot.

        Args:
            output_path: Path to save the screenshot
            region: Optional region (x, y, width, height)

        Returns:
            Path to the saved screenshot
        """
        self._ensure_pyautogui()
        pyautogui = self._pyautogui

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if region:
            screenshot = pyautogui.screenshot(region=region)
        else:
            screenshot = pyautogui.screenshot()

        screenshot.save(str(output_path))
        logger.debug(f"Screenshot saved: {output_path}")

        return str(output_path)

    def generate_diff_image(
        self,
        before_path: Union[str, Path],
        after_path: Union[str, Path],
        output_path: Union[str, Path],
    ) -> str:
        """
        Generate a visual diff image.

        Creates an image showing:
        - Red overlay on removed regions
        - Green overlay on added regions
        - Side-by-side comparison

        Args:
            before_path: Path to the "before" screenshot
            after_path: Path to the "after" screenshot
            output_path: Path to save the diff image

        Returns:
            Path to the saved diff image
        """
        self._ensure_imports()
        cv2 = self._cv2
        np = self._np

        before_path = Path(before_path)
        after_path = Path(after_path)
        output_path = Path(output_path)

        # Load images
        before_img = cv2.imread(str(before_path))
        after_img = cv2.imread(str(after_path))

        if before_img is None or after_img is None:
            raise ScreenshotComparisonError("Failed to load images for diff")

        # Ensure same size
        h = max(before_img.shape[0], after_img.shape[0])
        w = max(before_img.shape[1], after_img.shape[1])

        # Pad images to same size
        before_padded = np.zeros((h, w, 3), dtype=np.uint8)
        after_padded = np.zeros((h, w, 3), dtype=np.uint8)

        before_padded[:before_img.shape[0], :before_img.shape[1]] = before_img
        after_padded[:after_img.shape[0], :after_img.shape[1]] = after_img

        # Calculate difference
        diff = cv2.absdiff(before_padded, after_padded)
        diff_gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
        _, mask = cv2.threshold(diff_gray, 30, 255, cv2.THRESH_BINARY)

        # Create colored diff overlay
        diff_colored = after_padded.copy()
        diff_colored[mask > 0] = [0, 0, 255]  # Red for differences

        # Create side-by-side comparison
        combined = np.hstack([before_padded, diff_colored, after_padded])

        # Add labels
        font = cv2.FONT_HERSHEY_SIMPLEX
        cv2.putText(combined, "Before", (10, 30), font, 1, (255, 255, 255), 2)
        cv2.putText(combined, "Diff", (w + 10, 30), font, 1, (255, 255, 255), 2)
        cv2.putText(combined, "After", (2 * w + 10, 30), font, 1, (255, 255, 255), 2)

        # Save
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(output_path), combined)

        logger.debug(f"Diff image saved: {output_path}")
        return str(output_path)

    def take_and_compare(
        self,
        baseline_path: Union[str, Path],
        threshold: float = 0.95,
        region: Optional[Tuple[int, int, int, int]] = None,
    ) -> ComparisonResult:
        """
        Take a screenshot and compare with baseline.

        Args:
            baseline_path: Path to baseline screenshot
            threshold: Similarity threshold
            region: Optional region to capture

        Returns:
            ComparisonResult
        """
        # Generate unique filename for current screenshot
        timestamp = int(time.time() * 1000)
        current_path = self.screenshot_dir / f"current_{timestamp}.png"

        self.take_screenshot(current_path, region)
        return self.compare(baseline_path, current_path, threshold)

    def wait_for_ui_change(
        self,
        baseline_path: Optional[Union[str, Path]] = None,
        timeout: float = 30.0,
        poll_interval: float = 0.5,
        threshold: float = 0.95,
        region: Optional[Tuple[int, int, int, int]] = None,
        min_stable_time: float = 0.3,
    ) -> Tuple[bool, float]:
        """
        Wait for UI to change from baseline.

        This is more reliable than fixed timeouts because it detects
        actual UI changes rather than guessing how long to wait.

        Args:
            baseline_path: Path to baseline screenshot. If None, takes one now.
            timeout: Maximum wait time in seconds
            poll_interval: Time between checks in seconds
            threshold: Similarity threshold (lower = more sensitive to changes)
            region: Optional region to monitor
            min_stable_time: Minimum time UI must be stable after change

        Returns:
            Tuple of (changed, elapsed_time)
        """
        self._ensure_imports()
        self._ensure_pyautogui()

        start_time = time.time()

        # Take baseline if not provided
        if baseline_path is None:
            baseline_path = self.screenshot_dir / f"baseline_{int(start_time * 1000)}.png"
            self.take_screenshot(baseline_path, region)

        baseline_path = Path(baseline_path)
        last_change_time = None
        change_detected = False

        logger.debug(f"Waiting for UI change (timeout={timeout}s, threshold={threshold})")

        while time.time() - start_time < timeout:
            # Take current screenshot
            current_path = self.screenshot_dir / f"poll_{int(time.time() * 1000)}.png"
            self.take_screenshot(current_path, region)

            # Compare with baseline
            try:
                result = self.compare(baseline_path, current_path, threshold)

                if result.is_different:
                    if not change_detected:
                        change_detected = True
                        last_change_time = time.time()
                        logger.debug(f"UI change detected (similarity={result.similarity_score:.2%})")

                    # Check if UI has been stable for min_stable_time
                    if time.time() - last_change_time >= min_stable_time:
                        elapsed = time.time() - start_time
                        logger.info(f"UI change confirmed after {elapsed:.2f}s")
                        self._cleanup_temp_screenshot(current_path)
                        return True, elapsed
                else:
                    # UI reverted or still same - reset change detection
                    if change_detected:
                        logger.debug("UI reverted, continuing to wait...")
                    change_detected = False
                    last_change_time = None

            except Exception as e:
                logger.warning(f"Comparison failed: {e}")

            finally:
                self._cleanup_temp_screenshot(current_path)

            time.sleep(poll_interval)

        elapsed = time.time() - start_time
        logger.warning(f"Timeout waiting for UI change after {elapsed:.2f}s")
        return False, elapsed

    def _cleanup_temp_screenshot(self, path: Path):
        """Clean up temporary screenshot file"""
        try:
            if path.exists():
                path.unlink()
        except Exception:
            pass

    def capture_and_wait_for_change(
        self,
        action_callback,
        timeout: float = 10.0,
        poll_interval: float = 0.3,
        threshold: float = 0.90,
        region: Optional[Tuple[int, int, int, int]] = None,
        min_stable_time: float = 0.2,
    ) -> Tuple[bool, float]:
        """
        Capture baseline, perform action, then wait for UI to change.

        This is the primary method for detecting UI responses.
        Uses an optimized algorithm:
        1. Take baseline screenshot
        2. Perform action
        3. Immediately take screenshot and compare (no delay)
        4. If not changed, poll every 0.3s until change detected or timeout
        5. After change detected, wait for UI to stabilize

        Usage:
            def click_button():
                button.click()

            changed, elapsed = comparator.capture_and_wait_for_change(
                click_button,
                timeout=10.0
            )

        Args:
            action_callback: Function to call that triggers UI change
            timeout: Maximum wait time (default: 10s)
            poll_interval: Time between checks (default: 0.3s)
            threshold: Similarity threshold (default: 0.90, lower = more sensitive)
            region: Optional region to monitor
            min_stable_time: Minimum time UI must be stable after change (default: 0.2s)

        Returns:
            Tuple of (changed, elapsed_time)
        """
        self._ensure_imports()
        self._ensure_pyautogui()

        start_time = time.time()

        # Take baseline screenshot
        baseline_path = self.screenshot_dir / f"baseline_{int(time.time() * 1000)}.png"
        self.take_screenshot(baseline_path, region)

        # Perform the action
        try:
            action_callback()
        except Exception as e:
            logger.error(f"Action failed: {e}")
            self._cleanup_temp_screenshot(baseline_path)
            raise

        # IMMEDIATE comparison - no delay, check right after action
        immediate_path = self.screenshot_dir / f"immediate_{int(time.time() * 1000)}.png"
        self.take_screenshot(immediate_path, region)

        try:
            result = self.compare(baseline_path, immediate_path, threshold)
            if result.is_different:
                # Change detected immediately!
                elapsed = time.time() - start_time
                logger.debug(f"UI change detected immediately (similarity={result.similarity_score:.2%})")

                # Wait for UI to stabilize briefly
                time.sleep(min_stable_time)

                logger.info(f"UI change confirmed after {elapsed:.2f}s (immediate detection)")
                return True, elapsed
        except Exception as e:
            logger.warning(f"Immediate comparison failed: {e}")
        finally:
            self._cleanup_temp_screenshot(immediate_path)

        # POLLING LOOP - check periodically until change detected or timeout
        change_detected = False
        last_change_time = None

        while time.time() - start_time < timeout:
            time.sleep(poll_interval)

            # Take current screenshot
            current_path = self.screenshot_dir / f"poll_{int(time.time() * 1000)}.png"
            self.take_screenshot(current_path, region)

            try:
                result = self.compare(baseline_path, current_path, threshold)

                if result.is_different:
                    if not change_detected:
                        change_detected = True
                        last_change_time = time.time()
                        logger.debug(f"UI change detected (similarity={result.similarity_score:.2%})")

                    # Check if UI has been stable for min_stable_time
                    if time.time() - last_change_time >= min_stable_time:
                        elapsed = time.time() - start_time
                        logger.info(f"UI change confirmed after {elapsed:.2f}s")
                        self._cleanup_temp_screenshot(current_path)
                        self._cleanup_temp_screenshot(baseline_path)
                        return True, elapsed
                else:
                    # UI same as baseline or reverted
                    if change_detected:
                        logger.debug("UI appears to have reverted, continuing to monitor...")
                    change_detected = False
                    last_change_time = None

            except Exception as e:
                logger.warning(f"Comparison failed: {e}")
            finally:
                self._cleanup_temp_screenshot(current_path)

        # Timeout reached
        elapsed = time.time() - start_time
        logger.warning(f"Timeout waiting for UI change after {elapsed:.2f}s")
        self._cleanup_temp_screenshot(baseline_path)
        return False, elapsed

    def wait_for_ui_stable(
        self,
        timeout: float = 30.0,
        poll_interval: float = 0.5,
        stable_duration: float = 1.0,
        threshold: float = 0.98,
        region: Optional[Tuple[int, int, int, int]] = None,
    ) -> Tuple[bool, float]:
        """
        Wait for UI to become stable (stop changing).

        Useful for waiting for loading animations to complete.

        Args:
            timeout: Maximum wait time
            poll_interval: Time between checks
            stable_duration: How long UI must be unchanged to be considered stable
            threshold: Similarity threshold for "same"
            region: Optional region to monitor

        Returns:
            Tuple of (is_stable, elapsed_time)
        """
        self._ensure_imports()
        self._ensure_pyautogui()

        start_time = time.time()
        last_screenshot_path = None
        stable_start_time = None

        logger.debug(f"Waiting for UI to stabilize (timeout={timeout}s)")

        while time.time() - start_time < timeout:
            # Take current screenshot
            current_path = self.screenshot_dir / f"stable_{int(time.time() * 1000)}.png"
            self.take_screenshot(current_path, region)

            if last_screenshot_path is not None:
                try:
                    result = self.compare(last_screenshot_path, current_path, threshold)

                    if result.is_same:
                        # UI is same as last check
                        if stable_start_time is None:
                            stable_start_time = time.time()
                            logger.debug("UI appears stable, monitoring...")

                        # Check if stable for long enough
                        if time.time() - stable_start_time >= stable_duration:
                            elapsed = time.time() - start_time
                            logger.info(f"UI stabilized after {elapsed:.2f}s")
                            self._cleanup_temp_screenshot(current_path)
                            self._cleanup_temp_screenshot(last_screenshot_path)
                            return True, elapsed
                    else:
                        # UI changed
                        stable_start_time = None
                        logger.debug(f"UI still changing (similarity={result.similarity_score:.2%})")

                except Exception as e:
                    logger.warning(f"Comparison failed: {e}")
                    stable_start_time = None

                finally:
                    self._cleanup_temp_screenshot(last_screenshot_path)

            last_screenshot_path = current_path
            time.sleep(poll_interval)

        # Cleanup
        if last_screenshot_path:
            self._cleanup_temp_screenshot(last_screenshot_path)

        elapsed = time.time() - start_time
        logger.warning(f"Timeout waiting for UI to stabilize after {elapsed:.2f}s")
        return False, elapsed
