"""Embedding back-ends used by the RAG memory.

The embedding model string carries its provider as a prefix::

    ollama/<model>        Ollama ``/api/embed``            (default when no prefix)
    openai/<model>        OpenAI-compatible ``/embeddings`` (key from env / saved settings)
    local/<model>         sentence-transformers, if installed
    gguf/<path-to.gguf>   a local embedding GGUF through llama-cpp-python
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests

from core.llm.errors import LLMError, LLMUnavailableError

OPENAI_EMBED_BASE = "https://api.openai.com/v1"


def split_model(model_name: str | None, default: str = "ollama/qwen3-embedding:0.6b") -> tuple[str, str]:
    """Return ``(provider_prefix, bare_model_name)``."""
    model = (model_name or default).strip()
    prefix, sep, rest = model.partition("/")
    if sep and prefix.lower() in {"ollama", "openai", "local", "gguf"}:
        return prefix.lower(), rest
    return "ollama", model


def provider_label(model_name: str | None) -> str:
    return {"ollama": "Ollama", "openai": "OpenAI", "local": "Local", "gguf": "GGUF"}[split_model(model_name)[0]]


def ollama_embed_url(endpoint_url: str) -> str:
    endpoint = (endpoint_url or "http://localhost:11434").strip().rstrip("/")
    parsed = urlsplit(endpoint)
    path = parsed.path.rstrip("/")
    for suffix in ("/api/chat", "/chat/completions", "/embeddings", "/v1"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    return urlunsplit(parsed._replace(path=f"{path}/api/embed"))


def _openai_key() -> str:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if key:
        return key
    try:  # fall back to the key saved in the LLM settings dialog
        from core.llm.settings import LLMConfigManager

        return str(LLMConfigManager().profile_for("openai").get("api_key") or "").strip()
    except Exception:
        return ""


def _openai_embed_url(endpoint_url: str) -> str:
    endpoint = (endpoint_url or "").strip().rstrip("/")
    parsed = urlsplit(endpoint) if endpoint else None
    # The endpoint handed in by the app is normally Ollama's; only honour it when it
    # is clearly another OpenAI-compatible server.
    if not parsed or not parsed.netloc or parsed.port == 11434:
        return f"{OPENAI_EMBED_BASE}/embeddings"
    path = parsed.path.rstrip("/")
    for suffix in ("/chat/completions", "/embeddings"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
    return urlunsplit(parsed._replace(path=f"{path or '/v1'}/embeddings"))


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    response = requests.post(url, json=payload, headers=headers, timeout=(5, 90))
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("Embedding server returned an unexpected payload")
    return data


def embed_texts(texts: list[str], endpoint_url: str, model_name: str | None, api_key: str = "") -> list[list[float]]:
    if not texts:
        return []
    kind, model = split_model(model_name)
    if kind == "ollama":
        data = _post_json(
            ollama_embed_url(endpoint_url), {"model": model, "input": texts}, {"Content-Type": "application/json"}
        )
        vectors = data.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise ValueError("Ollama returned an unexpected embedding count")
    elif kind == "openai":
        key = api_key or _openai_key()
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        data = _post_json(_openai_embed_url(endpoint_url), {"model": model, "input": texts}, headers)
        items = sorted(data.get("data") or [], key=lambda item: item.get("index", 0))
        vectors = [item.get("embedding") for item in items]
        if len(vectors) != len(texts):
            raise ValueError("Embedding server returned an unexpected embedding count")
    elif kind == "local":
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
        except ImportError as exc:
            raise LLMUnavailableError(
                "sentence-transformers is not installed.", hint="pip install sentence-transformers"
            ) from exc
        vectors = SentenceTransformer(model).encode(texts).tolist()
    else:  # gguf
        from core.llm.gguf_backend import validate_gguf

        path = validate_gguf(model)
        try:
            import llama_cpp  # type: ignore[import-not-found]
        except ImportError as exc:
            raise LLMUnavailableError(
                "llama-cpp-python is not installed.", hint="pip install llama-cpp-python"
            ) from exc
        try:
            llm = llama_cpp.Llama(model_path=str(path), embedding=True, verbose=False)
            vectors = [llm.embed(text) for text in texts]
        except Exception as exc:
            raise LLMError(f"GGUF embedding failed: {exc}") from exc
    return [[float(v) for v in vector] for vector in vectors]
