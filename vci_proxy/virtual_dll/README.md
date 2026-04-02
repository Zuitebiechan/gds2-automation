# Virtual J2534 DLL

This README is scoped only to the Windows virtual DLL component under `vci_proxy/virtual_dll/`.

For the full tunnel and local/cloud architecture, read:

- `../../agent_docs/ops/vci_proxy_and_tunnel.md`
- `../../agent_docs/ops/deployment_and_operations.md`

## Purpose

`virtual_j2534.dll` is the cloud-side J2534 shim used by OEM software such as GDS2.

Its job is to:

- expose a standard J2534 DLL to the OEM software
- forward J2534 requests to the local proxy server on `localhost:9001`
- let the cloud-side diagnostics software talk to hardware that actually exists on the local side

## Component Boundary

The DLL does not implement vehicle communication itself.

Instead, the data path is:

1. cloud OEM software loads `virtual_j2534.dll`
2. the DLL forwards J2534 calls to `reverse_server.py` on `localhost:9001`
3. `reverse_server.py` bridges those calls over the reverse tunnel
4. `reverse_client.py` executes the real J2534 calls against the local device

## Build

### Visual Studio (recommended)

From an x86 Developer Command Prompt:

```cmd
build_msvc.bat
```

### MinGW 32-bit

```bash
i686-w64-mingw32-gcc -shared -o virtual_j2534.dll virtual_j2534.c -lws2_32
```

## Install

### 1. Copy the DLL

Example destination:

```text
C:\Program Files (x86)\VCI_Proxy\virtual_j2534.dll
```

### 2. Register the J2534 device

Use `register_vci_proxy.reg`, or create the equivalent registry entry under:

```text
HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\PassThruSupport.04.04\VCI Proxy
```

Important values:

- `Name` = `VCI Proxy (Remote)`
- `FunctionLibrary` = full path to `virtual_j2534.dll`

### 3. Match the local proxy listener

The DLL forwards to `127.0.0.1:9001` by default.

If you change that, update `virtual_j2534.c` accordingly.

## Typical Usage

1. Start the cloud reverse server:

   ```bash
   python -m vci_proxy.reverse_server
   ```

2. Start the local reverse client and connect it to the cloud listener.
3. Launch the OEM software on the cloud machine.
4. In the OEM software, select `VCI Proxy (Remote)` as the J2534 device.

## Supported J2534 Coverage

Current implementation status in this component:

- implemented:
  - `PassThruOpen`
  - `PassThruClose`
  - `PassThruConnect`
  - `PassThruDisconnect`
  - `PassThruReadMsgs`
  - `PassThruWriteMsgs`
  - `PassThruReadVersion`
  - `PassThruGetLastError`
- partial:
  - `PassThruStartMsgFilter`
  - `PassThruStopMsgFilter`
  - `PassThruIoctl`
- not implemented:
  - `PassThruStartPeriodicMsg`
  - `PassThruStopPeriodicMsg`
  - `PassThruSetProgrammingVoltage`

For cache behavior, authentication, and tunnel-quality monitoring around these calls, read `../../agent_docs/ops/vci_proxy_and_tunnel.md`.
