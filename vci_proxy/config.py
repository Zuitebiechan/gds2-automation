"""
VCI Proxy configuration.

Centralized, immutable configuration using frozen dataclasses.
"""

import os
from dataclasses import dataclass
from typing import Mapping, Optional


READ_AHEAD_ENABLED_ENV = "VCI_PROXY_READ_AHEAD"
READ_AHEAD_WINDOW_MS_ENV = "VCI_PROXY_READ_AHEAD_WINDOW_MS"
READ_AHEAD_MAX_READS_ENV = "VCI_PROXY_READ_AHEAD_MAX_READS"
READ_AHEAD_READ_TIMEOUT_MS_ENV = "VCI_PROXY_READ_AHEAD_READ_TIMEOUT_MS"
READ_AHEAD_MAX_MESSAGES_ENV = "VCI_PROXY_READ_AHEAD_MAX_MESSAGES"

READ_AHEAD_ENV_NAMES = (
    READ_AHEAD_ENABLED_ENV,
    READ_AHEAD_WINDOW_MS_ENV,
    READ_AHEAD_MAX_READS_ENV,
    READ_AHEAD_READ_TIMEOUT_MS_ENV,
    READ_AHEAD_MAX_MESSAGES_ENV,
)

_TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}
_FALSE_VALUES = {"0", "false", "no", "off", "disabled", ""}


def _resolve_environ(environ: Mapping[str, str] | None = None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def env_bool(
    name: str,
    default: bool = False,
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Read a boolean environment value, falling back on invalid input."""
    raw = _resolve_environ(environ).get(name)
    if raw is None:
        return default
    value = str(raw).strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    return default


def env_int(
    name: str,
    default: int,
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Read an integer environment value, falling back on invalid input."""
    raw = _resolve_environ(environ).get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        return default


def read_ahead_env_is_configured(
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return True when any shared read-ahead env setting is present."""
    env = _resolve_environ(environ)
    return any(name in env for name in READ_AHEAD_ENV_NAMES)


@dataclass(frozen=True)
class ReadMsgsCacheConfig:
    """ReadMsgs BUFFER_EMPTY short-circuit cache."""
    enabled: bool = True
    ttl_ms: int = 150
    post_write_bypass_ms: int = 150
    active_ttl_ms: int = 25
    active_window_ms: int = 500
    max_cacheable_timeout_ms: int = 25


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
class ReadAheadConfig:
    """Local-side ReadMsgs read-ahead after successful writes."""
    enabled: bool = False
    window_ms: int = 200
    max_reads: int = 3
    read_timeout_ms: int = 0
    max_messages: int = 16


def read_ahead_config_from_env(
    base: ReadAheadConfig | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> ReadAheadConfig:
    """Apply shared read-ahead environment settings on top of a base config."""
    base = base or ReadAheadConfig()
    return ReadAheadConfig(
        enabled=env_bool(READ_AHEAD_ENABLED_ENV, base.enabled, environ=environ),
        window_ms=env_int(READ_AHEAD_WINDOW_MS_ENV, base.window_ms, environ=environ),
        max_reads=env_int(READ_AHEAD_MAX_READS_ENV, base.max_reads, environ=environ),
        read_timeout_ms=env_int(
            READ_AHEAD_READ_TIMEOUT_MS_ENV,
            base.read_timeout_ms,
            environ=environ,
        ),
        max_messages=env_int(
            READ_AHEAD_MAX_MESSAGES_ENV,
            base.max_messages,
            environ=environ,
        ),
    )


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
    read_ahead: ReadAheadConfig = ReadAheadConfig()
    tls: TlsConfig = TlsConfig()

    @classmethod
    def from_args(cls, **kwargs) -> "ProxyConfig":
        """Build config from CLI argument dict.

        Accepts flat keys like auth_token, no_read_cache, read_cache_ttl, etc.
        """
        environ = kwargs.get("environ")
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
            post_write_bypass_ms=kwargs.get("read_cache_post_write_bypass_ms", 150),
            active_ttl_ms=kwargs.get("read_cache_active_ttl_ms", 25),
            active_window_ms=kwargs.get("read_cache_active_window_ms", 500),
            max_cacheable_timeout_ms=kwargs.get("read_cache_max_timeout_ms", 25),
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
        read_ahead_defaults = read_ahead_config_from_env(environ=environ)
        read_ahead = ReadAheadConfig(
            enabled=(
                read_ahead_defaults.enabled
                if kwargs.get("read_ahead_enabled") is None
                else bool(kwargs.get("read_ahead_enabled"))
            ),
            window_ms=(
                read_ahead_defaults.window_ms
                if kwargs.get("read_ahead_window_ms") is None
                else kwargs.get("read_ahead_window_ms")
            ),
            max_reads=(
                read_ahead_defaults.max_reads
                if kwargs.get("read_ahead_max_reads") is None
                else kwargs.get("read_ahead_max_reads")
            ),
            read_timeout_ms=(
                read_ahead_defaults.read_timeout_ms
                if kwargs.get("read_ahead_read_timeout_ms") is None
                else kwargs.get("read_ahead_read_timeout_ms")
            ),
            max_messages=(
                read_ahead_defaults.max_messages
                if kwargs.get("read_ahead_max_messages") is None
                else kwargs.get("read_ahead_max_messages")
            ),
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
            read_ahead=read_ahead,
            tls=tls,
        )
