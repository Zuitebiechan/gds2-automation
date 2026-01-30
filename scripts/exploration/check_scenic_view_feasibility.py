"""
Scenic View Feasibility Check for GDS2

Tests whether Scenic View can be used to inspect GDS2 JavaFX application.
"""

import psutil
import subprocess
import os
from pathlib import Path


def find_gds2_process():
    """Find GDS2 Java process."""
    print("=" * 60)
    print("Step 1: Finding GDS2 Process")
    print("=" * 60)

    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            cmdline = proc.info['cmdline']
            if not cmdline:
                continue

            cmdline_str = ' '.join(cmdline)

            # Check if it's a Java process with GDS2
            if 'java' in proc.info['name'].lower() or 'javaw' in proc.info['name'].lower():
                if 'GDS' in cmdline_str or 'gds' in cmdline_str.lower():
                    print(f"\n[OK] Found GDS2 Process:")
                    print(f"  PID: {proc.info['pid']}")
                    print(f"  Name: {proc.info['name']}")
                    print(f"\n  Command Line:")
                    for arg in cmdline[:10]:  # First 10 args
                        print(f"    {arg}")
                    if len(cmdline) > 10:
                        print(f"    ... and {len(cmdline) - 10} more arguments")
                    return proc.info['pid'], cmdline
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    print("\n[ERROR] GDS2 process not found!")
    print("  Make sure GDS2 is running.")
    return None, None


def check_java_version(cmdline):
    """Extract and check Java version from command line."""
    print("\n" + "=" * 60)
    print("Step 2: Checking Java Version")
    print("=" * 60)

    # Find java executable path
    java_exe = None
    for arg in cmdline:
        if 'java' in arg.lower() and arg.endswith('.exe'):
            java_exe = arg
            break

    if not java_exe:
        # Try first argument
        java_exe = cmdline[0] if cmdline else None

    if java_exe and os.path.exists(java_exe):
        print(f"\nJava Executable: {java_exe}")
        try:
            result = subprocess.run(
                [java_exe, '-version'],
                capture_output=True,
                text=True,
                timeout=5
            )
            version_output = result.stderr if result.stderr else result.stdout
            print(f"\nJava Version:\n{version_output}")

            # Check for JavaFX
            if 'javafx' in version_output.lower():
                print("\n[OK] JavaFX detected in Java version")
            else:
                print("\n[WARN] JavaFX not explicitly mentioned (might be bundled)")

            return True
        except Exception as e:
            print(f"\n[ERROR] Failed to get Java version: {e}")
    else:
        print(f"\n[ERROR] Java executable not found or inaccessible")

    return False


def check_jvm_arguments(cmdline):
    """Check JVM arguments for agent loading capability."""
    print("\n" + "=" * 60)
    print("Step 3: Checking JVM Arguments")
    print("=" * 60)

    cmdline_str = ' '.join(cmdline)

    # Check for relevant JVM arguments
    checks = {
        "Agent Already Loaded": "-javaagent:" in cmdline_str,
        "Dynamic Agent Loading": "-XX:+EnableDynamicAgentLoading" in cmdline_str,
        "Attach Mechanism Disabled": "-XX:+DisableAttachMechanism" in cmdline_str,
        "JavaFX Application": "--module-path" in cmdline_str and "javafx" in cmdline_str.lower(),
    }

    print("\nJVM Configuration:")
    for check, result in checks.items():
        symbol = "[OK]" if result else "[X]"
        print(f"  {symbol} {check}: {result}")

    # Extract -D properties
    print("\nSystem Properties (-D flags):")
    d_props = [arg for arg in cmdline if arg.startswith('-D')]
    if d_props:
        for prop in d_props[:5]:
            print(f"  {prop}")
        if len(d_props) > 5:
            print(f"  ... and {len(d_props) - 5} more")
    else:
        print("  (none found)")

    return checks


def check_scenic_view_requirements():
    """Check if Scenic View can be used."""
    print("\n" + "=" * 60)
    print("Step 4: Scenic View Feasibility Analysis")
    print("=" * 60)

    print("\nScenic View Requirements:")
    print("  1. JavaFX application: [OK] (GDS2 is JavaFX)")
    print("  2. Agent injection method:")
    print("     - Option A: Modify startup command (add -javaagent)")
    print("     - Option B: Dynamic attach (Java 9+)")
    print("  3. No -XX:+DisableAttachMechanism flag")

    print("\nNext Steps:")
    print("\n[LIST] Method 1: Modify GDS2 Startup (Recommended)")
    print("  1. Find GDS2 launcher (shortcut or script)")
    print("  2. Download Scenic View JAR:")
    print("     https://github.com/JonathanGiles/scenic-view/releases")
    print("  3. Modify launch command to add:")
    print("     -javaagent:path\\to\\scenic-view.jar")
    print("  4. Restart GDS2")
    print("  5. Scenic View window should appear automatically")

    print("\n[TOOL] Method 2: Dynamic Attach (Advanced)")
    print("  1. Only works if Java 9+ with EnableDynamicAgentLoading")
    print("  2. Requires custom Java agent to attach at runtime")
    print("  3. More complex, less reliable")

    print("\n[WARN] Risks and Considerations:")
    print("  - GDS2 is commercial software - modifying launch may violate EULA")
    print("  - GDS2 might use a custom JRE that's hard to modify")
    print("  - Updates to GDS2 might reset your changes")
    print("  - Scenic View is primarily a debugging tool, not automation")


def main():
    """Main function."""
    print("\n" + "=" * 60)
    print("  GDS2 Scenic View Feasibility Check")
    print("=" * 60)

    # Step 1: Find GDS2 process
    pid, cmdline = find_gds2_process()
    if not pid:
        print("\n[WARN] Cannot proceed without running GDS2 process.")
        print("   Please start GDS2 and run this script again.")
        return

    # Step 2: Check Java version
    check_java_version(cmdline)

    # Step 3: Check JVM arguments
    jvm_checks = check_jvm_arguments(cmdline)

    # Step 4: Scenic View feasibility
    check_scenic_view_requirements()

    # Final verdict
    print("\n" + "=" * 60)
    print("Final Verdict")
    print("=" * 60)

    if jvm_checks.get("Agent Already Loaded"):
        print("\n[OK] Agent can be loaded (already has -javaagent)")
        print("  Feasibility: POSSIBLE (but need startup modification)")
    elif jvm_checks.get("Attach Mechanism Disabled"):
        print("\n[X] Attach mechanism disabled")
        print("  Feasibility: VERY LOW")
    else:
        print("\n[WARN]  No agent loaded, attach not explicitly disabled")
        print("  Feasibility: MEDIUM (requires startup modification)")

    print("\n[TIP] Recommendation:")
    print("   Try Method 5 (pywinauto direct read) first!")
    print("   It's simpler and less invasive than Scenic View.")

    print("\n" + "=" * 60)


if __name__ == '__main__':
    main()
