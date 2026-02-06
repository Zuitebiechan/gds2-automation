# VCI Proxy - J2534 Network Proxy
# 将本地 J2534 设备通过反向连接暴露给云端

__version__ = "0.2.0"

from .protocol import (
    MAGIC,
    HEADER_SIZE,
    MsgType,
    Message,
    ProtocolEncoder,
    ProtocolDecoder,
)
from .j2534_driver import J2534Driver
from .reverse_client import ReverseProxyClient
from .reverse_server import ReverseProxyServer

__all__ = [
    "MAGIC",
    "HEADER_SIZE",
    "MsgType",
    "Message",
    "ProtocolEncoder",
    "ProtocolDecoder",
    "J2534Driver",
    "ReverseProxyClient",
    "ReverseProxyServer",
]
