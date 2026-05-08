from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from vci_proxy.auth import compute_signature, verify_signature
from vci_proxy.config import (
    AuthConfig,
    IoctlCacheConfig,
    LOCAL_SWEEP_MIN_ITEM_INTERVAL_FLOOR_MS,
    LocalSweepConfig,
    ProxyConfig,
    ReadAheadConfig,
    ReadMsgsCacheConfig,
)


def test_compute_signature_is_deterministic_for_token_and_timestamp() -> None:
    signature = compute_signature("shared-secret", 1_700_000_000)

    assert signature.hex() == (
        "dc4cf12a7e3aad4cefc5ffbc161f1b7ca10641879611496b2a3891f8a923acb8"
    )


def test_verify_signature_accepts_matching_hmac_within_allowed_drift(monkeypatch) -> None:
    timestamp = 1_700_000_000
    signature = compute_signature("shared-secret", timestamp)
    monkeypatch.setattr("vci_proxy.auth.time.time", lambda: timestamp + 120)

    valid, reason = verify_signature("shared-secret", timestamp, signature)

    assert (valid, reason) == (True, "ok")


def test_verify_signature_rejects_excessive_drift_and_signature_mismatch(monkeypatch) -> None:
    timestamp = 1_700_000_000
    signature = compute_signature("shared-secret", timestamp)

    monkeypatch.setattr("vci_proxy.auth.time.time", lambda: timestamp + 301)
    valid, reason = verify_signature("shared-secret", timestamp, signature)
    assert valid is False
    assert "timestamp drift too large" in reason

    monkeypatch.setattr("vci_proxy.auth.time.time", lambda: timestamp)
    valid, reason = verify_signature("shared-secret", timestamp, b"x" * 32)
    assert (valid, reason) == (False, "HMAC signature mismatch")


def test_proxy_config_from_args_maps_flat_cli_flags_to_nested_configs() -> None:
    config = ProxyConfig.from_args(
        auth_token="shared-secret",
        auth_timeout_s=7,
        no_read_cache=True,
        read_cache_ttl=75,
        read_cache_post_write_bypass_ms=25,
        read_cache_active_ttl_ms=15,
        read_cache_active_window_ms=300,
        read_cache_max_timeout_ms=10,
        no_filter_dedup=True,
        no_vbatt_cache=True,
        vbatt_ttl=9,
        no_ioctl_cache=True,
        ioctl_ttl=11,
        read_ahead_enabled=True,
        read_ahead_window_ms=125,
        read_ahead_max_reads=4,
        read_ahead_read_timeout_ms=5,
        read_ahead_max_messages=12,
        read_ahead_max_empty_reads=6,
        read_ahead_max_consecutive_empty_reads=2,
        read_ahead_transaction_enabled=True,
        local_sweep_enabled=True,
        local_sweep_mode="shadow_local",
        local_sweep_min_cycles=3,
        local_sweep_max_items=12,
        local_sweep_allow_gm_a9_packet=False,
        local_sweep_shadow_allow_gm_a9_packet=True,
        local_sweep_min_item_interval_ms=500,
        local_sweep_shadow_max_seconds=30,
        local_sweep_plan_delay_ms=250,
    )

    assert config == ProxyConfig(
        read_msgs_cache=ReadMsgsCacheConfig(
            enabled=False,
            ttl_ms=75,
            post_write_bypass_ms=25,
            active_ttl_ms=15,
            active_window_ms=300,
            max_cacheable_timeout_ms=10,
        ),
        auth=AuthConfig(enabled=True, token="shared-secret", auth_timeout_s=7),
        vbatt_cache=config.vbatt_cache.__class__(enabled=False, ttl_s=9),
        filter_dedup=config.filter_dedup.__class__(enabled=False),
        ioctl_cache=IoctlCacheConfig(enabled=False, ttl_s=11),
        read_ahead=ReadAheadConfig(
            enabled=True,
            window_ms=125,
            max_reads=4,
            read_timeout_ms=5,
            max_messages=12,
            max_empty_reads=6,
            max_consecutive_empty_reads=2,
            transaction_enabled=True,
        ),
        local_sweep=LocalSweepConfig(
            enabled=True,
            mode="shadow_local",
            min_cycles=3,
            max_items=12,
            allow_gm_a9_packet=False,
            shadow_allow_gm_a9_packet=True,
            min_item_interval_ms=500,
            shadow_max_seconds=30,
            plan_delay_ms=250,
        ),
    )


def test_proxy_config_from_args_uses_shared_read_ahead_env_defaults() -> None:
    config = ProxyConfig.from_args(
        environ={
            "VCI_PROXY_READ_AHEAD": "1",
            "VCI_PROXY_READ_AHEAD_WINDOW_MS": "175",
            "VCI_PROXY_READ_AHEAD_MAX_READS": "5",
            "VCI_PROXY_READ_AHEAD_READ_TIMEOUT_MS": "2",
            "VCI_PROXY_READ_AHEAD_MAX_MESSAGES": "9",
            "VCI_PROXY_READ_AHEAD_MAX_EMPTY_READS": "7",
            "VCI_PROXY_READ_AHEAD_MAX_CONSECUTIVE_EMPTY_READS": "3",
            "VCI_PROXY_READ_AHEAD_TRANSACTION": "1",
            "VCI_PROXY_READ_AHEAD_TRANSACTION_MAX_NETWORK_MS": "640",
            "VCI_PROXY_READ_AHEAD_TRANSACTION_COOLDOWN_MS": "3000",
        }
    )

    assert config.read_ahead == ReadAheadConfig(
        enabled=True,
        window_ms=175,
        max_reads=5,
        read_timeout_ms=2,
        max_messages=9,
        max_empty_reads=7,
        max_consecutive_empty_reads=3,
        transaction_enabled=True,
        transaction_max_network_ms=640,
        transaction_cooldown_ms=3000,
    )


def test_proxy_config_explicit_read_ahead_args_override_env_defaults() -> None:
    config = ProxyConfig.from_args(
        environ={
            "VCI_PROXY_READ_AHEAD": "1",
            "VCI_PROXY_READ_AHEAD_WINDOW_MS": "175",
            "VCI_PROXY_READ_AHEAD_MAX_READS": "5",
            "VCI_PROXY_READ_AHEAD_READ_TIMEOUT_MS": "2",
            "VCI_PROXY_READ_AHEAD_MAX_MESSAGES": "9",
            "VCI_PROXY_READ_AHEAD_MAX_EMPTY_READS": "7",
            "VCI_PROXY_READ_AHEAD_MAX_CONSECUTIVE_EMPTY_READS": "3",
            "VCI_PROXY_READ_AHEAD_TRANSACTION": "1",
            "VCI_PROXY_READ_AHEAD_TRANSACTION_MAX_NETWORK_MS": "640",
            "VCI_PROXY_READ_AHEAD_TRANSACTION_COOLDOWN_MS": "3000",
        },
        read_ahead_enabled=False,
        read_ahead_window_ms=0,
        read_ahead_max_reads=1,
        read_ahead_read_timeout_ms=0,
        read_ahead_max_messages=4,
        read_ahead_max_empty_reads=0,
        read_ahead_max_consecutive_empty_reads=1,
        read_ahead_transaction_enabled=False,
        read_ahead_transaction_max_network_ms=0,
        read_ahead_transaction_cooldown_ms=0,
    )

    assert config.read_ahead == ReadAheadConfig(
        enabled=False,
        window_ms=0,
        max_reads=1,
        read_timeout_ms=0,
        max_messages=4,
        max_empty_reads=0,
        max_consecutive_empty_reads=1,
        transaction_enabled=False,
        transaction_max_network_ms=0,
        transaction_cooldown_ms=0,
    )


def test_proxy_config_read_ahead_env_falls_back_on_invalid_values() -> None:
    config = ProxyConfig.from_args(
        environ={
            "VCI_PROXY_READ_AHEAD": "maybe",
            "VCI_PROXY_READ_AHEAD_WINDOW_MS": "bad",
            "VCI_PROXY_READ_AHEAD_MAX_READS": "",
        }
    )

    assert config.read_ahead == ReadAheadConfig(
        enabled=False,
        window_ms=200,
        max_reads=3,
        read_timeout_ms=0,
        max_messages=16,
    )


def test_proxy_config_uses_shared_local_sweep_env_defaults() -> None:
    config = ProxyConfig.from_args(
        environ={
            "VCI_PROXY_LOCAL_SWEEP": "1",
            "VCI_PROXY_LOCAL_SWEEP_MODE": "shadow_local",
            "VCI_PROXY_LOCAL_SWEEP_MIN_CYCLES": "4",
            "VCI_PROXY_LOCAL_SWEEP_MAX_ITEMS": "7",
            "VCI_PROXY_LOCAL_SWEEP_ALLOW_UDS_RDBI": "0",
            "VCI_PROXY_LOCAL_SWEEP_ALLOW_OBD_MODE01": "1",
            "VCI_PROXY_LOCAL_SWEEP_ALLOW_GM_A9_PACKET": "0",
            "VCI_PROXY_LOCAL_SWEEP_SHADOW_ALLOW_GM_A9_PACKET": "1",
            "VCI_PROXY_LOCAL_SWEEP_MAX_RESULT_AGE_MS": "750",
            "VCI_PROXY_LOCAL_SWEEP_MIN_ITEM_INTERVAL_MS": "500",
            "VCI_PROXY_LOCAL_SWEEP_READ_TIMEOUT_MS": "1",
            "VCI_PROXY_LOCAL_SWEEP_SHADOW_MAX_SECONDS": "60",
            "VCI_PROXY_LOCAL_SWEEP_PLAN_DELAY_MS": "450",
            "VCI_PROXY_LOCAL_SWEEP_MISMATCH_THRESHOLD": "2",
            "VCI_PROXY_LOCAL_SWEEP_ERROR_THRESHOLD": "5",
        }
    )

    assert config.local_sweep == LocalSweepConfig(
        enabled=True,
        mode="shadow_local",
        min_cycles=4,
        max_items=7,
        allow_uds_rdbi=False,
        allow_obd_mode01=True,
        allow_gm_a9_packet=False,
        shadow_allow_gm_a9_packet=True,
        max_result_age_ms=750,
        min_item_interval_ms=500,
        read_timeout_ms=1,
        shadow_max_seconds=60,
        plan_delay_ms=450,
        mismatch_threshold=2,
        error_threshold=5,
    )


def test_proxy_config_enforces_local_sweep_shadow_interval_floor() -> None:
    config = ProxyConfig.from_args(
        environ={
            "VCI_PROXY_LOCAL_SWEEP": "1",
            "VCI_PROXY_LOCAL_SWEEP_MODE": "shadow_local",
            "VCI_PROXY_LOCAL_SWEEP_MIN_ITEM_INTERVAL_MS": "5",
        }
    )

    assert config.local_sweep.min_item_interval_ms == (
        LOCAL_SWEEP_MIN_ITEM_INTERVAL_FLOOR_MS
    )


def test_proxy_config_accepts_active_replay_mode() -> None:
    config = ProxyConfig.from_args(
        environ={
            "VCI_PROXY_LOCAL_SWEEP": "1",
            "VCI_PROXY_LOCAL_SWEEP_MODE": "active_replay",
        }
    )

    assert config.local_sweep == LocalSweepConfig(enabled=True, mode="active_replay")


def test_proxy_config_defaults_are_enabled_and_frozen() -> None:
    config = ProxyConfig()

    assert config.read_msgs_cache == ReadMsgsCacheConfig(
        enabled=True,
        ttl_ms=150,
        post_write_bypass_ms=150,
        active_ttl_ms=25,
        active_window_ms=500,
        max_cacheable_timeout_ms=25,
    )
    assert config.auth == AuthConfig(enabled=True, token=None, auth_timeout_s=10)
    assert config.ioctl_cache == IoctlCacheConfig(enabled=True, ttl_s=5)
    assert config.read_ahead == ReadAheadConfig(
        enabled=False,
        window_ms=200,
        max_reads=3,
        read_timeout_ms=0,
        max_messages=16,
    )
    assert config.local_sweep == LocalSweepConfig()

    with pytest.raises(FrozenInstanceError):
        config.auth.enabled = True
