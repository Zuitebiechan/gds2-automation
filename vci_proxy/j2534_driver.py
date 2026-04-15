"""
J2534 驱动封装

Wraps any SAE J2534-compliant DLL with auto-discovery via Windows registry.
"""

import os
import sys
import struct
import ctypes
from ctypes import (
    POINTER, Structure, byref,
    c_long, c_ulong, c_ubyte, c_char_p, c_void_p,
    create_string_buffer
)
from typing import Optional, Tuple, List
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)

_SUPPORTED_ARCHITECTURES = ("x86", "x64")


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

# IOCTL IDs
IOCTL_GET_CONFIG = 0x01
IOCTL_SET_CONFIG = 0x02
IOCTL_READ_VBATT = 0x03
IOCTL_FIVE_BAUD_INIT = 0x04
IOCTL_FAST_INIT = 0x05
IOCTL_CLEAR_TX_BUFFER = 0x07
IOCTL_CLEAR_RX_BUFFER = 0x08
IOCTL_CLEAR_PERIODIC_MSGS = 0x09
IOCTL_CLEAR_MSG_FILTERS = 0x0A

IOCTL_NAMES = {
    IOCTL_GET_CONFIG: "GET_CONFIG",
    IOCTL_SET_CONFIG: "SET_CONFIG",
    IOCTL_READ_VBATT: "READ_VBATT",
    IOCTL_FIVE_BAUD_INIT: "FIVE_BAUD_INIT",
    IOCTL_FAST_INIT: "FAST_INIT",
    IOCTL_CLEAR_TX_BUFFER: "CLEAR_TX_BUFFER",
    IOCTL_CLEAR_RX_BUFFER: "CLEAR_RX_BUFFER",
    IOCTL_CLEAR_PERIODIC_MSGS: "CLEAR_PERIODIC_MSGS",
    IOCTL_CLEAR_MSG_FILTERS: "CLEAR_MSG_FILTERS",
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


class SCONFIG(Structure):
    """J2534 配置参数结构"""
    _fields_ = [
        ("Parameter", c_ulong),
        ("Value", c_ulong),
    ]


class SCONFIG_LIST(Structure):
    """J2534 配置参数列表"""
    _fields_ = [
        ("NumOfParams", c_ulong),
        ("ConfigPtr", POINTER(SCONFIG)),
    ]


# ============================================================================
# J2534 Registry Discovery
# ============================================================================


# Standard registry key per SAE J2534-1 specification
J2534_REGISTRY_KEY = r"SOFTWARE\PassThruSupport.04.04"


def get_python_architecture() -> str:
    """Return the current Python process architecture."""
    return "x64" if struct.calcsize("P") * 8 == 64 else "x86"


def normalize_architecture(value: object) -> Optional[str]:
    """Normalize architecture values to x86/x64/None."""
    text = str(value or "").strip().lower()
    if text in _SUPPORTED_ARCHITECTURES:
        return text
    if text in {"32", "32-bit", "win32", "x32", "i386", "i686"}:
        return "x86"
    if text in {"64", "64-bit", "amd64", "x86_64"}:
        return "x64"
    return None


def normalize_dll_path(dll_path: str) -> str:
    """Return a normalized DLL path used for stable identity comparisons."""
    return os.path.normcase(os.path.abspath(str(dll_path)))


def get_dll_architecture(dll_path: str) -> Optional[str]:
    """Best-effort PE architecture detection for a J2534 DLL."""
    try:
        with open(dll_path, "rb") as fh:
            header = fh.read(4096)
    except OSError:
        return None

    if len(header) < 64 or header[:2] != b"MZ":
        return None

    pe_offset = struct.unpack_from("<I", header, 0x3C)[0]
    if pe_offset + 6 > len(header):
        return None
    if header[pe_offset:pe_offset + 4] != b"PE\0\0":
        return None

    machine = struct.unpack_from("<H", header, pe_offset + 4)[0]
    if machine == 0x014C:
        return "x86"
    if machine == 0x8664:
        return "x64"
    return None


def _driver_preference_key(driver: dict[str, object]) -> tuple:
    name = str(driver.get("name") or "")
    vendor = str(driver.get("vendor") or "")
    dll_path = str(driver.get("dll_path") or "")
    name_lower = name.lower()
    vendor_lower = vendor.lower()
    is_scanmatik = "scanmatik" in name_lower or "scanmatik" in vendor_lower
    is_sm = name_lower.startswith("sm2") or name_lower.startswith("sm3")
    architecture = normalize_architecture(driver.get("architecture"))
    known_arch_priority = 0 if architecture is not None else 1
    vendor_priority = 0 if (is_scanmatik or is_sm) else 1
    return (
        vendor_priority,
        known_arch_priority,
        name_lower,
        vendor_lower,
        normalize_dll_path(dll_path) if dll_path else "",
    )


def select_best_j2534_driver(drivers: list[dict[str, object]] | None) -> Optional[dict[str, object]]:
    """Select the most deterministic auto-detect candidate from discovered drivers."""
    if not drivers:
        return None
    return sorted(drivers, key=_driver_preference_key)[0]


def discover_j2534_drivers() -> list[dict[str, object]]:
    """Discover installed J2534 drivers from Windows registry.

    Reads HKLM\\SOFTWARE\\PassThruSupport.04.04 which is the standard
    location where SAE J2534-compliant drivers register themselves.

    Returns:
        List of dicts with keys such as 'name', 'dll_path', 'vendor',
        'architecture', and 'compatible'
        Sorted by name. Empty list on non-Windows or if no drivers found.
    """
    if sys.platform != 'win32':
        return []

    import winreg
    drivers = []
    seen_paths: set[str] = set()
    python_arch = get_python_architecture()

    for hive_flag in (winreg.KEY_READ, winreg.KEY_READ | winreg.KEY_WOW64_32KEY):
        try:
            root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, J2534_REGISTRY_KEY, 0, hive_flag)
        except OSError:
            continue

        try:
            idx = 0
            while True:
                try:
                    subkey_name = winreg.EnumKey(root, idx)
                    idx += 1
                except OSError:
                    break

                try:
                    subkey = winreg.OpenKey(root, subkey_name, 0, hive_flag)
                    try:
                        dll_path, _ = winreg.QueryValueEx(subkey, 'FunctionLibrary')
                        vendor = ''
                        try:
                            vendor, _ = winreg.QueryValueEx(subkey, 'Vendor')
                        except OSError:
                            pass

                        name = subkey_name
                        try:
                            name, _ = winreg.QueryValueEx(subkey, 'Name')
                        except OSError:
                            pass

                        if dll_path and os.path.exists(dll_path):
                            normalized_path = normalize_dll_path(dll_path)
                            # Avoid duplicates (same DLL from 32/64-bit views)
                            if normalized_path not in seen_paths:
                                dll_arch = get_dll_architecture(dll_path)
                                architecture = normalize_architecture(dll_arch)
                                drivers.append({
                                    'name': name,
                                    'dll_path': dll_path,
                                    'vendor': vendor,
                                    'architecture': architecture,
                                    'compatible': architecture in (None, python_arch),
                                })
                                seen_paths.add(normalized_path)
                    finally:
                        winreg.CloseKey(subkey)
                except OSError:
                    continue
        finally:
            winreg.CloseKey(root)

    drivers.sort(key=_driver_preference_key)
    return drivers


# ============================================================================
# J2534 驱动类
# ============================================================================

class J2534Driver:
    """J2534 驱动封装"""

    # Legacy fallback paths (Scanmatik) for systems without registry entries
    FALLBACK_DLL_PATHS = [
        r"C:\Program Files (x86)\Scanmatik\smj2534_0404_usb_sm3.dll",
        r"C:\Program Files (x86)\Scanmatik\smj2534.dll",
    ]

    def __init__(self, dll_path: Optional[str] = None):
        self.dll = None
        self.dll_path = None
        self._device_id: Optional[int] = None
        self._channels: dict = {}  # channel_id -> info
        python_arch = get_python_architecture()
        load_failures: List[Tuple[str, str]] = []

        if dll_path:
            # Explicit path provided (from config/GUI)
            paths_to_try = [dll_path]
        else:
            # Auto-discover: registry first, then Scanmatik fallback
            discovered = discover_j2534_drivers()
            paths_to_try = [d['dll_path'] for d in discovered]
            if not paths_to_try:
                paths_to_try = self.FALLBACK_DLL_PATHS
                logger.info("No J2534 drivers in registry, trying Scanmatik fallback paths")
            else:
                names = ', '.join(d['name'] for d in discovered)
                logger.info(f"Found {len(discovered)} J2534 driver(s) in registry: {names}")

        for path in paths_to_try:
            if not path:
                continue
            if not os.path.exists(path):
                load_failures.append((path, "path does not exist"))
                continue

            dll_arch = get_dll_architecture(path)
            if dll_arch is not None and dll_arch != python_arch:
                load_failures.append(
                    (path, f"incompatible architecture: DLL is {dll_arch}, Python is {python_arch}")
                )
                logger.warning(
                    "Skipping J2534 DLL due to architecture mismatch path=%s dll_arch=%s python_arch=%s",
                    path,
                    dll_arch,
                    python_arch,
                )
                continue

            try:
                self.dll = ctypes.WinDLL(path)
                self.dll_path = path
                break
            except OSError as exc:
                load_failures.append((path, str(exc)))
                logger.warning("Failed to load J2534 DLL path=%s error=%s", path, exc)
                continue

        if not self.dll:
            detail = "; ".join(f"{path} -> {reason}" for path, reason in load_failures)
            raise RuntimeError(
                "无法加载 J2534 DLL. "
                "Please install a J2534-compatible VCI driver, "
                "or specify the DLL path in client settings. "
                f"Python architecture: {python_arch}. "
                f"Tried: {detail or 'no valid DLL paths'}"
            )

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

    def ioctl(self, channel_id: int, ioctl_id: int,
              input_data: Optional[bytes] = None) -> Tuple[int, Optional[bytes]]:
        """
        执行 IOCTL 操作

        input_data format for SET_CONFIG/GET_CONFIG:
            4 bytes: NumOfParams (big-endian)
            N * 8 bytes: Parameter(4) + Value(4) (big-endian)

        Returns: (return_code, output_data)
        """
        ioctl_label = IOCTL_NAMES.get(ioctl_id, f"0x{ioctl_id:02x}")
        logger.info(f"ioctl(ch={channel_id}, id={ioctl_label})")

        # SET_CONFIG (0x02) - 设置配置参数
        if ioctl_id == IOCTL_SET_CONFIG and input_data:
            return self._ioctl_set_config(channel_id, input_data)

        # GET_CONFIG (0x01) - 获取配置参数
        if ioctl_id == IOCTL_GET_CONFIG and input_data:
            return self._ioctl_get_config(channel_id, input_data)

        # READ_VBATT (0x03) - 读取电池电压
        if ioctl_id == IOCTL_READ_VBATT:
            voltage = c_ulong()
            ret = self.dll.PassThruIoctl(channel_id, ioctl_id, None, byref(voltage))
            if ret == STATUS_NOERROR:
                return ret, struct.pack('>I', voltage.value)
            return ret, None

        # CLEAR_TX_BUFFER (0x07), CLEAR_RX_BUFFER (0x08), etc.
        if ioctl_id in (IOCTL_CLEAR_TX_BUFFER, IOCTL_CLEAR_RX_BUFFER,
                        IOCTL_CLEAR_PERIODIC_MSGS, IOCTL_CLEAR_MSG_FILTERS):
            ret = self.dll.PassThruIoctl(channel_id, ioctl_id, None, None)
            return ret, None

        # 其他 IOCTL - 直接调用
        ret = self.dll.PassThruIoctl(channel_id, ioctl_id, None, None)
        return ret, None

    def _ioctl_set_config(self, channel_id: int,
                          input_data: bytes) -> Tuple[int, Optional[bytes]]:
        """SET_CONFIG: 将序列化的参数写入真实 DLL"""
        if len(input_data) < 4:
            logger.error(f"SET_CONFIG: input_data too short: {len(input_data)}")
            return ERR_FAILED, None

        num_params = struct.unpack('>I', input_data[:4])[0]
        expected_len = 4 + num_params * 8
        if len(input_data) < expected_len:
            logger.error(f"SET_CONFIG: input_data too short: {len(input_data)} < {expected_len}")
            return ERR_FAILED, None

        params = []
        offset = 4
        for _ in range(num_params):
            param_id, value = struct.unpack('>II', input_data[offset:offset + 8])
            params.append((param_id, value))
            offset += 8

        # 构建 SCONFIG_LIST
        config_array = (SCONFIG * num_params)()
        for i, (param_id, value) in enumerate(params):
            config_array[i].Parameter = param_id
            config_array[i].Value = value
            logger.info(f"  SET param=0x{param_id:04x} value={value}")

        config_list = SCONFIG_LIST()
        config_list.NumOfParams = num_params
        config_list.ConfigPtr = ctypes.cast(config_array, POINTER(SCONFIG))

        ret = self.dll.PassThruIoctl(
            channel_id, IOCTL_SET_CONFIG, byref(config_list), None
        )
        logger.info(f"  SET_CONFIG -> {self.get_error_name(ret)}")
        return ret, None

    def _ioctl_get_config(self, channel_id: int,
                          input_data: bytes) -> Tuple[int, Optional[bytes]]:
        """GET_CONFIG: 从真实 DLL 读取配置参数"""
        if len(input_data) < 4:
            logger.error(f"GET_CONFIG: input_data too short: {len(input_data)}")
            return ERR_FAILED, None

        num_params = struct.unpack('>I', input_data[:4])[0]
        expected_len = 4 + num_params * 8
        if len(input_data) < expected_len:
            logger.error(f"GET_CONFIG: input_data too short: {len(input_data)} < {expected_len}")
            return ERR_FAILED, None

        param_ids = []
        offset = 4
        for _ in range(num_params):
            param_id, _ = struct.unpack('>II', input_data[offset:offset + 8])
            param_ids.append(param_id)
            offset += 8

        # 构建 SCONFIG_LIST (Parameter 填入 ID, Value 由 DLL 填充)
        config_array = (SCONFIG * num_params)()
        for i, param_id in enumerate(param_ids):
            config_array[i].Parameter = param_id
            config_array[i].Value = 0

        config_list = SCONFIG_LIST()
        config_list.NumOfParams = num_params
        config_list.ConfigPtr = ctypes.cast(config_array, POINTER(SCONFIG))

        ret = self.dll.PassThruIoctl(
            channel_id, IOCTL_GET_CONFIG, byref(config_list), None
        )

        if ret == STATUS_NOERROR:
            # 序列化结果: NumOfParams + N * (Parameter + Value)
            output = struct.pack('>I', num_params)
            for i in range(num_params):
                output += struct.pack('>II',
                                      config_array[i].Parameter,
                                      config_array[i].Value)
                logger.info(f"  GET param=0x{config_array[i].Parameter:04x} "
                          f"value={config_array[i].Value}")
            return ret, output

        logger.info(f"  GET_CONFIG -> {self.get_error_name(ret)}")
        return ret, None

    @property
    def is_open(self) -> bool:
        """设备是否已打开"""
        return self._device_id is not None
