# Virtual J2534 DLL

将 J2534 API 调用转发到 VCI Proxy 服务器的虚拟 DLL。

## 架构

```
GDS2 (云端)
    ↓
virtual_j2534.dll  ──TCP──▶  reverse_server.py (localhost:9001)
                                    ↑
                               reverse_client.py (本地)
                                    ↓
                              真实 J2534 设备
```

## 编译

### 方法 1: Visual Studio (推荐)

1. 安装 Visual Studio 2019/2022 (含 C++ 桌面开发工具)
2. 打开 "Developer Command Prompt for VS 2022" (必须是 x86 版本)
3. 进入此目录并运行:
   ```cmd
   build_msvc.bat
   ```

### 方法 2: MinGW 32-bit

```bash
i686-w64-mingw32-gcc -shared -o virtual_j2534.dll virtual_j2534.c -lws2_32
```

## 安装

### 1. 复制 DLL

将 `virtual_j2534.dll` 复制到:
```
C:\Program Files (x86)\VCI_Proxy\virtual_j2534.dll
```

### 2. 注册 J2534 设备

创建注册表项 (以管理员身份运行):

```reg
Windows Registry Editor Version 5.00

[HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\PassThruSupport.04.04\VCI Proxy]
"Name"="VCI Proxy (Remote)"
"Vendor"="VCI Proxy"
"ConfigApplication"=""
"FunctionLibrary"="C:\\Program Files (x86)\\VCI_Proxy\\virtual_j2534.dll"
"CAN"=dword:00000001
"ISO15765"=dword:00000001
"ISO14230"=dword:00000001
"ISO9141"=dword:00000001
"J1850PWM"=dword:00000001
"J1850VPW"=dword:00000001
```

保存为 `register_vci_proxy.reg` 并双击导入。

### 3. 配置服务器地址

默认连接 `127.0.0.1:9001`。如需修改，编辑 `virtual_j2534.c` 中的:
```c
static const char* SERVER_HOST = "127.0.0.1";
static const int SERVER_PORT = 9001;
```

## 使用

1. 在云端运行 `reverse_server.py`:
   ```
   python reverse_server.py
   ```

2. 在本地运行 `reverse_client.py`:
   ```
   python reverse_client.py --host <云端IP> --port 9000
   ```

3. 启动 GDS2，选择 "VCI Proxy (Remote)" 作为设备

## 已实现的 API

| API | 状态 |
|-----|------|
| PassThruOpen | ✅ 完成 |
| PassThruClose | ✅ 完成 |
| PassThruConnect | ✅ 完成 |
| PassThruDisconnect | ✅ 完成 |
| PassThruReadMsgs | ✅ 完成 |
| PassThruWriteMsgs | ✅ 完成 |
| PassThruReadVersion | ✅ 完成 |
| PassThruGetLastError | ✅ 完成 |
| PassThruStartMsgFilter | ⚠️ 存根 |
| PassThruStopMsgFilter | ⚠️ 存根 |
| PassThruIoctl | ⚠️ 部分 |
| PassThruStartPeriodicMsg | ❌ 未实现 |
| PassThruStopPeriodicMsg | ❌ 未实现 |
| PassThruSetProgrammingVoltage | ❌ 未实现 |

## 测试

使用 `test_client.py` 测试连接:
```
python test_client.py --host localhost --port 9001
```
