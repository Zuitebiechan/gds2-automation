# J2534 网络代理架构设计

## 版本历史

| 版本 | 日期 | 作者 | 说明 |
|------|------|------|------|
| 1.0 | 2026-02-06 | Claude | 初始设计 |
| 1.1 | 2026-02-06 | Claude | 添加附录A：GDS2实际J2534接口分析（HardwareJ2534.class反编译、注册表分析、DLL结构） |

---

## 1. 概述

### 1.1 背景

USB over IP 技术将 USB 协议透传到网络上，导致每个 USB 数据包都需要一次网络往返。对于 J2534 车辆诊断协议，一次简单的读取操作可能涉及几十到几百个 USB 包，造成严重的延迟累积。

### 1.2 解决方案

将网络通信层从 USB 协议层提升到 J2534 API 层，大幅减少网络往返次数。

### 1.3 性能目标

| 指标 | USB over IP | J2534 网络代理 | 改善 |
|------|-------------|---------------|------|
| 读取 DTC | 6-12秒 | 300-500ms | 20倍 |
| 实时数据 | 断联 | 200-400ms/次 | 可用 |
| 网络往返/操作 | 50-200次 | 3-10次 | 10-50倍 |

---

## 2. 系统架构

### 2.1 整体架构图

```
┌─────────────────────────────────────────────────────────────────┐
│                         云服务器 (阿里云)                         │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │                        GDS2                              │   │
│  │                    (车辆诊断软件)                          │   │
│  └────────────────────────┬────────────────────────────────┘   │
│                           │ J2534 API 调用                      │
│                           ▼                                     │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │              Virtual J2534 Driver (虚拟驱动)              │   │
│  │                    virtual_j2534.dll                      │   │
│  │  - 实现标准 J2534 API 接口                                 │   │
│  │  - 将 API 调用序列化为网络消息                              │   │
│  │  - 通过 TCP 发送到 VCI Proxy                              │   │
│  └────────────────────────┬────────────────────────────────┘   │
│                           │ TCP (端口 9000)                     │
└───────────────────────────┼─────────────────────────────────────┘
                            │
                      ══════╪══════  互联网 (30ms 延迟)
                            │
┌───────────────────────────┼─────────────────────────────────────┐
│                           │              本地电脑                 │
│  ┌────────────────────────▼────────────────────────────────┐   │
│  │                    VCI Proxy                             │   │
│  │                  (Python 服务程序)                        │   │
│  │  - 监听 TCP 端口 9000                                     │   │
│  │  - 反序列化网络消息为 J2534 API 调用                        │   │
│  │  - 调用真实的 J2534 驱动                                   │   │
│  │  - 返回结果给云服务器                                      │   │
│  └────────────────────────┬────────────────────────────────┘   │
│                           │ J2534 API 调用 (本地, <1ms)         │
│                           ▼                                     │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │           Scanmatik J2534 Driver (真实驱动)               │   │
│  │                     smj2534.dll                           │   │
│  └────────────────────────┬────────────────────────────────┘   │
│                           │ USB (本地, <1ms)                    │
│                           ▼                                     │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │                    SM3 USB 设备                           │   │
│  └────────────────────────┬────────────────────────────────┘   │
│                           │ OBD-II 线缆                         │
└───────────────────────────┼─────────────────────────────────────┘
                            │
                            ▼
                    ┌───────────────┐
                    │     车辆       │
                    └───────────────┘
```

### 2.2 组件职责

| 组件 | 位置 | 职责 | 技术栈 |
|------|------|------|--------|
| GDS2 | 云服务器 | 车辆诊断软件，调用 J2534 API | Java (已有) |
| Virtual J2534 Driver | 云服务器 | 将 J2534 调用转发到网络 | C/C++ DLL |
| VCI Proxy | 本地电脑 | 接收网络请求，调用真实驱动 | Python |
| Scanmatik Driver | 本地电脑 | 真实的 J2534 驱动 | C (已有) |
| SM3 USB | 本地电脑 | 物理 VCI 设备 | 硬件 (已有) |

---

## 3. J2534 API 接口

### 3.1 需要实现的 J2534 函数

根据 SAE J2534-1 标准，需要实现以下核心函数：

| 函数 | 说明 | 网络调用频率 |
|------|------|-------------|
| `PassThruOpen` | 打开设备连接 | 低（初始化时） |
| `PassThruClose` | 关闭设备连接 | 低（结束时） |
| `PassThruConnect` | 建立协议通道 | 低 |
| `PassThruDisconnect` | 断开协议通道 | 低 |
| `PassThruReadMsgs` | 读取消息 | **高**（核心操作） |
| `PassThruWriteMsgs` | 发送消息 | **高**（核心操作） |
| `PassThruStartMsgFilter` | 设置消息过滤 | 低 |
| `PassThruStopMsgFilter` | 停止消息过滤 | 低 |
| `PassThruStartPeriodicMsg` | 启动周期消息 | 低 |
| `PassThruStopPeriodicMsg` | 停止周期消息 | 低 |
| `PassThruIoctl` | 设备控制 | 中 |
| `PassThruSetProgrammingVoltage` | 设置编程电压 | 低 |
| `PassThruReadVersion` | 读取版本信息 | 低 |
| `PassThruGetLastError` | 获取错误信息 | 低 |

### 3.2 J2534 数据结构

```c
// 消息结构
typedef struct {
    unsigned long ProtocolID;      // 协议类型
    unsigned long RxStatus;        // 接收状态
    unsigned long TxFlags;         // 发送标志
    unsigned long Timestamp;       // 时间戳
    unsigned long DataSize;        // 数据长度
    unsigned long ExtraDataIndex;  // 额外数据索引
    unsigned char Data[4128];      // 数据缓冲区
} PASSTHRU_MSG;

// 协议类型
#define J1850VPW        1
#define J1850PWM        2
#define ISO9141         3
#define ISO14230        4
#define CAN             5
#define ISO15765        6
#define SCI_A_ENGINE    7
#define SCI_A_TRANS     8
#define SCI_B_ENGINE    9
#define SCI_B_TRANS     10
```

---

## 4. 网络协议设计

### 4.1 传输层

- **协议**: TCP
- **端口**: 9000
- **编码**: 自定义二进制协议 (高效) 或 JSON (易调试)
- **连接**: 长连接，保持会话状态

### 4.2 消息格式

采用简单的 **请求-响应** 模式：

```
┌──────────────────────────────────────────────────────────┐
│                      Message Header                       │
├──────────┬──────────┬──────────┬─────────────────────────┤
│  Magic   │  Length  │  MsgType │       Sequence          │
│  4 bytes │  4 bytes │  2 bytes │        4 bytes          │
├──────────┴──────────┴──────────┴─────────────────────────┤
│                      Message Body                         │
│                    (Variable Length)                      │
└──────────────────────────────────────────────────────────┘

Magic:    0x4A325334 ("J254" in ASCII)
Length:   整个消息的长度（包括头部）
MsgType:  消息类型（见下表）
Sequence: 序列号，用于匹配请求和响应
```

### 4.3 消息类型

| MsgType | 名称 | 方向 | 说明 |
|---------|------|------|------|
| 0x0001 | OPEN_REQ | 云→本地 | PassThruOpen 请求 |
| 0x8001 | OPEN_RSP | 本地→云 | PassThruOpen 响应 |
| 0x0002 | CLOSE_REQ | 云→本地 | PassThruClose 请求 |
| 0x8002 | CLOSE_RSP | 本地→云 | PassThruClose 响应 |
| 0x0003 | CONNECT_REQ | 云→本地 | PassThruConnect 请求 |
| 0x8003 | CONNECT_RSP | 本地→云 | PassThruConnect 响应 |
| 0x0004 | DISCONNECT_REQ | 云→本地 | PassThruDisconnect 请求 |
| 0x8004 | DISCONNECT_RSP | 本地→云 | PassThruDisconnect 响应 |
| 0x0005 | READ_MSGS_REQ | 云→本地 | PassThruReadMsgs 请求 |
| 0x8005 | READ_MSGS_RSP | 本地→云 | PassThruReadMsgs 响应 |
| 0x0006 | WRITE_MSGS_REQ | 云→本地 | PassThruWriteMsgs 请求 |
| 0x8006 | WRITE_MSGS_RSP | 本地→云 | PassThruWriteMsgs 响应 |
| 0x0007 | IOCTL_REQ | 云→本地 | PassThruIoctl 请求 |
| 0x8007 | IOCTL_RSP | 本地→云 | PassThruIoctl 响应 |
| 0x0010 | START_FILTER_REQ | 云→本地 | PassThruStartMsgFilter 请求 |
| 0x8010 | START_FILTER_RSP | 本地→云 | PassThruStartMsgFilter 响应 |
| 0x0011 | STOP_FILTER_REQ | 云→本地 | PassThruStopMsgFilter 请求 |
| 0x8011 | STOP_FILTER_RSP | 本地→云 | PassThruStopMsgFilter 响应 |
| 0x00FF | HEARTBEAT | 双向 | 心跳保活 |
| 0x80FF | HEARTBEAT_ACK | 双向 | 心跳响应 |

### 4.4 消息体定义

#### PassThruOpen 请求 (0x0001)
```
┌─────────────────────────────────────┐
│  DeviceName (null-terminated string) │
└─────────────────────────────────────┘
```

#### PassThruOpen 响应 (0x8001)
```
┌──────────────┬──────────────┐
│   ReturnCode │   DeviceID   │
│   4 bytes    │   4 bytes    │
└──────────────┴──────────────┘
```

#### PassThruConnect 请求 (0x0003)
```
┌──────────────┬──────────────┬──────────────┬──────────────┐
│   DeviceID   │  ProtocolID  │    Flags     │   Baudrate   │
│   4 bytes    │   4 bytes    │   4 bytes    │   4 bytes    │
└──────────────┴──────────────┴──────────────┴──────────────┘
```

#### PassThruConnect 响应 (0x8003)
```
┌──────────────┬──────────────┐
│   ReturnCode │   ChannelID  │
│   4 bytes    │   4 bytes    │
└──────────────┴──────────────┘
```

#### PassThruReadMsgs 请求 (0x0005)
```
┌──────────────┬──────────────┬──────────────┐
│   ChannelID  │   NumMsgs    │   Timeout    │
│   4 bytes    │   4 bytes    │   4 bytes    │
└──────────────┴──────────────┴──────────────┘
```

#### PassThruReadMsgs 响应 (0x8005)
```
┌──────────────┬──────────────┬─────────────────────────────┐
│   ReturnCode │   NumMsgs    │   Messages (array)          │
│   4 bytes    │   4 bytes    │   Variable                  │
└──────────────┴──────────────┴─────────────────────────────┘

Each Message:
┌──────────────┬──────────────┬──────────────┬──────────────┐
│  ProtocolID  │   RxStatus   │   TxFlags    │  Timestamp   │
│   4 bytes    │   4 bytes    │   4 bytes    │   4 bytes    │
├──────────────┼──────────────┴──────────────┴──────────────┤
│   DataSize   │              Data[DataSize]                │
│   4 bytes    │              Variable                      │
└──────────────┴───────────────────────────────────────────┘
```

#### PassThruWriteMsgs 请求 (0x0006)
```
┌──────────────┬──────────────┬──────────────┬─────────────────┐
│   ChannelID  │   NumMsgs    │   Timeout    │ Messages (array)│
│   4 bytes    │   4 bytes    │   4 bytes    │   Variable      │
└──────────────┴──────────────┴──────────────┴─────────────────┘
```

#### PassThruWriteMsgs 响应 (0x8006)
```
┌──────────────┬──────────────┐
│   ReturnCode │   NumMsgs    │
│   4 bytes    │   4 bytes    │
└──────────────┴──────────────┘
```

---

## 5. VCI Proxy 设计（本地电脑）

### 5.1 模块结构

```
vci_proxy/
├── main.py                 # 入口点
├── config.py               # 配置管理
├── server.py               # TCP 服务器
├── j2534_wrapper.py        # J2534 DLL 封装
├── protocol.py             # 网络协议编解码
├── handlers.py             # 消息处理器
└── utils/
    ├── logger.py           # 日志
    └── errors.py           # 错误码定义
```

### 5.2 核心类设计

```python
# j2534_wrapper.py
from ctypes import *
from typing import Optional, Tuple, List

class J2534Error(Exception):
    """J2534 错误"""
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message

class PassThruMsg(Structure):
    """J2534 消息结构"""
    _fields_ = [
        ("ProtocolID", c_ulong),
        ("RxStatus", c_ulong),
        ("TxFlags", c_ulong),
        ("Timestamp", c_ulong),
        ("DataSize", c_ulong),
        ("ExtraDataIndex", c_ulong),
        ("Data", c_ubyte * 4128),
    ]

class J2534Driver:
    """J2534 驱动封装"""

    def __init__(self, dll_path: str):
        self.dll = CDLL(dll_path)
        self._setup_functions()

    def _setup_functions(self):
        """设置函数原型"""
        # PassThruOpen
        self.dll.PassThruOpen.argtypes = [c_void_p, POINTER(c_ulong)]
        self.dll.PassThruOpen.restype = c_long

        # PassThruClose
        self.dll.PassThruClose.argtypes = [c_ulong]
        self.dll.PassThruClose.restype = c_long

        # PassThruConnect
        self.dll.PassThruConnect.argtypes = [
            c_ulong, c_ulong, c_ulong, c_ulong, POINTER(c_ulong)
        ]
        self.dll.PassThruConnect.restype = c_long

        # PassThruDisconnect
        self.dll.PassThruDisconnect.argtypes = [c_ulong]
        self.dll.PassThruDisconnect.restype = c_long

        # PassThruReadMsgs
        self.dll.PassThruReadMsgs.argtypes = [
            c_ulong, POINTER(PassThruMsg), POINTER(c_ulong), c_ulong
        ]
        self.dll.PassThruReadMsgs.restype = c_long

        # PassThruWriteMsgs
        self.dll.PassThruWriteMsgs.argtypes = [
            c_ulong, POINTER(PassThruMsg), POINTER(c_ulong), c_ulong
        ]
        self.dll.PassThruWriteMsgs.restype = c_long

    def open(self, device_name: Optional[str] = None) -> Tuple[int, int]:
        """打开设备"""
        device_id = c_ulong()
        name = device_name.encode() if device_name else None
        ret = self.dll.PassThruOpen(name, byref(device_id))
        return ret, device_id.value

    def close(self, device_id: int) -> int:
        """关闭设备"""
        return self.dll.PassThruClose(device_id)

    def connect(self, device_id: int, protocol_id: int,
                flags: int, baudrate: int) -> Tuple[int, int]:
        """建立通道"""
        channel_id = c_ulong()
        ret = self.dll.PassThruConnect(
            device_id, protocol_id, flags, baudrate, byref(channel_id)
        )
        return ret, channel_id.value

    def disconnect(self, channel_id: int) -> int:
        """断开通道"""
        return self.dll.PassThruDisconnect(channel_id)

    def read_msgs(self, channel_id: int, num_msgs: int,
                  timeout: int) -> Tuple[int, List[PassThruMsg]]:
        """读取消息"""
        msgs = (PassThruMsg * num_msgs)()
        num = c_ulong(num_msgs)
        ret = self.dll.PassThruReadMsgs(channel_id, msgs, byref(num), timeout)
        return ret, list(msgs[:num.value])

    def write_msgs(self, channel_id: int, msgs: List[PassThruMsg],
                   timeout: int) -> Tuple[int, int]:
        """发送消息"""
        msg_array = (PassThruMsg * len(msgs))(*msgs)
        num = c_ulong(len(msgs))
        ret = self.dll.PassThruWriteMsgs(channel_id, msg_array, byref(num), timeout)
        return ret, num.value
```

```python
# server.py
import asyncio
import struct
from typing import Dict, Callable

MAGIC = 0x4A325334  # "J254"

class VCIProxyServer:
    """VCI Proxy TCP 服务器"""

    def __init__(self, host: str, port: int, j2534_driver: J2534Driver):
        self.host = host
        self.port = port
        self.driver = j2534_driver
        self.handlers: Dict[int, Callable] = {}
        self._setup_handlers()

    def _setup_handlers(self):
        """注册消息处理器"""
        self.handlers[0x0001] = self._handle_open
        self.handlers[0x0002] = self._handle_close
        self.handlers[0x0003] = self._handle_connect
        self.handlers[0x0004] = self._handle_disconnect
        self.handlers[0x0005] = self._handle_read_msgs
        self.handlers[0x0006] = self._handle_write_msgs
        self.handlers[0x0007] = self._handle_ioctl
        self.handlers[0x00FF] = self._handle_heartbeat

    async def start(self):
        """启动服务器"""
        server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        print(f"VCI Proxy listening on {self.host}:{self.port}")
        async with server:
            await server.serve_forever()

    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter):
        """处理客户端连接"""
        addr = writer.get_extra_info('peername')
        print(f"Client connected: {addr}")

        try:
            while True:
                # 读取消息头
                header = await reader.readexactly(14)
                magic, length, msg_type, sequence = struct.unpack('>IHHI', header)

                if magic != MAGIC:
                    print(f"Invalid magic: {magic:#x}")
                    break

                # 读取消息体
                body_length = length - 14
                body = await reader.readexactly(body_length) if body_length > 0 else b''

                # 处理消息
                handler = self.handlers.get(msg_type)
                if handler:
                    response = await handler(body, sequence)
                    writer.write(response)
                    await writer.drain()
                else:
                    print(f"Unknown message type: {msg_type:#x}")

        except asyncio.IncompleteReadError:
            print(f"Client disconnected: {addr}")
        finally:
            writer.close()
            await writer.wait_closed()

    async def _handle_open(self, body: bytes, sequence: int) -> bytes:
        """处理 PassThruOpen"""
        device_name = body.decode().rstrip('\x00') if body else None
        ret, device_id = self.driver.open(device_name)
        return self._build_response(0x8001, sequence, struct.pack('>II', ret, device_id))

    async def _handle_close(self, body: bytes, sequence: int) -> bytes:
        """处理 PassThruClose"""
        device_id = struct.unpack('>I', body)[0]
        ret = self.driver.close(device_id)
        return self._build_response(0x8002, sequence, struct.pack('>I', ret))

    async def _handle_connect(self, body: bytes, sequence: int) -> bytes:
        """处理 PassThruConnect"""
        device_id, protocol_id, flags, baudrate = struct.unpack('>IIII', body)
        ret, channel_id = self.driver.connect(device_id, protocol_id, flags, baudrate)
        return self._build_response(0x8003, sequence, struct.pack('>II', ret, channel_id))

    async def _handle_disconnect(self, body: bytes, sequence: int) -> bytes:
        """处理 PassThruDisconnect"""
        channel_id = struct.unpack('>I', body)[0]
        ret = self.driver.disconnect(channel_id)
        return self._build_response(0x8004, sequence, struct.pack('>I', ret))

    async def _handle_read_msgs(self, body: bytes, sequence: int) -> bytes:
        """处理 PassThruReadMsgs"""
        channel_id, num_msgs, timeout = struct.unpack('>III', body)
        ret, msgs = self.driver.read_msgs(channel_id, num_msgs, timeout)

        # 序列化消息
        msg_data = b''
        for msg in msgs:
            msg_data += struct.pack('>IIIII',
                msg.ProtocolID, msg.RxStatus, msg.TxFlags,
                msg.Timestamp, msg.DataSize
            )
            msg_data += bytes(msg.Data[:msg.DataSize])

        response_body = struct.pack('>II', ret, len(msgs)) + msg_data
        return self._build_response(0x8005, sequence, response_body)

    async def _handle_write_msgs(self, body: bytes, sequence: int) -> bytes:
        """处理 PassThruWriteMsgs"""
        channel_id, num_msgs, timeout = struct.unpack('>III', body[:12])

        # 反序列化消息
        offset = 12
        msgs = []
        for _ in range(num_msgs):
            protocol_id, rx_status, tx_flags, timestamp, data_size = \
                struct.unpack('>IIIII', body[offset:offset+20])
            offset += 20
            data = body[offset:offset+data_size]
            offset += data_size

            msg = PassThruMsg()
            msg.ProtocolID = protocol_id
            msg.RxStatus = rx_status
            msg.TxFlags = tx_flags
            msg.Timestamp = timestamp
            msg.DataSize = data_size
            for i, b in enumerate(data):
                msg.Data[i] = b
            msgs.append(msg)

        ret, num_written = self.driver.write_msgs(channel_id, msgs, timeout)
        return self._build_response(0x8006, sequence, struct.pack('>II', ret, num_written))

    async def _handle_ioctl(self, body: bytes, sequence: int) -> bytes:
        """处理 PassThruIoctl"""
        # TODO: 实现 IOCTL 处理
        return self._build_response(0x8007, sequence, struct.pack('>I', 0))

    async def _handle_heartbeat(self, body: bytes, sequence: int) -> bytes:
        """处理心跳"""
        return self._build_response(0x80FF, sequence, b'')

    def _build_response(self, msg_type: int, sequence: int, body: bytes) -> bytes:
        """构建响应消息"""
        length = 14 + len(body)
        header = struct.pack('>IHHI', MAGIC, length, msg_type, sequence)
        return header + body
```

```python
# main.py
import asyncio
import argparse
from config import Config
from j2534_wrapper import J2534Driver
from server import VCIProxyServer

def main():
    parser = argparse.ArgumentParser(description='VCI Proxy Server')
    parser.add_argument('--host', default='0.0.0.0', help='Listen host')
    parser.add_argument('--port', type=int, default=9000, help='Listen port')
    parser.add_argument('--dll', default=r'C:\Program Files (x86)\Scanmatik\smj2534.dll',
                        help='J2534 DLL path')
    args = parser.parse_args()

    print(f"Loading J2534 driver: {args.dll}")
    driver = J2534Driver(args.dll)

    print(f"Starting VCI Proxy on {args.host}:{args.port}")
    server = VCIProxyServer(args.host, args.port, driver)

    asyncio.run(server.start())

if __name__ == '__main__':
    main()
```

---

## 6. Virtual J2534 Driver 设计（云服务器）

### 6.1 文件结构

```
virtual_j2534_driver/
├── virtual_j2534.c         # 主要实现
├── virtual_j2534.h         # 头文件
├── protocol.c              # 网络协议
├── protocol.h
├── client.c                # TCP 客户端
├── client.h
├── virtual_j2534.def       # DLL 导出定义
└── CMakeLists.txt          # 构建配置
```

### 6.2 DLL 导出函数

```c
// virtual_j2534.def
LIBRARY virtual_j2534
EXPORTS
    PassThruOpen
    PassThruClose
    PassThruConnect
    PassThruDisconnect
    PassThruReadMsgs
    PassThruWriteMsgs
    PassThruStartMsgFilter
    PassThruStopMsgFilter
    PassThruStartPeriodicMsg
    PassThruStopPeriodicMsg
    PassThruSetProgrammingVoltage
    PassThruReadVersion
    PassThruGetLastError
    PassThruIoctl
```

### 6.3 核心实现

```c
// virtual_j2534.c
#include <windows.h>
#include <winsock2.h>
#include <ws2tcpip.h>
#include <stdio.h>
#include "virtual_j2534.h"
#include "protocol.h"
#include "client.h"

#pragma comment(lib, "ws2_32.lib")

// 全局状态
static SOCKET g_socket = INVALID_SOCKET;
static CRITICAL_SECTION g_lock;
static char g_last_error[256] = {0};
static unsigned long g_sequence = 0;
static BOOL g_initialized = FALSE;

// 配置
static char g_proxy_host[256] = "127.0.0.1";
static int g_proxy_port = 9000;

// 初始化
BOOL WINAPI DllMain(HINSTANCE hinstDLL, DWORD fdwReason, LPVOID lpvReserved) {
    switch (fdwReason) {
        case DLL_PROCESS_ATTACH:
            InitializeCriticalSection(&g_lock);
            // 从注册表或配置文件读取代理地址
            load_config();
            break;
        case DLL_PROCESS_DETACH:
            if (g_socket != INVALID_SOCKET) {
                closesocket(g_socket);
            }
            WSACleanup();
            DeleteCriticalSection(&g_lock);
            break;
    }
    return TRUE;
}

// 连接到 VCI Proxy
static long connect_to_proxy() {
    if (g_socket != INVALID_SOCKET) {
        return STATUS_NOERROR;
    }

    WSADATA wsaData;
    if (WSAStartup(MAKEWORD(2, 2), &wsaData) != 0) {
        strcpy(g_last_error, "WSAStartup failed");
        return ERR_FAILED;
    }

    g_socket = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (g_socket == INVALID_SOCKET) {
        strcpy(g_last_error, "socket() failed");
        return ERR_FAILED;
    }

    // 设置超时
    int timeout = 30000; // 30秒
    setsockopt(g_socket, SOL_SOCKET, SO_RCVTIMEO, (char*)&timeout, sizeof(timeout));
    setsockopt(g_socket, SOL_SOCKET, SO_SNDTIMEO, (char*)&timeout, sizeof(timeout));

    // 禁用 Nagle 算法（减少延迟）
    int nodelay = 1;
    setsockopt(g_socket, IPPROTO_TCP, TCP_NODELAY, (char*)&nodelay, sizeof(nodelay));

    struct sockaddr_in server_addr;
    server_addr.sin_family = AF_INET;
    server_addr.sin_port = htons(g_proxy_port);
    inet_pton(AF_INET, g_proxy_host, &server_addr.sin_addr);

    if (connect(g_socket, (struct sockaddr*)&server_addr, sizeof(server_addr)) != 0) {
        sprintf(g_last_error, "connect() failed: %d", WSAGetLastError());
        closesocket(g_socket);
        g_socket = INVALID_SOCKET;
        return ERR_FAILED;
    }

    g_initialized = TRUE;
    return STATUS_NOERROR;
}

// 发送请求并接收响应
static long send_request(unsigned short msg_type, const void* body, int body_len,
                         void* response, int* response_len) {
    EnterCriticalSection(&g_lock);

    long result = connect_to_proxy();
    if (result != STATUS_NOERROR) {
        LeaveCriticalSection(&g_lock);
        return result;
    }

    // 构建请求
    unsigned long seq = ++g_sequence;
    unsigned char header[14];
    unsigned long magic = htonl(MAGIC_NUMBER);
    unsigned long length = htonl(14 + body_len);
    unsigned short type = htons(msg_type);
    unsigned long sequence = htonl(seq);

    memcpy(header, &magic, 4);
    memcpy(header + 4, &length, 4);
    memcpy(header + 8, &type, 2);
    memcpy(header + 10, &sequence, 4);

    // 发送请求
    if (send(g_socket, (char*)header, 14, 0) != 14) {
        strcpy(g_last_error, "send header failed");
        LeaveCriticalSection(&g_lock);
        return ERR_FAILED;
    }

    if (body_len > 0 && send(g_socket, (char*)body, body_len, 0) != body_len) {
        strcpy(g_last_error, "send body failed");
        LeaveCriticalSection(&g_lock);
        return ERR_FAILED;
    }

    // 接收响应头
    unsigned char resp_header[14];
    if (recv(g_socket, (char*)resp_header, 14, MSG_WAITALL) != 14) {
        strcpy(g_last_error, "recv header failed");
        LeaveCriticalSection(&g_lock);
        return ERR_FAILED;
    }

    // 解析响应头
    unsigned long resp_length;
    memcpy(&resp_length, resp_header + 4, 4);
    resp_length = ntohl(resp_length);

    // 接收响应体
    int resp_body_len = resp_length - 14;
    if (resp_body_len > 0) {
        if (recv(g_socket, (char*)response, resp_body_len, MSG_WAITALL) != resp_body_len) {
            strcpy(g_last_error, "recv body failed");
            LeaveCriticalSection(&g_lock);
            return ERR_FAILED;
        }
    }
    *response_len = resp_body_len;

    LeaveCriticalSection(&g_lock);
    return STATUS_NOERROR;
}

// J2534 API 实现

long WINAPI PassThruOpen(void* pName, unsigned long* pDeviceID) {
    char body[256] = {0};
    int body_len = 0;

    if (pName) {
        strcpy(body, (char*)pName);
        body_len = strlen(body) + 1;
    }

    unsigned char response[64];
    int response_len;

    long result = send_request(MSG_OPEN_REQ, body, body_len, response, &response_len);
    if (result != STATUS_NOERROR) {
        return result;
    }

    // 解析响应
    unsigned long ret_code, device_id;
    memcpy(&ret_code, response, 4);
    memcpy(&device_id, response + 4, 4);
    ret_code = ntohl(ret_code);
    device_id = ntohl(device_id);

    *pDeviceID = device_id;
    return ret_code;
}

long WINAPI PassThruClose(unsigned long DeviceID) {
    unsigned long body = htonl(DeviceID);
    unsigned char response[64];
    int response_len;

    long result = send_request(MSG_CLOSE_REQ, &body, 4, response, &response_len);
    if (result != STATUS_NOERROR) {
        return result;
    }

    unsigned long ret_code;
    memcpy(&ret_code, response, 4);
    return ntohl(ret_code);
}

long WINAPI PassThruConnect(unsigned long DeviceID, unsigned long ProtocolID,
                            unsigned long Flags, unsigned long Baudrate,
                            unsigned long* pChannelID) {
    unsigned char body[16];
    unsigned long vals[4] = {
        htonl(DeviceID), htonl(ProtocolID), htonl(Flags), htonl(Baudrate)
    };
    memcpy(body, vals, 16);

    unsigned char response[64];
    int response_len;

    long result = send_request(MSG_CONNECT_REQ, body, 16, response, &response_len);
    if (result != STATUS_NOERROR) {
        return result;
    }

    unsigned long ret_code, channel_id;
    memcpy(&ret_code, response, 4);
    memcpy(&channel_id, response + 4, 4);
    ret_code = ntohl(ret_code);
    channel_id = ntohl(channel_id);

    *pChannelID = channel_id;
    return ret_code;
}

long WINAPI PassThruDisconnect(unsigned long ChannelID) {
    unsigned long body = htonl(ChannelID);
    unsigned char response[64];
    int response_len;

    long result = send_request(MSG_DISCONNECT_REQ, &body, 4, response, &response_len);
    if (result != STATUS_NOERROR) {
        return result;
    }

    unsigned long ret_code;
    memcpy(&ret_code, response, 4);
    return ntohl(ret_code);
}

long WINAPI PassThruReadMsgs(unsigned long ChannelID, PASSTHRU_MSG* pMsg,
                             unsigned long* pNumMsgs, unsigned long Timeout) {
    unsigned char body[12];
    unsigned long vals[3] = {htonl(ChannelID), htonl(*pNumMsgs), htonl(Timeout)};
    memcpy(body, vals, 12);

    unsigned char response[65536];  // 大缓冲区用于消息
    int response_len;

    long result = send_request(MSG_READ_MSGS_REQ, body, 12, response, &response_len);
    if (result != STATUS_NOERROR) {
        return result;
    }

    // 解析响应
    unsigned long ret_code, num_msgs;
    memcpy(&ret_code, response, 4);
    memcpy(&num_msgs, response + 4, 4);
    ret_code = ntohl(ret_code);
    num_msgs = ntohl(num_msgs);

    // 解析消息
    int offset = 8;
    for (unsigned long i = 0; i < num_msgs && i < *pNumMsgs; i++) {
        unsigned long protocol_id, rx_status, tx_flags, timestamp, data_size;
        memcpy(&protocol_id, response + offset, 4); offset += 4;
        memcpy(&rx_status, response + offset, 4); offset += 4;
        memcpy(&tx_flags, response + offset, 4); offset += 4;
        memcpy(&timestamp, response + offset, 4); offset += 4;
        memcpy(&data_size, response + offset, 4); offset += 4;

        pMsg[i].ProtocolID = ntohl(protocol_id);
        pMsg[i].RxStatus = ntohl(rx_status);
        pMsg[i].TxFlags = ntohl(tx_flags);
        pMsg[i].Timestamp = ntohl(timestamp);
        pMsg[i].DataSize = ntohl(data_size);

        memcpy(pMsg[i].Data, response + offset, pMsg[i].DataSize);
        offset += pMsg[i].DataSize;
    }

    *pNumMsgs = num_msgs;
    return ret_code;
}

long WINAPI PassThruWriteMsgs(unsigned long ChannelID, PASSTHRU_MSG* pMsg,
                              unsigned long* pNumMsgs, unsigned long Timeout) {
    // 构建请求体
    unsigned char body[65536];
    int offset = 0;

    unsigned long vals[3] = {htonl(ChannelID), htonl(*pNumMsgs), htonl(Timeout)};
    memcpy(body, vals, 12);
    offset = 12;

    for (unsigned long i = 0; i < *pNumMsgs; i++) {
        unsigned long msg_vals[5] = {
            htonl(pMsg[i].ProtocolID),
            htonl(pMsg[i].RxStatus),
            htonl(pMsg[i].TxFlags),
            htonl(pMsg[i].Timestamp),
            htonl(pMsg[i].DataSize)
        };
        memcpy(body + offset, msg_vals, 20);
        offset += 20;
        memcpy(body + offset, pMsg[i].Data, pMsg[i].DataSize);
        offset += pMsg[i].DataSize;
    }

    unsigned char response[64];
    int response_len;

    long result = send_request(MSG_WRITE_MSGS_REQ, body, offset, response, &response_len);
    if (result != STATUS_NOERROR) {
        return result;
    }

    unsigned long ret_code, num_msgs;
    memcpy(&ret_code, response, 4);
    memcpy(&num_msgs, response + 4, 4);
    ret_code = ntohl(ret_code);
    num_msgs = ntohl(num_msgs);

    *pNumMsgs = num_msgs;
    return ret_code;
}

long WINAPI PassThruGetLastError(char* pErrorDescription) {
    if (pErrorDescription) {
        strcpy(pErrorDescription, g_last_error);
    }
    return STATUS_NOERROR;
}

// TODO: 实现其他 J2534 函数
long WINAPI PassThruStartMsgFilter(unsigned long ChannelID, unsigned long FilterType,
                                   PASSTHRU_MSG* pMaskMsg, PASSTHRU_MSG* pPatternMsg,
                                   PASSTHRU_MSG* pFlowControlMsg, unsigned long* pFilterID) {
    // TODO
    return STATUS_NOERROR;
}

long WINAPI PassThruStopMsgFilter(unsigned long ChannelID, unsigned long FilterID) {
    // TODO
    return STATUS_NOERROR;
}

long WINAPI PassThruStartPeriodicMsg(unsigned long ChannelID, PASSTHRU_MSG* pMsg,
                                     unsigned long* pMsgID, unsigned long TimeInterval) {
    // TODO
    return STATUS_NOERROR;
}

long WINAPI PassThruStopPeriodicMsg(unsigned long ChannelID, unsigned long MsgID) {
    // TODO
    return STATUS_NOERROR;
}

long WINAPI PassThruSetProgrammingVoltage(unsigned long DeviceID, unsigned long PinNumber,
                                          unsigned long Voltage) {
    // TODO
    return STATUS_NOERROR;
}

long WINAPI PassThruReadVersion(unsigned long DeviceID, char* pFirmwareVersion,
                                char* pDllVersion, char* pApiVersion) {
    if (pFirmwareVersion) strcpy(pFirmwareVersion, "1.0.0");
    if (pDllVersion) strcpy(pDllVersion, "1.0.0");
    if (pApiVersion) strcpy(pApiVersion, "04.04");
    return STATUS_NOERROR;
}

long WINAPI PassThruIoctl(unsigned long ChannelID, unsigned long IoctlID,
                          void* pInput, void* pOutput) {
    // TODO: 实现 IOCTL
    return STATUS_NOERROR;
}
```

---

## 7. 注册表配置

### 7.1 Virtual J2534 Driver 注册

在云服务器上，需要将虚拟驱动注册到 J2534 注册表：

```
[HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\PassThruSupport.04.04\Virtual J2534 - VCI Proxy]
"Name"="Virtual J2534 - VCI Proxy"
"Vendor"="Custom"
"ConfigApplication"=""
"FunctionLibrary"="C:\\tools\\vci_proxy\\virtual_j2534.dll"
"CAN"=dword:00000001
"ISO15765"=dword:00000001
"ISO9141"=dword:00000001
"ISO14230"=dword:00000001
"J1850VPW"=dword:00000001
"J1850PWM"=dword:00000001
"SCI_A_ENGINE"=dword:00000001
"SCI_A_TRANS"=dword:00000001
"SCI_B_ENGINE"=dword:00000001
"SCI_B_TRANS"=dword:00000001
```

### 7.2 VCI Proxy 配置文件

在云服务器上创建配置文件 `C:\tools\vci_proxy\config.ini`：

```ini
[proxy]
host=<你的本地电脑公网IP或域名>
port=9000
```

---

## 8. 部署流程

### 8.1 本地电脑部署

1. 安装 Python 3.8+
2. 复制 VCI Proxy 代码到 `C:\tools\vci_proxy\`
3. 安装依赖：`pip install -r requirements.txt`
4. 配置防火墙允许 9000 端口入站
5. 运行：`python main.py --port 9000`

### 8.2 云服务器部署

1. 复制 `virtual_j2534.dll` 到 `C:\tools\vci_proxy\`
2. 创建配置文件指向本地电脑 IP
3. 运行注册表脚本注册虚拟驱动
4. 在 GDS2 中选择 "Virtual J2534 - VCI Proxy" 设备

### 8.3 网络配置

```
本地电脑需要：
1. 公网 IP 或 NAT 端口映射（端口 9000）
2. 或使用反向连接（云服务器主动连接本地）
```

---

## 9. 优化建议

### 9.1 批量操作

将多个 ReadMsgs/WriteMsgs 请求合并为一个批量请求：

```
// 批量读取请求
MSG_BATCH_READ_REQ: [ChannelID, Count, Timeout, Interval]
// 服务器端循环读取 Count 次，每次间隔 Interval ms，一次性返回所有结果
```

### 9.2 消息缓存

VCI Proxy 可以主动缓存接收到的消息，减少等待时间：

```python
class MessageCache:
    def __init__(self):
        self.cache = {}
        self.lock = threading.Lock()

    def start_background_read(self, channel_id):
        """后台持续读取消息"""
        def read_loop():
            while True:
                msgs = self.driver.read_msgs(channel_id, 10, 100)
                with self.lock:
                    self.cache[channel_id].extend(msgs)
        threading.Thread(target=read_loop, daemon=True).start()
```

### 9.3 压缩

对于大量数据传输，可以启用压缩：

```python
import zlib

def compress(data: bytes) -> bytes:
    return zlib.compress(data, level=1)  # 快速压缩

def decompress(data: bytes) -> bytes:
    return zlib.decompress(data)
```

---

## 10. 测试计划

### 10.1 单元测试

- J2534 Driver Wrapper 测试
- 协议编解码测试
- TCP 通信测试

### 10.2 集成测试

- VCI Proxy 端到端测试
- Virtual Driver 端到端测试
- 完整链路测试

### 10.3 性能测试

- 延迟测量（单次 API 调用）
- 吞吐量测试（批量消息）
- 长时间稳定性测试

---

## 11. 时间表

| 阶段 | 任务 | 预计时间 |
|------|------|---------|
| Week 1 | VCI Proxy 基础框架 + J2534 封装 | 5天 |
| Week 2 | Virtual J2534 Driver 开发 | 5天 |
| Week 3 | 集成测试 + 调试 | 5天 |
| Week 4 | 优化 + 文档 | 3天 |

---

## 12. 风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| J2534 API 兼容性问题 | GDS2 无法识别设备 | 详细研究 GDS2 使用的 API |
| 网络不稳定 | 诊断中断 | 实现重连机制和心跳 |
| 某些 IOCTL 不支持 | 功能受限 | 逐步添加支持 |
| 本地电脑无公网 IP | 无法连接 | 使用反向连接或 ngrok |

---

## 附录 A: GDS2 实际 J2534 接口分析

### A.1 GDS2 J2534 架构

通过分析 GDS2 安装目录，发现以下关键组件：

```
GDS2 (Java)
    │
    │ JNI 调用
    ▼
HardwareJ2534.class (com.Mahle.VCI.Hardware)
    │
    │ native 方法
    ▼
rtkJ2534Int.dll (GDS2 bin 目录)
    │
    │ 调用
    ▼
smj2534.dll 或 smj2534_0404_usb_sm3.dll (Scanmatik 驱动)
```

### A.2 HardwareJ2534 Native 方法（实际接口）

GDS2 使用的 J2534 接口与标准 SAE J2534-1 有所不同。以下是 `HardwareJ2534.class` 中的 native 方法：

**标准 J2534 方法：**
```java
public native int PassThruOpen(int[], char[], int, int[], int, char[], int, char[], int, int);
public native int PassThruClose(int);
public native int PassThruConnect(int, int, int, int, int[]);
public native int PassThruDisconnect(int);
public native int PassThruWriteMsgs(int, int, int, int, int, int, int, char[]);
public native int PassThruReadMsgsPacket(int, int, int, long[], int[], int[], int[], char[][], int[]);
public native int PassThruStartMsgFilter(int, int, int, int, int, int, int, int[], int);
public native int PassThruStopMsgFilter(int, int);
public native int PassThruStartPeriodicMsg(int, int, int, int, int, int, int, char[], int[]);
public native int PassThruStopPeriodicMsg(int, int);
public native int PassThruSetVoltage(int, int, int);
public native int PassThruReadVersion(int, char[][], int[]);
```

**GDS2 扩展方法（非标准）：**
```java
public native int PassThruSetVendor(char[], int);
public native int PassThruPerformInit(int, int, int, int, int[]);
public native int PassThruConfigure(int, int, int, int, int);
public native int PassThruConfigureJ2435DLLINterface(int, int);
public native int SetHardwareConfiguration(int, int, int, char[], int, int, char[]);
public native int GetHardwareConfiguration(int, int, int, char[], int[], int[], char[]);
public native int J2534WrapperControl(int);
public native boolean J2534WrapperIsDeviceAvailableOnUSB(int[], int[], int[]);
public native int J2534WrapperGetDefaultDeviceInfo(char[], int, char[], int);
public native int J2534WrapperGetConnectedDeviceInfo(char[], int, char[], int);
public native int J2534WrapperGetSupportedProtocols(char[], int[]);
public native int J2534WrapperGetSupportedProtocolsCount(int[]);
public native int J2534WrapperDOIPDiscovery(int, int, int[], char[][], int);
```

### A.3 已注册的 J2534 设备

系统当前注册的 J2534 设备（来自注册表 `HKLM\SOFTWARE\WOW6432Node\PassThruSupport.04.04`）：

| 设备名 | DLL 路径 | 厂商 |
|--------|----------|------|
| SM2 USB | `C:\Program Files (x86)\Scanmatik\smj2534.dll` | Scanmatik |
| SM3 USB | `C:\Program Files (x86)\Scanmatik\smj2534_0404_usb_sm3.dll` | Scanmatik |
| MDI | `C:\Program Files (x86)\GM MDI Software\...\BVTX4J32.dll` | Bosch |
| MDI 2 | `C:\Program Files (x86)\Bosch\VTX-VCI\...\BVTX4J32.dll` | Bosch |

### A.4 SM3 USB 支持的协议

从注册表获取的 SM3 USB 支持协议：

```
标准协议:
- CAN, CAN_PS (Controller Area Network)
- ISO15765, ISO15765_PS (CAN with ISO-TP)
- ISO9141, ISO9141_PS (K-Line)
- ISO14230, ISO14230_PS (KWP2000)
- J1850VPW, J1850VPW_PS (GM Class 2)
- J1850PWM, J1850PWM_PS (Ford SCP)
- GM_UART_PS (GM UART)

扩展协议:
- SW_CAN_PS, SW_ISO15765_PS (Single Wire CAN)
- FD_CAN_PS, FD_ISO15765_PS (CAN-FD)
- ETHERNET_NDIS (Ethernet/DoIP)
- SCI_A_ENGINE, SCI_A_TRANS (Chrysler SCI)
- SCI_B_ENGINE, SCI_B_TRANS
- J2610_PS
- HONDA_DIAG_PS
```

### A.5 GDS2 相关 DLL 文件

位于 `C:\Program Files (x86)\GDS 2\bin\`:

| 文件 | 功能 |
|------|------|
| `rtkJ2534Int.dll` | J2534 JNI 接口（连接 Java 和 native 驱动） |
| `rtkGMSecurityInt.dll` | GM 安全认证接口 |
| `SecurityAccess.dll` | 安全访问功能 |
| `MahleUtility.dll` | Mahle 工具函数 |
| `BinaryWriter.dll` | 二进制文件写入 |

### A.6 关键发现

1. **GDS2 使用扩展 J2534 API** - 标准 J2534-1 API 只有 14 个函数，但 GDS2 使用了约 25 个 native 方法，包括设备发现、配置管理等扩展功能。

2. **签名差异** - GDS2 的函数签名与标准 J2534 略有不同（例如 `PassThruOpen` 有更多参数），需要在 Virtual Driver 中适配。

3. **多层架构** - GDS2 → HardwareJ2534.class → rtkJ2534Int.dll → smj2534.dll，我们的 Virtual Driver 需要替换整个链条或仅替换最底层。

4. **DoIP 支持** - `J2534WrapperDOIPDiscovery` 方法表明 GDS2 支持 Ethernet/DoIP 诊断，这可能是未来的替代方案。

### A.7 实现建议

根据以上分析，建议的实现策略：

**方案 A: 替换 Scanmatik 驱动层**
- Virtual Driver 模拟 `smj2534.dll` 接口
- 优点：最小改动，兼容性好
- 缺点：需要支持所有 Scanmatik 特定功能

**方案 B: 替换 rtkJ2534Int.dll**
- Virtual Driver 模拟 rtkJ2534Int.dll 的 JNI 接口
- 优点：可以简化接口
- 缺点：需要理解 JNI 调用约定

**推荐方案 A**，因为：
1. smj2534.dll 使用标准 C 调用约定，更容易实现
2. 可以复用已有的 J2534 文档
3. 注册表注册机制已经标准化
