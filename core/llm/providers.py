"""Registry of supported LLM providers.

Every provider is served by one of four wire protocols ("backends"):

``openai``     OpenAI ``/chat/completions`` (also Gemini, Groq, Mistral, DeepSeek,
               xAI, OpenRouter, Together, LM Studio, vLLM, llama.cpp server, ...)
``ollama``     Ollama's native ``/api/chat``
``anthropic``  Anthropic ``/v1/messages``
``gguf``       A local ``.gguf`` file executed in-process through llama-cpp-python

The ``label`` of the first four entries is kept identical to the strings stored
by earlier versions of the application so existing ``llm_config.json`` files
keep working.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ProviderSpec:
    key: str
    label: str
    backend: str
    default_endpoint: str = ""
    default_model: str = ""
    api_key_env: str = ""
    needs_key: bool = True
    local: bool = False
    # Whether the wire protocol is trusted for native function calling.  Backends
    # that do not support it fall back to prompt-based JSON tool calls.
    native_tools: bool = True
    notes: str = ""


PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec("openai", "OpenAI", "openai", "https://api.openai.com/v1", "gpt-4o-mini", "OPENAI_API_KEY"),
    ProviderSpec("anthropic", "Anthropic", "anthropic", "https://api.anthropic.com", "claude-3-5-haiku-latest", "ANTHROPIC_API_KEY"),
    ProviderSpec(
        "gemini", "Google Gemini", "openai",
        "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.0-flash", "GEMINI_API_KEY",
        notes="Uses Google's OpenAI-compatible endpoint.",
    ),
    ProviderSpec("groq", "Groq", "openai", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", "GROQ_API_KEY"),
    ProviderSpec("mistral", "Mistral", "openai", "https://api.mistral.ai/v1", "mistral-small-latest", "MISTRAL_API_KEY"),
    ProviderSpec("deepseek", "DeepSeek", "openai", "https://api.deepseek.com/v1", "deepseek-chat", "DEEPSEEK_API_KEY"),
    ProviderSpec("xai", "xAI (Grok)", "openai", "https://api.x.ai/v1", "grok-3-mini", "XAI_API_KEY"),
    ProviderSpec("openrouter", "OpenRouter", "openai", "https://openrouter.ai/api/v1", "openai/gpt-4o-mini", "OPENROUTER_API_KEY"),
    ProviderSpec("together", "Together AI", "openai", "https://api.together.xyz/v1", "meta-llama/Llama-3.3-70B-Instruct-Turbo", "TOGETHER_API_KEY"),
    ProviderSpec(
        "ollama", "Ollama (Local)", "ollama", "http://localhost:11434", "llama3.1",
        needs_key=False, local=True,
    ),
    ProviderSpec(
        "lmstudio", "LM Studio (Local)", "openai", "http://localhost:1234/v1", "local-model",
        needs_key=False, local=True,
    ),
    ProviderSpec(
        "llamacpp", "llama.cpp Server (Local)", "openai", "http://localhost:8080/v1", "local-model",
        needs_key=False, local=True,
    ),
    ProviderSpec(
        "gguf", "Local GGUF File", "gguf", "", "",
        needs_key=False, local=True, native_tools=False,
        notes="Runs a .gguf model in-process (requires: pip install llama-cpp-python).",
    ),
    ProviderSpec(
        "custom", "Custom / Local HTTP", "openai", "http://localhost:1234/v1", "local-model",
        needs_key=False, local=True,
        notes="Any OpenAI-compatible server (vLLM, text-generation-webui, LiteLLM, ...).",
    ),
)

_BY_KEY = {p.key: p for p in PROVIDERS}
_BY_LABEL = {p.label.lower(): p for p in PROVIDERS}
_ALIASES = {
    "ollama": "ollama",
    "ollama (local)": "ollama",
    "claude": "anthropic",
    "google": "gemini",
    "gguf": "gguf",
    "llama.cpp": "llamacpp",
    "custom": "custom",
    "local": "custom",
}


def provider_labels() -> list[str]:
    return [p.label for p in PROVIDERS]


def get_provider(name: str | None) -> ProviderSpec | None:
    """Look a provider up by key or label (case-insensitive); None if unknown."""
    if not name:
        return None
    text = str(name).strip().lower()
    return _BY_KEY.get(text) or _BY_LABEL.get(text) or _BY_KEY.get(_ALIASES.get(text, ""))


def is_local_endpoint(url: str) -> bool:
    """True when the endpoint points at this machine or a private network host."""
    try:
        host = (urlsplit(url if "//" in url else f"//{url}").hostname or "").lower()
    except ValueError:
        return False
    if host in {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"} or host.endswith(".local"):
        return True
    parts = host.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        a, b = int(parts[0]), int(parts[1])
        return a == 10 or a == 127 or (a == 192 and b == 168) or (a == 172 and 16 <= b <= 31)
    return False


def resolve_provider(config: dict[str, Any]) -> ProviderSpec:
    """Return the provider for ``config``, inferring one for legacy/partial configs."""
    spec = get_provider(config.get("provider"))
    if spec:
        return spec
    endpoint = str(config.get("endpoint_url") or "").strip().lower()
    if str(config.get("gguf_path") or "").strip() and not endpoint:
        return _BY_KEY["gguf"]
    if "anthropic.com" in endpoint:
        return _BY_KEY["anthropic"]
    if endpoint.rstrip("/").endswith("/api/chat") or ":11434" in endpoint:
        return _BY_KEY["ollama"] if not endpoint.rstrip("/").endswith("/v1") else _BY_KEY["custom"]
    return _BY_KEY["custom"]


def is_cloud(config: dict[str, Any]) -> bool:
    """True when prompts would leave this machine / local network."""
    spec = resolve_provider(config)
    if spec.backend == "gguf":
        return False
    endpoint = str(config.get("endpoint_url") or spec.default_endpoint)
    if spec.local and not endpoint:
        return False
    return not is_local_endpoint(endpoint)
