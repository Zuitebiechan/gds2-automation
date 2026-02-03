"""
Setup Scenic View for GDS2

This script helps you inject Scenic View into GDS2 to inspect JavaFX controls.
"""

import os
import winshell
import requests
from pathlib import Path
import subprocess
import sys


def find_gds2_shortcut():
    """Find GDS2 shortcut."""
    print("=" * 60)
    print("Step 1: Finding GDS2 Shortcut")
    print("=" * 60)

    shortcut_path = Path(r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\GDS 2\GDS 2.lnk")

    if not shortcut_path.exists():
        print(f"\n[ERROR] GDS2 shortcut not found at: {shortcut_path}")

        # Try desktop
        desktop = Path.home() / "Desktop" / "GDS 2.lnk"
        if desktop.exists():
            shortcut_path = desktop
            print(f"[OK] Found on Desktop: {desktop}")
        else:
            return None
    else:
        print(f"[OK] Found shortcut: {shortcut_path}")

    try:
        shortcut = winshell.shortcut(str(shortcut_path))
        print(f"\nShortcut Details:")
        print(f"  Target: {shortcut.path}")
        print(f"  Arguments: {shortcut.arguments}")
        print(f"  Working Directory: {shortcut.working_directory}")

        return {
            'path': str(shortcut_path),
            'target': shortcut.path,
            'arguments': shortcut.arguments,
            'working_dir': shortcut.working_directory
        }
    except Exception as e:
        print(f"\n[ERROR] Failed to read shortcut: {e}")
        return None


def download_scenic_view():
    """Download Scenic View JAR."""
    print("\n" + "=" * 60)
    print("Step 2: Getting Scenic View")
    print("=" * 60)

    tools_dir = Path(r"C:\tools")
    tools_dir.mkdir(exist_ok=True)

    scenic_view_jar = tools_dir / "scenic-view-8.8.0.jar"

    if scenic_view_jar.exists():
        print(f"\n[OK] Scenic View already exists: {scenic_view_jar}")
        return str(scenic_view_jar)

    print(f"\n[INFO] Scenic View needs to be downloaded manually.")
    print(f"Target location: {scenic_view_jar}")

    print("\n[DOWNLOAD INSTRUCTIONS]")
    print("  1. Open in browser:")
    print("     https://github.com/JonathanGiles/scenic-view/releases")
    print("  2. Find version 8.8.0 or latest Java 8 compatible version")
    print("  3. Download the JAR file")
    print(f"  4. Save it to: {scenic_view_jar}")

    print("\n[ALTERNATIVE] Try these direct links:")
    print("  - Maven Central:")
    print("    https://repo1.maven.org/maven2/org/scenicview/scenic-view/8.8.0/scenic-view-8.8.0.jar")
    print("  - Or search 'scenic view jar download' in Google")

    print("\n[WAITING] Please download the JAR file and save it to the location above.")
    print("          Then press ENTER to continue...")

    # Wait for file to exist (with timeout)
    max_wait = 300  # 5 minutes
    wait_time = 0
    while not scenic_view_jar.exists() and wait_time < max_wait:
        import time
        time.sleep(2)
        wait_time += 2

    if scenic_view_jar.exists():
        print(f"\n[OK] Scenic View found! Size: {scenic_view_jar.stat().st_size / 1024:.1f} KB")
        return str(scenic_view_jar)
    else:
        print("\n[ERROR] Scenic View not found. Cannot proceed.")
        print(f"Expected location: {scenic_view_jar}")
        return None


def create_startup_script(shortcut_info, scenic_view_jar):
    """Create modified startup script with Scenic View."""
    print("\n" + "=" * 60)
    print("Step 3: Creating Startup Script")
    print("=" * 60)

    # From our detection script, we know the command
    java_exe = r"C:\Program Files (x86)\GDS 2\jre6\bin\javaw.exe"
    working_dir = r"C:\Program Files (x86)\GDS 2"

    # Create batch file
    script_dir = Path(r"C:\tools")
    script_dir.mkdir(exist_ok=True)

    batch_file = script_dir / "GDS2_with_ScenicView.bat"

    # Build command with Scenic View agent
    batch_content = f'''@echo off
REM GDS2 Launcher with Scenic View
REM Created by setup_scenic_view.py

cd /d "{working_dir}"

echo Starting GDS2 with Scenic View...
echo Scenic View window should appear when GDS2 opens.
echo.

"{java_exe}" ^
  -javaagent:"{scenic_view_jar}" ^
  -Xms250m ^
  -Xmx768m ^
  -Dprism.order=sw ^
  -cp RTKApplet.jar;.\\itextpdf-5.5.13.1.jar;C:\\Program Files (x86)\\GM\\TIS2WebProxy\\dls-nativelibs.jar;C:\\Program Files (x86)\\GM\\TIS2WebProxy\\t2w-proxy.jar;C:\\Program Files (x86)\\GM\\TIS2WebProxy\\t2w-proxy-impl.jar;C:\\Program Files (x86)\\GM\\TIS2WebProxy\\jCookie-0.8c.jar;C:\\Program Files (x86)\\GM\\TIS2WebProxy\\jRegistryKey.jar;C:\\Program Files (x86)\\GM\\TIS2WebProxy\\log4j-1.2-api-2.17.1.jar;C:\\Program Files (x86)\\GM\\TIS2WebProxy\\scsm.jar;TeeChart.Swing.jar;TeeChart.SWT.jar;secdo-public-0.0.1.jar;common-framework-io-0.0.1.jar;commons-lang3-3.9.jar;hamcrest-core-1.3.jar;jackson-annotations-2.10.0.jar;jackson-core-2.10.0.jar;jackson-databind-2.10.0.jar;jackson-dataformat-xml-2.10.0.jar;jackson-module-jaxb-annotations-2.10.0.jar;json-simple-1.1.1.jar;junit-4.12.jar;log4j-api-2.17.1.jar;log4j-core-2.17.1.jar;mockito-all-1.10.19.jar;stax-api-1.0-2.jar;stax2-api-3.1.4.jar;woodstox-core-asl-4.4.1.jar;shared-data-analytics.jar;shared-data-base.jar;shared-data-dtc.jar;shared-data-gds2.jar;shared-data-generic.jar;shared-data-preferences.jar;shared-data-shell.jar;shared-data-vehicle.jar;shared-data-vin.jar;shared-data-system.jar;commons-collections4-4.4.jar;poi-4.1.1.jar;poi-ooxml-4.1.1.jar;poi-ooxml-schemas-4.1.1.jar;xmlbeans-2.6.0.jar; ^
  com.Mahle.Applets.RXMainFX

if errorlevel 1 (
    echo.
    echo [ERROR] GDS2 failed to start with Scenic View.
    echo Check if Scenic View JAR is compatible with Java 8.
    pause
)
'''

    with open(batch_file, 'w') as f:
        f.write(batch_content)

    print(f"\n[OK] Created startup script: {batch_file}")

    # Create shortcut to batch file
    desktop = Path.home() / "Desktop" / "GDS2 with Scenic View.lnk"

    try:
        shortcut = winshell.shortcut(str(desktop))
        shortcut.path = str(batch_file)
        shortcut.working_directory = str(script_dir)
        shortcut.description = "GDS2 with Scenic View JavaFX Inspector"
        shortcut.write()
        print(f"[OK] Created desktop shortcut: {desktop}")
    except Exception as e:
        print(f"[WARN] Could not create shortcut: {e}")
        print(f"       You can manually run: {batch_file}")

    return str(batch_file)


def main():
    """Main function."""
    print("\n" + "=" * 60)
    print("  GDS2 Scenic View Setup")
    print("=" * 60)

    print("\n[INFO] This script will:")
    print("  1. Find GDS2 shortcut")
    print("  2. Download Scenic View JAR (if needed)")
    print("  3. Create startup script with Scenic View agent")
    print("  4. Create desktop shortcut")

    # Check if requests is installed
    try:
        import requests
    except ImportError:
        print("\n[ERROR] 'requests' module not installed.")
        print("Install with: pip install requests")
        return

    # Check if winshell is installed
    try:
        import winshell
    except ImportError:
        print("\n[ERROR] 'winshell' module not installed.")
        print("Install with: pip install winshell")
        return

    # Auto-run mode
    print("\n[AUTO-RUN MODE] Proceeding with setup...")

    # Step 1: Find shortcut
    shortcut_info = find_gds2_shortcut()
    if not shortcut_info:
        print("\n[ERROR] Could not find GDS2 shortcut. Please find it manually.")
        return

    # Step 2: Download Scenic View
    scenic_view_jar = download_scenic_view()
    if not scenic_view_jar:
        print("\n[ERROR] Could not download Scenic View. Please download manually.")
        return

    # Step 3: Create startup script
    batch_file = create_startup_script(shortcut_info, scenic_view_jar)

    # Final instructions
    print("\n" + "=" * 60)
    print("Setup Complete!")
    print("=" * 60)

    print("\n[NEXT STEPS]")
    print("  1. Close GDS2 if it's currently running")
    print("  2. Double-click the desktop shortcut: 'GDS2 with Scenic View'")
    print("  3. Two windows should open:")
    print("     - GDS2 main application")
    print("     - Scenic View inspector window")
    print("  4. In GDS2, navigate to Data Display page")
    print("  5. In Scenic View, you can:")
    print("     - Browse the JavaFX scene graph")
    print("     - Inspect properties of UI elements")
    print("     - View the data table structure")

    print("\n[TIPS]")
    print("  - Scenic View shows a tree of all UI components")
    print("  - Look for TableView or ListView controls")
    print("  - You can see all rows, not just visible ones")
    print("  - Right-click controls to see their properties")

    print("\n[TROUBLESHOOTING]")
    print("  - If GDS2 doesn't start: Scenic View might not be Java 8 compatible")
    print("  - If Scenic View doesn't appear: Check console for errors")
    print("  - Alternative: Use the regular GDS2 shortcut to start normally")

    print("\n" + "=" * 60)

    # Auto-skip launch for now
    print("\n[INFO] Setup complete. You can manually run:")
    print(f"       {batch_file}")
    print("\nOr use the desktop shortcut: 'GDS2 with Scenic View'")


if __name__ == '__main__':
    main()
