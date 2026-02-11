#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Phase 0: J2534 本地调用测试

目标：验证 Python ctypes 能否正确调用 Scanmatik J2534 DLL

注意事项：
1. smj2534.dll 是 32 位 DLL，需要 32 位 Python
2. 需要 SM2/SM3 USB 设备已连接
3. 需要以管理员权限运行（某些情况下）

使用方法：
    python scripts/test_j2534_local.py
"""

import sys
import os
import ctypes
from ctypes import (
    CDLL, POINTER, Structure, byref,
    c_long, c_ulong, c_ubyte, c_char, c_char_p, c_void_p,
    create_string_buffer, sizeof
)
from typing import Optional, Tuple, List
import time

# ============================================================================
# J2534 常量定义
# ============================================================================

# 错误码
STATUS_NOERROR = 0x00
ERR_NOT_SUPPORTED = 0x01
ERR_INVALID_CHANNEL_ID = 0x02
ERR_INVALID_PROTOCOL_ID = 0x03
ERR_NULL_PARAMETER = 0x04
ERR_INVALID_IOCTL_VALUE = 0x05
ERR_INVALID_FLAGS = 0x06
ERR_FAILED = 0x07
ERR_DEVICE_NOT_CONNECTED = 0x08
ERR_TIMEOUT = 0x09
ERR_INVALID_MSG = 0x0A
ERR_INVALID_TIME_INTERVAL = 0x0B
ERR_EXCEEDED_LIMIT = 0x0C
ERR_INVALID_MSG_ID = 0x0D
ERR_DEVICE_IN_USE = 0x0E
ERR_INVALID_IOCTL_ID = 0x0F
ERR_BUFFER_EMPTY = 0x10
ERR_BUFFER_FULL = 0x11
ERR_BUFFER_OVERFLOW = 0x12
ERR_PIN_INVALID = 0x13
ERR_CHANNEL_IN_USE = 0x14
ERR_MSG_PROTOCOL_ID = 0x15
ERR_INVALID_FILTER_ID = 0x16
ERR_NO_FLOW_CONTROL = 0x17
ERR_NOT_UNIQUE = 0x18
ERR_INVALID_BAUDRATE = 0x19
ERR_INVALID_DEVICE_ID = 0x1A

ERROR_NAMES = {
    STATUS_NOERROR: "STATUS_NOERROR",
    ERR_NOT_SUPPORTED: "ERR_NOT_SUPPORTED",
    ERR_INVALID_CHANNEL_ID: "ERR_INVALID_CHANNEL_ID",
    ERR_INVALID_PROTOCOL_ID: "ERR_INVALID_PROTOCOL_ID",
    ERR_NULL_PARAMETER: "ERR_NULL_PARAMETER",
    ERR_INVALID_IOCTL_VALUE: "ERR_INVALID_IOCTL_VALUE",
    ERR_INVALID_FLAGS: "ERR_INVALID_FLAGS",
    ERR_FAILED: "ERR_FAILED",
    ERR_DEVICE_NOT_CONNECTED: "ERR_DEVICE_NOT_CONNECTED",
    ERR_TIMEOUT: "ERR_TIMEOUT",
    ERR_INVALID_MSG: "ERR_INVALID_MSG",
    ERR_INVALID_TIME_INTERVAL: "ERR_INVALID_TIME_INTERVAL",
    ERR_EXCEEDED_LIMIT: "ERR_EXCEEDED_LIMIT",
    ERR_INVALID_MSG_ID: "ERR_INVALID_MSG_ID",
    ERR_DEVICE_IN_USE: "ERR_DEVICE_IN_USE",
    ERR_INVALID_IOCTL_ID: "ERR_INVALID_IOCTL_ID",
    ERR_BUFFER_EMPTY: "ERR_BUFFER_EMPTY",
    ERR_BUFFER_FULL: "ERR_BUFFER_FULL",
    ERR_BUFFER_OVERFLOW: "ERR_BUFFER_OVERFLOW",
    ERR_PIN_INVALID: "ERR_PIN_INVALID",
    ERR_CHANNEL_IN_USE: "ERR_CHANNEL_IN_USE",
    ERR_MSG_PROTOCOL_ID: "ERR_MSG_PROTOCOL_ID",
    ERR_INVALID_FILTER_ID: "ERR_INVALID_FILTER_ID",
    ERR_NO_FLOW_CONTROL: "ERR_NO_FLOW_CONTROL",
    ERR_NOT_UNIQUE: "ERR_NOT_UNIQUE",
    ERR_INVALID_BAUDRATE: "ERR_INVALID_BAUDRATE",
    ERR_INVALID_DEVICE_ID: "ERR_INVALID_DEVICE_ID",
}

# 协议类型
J1850VPW = 1
J1850PWM = 2
ISO9141 = 3
ISO14230 = 4
CAN = 5
ISO15765 = 6
SCI_A_ENGINE = 7
SCI_A_TRANS = 8
SCI_B_ENGINE = 9
SCI_B_TRANS = 10

PROTOCOL_NAMES = {
    J1850VPW: "J1850VPW",
    J1850PWM: "J1850PWM",
    ISO9141: "ISO9141",
    ISO14230: "ISO14230",
    CAN: "CAN",
    ISO15765: "ISO15765",
    SCI_A_ENGINE: "SCI_A_ENGINE",
    SCI_A_TRANS: "SCI_A_TRANS",
    SCI_B_ENGINE: "SCI_B_ENGINE",
    SCI_B_TRANS: "SCI_B_TRANS",
}

# 连接标志
CAN_29BIT_ID = 0x00000100
ISO9141_NO_CHECKSUM = 0x00000200
CAN_ID_BOTH = 0x00000800
ISO9141_K_LINE_ONLY = 0x00001000

# ============================================================================
# J2534 数据结构
# ============================================================================

class PASSTHRU_MSG(Structure):
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

    def __repr__(self):
        data_hex = ' '.join(f'{self.Data[i]:02X}' for i in range(min(self.DataSize, 32)))
        if self.DataSize > 32:
            data_hex += ' ...'
        return (f"PASSTHRU_MSG(Protocol={PROTOCOL_NAMES.get(self.ProtocolID, self.ProtocolID)}, "
                f"DataSize={self.DataSize}, Data=[{data_hex}])")


class SCONFIG(Structure):
    """配置项结构"""
    _fields_ = [
        ("Parameter", c_ulong),
        ("Value", c_ulong),
    ]


class SCONFIG_LIST(Structure):
    """配置列表结构"""
    _fields_ = [
        ("NumOfParams", c_ulong),
        ("ConfigPtr", POINTER(SCONFIG)),
    ]


# ============================================================================
# J2534 驱动封装类
# ============================================================================

class J2534Driver:
    """J2534 驱动封装"""

    # 已知的 DLL 路径
    DLL_PATHS = [
        r"C:\Program Files (x86)\Scanmatik\smj2534_0404_usb_sm3.dll",  # SM3 USB (04.04)
        r"C:\Program Files (x86)\Scanmatik\smj2534.dll",              # SM2 USB
        r"C:\Program Files (x86)\Scanmatik\smj2534_0202_usb_sm3.dll", # SM3 USB (02.02)
    ]

    def __init__(self, dll_path: Optional[str] = None):
        self.dll = None
        self.dll_path = None
        self._device_id = None
        self._channel_id = None

        # 尝试加载 DLL
        paths_to_try = [dll_path] if dll_path else self.DLL_PATHS

        for path in paths_to_try:
            if path and os.path.exists(path):
                try:
                    print(f"尝试加载 DLL: {path}")
                    self.dll = ctypes.WinDLL(path)  # 使用 WinDLL (stdcall)
                    self.dll_path = path
                    print(f"✓ 成功加载 DLL")
                    break
                except OSError as e:
                    print(f"✗ 加载失败: {e}")
                    continue

        if not self.dll:
            raise RuntimeError("无法加载 J2534 DLL。请确认：\n"
                             "1. Scanmatik 驱动已安装\n"
                             "2. 使用 32 位 Python（DLL 是 32 位）")

        self._setup_functions()

    def _setup_functions(self):
        """设置函数原型"""

        # PassThruOpen
        # long PassThruOpen(void *pName, unsigned long *pDeviceID)
        self.dll.PassThruOpen.argtypes = [c_void_p, POINTER(c_ulong)]
        self.dll.PassThruOpen.restype = c_long

        # PassThruClose
        # long PassThruClose(unsigned long DeviceID)
        self.dll.PassThruClose.argtypes = [c_ulong]
        self.dll.PassThruClose.restype = c_long

        # PassThruConnect
        # long PassThruConnect(unsigned long DeviceID, unsigned long ProtocolID,
        #                      unsigned long Flags, unsigned long Baudrate,
        #                      unsigned long *pChannelID)
        self.dll.PassThruConnect.argtypes = [c_ulong, c_ulong, c_ulong, c_ulong, POINTER(c_ulong)]
        self.dll.PassThruConnect.restype = c_long

        # PassThruDisconnect
        # long PassThruDisconnect(unsigned long ChannelID)
        self.dll.PassThruDisconnect.argtypes = [c_ulong]
        self.dll.PassThruDisconnect.restype = c_long

        # PassThruReadMsgs
        # long PassThruReadMsgs(unsigned long ChannelID, PASSTHRU_MSG *pMsg,
        #                       unsigned long *pNumMsgs, unsigned long Timeout)
        self.dll.PassThruReadMsgs.argtypes = [c_ulong, POINTER(PASSTHRU_MSG), POINTER(c_ulong), c_ulong]
        self.dll.PassThruReadMsgs.restype = c_long

        # PassThruWriteMsgs
        # long PassThruWriteMsgs(unsigned long ChannelID, PASSTHRU_MSG *pMsg,
        #                        unsigned long *pNumMsgs, unsigned long Timeout)
        self.dll.PassThruWriteMsgs.argtypes = [c_ulong, POINTER(PASSTHRU_MSG), POINTER(c_ulong), c_ulong]
        self.dll.PassThruWriteMsgs.restype = c_long

        # PassThruStartMsgFilter
        self.dll.PassThruStartMsgFilter.argtypes = [
            c_ulong, c_ulong, POINTER(PASSTHRU_MSG), POINTER(PASSTHRU_MSG),
            POINTER(PASSTHRU_MSG), POINTER(c_ulong)
        ]
        self.dll.PassThruStartMsgFilter.restype = c_long

        # PassThruStopMsgFilter
        self.dll.PassThruStopMsgFilter.argtypes = [c_ulong, c_ulong]
        self.dll.PassThruStopMsgFilter.restype = c_long

        # PassThruReadVersion
        # long PassThruReadVersion(unsigned long DeviceID, char *pFirmwareVersion,
        #                          char *pDllVersion, char *pApiVersion)
        self.dll.PassThruReadVersion.argtypes = [c_ulong, c_char_p, c_char_p, c_char_p]
        self.dll.PassThruReadVersion.restype = c_long

        # PassThruGetLastError
        # long PassThruGetLastError(char *pErrorDescription)
        self.dll.PassThruGetLastError.argtypes = [c_char_p]
        self.dll.PassThruGetLastError.restype = c_long

        # PassThruIoctl
        self.dll.PassThruIoctl.argtypes = [c_ulong, c_ulong, c_void_p, c_void_p]
        self.dll.PassThruIoctl.restype = c_long

    def get_error_name(self, code: int) -> str:
        """获取错误码名称"""
        return ERROR_NAMES.get(code, f"UNKNOWN_ERROR_{code:#x}")

    def get_last_error(self) -> str:
        """获取最后一次错误的描述"""
        buf = create_string_buffer(256)
        self.dll.PassThruGetLastError(buf)
        return buf.value.decode('utf-8', errors='replace')

    def open(self, device_name: Optional[str] = None) -> Tuple[int, int]:
        """
        打开设备

        Args:
            device_name: 设备名称（可选，None 表示使用默认设备）

        Returns:
            (return_code, device_id)
        """
        device_id = c_ulong()
        name = device_name.encode('utf-8') if device_name else None
        ret = self.dll.PassThruOpen(name, byref(device_id))

        if ret == STATUS_NOERROR:
            self._device_id = device_id.value

        return ret, device_id.value

    def close(self, device_id: Optional[int] = None) -> int:
        """关闭设备"""
        dev_id = device_id if device_id is not None else self._device_id
        if dev_id is None:
            return ERR_INVALID_DEVICE_ID

        ret = self.dll.PassThruClose(dev_id)
        if ret == STATUS_NOERROR and dev_id == self._device_id:
            self._device_id = None
        return ret

    def read_version(self, device_id: Optional[int] = None) -> Tuple[int, str, str, str]:
        """
        读取版本信息

        Returns:
            (return_code, firmware_version, dll_version, api_version)
        """
        dev_id = device_id if device_id is not None else self._device_id
        if dev_id is None:
            return ERR_INVALID_DEVICE_ID, "", "", ""

        fw_ver = create_string_buffer(80)
        dll_ver = create_string_buffer(80)
        api_ver = create_string_buffer(80)

        ret = self.dll.PassThruReadVersion(dev_id, fw_ver, dll_ver, api_ver)

        return (ret,
                fw_ver.value.decode('utf-8', errors='replace'),
                dll_ver.value.decode('utf-8', errors='replace'),
                api_ver.value.decode('utf-8', errors='replace'))

    def connect(self, protocol_id: int, flags: int = 0, baudrate: int = 500000,
                device_id: Optional[int] = None) -> Tuple[int, int]:
        """
        建立协议通道

        Args:
            protocol_id: 协议类型 (CAN, ISO15765, etc.)
            flags: 连接标志
            baudrate: 波特率
            device_id: 设备ID（可选）

        Returns:
            (return_code, channel_id)
        """
        dev_id = device_id if device_id is not None else self._device_id
        if dev_id is None:
            return ERR_INVALID_DEVICE_ID, 0

        channel_id = c_ulong()
        ret = self.dll.PassThruConnect(dev_id, protocol_id, flags, baudrate, byref(channel_id))

        if ret == STATUS_NOERROR:
            self._channel_id = channel_id.value

        return ret, channel_id.value

    def disconnect(self, channel_id: Optional[int] = None) -> int:
        """断开协议通道"""
        ch_id = channel_id if channel_id is not None else self._channel_id
        if ch_id is None:
            return ERR_INVALID_CHANNEL_ID

        ret = self.dll.PassThruDisconnect(ch_id)
        if ret == STATUS_NOERROR and ch_id == self._channel_id:
            self._channel_id = None
        return ret

    def read_msgs(self, num_msgs: int = 1, timeout: int = 1000,
                  channel_id: Optional[int] = None) -> Tuple[int, List[PASSTHRU_MSG]]:
        """
        读取消息

        Args:
            num_msgs: 最大读取消息数
            timeout: 超时时间（毫秒）
            channel_id: 通道ID（可选）

        Returns:
            (return_code, messages)
        """
        ch_id = channel_id if channel_id is not None else self._channel_id
        if ch_id is None:
            return ERR_INVALID_CHANNEL_ID, []

        msgs = (PASSTHRU_MSG * num_msgs)()
        num = c_ulong(num_msgs)

        ret = self.dll.PassThruReadMsgs(ch_id, msgs, byref(num), timeout)

        return ret, list(msgs[:num.value])

    def write_msgs(self, msgs: List[PASSTHRU_MSG], timeout: int = 1000,
                   channel_id: Optional[int] = None) -> Tuple[int, int]:
        """
        发送消息

        Args:
            msgs: 消息列表
            timeout: 超时时间（毫秒）
            channel_id: 通道ID（可选）

        Returns:
            (return_code, num_msgs_written)
        """
        ch_id = channel_id if channel_id is not None else self._channel_id
        if ch_id is None:
            return ERR_INVALID_CHANNEL_ID, 0

        msg_array = (PASSTHRU_MSG * len(msgs))(*msgs)
        num = c_ulong(len(msgs))

        ret = self.dll.PassThruWriteMsgs(ch_id, msg_array, byref(num), timeout)

        return ret, num.value


# ============================================================================
# 测试函数
# ============================================================================

def test_dll_load():
    """测试 1: DLL 加载"""
    print("\n" + "="*60)
    print("测试 1: DLL 加载")
    print("="*60)

    try:
        driver = J2534Driver()
        print(f"✓ DLL 加载成功: {driver.dll_path}")
        return driver
    except Exception as e:
        print(f"✗ DLL 加载失败: {e}")
        return None


def test_open_close(driver: J2534Driver):
    """测试 2: 设备打开/关闭"""
    print("\n" + "="*60)
    print("测试 2: 设备打开/关闭")
    print("="*60)

    # 打开设备
    print("调用 PassThruOpen(NULL)...")
    ret, device_id = driver.open()
    print(f"  返回值: {ret} ({driver.get_error_name(ret)})")
    print(f"  DeviceID: {device_id}")

    if ret != STATUS_NOERROR:
        print(f"  错误描述: {driver.get_last_error()}")
        return False

    print("✓ 设备打开成功")

    # 读取版本
    print("\n调用 PassThruReadVersion()...")
    ret, fw_ver, dll_ver, api_ver = driver.read_version()
    if ret == STATUS_NOERROR:
        print(f"  固件版本: {fw_ver}")
        print(f"  DLL 版本: {dll_ver}")
        print(f"  API 版本: {api_ver}")
        print("✓ 版本读取成功")
    else:
        print(f"  返回值: {ret} ({driver.get_error_name(ret)})")

    # 关闭设备
    print("\n调用 PassThruClose()...")
    ret = driver.close()
    print(f"  返回值: {ret} ({driver.get_error_name(ret)})")

    if ret == STATUS_NOERROR:
        print("✓ 设备关闭成功")
        return True
    else:
        print(f"✗ 设备关闭失败: {driver.get_last_error()}")
        return False


def test_connect_can(driver: J2534Driver):
    """测试 3: CAN 通道连接"""
    print("\n" + "="*60)
    print("测试 3: CAN 通道连接")
    print("="*60)

    # 打开设备
    ret, device_id = driver.open()
    if ret != STATUS_NOERROR:
        print(f"✗ 设备打开失败: {driver.get_error_name(ret)}")
        return False

    print(f"设备已打开, DeviceID={device_id}")

    # 连接 CAN 通道
    print("\n调用 PassThruConnect(CAN, 500000)...")
    ret, channel_id = driver.connect(
        protocol_id=CAN,
        flags=0,
        baudrate=500000
    )
    print(f"  返回值: {ret} ({driver.get_error_name(ret)})")
    print(f"  ChannelID: {channel_id}")

    if ret != STATUS_NOERROR:
        print(f"  错误描述: {driver.get_last_error()}")
        driver.close()
        return False

    print("✓ CAN 通道连接成功")

    # 尝试读取消息
    print("\n调用 PassThruReadMsgs(timeout=500)...")
    ret, msgs = driver.read_msgs(num_msgs=10, timeout=500)
    print(f"  返回值: {ret} ({driver.get_error_name(ret)})")
    print(f"  收到消息数: {len(msgs)}")

    if ret == STATUS_NOERROR and msgs:
        print("  消息内容:")
        for i, msg in enumerate(msgs[:5]):  # 最多显示 5 条
            print(f"    [{i}] {msg}")
    elif ret == ERR_BUFFER_EMPTY:
        print("  (缓冲区为空，没有收到 CAN 消息)")
    elif ret == ERR_TIMEOUT:
        print("  (超时，没有收到 CAN 消息)")

    # 断开通道
    print("\n调用 PassThruDisconnect()...")
    ret = driver.disconnect()
    print(f"  返回值: {ret} ({driver.get_error_name(ret)})")

    # 关闭设备
    driver.close()

    return True


def test_connect_iso15765(driver: J2534Driver):
    """测试 4: ISO15765 通道连接（UDS 诊断）"""
    print("\n" + "="*60)
    print("测试 4: ISO15765 通道连接 (UDS 诊断)")
    print("="*60)

    # 打开设备
    ret, device_id = driver.open()
    if ret != STATUS_NOERROR:
        print(f"✗ 设备打开失败: {driver.get_error_name(ret)}")
        return False

    print(f"设备已打开, DeviceID={device_id}")

    # 连接 ISO15765 通道
    print("\n调用 PassThruConnect(ISO15765, 500000)...")
    ret, channel_id = driver.connect(
        protocol_id=ISO15765,
        flags=0,
        baudrate=500000
    )
    print(f"  返回值: {ret} ({driver.get_error_name(ret)})")
    print(f"  ChannelID: {channel_id}")

    if ret != STATUS_NOERROR:
        print(f"  错误描述: {driver.get_last_error()}")
        driver.close()
        return False

    print("✓ ISO15765 通道连接成功")

    # 断开通道
    ret = driver.disconnect()
    driver.close()

    return True


def run_all_tests():
    """运行所有测试"""
    print("="*60)
    print("J2534 本地调用测试 - Phase 0")
    print("="*60)
    print(f"Python 版本: {sys.version}")
    print(f"Python 架构: {8 * ctypes.sizeof(ctypes.c_void_p)} 位")

    if 8 * ctypes.sizeof(ctypes.c_void_p) != 32:
        print("\n⚠️ 警告: 当前使用的是 64 位 Python")
        print("   Scanmatik J2534 DLL 是 32 位的，可能无法加载")
        print("   如果加载失败，请安装 32 位 Python")

    # 测试 1: DLL 加载
    driver = test_dll_load()
    if not driver:
        print("\n测试终止：无法加载 DLL")
        return

    # 测试 2: 设备打开/关闭
    success = test_open_close(driver)
    if not success:
        print("\n⚠️ 设备打开失败，可能原因：")
        print("   1. SM2/SM3 USB 设备未连接")
        print("   2. 设备被其他程序占用（如 GDS2）")
        print("   3. 驱动未正确安装")
        return

    # 测试 3: CAN 通道
    test_connect_can(driver)

    # 测试 4: ISO15765 通道
    test_connect_iso15765(driver)

    print("\n" + "="*60)
    print("测试完成")
    print("="*60)


if __name__ == "__main__":
    try:
        run_all_tests()
    except Exception as e:
        print(f"\n发生异常: {e}")
        import traceback
        traceback.print_exc()

    input("\n按 Enter 键退出...")
