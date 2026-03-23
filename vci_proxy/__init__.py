# VCI Proxy - J2534 Network Proxy
# 将本地 J2534 设备通过反向连接暴露给云端

__version__ = "0.3.0"

from .protocol import (
    MAGIC,
    HEADER_SIZE,
    MsgType,
    MSG_NAMES,
    Message,
    ProtocolEncoder,
    ProtocolDecoder,
)
from .config import (
    ProxyConfig,
    ReadMsgsCacheConfig,
    AuthConfig,
    FilterDeduplicationConfig,
    VbattCacheConfig,
)
from .cache_read_msgs import ReadMsgsCache
from .cache_filter_dedup import FilterDeduplicationCache
from .cache_vbatt import VbattCache
from .auth import compute_signature, verify_signature
from .benchmark import (
    JsonlBenchmarkWriter,
    compare_benchmark_summaries,
    load_benchmark_events,
    make_proxy_benchmark_event,
    summarize_benchmark_events,
)
from .j2534_driver import J2534Driver
from .reverse_client import ReverseProxyClient
from .reverse_server import ReverseProxyServer

__all__ = [
    "MAGIC",
    "HEADER_SIZE",
    "MsgType",
    "MSG_NAMES",
    "Message",
    "ProtocolEncoder",
    "ProtocolDecoder",
    "ProxyConfig",
    "ReadMsgsCacheConfig",
    "AuthConfig",
    "FilterDeduplicationConfig",
    "VbattCacheConfig",
    "ReadMsgsCache",
    "FilterDeduplicationCache",
    "VbattCache",
    "compute_signature",
    "verify_signature",
    "JsonlBenchmarkWriter",
    "make_proxy_benchmark_event",
    "summarize_benchmark_events",
    "compare_benchmark_summaries",
    "load_benchmark_events",
    "J2534Driver",
    "ReverseProxyClient",
    "ReverseProxyServer",
]
