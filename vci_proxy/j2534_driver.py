"""
J2534 驱动封装

封装 Scanmatik J2534 DLL，提供 Python 接口
"""

import os
import ctypes
from ctypes import (
    POINTER, Structure, byref,
    c_long, c_ulong, c_ubyte, c_char_p, c_void_p,
    create_string_buffer
)
from typing import Optional, Tuple, List
from dataclasses import dataclass


# ============================================================================
# J2534 常量
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
    ERR_INVALID_FLAGS: "ERR_INVALID_FLAGS",
    ERR_FAILED: "ERR_FAILED",
    ERR_DEVICE_NOT_CONNECTED: "ERR_DEVICE_NOT_CONNECTED",
    ERR_TIMEOUT: "ERR_TIMEOUT",
    ERR_BUFFER_EMPTY: "ERR_BUFFER_EMPTY",
    ERR_DEVICE_IN_USE: "ERR_DEVICE_IN_USE",
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
}


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

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            'protocol_id': self.ProtocolID,
            'rx_status': self.RxStatus,
            'tx_flags': self.TxFlags,
            'timestamp': self.Timestamp,
            'data': bytes(self.Data[:self.DataSize])
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'PASSTHRU_MSG':
        """从字典创建"""
        msg = cls()
        msg.ProtocolID = d.get('protocol_id', 0)
        msg.RxStatus = d.get('rx_status', 0)
        msg.TxFlags = d.get('tx_flags', 0)
        msg.Timestamp = d.get('timestamp', 0)
        data = d.get('data', b'')
        msg.DataSize = len(data)
        for i, b in enumerate(data):
            msg.Data[i] = b
        return msg


# ============================================================================
# J2534 驱动类
# ============================================================================

class J2534Driver:
    """J2534 驱动封装"""

    # 默认 DLL 路径
    DEFAULT_DLL_PATHS = [
        r"C:\Program Files (x86)\Scanmatik\smj2534_0404_usb_sm3.dll",
        r"C:\Program Files (x86)\Scanmatik\smj2534.dll",
    ]

    def __init__(self, dll_path: Optional[str] = None):
        self.dll = None
        self.dll_path = None
        self._device_id: Optional[int] = None
        self._channels: dict = {}  # channel_id -> info

        # 尝试加载 DLL
        paths_to_try = [dll_path] if dll_path else self.DEFAULT_DLL_PATHS

        for path in paths_to_try:
            if path and os.path.exists(path):
                try:
                    self.dll = ctypes.WinDLL(path)
                    self.dll_path = path
                    break
                except OSError:
                    continue

        if not self.dll:
            raise RuntimeError("无法加载 J2534 DLL")

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
            c_ulong, POINTER(PASSTHRU_MSG), POINTER(c_ulong), c_ulong
        ]
        self.dll.PassThruReadMsgs.restype = c_long

        # PassThruWriteMsgs
        self.dll.PassThruWriteMsgs.argtypes = [
            c_ulong, POINTER(PASSTHRU_MSG), POINTER(c_ulong), c_ulong
        ]
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
        self.dll.PassThruReadVersion.argtypes = [c_ulong, c_char_p, c_char_p, c_char_p]
        self.dll.PassThruReadVersion.restype = c_long

        # PassThruGetLastError
        self.dll.PassThruGetLastError.argtypes = [c_char_p]
        self.dll.PassThruGetLastError.restype = c_long

        # PassThruIoctl
        self.dll.PassThruIoctl.argtypes = [c_ulong, c_ulong, c_void_p, c_void_p]
        self.dll.PassThruIoctl.restype = c_long

    def get_error_name(self, code: int) -> str:
        """获取错误码名称"""
        return ERROR_NAMES.get(code, f"ERROR_{code:#x}")

    def get_last_error(self) -> str:
        """获取最后一次错误的描述"""
        buf = create_string_buffer(256)
        self.dll.PassThruGetLastError(buf)
        return buf.value.decode('utf-8', errors='replace')

    def open(self, device_name: Optional[str] = None) -> Tuple[int, int]:
        """
        打开设备

        Returns: (return_code, device_id)
        """
        device_id = c_ulong()
        name = device_name.encode('utf-8') if device_name else None
        ret = self.dll.PassThruOpen(name, byref(device_id))

        if ret == STATUS_NOERROR:
            self._device_id = device_id.value

        return ret, device_id.value

    def close(self, device_id: int) -> int:
        """关闭设备"""
        ret = self.dll.PassThruClose(device_id)
        if ret == STATUS_NOERROR and device_id == self._device_id:
            self._device_id = None
        return ret

    def read_version(self, device_id: int) -> Tuple[int, str, str, str]:
        """
        读取版本信息

        Returns: (return_code, firmware_version, dll_version, api_version)
        """
        fw_ver = create_string_buffer(80)
        dll_ver = create_string_buffer(80)
        api_ver = create_string_buffer(80)

        ret = self.dll.PassThruReadVersion(device_id, fw_ver, dll_ver, api_ver)

        return (ret,
                fw_ver.value.decode('utf-8', errors='replace'),
                dll_ver.value.decode('utf-8', errors='replace'),
                api_ver.value.decode('utf-8', errors='replace'))

    def connect(self, device_id: int, protocol_id: int, flags: int,
                baudrate: int) -> Tuple[int, int]:
        """
        建立协议通道

        Returns: (return_code, channel_id)
        """
        channel_id = c_ulong()
        ret = self.dll.PassThruConnect(
            device_id, protocol_id, flags, baudrate, byref(channel_id)
        )

        if ret == STATUS_NOERROR:
            self._channels[channel_id.value] = {
                'device_id': device_id,
                'protocol_id': protocol_id,
                'baudrate': baudrate
            }

        return ret, channel_id.value

    def disconnect(self, channel_id: int) -> int:
        """断开协议通道"""
        ret = self.dll.PassThruDisconnect(channel_id)
        if ret == STATUS_NOERROR:
            self._channels.pop(channel_id, None)
        return ret

    def read_msgs(self, channel_id: int, num_msgs: int,
                  timeout: int) -> Tuple[int, List[dict]]:
        """
        读取消息

        Returns: (return_code, messages)
        """
        msgs = (PASSTHRU_MSG * num_msgs)()
        num = c_ulong(num_msgs)

        ret = self.dll.PassThruReadMsgs(channel_id, msgs, byref(num), timeout)

        result = [msgs[i].to_dict() for i in range(num.value)]
        return ret, result

    def write_msgs(self, channel_id: int, messages: List[dict],
                   timeout: int) -> Tuple[int, int]:
        """
        发送消息

        Returns: (return_code, num_msgs_written)
        """
        num = len(messages)
        msg_array = (PASSTHRU_MSG * num)()
        for i, m in enumerate(messages):
            msg_array[i] = PASSTHRU_MSG.from_dict(m)

        num_written = c_ulong(num)
        ret = self.dll.PassThruWriteMsgs(channel_id, msg_array, byref(num_written), timeout)

        return ret, num_written.value

    def start_msg_filter(self, channel_id: int, filter_type: int,
                        mask_msg: Optional[dict], pattern_msg: Optional[dict],
                        flow_control_msg: Optional[dict]) -> Tuple[int, int]:
        """
        设置消息过滤器

        Returns: (return_code, filter_id)
        """
        mask = PASSTHRU_MSG.from_dict(mask_msg) if mask_msg else None
        pattern = PASSTHRU_MSG.from_dict(pattern_msg) if pattern_msg else None
        flow = PASSTHRU_MSG.from_dict(flow_control_msg) if flow_control_msg else None

        filter_id = c_ulong()
        ret = self.dll.PassThruStartMsgFilter(
            channel_id, filter_type,
            byref(mask) if mask else None,
            byref(pattern) if pattern else None,
            byref(flow) if flow else None,
            byref(filter_id)
        )

        return ret, filter_id.value

    def stop_msg_filter(self, channel_id: int, filter_id: int) -> int:
        """停止消息过滤器"""
        return self.dll.PassThruStopMsgFilter(channel_id, filter_id)

    @property
    def is_open(self) -> bool:
        """设备是否已打开"""
        return self._device_id is not None
