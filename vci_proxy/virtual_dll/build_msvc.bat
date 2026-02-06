@echo off
REM Build Virtual J2534 DLL using MSVC (32-bit)
REM
REM Prerequisites:
REM   1. Visual Studio with C++ build tools installed
REM   2. Run from "x86 Native Tools Command Prompt for VS 2022"
REM
REM Usage:
REM   build_msvc.bat

echo Building Virtual J2534 DLL (32-bit)...

REM Compile and link with DEF file to export functions correctly
cl /LD /O2 /W3 virtual_j2534.c /link ws2_32.lib /DEF:virtual_j2534.def /out:virtual_j2534.dll

if %ERRORLEVEL% EQU 0 (
    echo.
    echo Build successful!
    echo Output: virtual_j2534.dll
    echo.
    echo Next steps:
    echo   1. Copy virtual_j2534.dll to C:\Program Files (x86)\VCI_Proxy\
    echo   2. Run: regedit /s register_vci_proxy.reg
    echo   3. Start reverse_server.py on cloud
    echo   4. Connect reverse_client.py from local PC
    echo   5. Start GDS2 and select "VCI Proxy (Remote)"
) else (
    echo.
    echo Build failed!
)

pause
