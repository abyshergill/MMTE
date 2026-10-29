"""Pick and build the right backend for a settings dictionary."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

from core.llm.anthropic_backend import AnthropicBackend
from core.llm.base import LLMBackend
from core.llm.errors import LLMError
from core.llm.gguf_backend import GGUFBackend
from core.llm.ollama_backend import OllamaBackend
from core.llm.openai_backend import OpenAIBackend, openai_chat_url
from core.llm.providers import resolve_provider


def base_endpoint(config: dict[str, Any]) -> str:
    spec = resolve_provider(config)
    return str(config.get("endpoint_url") or spec.default_endpoint or "http://localhost:11434/v1").strip()


def chat_endpoint(config: dict[str, Any]) -> tuple[str, bool]:
    """Return ``(chat_url, ollama_native)`` for HTTP providers.

    * ``.../api/chat``                      -> Ollama native
    * bare host on Ollama's port / provider -> Ollama native (``/api/chat`` appended)
    * ``.../chat/completions`` or ``.../v1`` -> OpenAI-compatible
    * any other bare host                    -> OpenAI-compatible (``/v1/chat/completions``)
    """
    endpoint = base_endpoint(config).rstrip("/")
    parsed = urlsplit(endpoint)
    path = parsed.path.rstrip("/")
    if path.endswith("/api/chat"):
        return endpoint, True
    if path.endswith("/chat/completions"):
        return endpoint, False
    spec = resolve_provider(config)
    if not path and (spec.backend == "ollama" or parsed.port == 11434):
        return urlunsplit(parsed._replace(path="/api/chat")), True
    return openai_chat_url(endpoint), False


def create_backend(config: dict[str, Any]) -> LLMBackend:
    spec = resolve_provider(config)
    if spec.backend == "gguf":
        return GGUFBackend(config)
    if spec.backend == "anthropic":
        return AnthropicBackend(config)
    _url, native = chat_endpoint(config)
    return OllamaBackend(config) if native else OpenAIBackend(config)


def list_models(config: dict[str, Any]) -> list[str]:
    return create_backend(config).list_models()


def test_connection(config: dict[str, Any]) -> tuple[bool, str]:
    """Probe the configured provider; returns ``(ok, human readable message)``."""
    spec = resolve_provider(config)
    try:
        models = list_models(config)
    except LLMError as exc:
        return False, str(exc)
    except Exception as exc:  # defensive: never let a probe crash the dialog
        return False, f"{type(exc).__name__}: {exc}"
    wanted = str(config.get("model_name") or "").strip()
    if spec.backend == "gguf":
        return True, f"GGUF file OK: {models[0] if models else '?'}"
    if not models:
        return True, f"Connected to {spec.label} (no model list returned)."
    if wanted and wanted not in models and f"{wanted}:latest" not in models:
        sample = ", ".join(models[:6])
        return False, f"Connected, but model '{wanted}' was not found. Available: {sample}"
    return True, f"Connected to {spec.label}: {len(models)} model(s) available."
