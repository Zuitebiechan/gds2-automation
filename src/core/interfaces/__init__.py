"""
Core Interfaces for Vision Features

Provides abstract base classes for screenshot comparison.
"""

from .screenshot import ScreenshotComparator, ComparisonResult

__all__ = [
    "ScreenshotComparator",
    "ComparisonResult",
]
