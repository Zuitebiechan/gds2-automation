from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from vci_proxy.auth import compute_signature, verify_signature
from vci_proxy.config import (
    AuthConfig,
    IoctlCacheConfig,
    ProxyConfig,
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
        no_filter_dedup=True,
        no_vbatt_cache=True,
        vbatt_ttl=9,
        no_ioctl_cache=True,
        ioctl_ttl=11,
    )

    assert config == ProxyConfig(
        read_msgs_cache=ReadMsgsCacheConfig(enabled=False, ttl_ms=75),
        auth=AuthConfig(enabled=True, token="shared-secret", auth_timeout_s=7),
        vbatt_cache=config.vbatt_cache.__class__(enabled=False, ttl_s=9),
        filter_dedup=config.filter_dedup.__class__(enabled=False),
        ioctl_cache=IoctlCacheConfig(enabled=False, ttl_s=11),
    )


def test_proxy_config_defaults_are_enabled_and_frozen() -> None:
    config = ProxyConfig()

    assert config.read_msgs_cache == ReadMsgsCacheConfig(enabled=True, ttl_ms=150)
    assert config.auth == AuthConfig(enabled=False, token=None, auth_timeout_s=10)
    assert config.ioctl_cache == IoctlCacheConfig(enabled=True, ttl_s=5)

    with pytest.raises(FrozenInstanceError):
        config.auth.enabled = True
