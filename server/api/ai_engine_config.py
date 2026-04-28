"""AI engine configuration helpers shared by session API handlers."""

from __future__ import annotations

import json
import os

from diagnostic_platform.runtime.worker_runtime import get_worker_runtime
from server.api.http_utils import read_text_mapping_field
from src.diagnosis.ai_engine import AIEngine


def _runtime():
    return get_worker_runtime()


def _vci_proxy_config_path() -> str:
    config_dir = os.path.join(
        os.environ.get("APPDATA", os.path.expanduser("~")),
        "VCI_Proxy",
    )
    return os.path.join(config_dir, "config.json")


def _load_vci_proxy_config() -> dict[str, object]:
    config_path = _vci_proxy_config_path()
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        return payload if isinstance(payload, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _load_openai_api_key() -> str | None:
    env_key = str(os.environ.get("OPENAI_API_KEY") or "").strip()
    if env_key:
        return env_key
    try:
        config = _load_vci_proxy_config()
        return read_text_mapping_field(config, "openai_api_key") or None
    except ValueError:
        return None


def _load_openai_base_url() -> str | None:
    env_base_url = str(
        os.environ.get("OPENAI_BASE_URL")
        or os.environ.get("OPENAI_API_BASE")
        or ""
    ).strip()
    if env_base_url:
        return env_base_url
    try:
        config = _load_vci_proxy_config()
        return read_text_mapping_field(config, "openai_base_url") or None
    except ValueError:
        return None


def _load_openai_model() -> str:
    env_model = str(os.environ.get("OPENAI_MODEL") or "").strip()
    if env_model:
        return env_model
    try:
        config = _load_vci_proxy_config()
        return read_text_mapping_field(config, "openai_model", default="gpt-5.4") or "gpt-5.4"
    except ValueError:
        return "gpt-5.4"


def _load_openai_reasoning_effort() -> str:
    env_reasoning = str(os.environ.get("OPENAI_REASONING_EFFORT") or "").strip()
    if env_reasoning:
        return env_reasoning
    try:
        config = _load_vci_proxy_config()
        return read_text_mapping_field(config, "openai_reasoning_effort", default="none") or "none"
    except ValueError:
        return "none"


def _load_zhipu_api_key() -> str | None:
    """Legacy config reader retained for older local config validation."""
    try:
        config = _load_vci_proxy_config()
        return read_text_mapping_field(config, "zhipu_api_key") or None
    except ValueError:
        return None


def _build_ai_engine() -> AIEngine:
    """Build the worker-scoped AI engine from persisted config."""
    api_key = _load_openai_api_key()
    if not api_key:
        raise RuntimeError(
            "OpenAI API key not configured. "
            "Set OPENAI_API_KEY or %APPDATA%/VCI_Proxy/config.json under 'openai_api_key'."
        )
    return AIEngine(
        api_key=api_key,
        model=_load_openai_model(),
        base_url=_load_openai_base_url(),
        reasoning_effort=_load_openai_reasoning_effort(),
    )


def get_ai_engine() -> AIEngine:
    """Return the shared worker-scoped AI engine."""
    return _runtime().get_ai_engine(_build_ai_engine)
