#!/usr/bin/env python
"""
GDS2 UI 检查工具

用于分析 GDS2 的 UI 结构，帮助调试和配置 Selector。

使用方法：
    python scripts/inspect_gds2.py

功能：
1. 打印 GDS2 窗口的控件树
2. 帮助确定正确的 Selector
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from pywinauto import Application, Desktop
from pywinauto.findwindows import ElementNotFoundError


def find_gds2_windows():
    """查找所有可能是 GDS2 的窗口"""
    print("Searching for GDS2 windows...\n")

    desktop = Desktop(backend="uia")
    all_windows = desktop.windows()

    gds2_candidates = []
    for win in all_windows:
        try:
            title = win.window_text()
            if any(keyword in title.upper() for keyword in ["GDS", "DIAGNOSTIC", "GM"]):
                gds2_candidates.append(win)
                print(f"Found potential GDS2 window: '{title}'")
        except Exception:
            continue

    if not gds2_candidates:
        print("No GDS2 windows found. Make sure GDS2 is running.")
        print("\nAll visible windows:")
        for win in all_windows:
            try:
                title = win.window_text()
                if title.strip():
                    print(f"  - {title}")
            except:
                continue

    return gds2_candidates


def print_control_tree(window, max_depth=4):
    """打印控件树"""
    print("\n" + "=" * 60)
    print(f"Control Tree for: {window.window_text()}")
    print("=" * 60 + "\n")

    try:
        window.print_control_identifiers(depth=max_depth)
    except Exception as e:
        print(f"Error printing control tree: {e}")


def connect_and_inspect():
    """连接并检查 GDS2"""
    # 先尝试 UIA 后端
    print("Attempting to connect with UIA backend...")
    try:
        app = Application(backend="uia").connect(title_re=".*GDS.*", timeout=5)
        print("Connected with UIA backend!")
        main_window = app.top_window()
        print_control_tree(main_window)
        return
    except ElementNotFoundError:
        print("UIA connection failed.\n")

    # 尝试 Win32 后端
    print("Attempting to connect with Win32 backend...")
    try:
        app = Application(backend="win32").connect(title_re=".*GDS.*", timeout=5)
        print("Connected with Win32 backend!")
        main_window = app.top_window()
        print_control_tree(main_window)
        return
    except ElementNotFoundError:
        print("Win32 connection failed.\n")

    # 手动搜索
    find_gds2_windows()


def main():
    print("=" * 60)
    print("GDS2 UI Inspector")
    print("=" * 60)
    print("\nThis tool helps you understand GDS2's UI structure.")
    print("Make sure GDS2 is running before using this tool.\n")

    connect_and_inspect()

    print("\n" + "=" * 60)
    print("Tips for configuring Selectors:")
    print("=" * 60)
    print("""
1. Look for 'auto_id' values - they are the most reliable
2. If no auto_id, use 'control_type' + 'title' combination
3. For custom controls, you may need image-based locators
4. Use the control tree output to update src/workers/gds2/selectors.py
""")


if __name__ == "__main__":
    main()
