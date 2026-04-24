---
name: build-vci-proxy-exe
description: Build the latest Windows VCI Proxy client executables for this repository. Use when the user asks to package the client as an exe, build the Windows client bundle, run the PyInstaller packaging flow, generate the latest VCI_Proxy_Client.exe, or rebuild the x86/x64 J2534 worker executables.
---

# Build VCI Proxy EXE

Build the Windows client bundle by using the repository's supported packaging path in `scripts/build_vci_proxy_client.ps1`.

## Workflow

1. Confirm the build target.
   - Default target is the full Windows bundle:
     - `dist/VCI_Proxy_Client/VCI_Proxy_Client.exe`
     - `dist/VCI_Proxy_Client/workers/VCI_Proxy_J2534_Worker_x86.exe`
     - `dist/VCI_Proxy_Client/workers/VCI_Proxy_J2534_Worker_x64.exe`
   - If the user wants only a specific worker or a zip, say so explicitly in the run summary.

2. Check prerequisites before building.
   - Read `scripts/build_vci_proxy_client.ps1` if the build behavior is in doubt.
   - Ensure an x86 Python is available for the x86 worker and an x64 Python is available for the x64 worker and main client.
   - Ensure both interpreters can run `python -m PyInstaller --version`.
   - Ensure client build dependencies from `requirements-client.txt` exist in the relevant environments.

3. Use the repo-supported build command.
   - Preferred:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\build_vci_proxy_client.ps1 -Clean
   ```
   - If auto-detection fails, pass explicit interpreters:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\build_vci_proxy_client.ps1 `
     -Python32 "<x86-python>" `
     -Python64 "<x64-python>" `
     -Clean
   ```
   - Do not reimplement the packaging flow with ad hoc `pyinstaller` commands unless the script is broken and you are fixing it.

4. Resolve common failures directly.
   - If `py -0p` cannot find interpreters, rerun with explicit `-Python32` and `-Python64`.
   - If the x86 environment lacks `PyInstaller`, install it into that environment and retry.
   - If Pillow, `pystray`, or `requests` are missing, install `requirements-client.txt` into the affected environment and retry.
   - If the build fails due to stale output, rerun with `-Clean`.

5. Verify the output.
   - Check that these files exist after the build:
     - `dist/VCI_Proxy_Client/VCI_Proxy_Client.exe`
     - `dist/VCI_Proxy_Client/workers/VCI_Proxy_J2534_Worker_x86.exe`
     - `dist/VCI_Proxy_Client/workers/VCI_Proxy_J2534_Worker_x64.exe`
   - Report the final output directory and artifact timestamps or sizes when useful.

## Notes

- The build script is the source of truth for packaging behavior.
- The client packaging flow uses:
  - `pyinstaller_client.spec`
  - `pyinstaller_j2534_worker.spec`
- The final distributable directory is `dist/VCI_Proxy_Client`.
- If the user asks for a packaged zip, create it from `dist/VCI_Proxy_Client` after the exe build succeeds.
