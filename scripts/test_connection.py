#!/usr/bin/env python
"""
GDS2 连接测试脚本

验证基本的连接和操作是否正常工作。
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.utils.logger import setup_logging
from src.workers.gds2 import GDS2Worker


def test_connection():
    """测试连接"""
    setup_logging(level="DEBUG")

    config = {
        "backend": "uia",
        "default_timeout": 10,
    }

    print("Testing GDS2 connection...")

    try:
        with GDS2Worker(config) as worker:
            print("✓ Connection successful!")

            # 测试 is_ready
            if worker.is_ready():
                print("✓ Worker is ready!")
            else:
                print("✗ Worker not ready")

            # 截图测试
            try:
                screenshot_path = worker.take_screenshot("test_screenshot.png")
                print(f"✓ Screenshot saved: {screenshot_path}")
            except Exception as e:
                print(f"✗ Screenshot failed: {e}")

            # 打印控件树
            print("\nControl tree (first 3 levels):")
            try:
                worker.get_window_tree()
            except Exception as e:
                print(f"Error: {e}")

    except Exception as e:
        print(f"✗ Connection failed: {e}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(test_connection())
