"""
Screenshot Comparison Interface

Abstract base class for screenshot comparison implementations.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple, Union


@dataclass
class ComparisonResult:
    """Result of screenshot comparison"""
    is_different: bool
    similarity_score: float  # 0.0 to 1.0 (1.0 = identical)
    diff_regions: list  # List of (x, y, width, height) tuples for different regions
    before_path: str
    after_path: str
    diff_image_path: Optional[str] = None  # Path to diff visualization image

    @property
    def is_same(self) -> bool:
        """Returns True if screenshots are essentially the same"""
        return not self.is_different

    def __str__(self) -> str:
        status = "DIFFERENT" if self.is_different else "SAME"
        return f"ComparisonResult({status}, similarity={self.similarity_score:.2%})"


class ScreenshotComparator(ABC):
    """
    Abstract base class for screenshot comparison.

    Implementations can use different algorithms:
    - Histogram comparison
    - Structural similarity (SSIM)
    - Pixel-by-pixel comparison
    - Feature-based comparison
    """

    @abstractmethod
    def compare(
        self,
        before_path: Union[str, Path],
        after_path: Union[str, Path],
        threshold: float = 0.95,
    ) -> ComparisonResult:
        """
        Compare two screenshots.

        Args:
            before_path: Path to the "before" screenshot
            after_path: Path to the "after" screenshot
            threshold: Similarity threshold (0.0-1.0).
                       If similarity >= threshold, screenshots are considered the same.

        Returns:
            ComparisonResult with comparison details
        """
        pass

    @abstractmethod
    def take_screenshot(
        self,
        output_path: Union[str, Path],
        region: Optional[Tuple[int, int, int, int]] = None,
    ) -> str:
        """
        Take a screenshot.

        Args:
            output_path: Path to save the screenshot
            region: Optional region (x, y, width, height) to capture.
                    If None, captures the entire screen.

        Returns:
            Path to the saved screenshot
        """
        pass

    @abstractmethod
    def generate_diff_image(
        self,
        before_path: Union[str, Path],
        after_path: Union[str, Path],
        output_path: Union[str, Path],
    ) -> str:
        """
        Generate a visual diff image highlighting differences.

        Args:
            before_path: Path to the "before" screenshot
            after_path: Path to the "after" screenshot
            output_path: Path to save the diff image

        Returns:
            Path to the saved diff image
        """
        pass

    def compare_with_validation(
        self,
        before_path: Union[str, Path],
        after_path: Union[str, Path],
        threshold: float = 0.95,
        generate_diff: bool = False,
        diff_output_dir: Optional[Union[str, Path]] = None,
    ) -> ComparisonResult:
        """
        Compare screenshots and optionally generate diff visualization.

        Args:
            before_path: Path to the "before" screenshot
            after_path: Path to the "after" screenshot
            threshold: Similarity threshold
            generate_diff: Whether to generate a diff image
            diff_output_dir: Directory to save diff images

        Returns:
            ComparisonResult with comparison details
        """
        result = self.compare(before_path, after_path, threshold)

        if generate_diff and result.is_different and diff_output_dir:
            diff_dir = Path(diff_output_dir)
            diff_dir.mkdir(parents=True, exist_ok=True)

            # Generate unique filename
            before_name = Path(before_path).stem
            after_name = Path(after_path).stem
            diff_path = diff_dir / f"diff_{before_name}_{after_name}.png"

            result.diff_image_path = self.generate_diff_image(
                before_path, after_path, diff_path
            )

        return result
