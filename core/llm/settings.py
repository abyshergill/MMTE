"""Persistent LLM settings (provider, model, endpoint, GGUF options, API keys)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from core.config import CONFIG_DIR
from core.llm.providers import ProviderSpec, get_provider, resolve_provider

LLM_CONFIG_FILE = CONFIG_DIR / "llm_config.json"

TOOL_MODES = ("auto", "native", "prompt", "off")

DEFAULT_LLM_CONFIG: dict[str, Any] = {
    # Local-first default: works with no account and matches the built-in RAG
    # memory, which uses an Ollama embedding model.
    "provider": "Ollama (Local)",
    "api_key": "",
    "endpoint_url": "http://localhost:11434",
    "model_name": "llama3.1",
    "temperature": 0.7,
    "max_tokens": 2048,
    # Local GGUF (llama-cpp-python) options.
    "gguf_path": "",
    "n_ctx": 4096,
    "n_gpu_layers": 0,
    "n_threads": 0,
    "chat_format": "",
    # Ollama context window (num_ctx). Ollama's own default (2-4k tokens) silently
    # truncates this app's system prompt + RAG + terminal scrollback.
    "context_length": 8192,
    # auto: native function calling when the provider supports it, otherwise a
    # JSON-in-prompt protocol.  native / prompt force one; off disables tools.
    "tool_mode": "auto",
    "request_timeout": 120,
    "embedding_endpoint_url": "http://localhost:11434",
    # Mask passwords/tokens in terminal output before it is sent to a cloud model.
    "redact_secrets": True,
    # Last-used endpoint/key/model per provider, so switching providers in the
    # settings dialog restores what was typed for them before.
    "provider_profiles": {},
}

_PROFILE_FIELDS = ("endpoint_url", "api_key", "model_name", "gguf_path")


def _as_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, number))


def _as_float(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, number))


def sanitize(config: dict[str, Any]) -> dict[str, Any]:
    """Coerce a possibly hand-edited config into valid types and ranges."""
    clean = dict(DEFAULT_LLM_CONFIG)
    clean.update({k: v for k, v in config.items() if v is not None})
    for key in ("provider", "api_key", "endpoint_url", "model_name", "gguf_path", "chat_format", "embedding_endpoint_url"):
        clean[key] = str(clean.get(key) or "").strip()
    clean["temperature"] = _as_float(clean.get("temperature"), 0.7, 0.0, 2.0)
    clean["max_tokens"] = _as_int(clean.get("max_tokens"), 2048, 16, 131072)
    clean["n_ctx"] = _as_int(clean.get("n_ctx"), 4096, 512, 1048576)
    clean["n_gpu_layers"] = _as_int(clean.get("n_gpu_layers"), 0, -1, 1024)
    clean["n_threads"] = _as_int(clean.get("n_threads"), 0, 0, 512)
    clean["context_length"] = _as_int(clean.get("context_length"), 8192, 512, 1048576)
    clean["request_timeout"] = _as_int(clean.get("request_timeout"), 120, 5, 3600)
    clean["redact_secrets"] = bool(clean.get("redact_secrets", True))
    if clean.get("tool_mode") not in TOOL_MODES:
        clean["tool_mode"] = "auto"
    if not isinstance(clean.get("provider_profiles"), dict):
        clean["provider_profiles"] = {}
    return clean


def resolve_api_key(config: dict[str, Any], spec: ProviderSpec | None = None) -> str:
    """API key from the config, falling back to the provider's environment variable."""
    key = str(config.get("api_key") or "").strip()
    if key:
        return key
    spec = spec or resolve_provider(config)
    return os.environ.get(spec.api_key_env, "").strip() if spec.api_key_env else ""


class LLMConfigManager:
    def __init__(self, path: Path = LLM_CONFIG_FILE):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.config = self.load()

    # -- persistence ---------------------------------------------------------
    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return sanitize({})
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return sanitize({})
        return sanitize(data if isinstance(data, dict) else {})

    def save(self, data: dict[str, Any]) -> None:
        merged = dict(self.config)
        merged.update(data)
        merged = sanitize(merged)
        spec = get_provider(merged["provider"])
        if spec:
            profiles = dict(merged["provider_profiles"])
            profiles[spec.key] = {k: merged.get(k, "") for k in _PROFILE_FIELDS}
            merged["provider_profiles"] = profiles
        self.config = merged
        self._write(merged)

    def _write(self, payload: dict[str, Any]) -> None:
        """Atomic write with owner-only permissions (the file may hold API keys)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), prefix=".llm_config.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            try:
                os.chmod(tmp_name, 0o600)
            except OSError:
                pass  # e.g. Windows
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # -- helpers -------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    def profile_for(self, provider: str) -> dict[str, Any]:
        """Remembered endpoint/key/model for ``provider`` (falls back to its defaults)."""
        spec = get_provider(provider)
        if not spec:
            return {}
        saved = dict(self.config.get("provider_profiles", {}).get(spec.key, {}))
        saved.setdefault("endpoint_url", spec.default_endpoint)
        saved.setdefault("model_name", spec.default_model)
        saved.setdefault("api_key", "")
        saved.setdefault("gguf_path", "")
        return saved
