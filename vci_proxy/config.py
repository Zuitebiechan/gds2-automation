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
READ_AHEAD_WRITE_COLLECT_MAX_READS_ENV = (
    "VCI_PROXY_READ_AHEAD_WRITE_COLLECT_MAX_READS"
)
READ_AHEAD_READ_TIMEOUT_MS_ENV = "VCI_PROXY_READ_AHEAD_READ_TIMEOUT_MS"
READ_AHEAD_MAX_MESSAGES_ENV = "VCI_PROXY_READ_AHEAD_MAX_MESSAGES"
READ_AHEAD_MAX_EMPTY_READS_ENV = "VCI_PROXY_READ_AHEAD_MAX_EMPTY_READS"
READ_AHEAD_MAX_CONSECUTIVE_EMPTY_READS_ENV = (
    "VCI_PROXY_READ_AHEAD_MAX_CONSECUTIVE_EMPTY_READS"
)
READ_AHEAD_MIN_DRAIN_MS_ENV = "VCI_PROXY_READ_AHEAD_MIN_DRAIN_MS"
READ_AHEAD_TRANSACTION_ENV = "VCI_PROXY_READ_AHEAD_TRANSACTION"
READ_AHEAD_TRANSACTION_MAX_NETWORK_MS_ENV = (
    "VCI_PROXY_READ_AHEAD_TRANSACTION_MAX_NETWORK_MS"
)
READ_AHEAD_TRANSACTION_COOLDOWN_MS_ENV = (
    "VCI_PROXY_READ_AHEAD_TRANSACTION_COOLDOWN_MS"
)
LOCAL_SWEEP_ENABLED_ENV = "VCI_PROXY_LOCAL_SWEEP"
LOCAL_SWEEP_MODE_ENV = "VCI_PROXY_LOCAL_SWEEP_MODE"
LOCAL_SWEEP_MIN_CYCLES_ENV = "VCI_PROXY_LOCAL_SWEEP_MIN_CYCLES"
LOCAL_SWEEP_MAX_ITEMS_ENV = "VCI_PROXY_LOCAL_SWEEP_MAX_ITEMS"
LOCAL_SWEEP_ALLOW_UDS_RDBI_ENV = "VCI_PROXY_LOCAL_SWEEP_ALLOW_UDS_RDBI"
LOCAL_SWEEP_ALLOW_OBD_MODE01_ENV = "VCI_PROXY_LOCAL_SWEEP_ALLOW_OBD_MODE01"
LOCAL_SWEEP_ALLOW_GM_A9_PACKET_ENV = "VCI_PROXY_LOCAL_SWEEP_ALLOW_GM_A9_PACKET"
LOCAL_SWEEP_SHADOW_ALLOW_GM_A9_PACKET_ENV = (
    "VCI_PROXY_LOCAL_SWEEP_SHADOW_ALLOW_GM_A9_PACKET"
)
LOCAL_SWEEP_MAX_RESULT_AGE_MS_ENV = "VCI_PROXY_LOCAL_SWEEP_MAX_RESULT_AGE_MS"
LOCAL_SWEEP_MIN_ITEM_INTERVAL_MS_ENV = "VCI_PROXY_LOCAL_SWEEP_MIN_ITEM_INTERVAL_MS"
LOCAL_SWEEP_READ_TIMEOUT_MS_ENV = "VCI_PROXY_LOCAL_SWEEP_READ_TIMEOUT_MS"
LOCAL_SWEEP_SHADOW_MAX_SECONDS_ENV = "VCI_PROXY_LOCAL_SWEEP_SHADOW_MAX_SECONDS"
LOCAL_SWEEP_PLAN_DELAY_MS_ENV = "VCI_PROXY_LOCAL_SWEEP_PLAN_DELAY_MS"
LOCAL_SWEEP_MISMATCH_THRESHOLD_ENV = "VCI_PROXY_LOCAL_SWEEP_MISMATCH_THRESHOLD"
LOCAL_SWEEP_ERROR_THRESHOLD_ENV = "VCI_PROXY_LOCAL_SWEEP_ERROR_THRESHOLD"

READ_AHEAD_ENV_NAMES = (
    READ_AHEAD_ENABLED_ENV,
    READ_AHEAD_WINDOW_MS_ENV,
    READ_AHEAD_MAX_READS_ENV,
    READ_AHEAD_WRITE_COLLECT_MAX_READS_ENV,
    READ_AHEAD_READ_TIMEOUT_MS_ENV,
    READ_AHEAD_MAX_MESSAGES_ENV,
    READ_AHEAD_MAX_EMPTY_READS_ENV,
    READ_AHEAD_MAX_CONSECUTIVE_EMPTY_READS_ENV,
    READ_AHEAD_MIN_DRAIN_MS_ENV,
    READ_AHEAD_TRANSACTION_ENV,
    READ_AHEAD_TRANSACTION_MAX_NETWORK_MS_ENV,
    READ_AHEAD_TRANSACTION_COOLDOWN_MS_ENV,
)

LOCAL_SWEEP_ENV_NAMES = (
    LOCAL_SWEEP_ENABLED_ENV,
    LOCAL_SWEEP_MODE_ENV,
    LOCAL_SWEEP_MIN_CYCLES_ENV,
    LOCAL_SWEEP_MAX_ITEMS_ENV,
    LOCAL_SWEEP_ALLOW_UDS_RDBI_ENV,
    LOCAL_SWEEP_ALLOW_OBD_MODE01_ENV,
    LOCAL_SWEEP_ALLOW_GM_A9_PACKET_ENV,
    LOCAL_SWEEP_SHADOW_ALLOW_GM_A9_PACKET_ENV,
    LOCAL_SWEEP_MAX_RESULT_AGE_MS_ENV,
    LOCAL_SWEEP_MIN_ITEM_INTERVAL_MS_ENV,
    LOCAL_SWEEP_READ_TIMEOUT_MS_ENV,
    LOCAL_SWEEP_SHADOW_MAX_SECONDS_ENV,
    LOCAL_SWEEP_PLAN_DELAY_MS_ENV,
    LOCAL_SWEEP_MISMATCH_THRESHOLD_ENV,
    LOCAL_SWEEP_ERROR_THRESHOLD_ENV,
)

_TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}
_FALSE_VALUES = {"0", "false", "no", "off", "disabled", ""}
LOCAL_SWEEP_MIN_ITEM_INTERVAL_FLOOR_MS = 250


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


def local_sweep_env_is_configured(
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return True when any shared local sweep env setting is present."""
    env = _resolve_environ(environ)
    return any(name in env for name in LOCAL_SWEEP_ENV_NAMES)


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
    write_collect_max_reads: int = 6
    read_timeout_ms: int = 0
    max_messages: int = 16
    max_empty_reads: int = 0
    max_consecutive_empty_reads: int = 0
    min_drain_ms: int = 0
    transaction_enabled: bool = False
    transaction_max_network_ms: int = 750
    transaction_cooldown_ms: int = 10000


@dataclass(frozen=True)
class LocalSweepConfig:
    """Guarded local Data Display sweep scheduler stage."""
    enabled: bool = False
    mode: str = "observe_only"
    min_cycles: int = 2
    max_items: int = 128
    allow_uds_rdbi: bool = True
    allow_obd_mode01: bool = True
    allow_gm_a9_packet: bool = True
    shadow_allow_gm_a9_packet: bool = False
    max_result_age_ms: int = 1000
    min_item_interval_ms: int = LOCAL_SWEEP_MIN_ITEM_INTERVAL_FLOOR_MS
    read_timeout_ms: int = 0
    shadow_max_seconds: int = 120
    plan_delay_ms: int = 300
    mismatch_threshold: int = 3
    error_threshold: int = 3

    @property
    def observe_only(self) -> bool:
        return self.enabled and self.mode == "observe_only"

    @property
    def shadow_local(self) -> bool:
        return self.enabled and self.mode == "shadow_local"

    @property
    def active_replay(self) -> bool:
        return self.enabled and self.mode == "active_replay"

    @property
    def shadow_transport_enabled(self) -> bool:
        return self.enabled and self.mode in {"shadow_local", "active_replay"}


def _normalized_sweep_mode(value: object, default: str) -> str:
    mode = str(value or "").strip().lower()
    if mode in {"observe_only", "shadow_local", "active_replay"}:
        return mode
    return default


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
        write_collect_max_reads=max(
            0,
            env_int(
                READ_AHEAD_WRITE_COLLECT_MAX_READS_ENV,
                base.write_collect_max_reads,
                environ=environ,
            ),
        ),
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
        max_empty_reads=max(
            0,
            env_int(
                READ_AHEAD_MAX_EMPTY_READS_ENV,
                base.max_empty_reads,
                environ=environ,
            ),
        ),
        max_consecutive_empty_reads=max(
            0,
            env_int(
                READ_AHEAD_MAX_CONSECUTIVE_EMPTY_READS_ENV,
                base.max_consecutive_empty_reads,
                environ=environ,
            ),
        ),
        min_drain_ms=max(
            0,
            env_int(
                READ_AHEAD_MIN_DRAIN_MS_ENV,
                base.min_drain_ms,
                environ=environ,
            ),
        ),
        transaction_enabled=env_bool(
            READ_AHEAD_TRANSACTION_ENV,
            base.transaction_enabled,
            environ=environ,
        ),
        transaction_max_network_ms=max(
            0,
            env_int(
                READ_AHEAD_TRANSACTION_MAX_NETWORK_MS_ENV,
                base.transaction_max_network_ms,
                environ=environ,
            ),
        ),
        transaction_cooldown_ms=max(
            0,
            env_int(
                READ_AHEAD_TRANSACTION_COOLDOWN_MS_ENV,
                base.transaction_cooldown_ms,
                environ=environ,
            ),
        ),
    )


def local_sweep_config_from_env(
    base: LocalSweepConfig | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> LocalSweepConfig:
    """Apply shared local sweep environment settings on top of a base config."""
    base = base or LocalSweepConfig()
    env = _resolve_environ(environ)
    return LocalSweepConfig(
        enabled=env_bool(LOCAL_SWEEP_ENABLED_ENV, base.enabled, environ=env),
        mode=_normalized_sweep_mode(env.get(LOCAL_SWEEP_MODE_ENV), base.mode),
        min_cycles=max(
            1,
            env_int(LOCAL_SWEEP_MIN_CYCLES_ENV, base.min_cycles, environ=env),
        ),
        max_items=max(1, env_int(LOCAL_SWEEP_MAX_ITEMS_ENV, base.max_items, environ=env)),
        allow_uds_rdbi=env_bool(
            LOCAL_SWEEP_ALLOW_UDS_RDBI_ENV,
            base.allow_uds_rdbi,
            environ=env,
        ),
        allow_obd_mode01=env_bool(
            LOCAL_SWEEP_ALLOW_OBD_MODE01_ENV,
            base.allow_obd_mode01,
            environ=env,
        ),
        allow_gm_a9_packet=env_bool(
            LOCAL_SWEEP_ALLOW_GM_A9_PACKET_ENV,
            base.allow_gm_a9_packet,
            environ=env,
        ),
        shadow_allow_gm_a9_packet=env_bool(
            LOCAL_SWEEP_SHADOW_ALLOW_GM_A9_PACKET_ENV,
            base.shadow_allow_gm_a9_packet,
            environ=env,
        ),
        max_result_age_ms=max(
            1,
            env_int(
                LOCAL_SWEEP_MAX_RESULT_AGE_MS_ENV,
                base.max_result_age_ms,
                environ=env,
            ),
        ),
        min_item_interval_ms=max(
            LOCAL_SWEEP_MIN_ITEM_INTERVAL_FLOOR_MS,
            env_int(
                LOCAL_SWEEP_MIN_ITEM_INTERVAL_MS_ENV,
                base.min_item_interval_ms,
                environ=env,
            ),
        ),
        read_timeout_ms=max(
            0,
            env_int(LOCAL_SWEEP_READ_TIMEOUT_MS_ENV, base.read_timeout_ms, environ=env),
        ),
        shadow_max_seconds=max(
            1,
            env_int(
                LOCAL_SWEEP_SHADOW_MAX_SECONDS_ENV,
                base.shadow_max_seconds,
                environ=env,
            ),
        ),
        plan_delay_ms=max(
            0,
            env_int(
                LOCAL_SWEEP_PLAN_DELAY_MS_ENV,
                base.plan_delay_ms,
                environ=env,
            ),
        ),
        mismatch_threshold=max(
            1,
            env_int(
                LOCAL_SWEEP_MISMATCH_THRESHOLD_ENV,
                base.mismatch_threshold,
                environ=env,
            ),
        ),
        error_threshold=max(
            1,
            env_int(
                LOCAL_SWEEP_ERROR_THRESHOLD_ENV,
                base.error_threshold,
                environ=env,
            ),
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
    local_sweep: LocalSweepConfig = LocalSweepConfig()
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
            write_collect_max_reads=max(
                0,
                read_ahead_defaults.write_collect_max_reads
                if kwargs.get("read_ahead_write_collect_max_reads") is None
                else int(kwargs.get("read_ahead_write_collect_max_reads")),
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
            max_empty_reads=max(
                0,
                read_ahead_defaults.max_empty_reads
                if kwargs.get("read_ahead_max_empty_reads") is None
                else int(kwargs.get("read_ahead_max_empty_reads")),
            ),
            max_consecutive_empty_reads=max(
                0,
                read_ahead_defaults.max_consecutive_empty_reads
                if kwargs.get("read_ahead_max_consecutive_empty_reads") is None
                else int(kwargs.get("read_ahead_max_consecutive_empty_reads")),
            ),
            min_drain_ms=max(
                0,
                read_ahead_defaults.min_drain_ms
                if kwargs.get("read_ahead_min_drain_ms") is None
                else int(kwargs.get("read_ahead_min_drain_ms")),
            ),
            transaction_enabled=(
                read_ahead_defaults.transaction_enabled
                if kwargs.get("read_ahead_transaction_enabled") is None
                else bool(kwargs.get("read_ahead_transaction_enabled"))
            ),
            transaction_max_network_ms=max(
                0,
                read_ahead_defaults.transaction_max_network_ms
                if kwargs.get("read_ahead_transaction_max_network_ms") is None
                else int(kwargs.get("read_ahead_transaction_max_network_ms")),
            ),
            transaction_cooldown_ms=max(
                0,
                read_ahead_defaults.transaction_cooldown_ms
                if kwargs.get("read_ahead_transaction_cooldown_ms") is None
                else int(kwargs.get("read_ahead_transaction_cooldown_ms")),
            ),
        )
        local_sweep_defaults = local_sweep_config_from_env(environ=environ)
        local_sweep = LocalSweepConfig(
            enabled=(
                local_sweep_defaults.enabled
                if kwargs.get("local_sweep_enabled") is None
                else bool(kwargs.get("local_sweep_enabled"))
            ),
            mode=_normalized_sweep_mode(
                kwargs.get("local_sweep_mode"),
                local_sweep_defaults.mode,
            ),
            min_cycles=max(
                1,
                local_sweep_defaults.min_cycles
                if kwargs.get("local_sweep_min_cycles") is None
                else int(kwargs.get("local_sweep_min_cycles")),
            ),
            max_items=max(
                1,
                local_sweep_defaults.max_items
                if kwargs.get("local_sweep_max_items") is None
                else int(kwargs.get("local_sweep_max_items")),
            ),
            allow_uds_rdbi=(
                local_sweep_defaults.allow_uds_rdbi
                if kwargs.get("local_sweep_allow_uds_rdbi") is None
                else bool(kwargs.get("local_sweep_allow_uds_rdbi"))
            ),
            allow_obd_mode01=(
                local_sweep_defaults.allow_obd_mode01
                if kwargs.get("local_sweep_allow_obd_mode01") is None
                else bool(kwargs.get("local_sweep_allow_obd_mode01"))
            ),
            allow_gm_a9_packet=(
                local_sweep_defaults.allow_gm_a9_packet
                if kwargs.get("local_sweep_allow_gm_a9_packet") is None
                else bool(kwargs.get("local_sweep_allow_gm_a9_packet"))
            ),
            shadow_allow_gm_a9_packet=(
                local_sweep_defaults.shadow_allow_gm_a9_packet
                if kwargs.get("local_sweep_shadow_allow_gm_a9_packet") is None
                else bool(kwargs.get("local_sweep_shadow_allow_gm_a9_packet"))
            ),
            max_result_age_ms=max(
                1,
                local_sweep_defaults.max_result_age_ms
                if kwargs.get("local_sweep_max_result_age_ms") is None
                else int(kwargs.get("local_sweep_max_result_age_ms")),
            ),
            min_item_interval_ms=max(
                LOCAL_SWEEP_MIN_ITEM_INTERVAL_FLOOR_MS,
                local_sweep_defaults.min_item_interval_ms
                if kwargs.get("local_sweep_min_item_interval_ms") is None
                else int(kwargs.get("local_sweep_min_item_interval_ms")),
            ),
            read_timeout_ms=max(
                0,
                local_sweep_defaults.read_timeout_ms
                if kwargs.get("local_sweep_read_timeout_ms") is None
                else int(kwargs.get("local_sweep_read_timeout_ms")),
            ),
            shadow_max_seconds=max(
                1,
                local_sweep_defaults.shadow_max_seconds
                if kwargs.get("local_sweep_shadow_max_seconds") is None
                else int(kwargs.get("local_sweep_shadow_max_seconds")),
            ),
            plan_delay_ms=max(
                0,
                local_sweep_defaults.plan_delay_ms
                if kwargs.get("local_sweep_plan_delay_ms") is None
                else int(kwargs.get("local_sweep_plan_delay_ms")),
            ),
            mismatch_threshold=max(
                1,
                local_sweep_defaults.mismatch_threshold
                if kwargs.get("local_sweep_mismatch_threshold") is None
                else int(kwargs.get("local_sweep_mismatch_threshold")),
            ),
            error_threshold=max(
                1,
                local_sweep_defaults.error_threshold
                if kwargs.get("local_sweep_error_threshold") is None
                else int(kwargs.get("local_sweep_error_threshold")),
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
            local_sweep=local_sweep,
            tls=tls,
        )
