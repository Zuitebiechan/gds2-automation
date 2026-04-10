"""
VCI Proxy configuration.

Centralized, immutable configuration using frozen dataclasses.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ReadMsgsCacheConfig:
    """ReadMsgs BUFFER_EMPTY short-circuit cache."""
    enabled: bool = True
    ttl_ms: int = 150


@dataclass(frozen=True)
class AuthConfig:
    """PSK authentication via HMAC-SHA256."""
    enabled: bool = True
    token: Optional[str] = None
    auth_timeout_s: int = 10


@dataclass(frozen=True)
class FilterDeduplicationConfig:
    """StartFilter request deduplication."""
    enabled: bool = True


@dataclass(frozen=True)
class VbattCacheConfig:
    """READ_VBATT response cache (legacy, kept for backward compat)."""
    enabled: bool = True
    ttl_s: int = 5


@dataclass(frozen=True)
class IoctlCacheConfig:
    """Generalized read-only IOCTL response cache."""
    enabled: bool = True
    ttl_s: int = 5


@dataclass(frozen=True)
class TlsConfig:
    """Optional TLS transport settings for the reverse tunnel."""
    enabled: bool = False
    certfile: Optional[str] = None
    keyfile: Optional[str] = None
    ca_file: Optional[str] = None
    server_name: Optional[str] = None
    require_client_cert: bool = False


@dataclass(frozen=True)
class ProxyConfig:
    """Aggregate proxy configuration."""
    read_msgs_cache: ReadMsgsCacheConfig = ReadMsgsCacheConfig()
    auth: AuthConfig = AuthConfig()
    filter_dedup: FilterDeduplicationConfig = FilterDeduplicationConfig()
    vbatt_cache: VbattCacheConfig = VbattCacheConfig()
    ioctl_cache: IoctlCacheConfig = IoctlCacheConfig()
    tls: TlsConfig = TlsConfig()

    @classmethod
    def from_args(cls, **kwargs) -> "ProxyConfig":
        """Build config from CLI argument dict.

        Accepts flat keys like auth_token, no_read_cache, read_cache_ttl, etc.
        """
        auth_token = kwargs.get("auth_token")
        auth_enabled = kwargs.get("auth_enabled", True)
        auth = AuthConfig(
            enabled=bool(auth_enabled),
            token=auth_token,
            auth_timeout_s=kwargs.get("auth_timeout_s", 10),
        )
        read_msgs_cache = ReadMsgsCacheConfig(
            enabled=not kwargs.get("no_read_cache", False),
            ttl_ms=kwargs.get("read_cache_ttl", 150),
        )
        filter_dedup = FilterDeduplicationConfig(
            enabled=not kwargs.get("no_filter_dedup", False),
        )
        vbatt_cache = VbattCacheConfig(
            enabled=not kwargs.get("no_vbatt_cache", False),
            ttl_s=kwargs.get("vbatt_ttl", 5),
        )
        ioctl_cache = IoctlCacheConfig(
            enabled=not kwargs.get("no_ioctl_cache", False),
            ttl_s=kwargs.get("ioctl_ttl", 5),
        )
        tls = TlsConfig(
            enabled=bool(kwargs.get("tls_enabled", False)),
            certfile=kwargs.get("tls_certfile"),
            keyfile=kwargs.get("tls_keyfile"),
            ca_file=kwargs.get("tls_ca_file"),
            server_name=kwargs.get("tls_server_name"),
            require_client_cert=bool(kwargs.get("tls_require_client_cert", False)),
        )
        return cls(
            read_msgs_cache=read_msgs_cache,
            auth=auth,
            filter_dedup=filter_dedup,
            vbatt_cache=vbatt_cache,
            ioctl_cache=ioctl_cache,
            tls=tls,
        )
