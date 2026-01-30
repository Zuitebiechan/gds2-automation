#!/usr/bin/env python
"""
Investigate Device Explorer process.

Checks:
1. Process IDs and relationships
2. Window handles
3. If we can control it via Windows API
"""

import subprocess
import ctypes
from ctypes import wintypes
import time

# Windows API
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# Callback type for EnumWindows
EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

def get_window_text(hwnd):
    """Get window title."""
    length = user32.GetWindowTextLengthW(hwnd) + 1
    buffer = ctypes.create_unicode_buffer(length)
    user32.GetWindowTextW(hwnd, buffer, length)
    return buffer.value

def get_window_class(hwnd):
    """Get window class name."""
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value

def get_window_pid(hwnd):
    """Get process ID of window."""
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value

def enum_all_windows():
    """Enumerate all windows."""
    windows = []

    def callback(hwnd, lparam):
        if user32.IsWindowVisible(hwnd):
            title = get_window_text(hwnd)
            if title:  # Only windows with titles
                windows.append({
                    'hwnd': hwnd,
                    'title': title,
                    'class': get_window_class(hwnd),
                    'pid': get_window_pid(hwnd),
                })
        return True

    user32.EnumWindows(EnumWindowsProc(callback), 0)
    return windows

def find_child_windows(parent_hwnd):
    """Find child windows of a parent."""
    children = []

    def callback(hwnd, lparam):
        title = get_window_text(hwnd)
        children.append({
            'hwnd': hwnd,
            'title': title,
            'class': get_window_class(hwnd),
        })
        return True

    user32.EnumChildWindows(parent_hwnd, EnumWindowsProc(callback), 0)
    return children

print("=" * 70)
print("Device Explorer Investigation")
print("=" * 70)

# 1. Find Java processes using tasklist
print("\n[1] Java Processes (tasklist)")
print("-" * 50)
result = subprocess.run(
    ['tasklist', '/FI', 'IMAGENAME eq java*', '/V', '/FO', 'LIST'],
    capture_output=True, text=True, encoding='gbk'
)
print(result.stdout[:2000] if result.stdout else "No Java processes found")

# 2. Find GDS2 and Device Explorer windows
print("\n[2] Windows with 'GDS' or 'Device Explorer' in title")
print("-" * 50)
windows = enum_all_windows()
gds_windows = []
for w in windows:
    if 'GDS' in w['title'] or 'Device' in w['title'] or 'Explorer' in w['title']:
        print(f"  HWND: {w['hwnd']}")
        print(f"  Title: {w['title']}")
        print(f"  Class: {w['class']}")
        print(f"  PID: {w['pid']}")
        print()
        gds_windows.append(w)

# 3. Check if Device Explorer and GDS2 share the same PID
print("\n[3] Process ID Analysis")
print("-" * 50)
pids = set(w['pid'] for w in gds_windows)
print(f"  Unique PIDs: {pids}")
if len(pids) == 1:
    print("  >> Same PID! Device Explorer is in SAME JVM as GDS2")
    print("  >> Java Agent SHOULD be able to control it")
elif len(pids) > 1:
    print("  >> Different PIDs! Device Explorer is SEPARATE process")
    print("  >> Java Agent CANNOT control it directly")

# 4. Check window class - JavaFX vs Swing vs Native
print("\n[4] Window Technology Detection")
print("-" * 50)
for w in gds_windows:
    wclass = w['class']
    if 'GlassWndClass' in wclass:
        tech = "JavaFX (Glass)"
    elif 'SunAwt' in wclass:
        tech = "Swing/AWT"
    elif 'GLFW' in wclass:
        tech = "GLFW (Native)"
    else:
        tech = f"Unknown ({wclass})"
    print(f"  {w['title']}: {tech}")

# 5. If Device Explorer found, get its child controls
print("\n[5] Device Explorer Child Windows/Controls")
print("-" * 50)
for w in gds_windows:
    if 'Device Explorer' in w['title']:
        print(f"  Parent: {w['title']} (HWND={w['hwnd']})")
        children = find_child_windows(w['hwnd'])
        print(f"  Found {len(children)} child controls:")
        for i, child in enumerate(children[:20]):
            if child['title'] or child['class']:
                print(f"    [{i}] Class: {child['class']}, Text: '{child['title']}'")

# 6. Conclusion and recommendations
print("\n" + "=" * 70)
print("CONCLUSION")
print("=" * 70)
