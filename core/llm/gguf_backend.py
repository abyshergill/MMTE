"""Run a local ``.gguf`` model in-process through llama-cpp-python.

llama-cpp-python is an *optional* dependency (``pip install llama-cpp-python``);
importing this module never requires it.  Tool use goes through the prompt-based
JSON protocol because function-calling support differs from one GGUF chat
template to the next.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from core.llm.base import ChatResult, ChunkCallback, LLMBackend, ResponseCallback
from core.llm.errors import LLMError, LLMUnavailableError
from core.llm.openai_backend import OpenAIStreamAccumulator

GGUF_MAGIC = b"GGUF"

_MODEL_CACHE: dict[tuple, Any] = {}
_CACHE_LOCK = threading.Lock()
# llama.cpp contexts are not thread-safe: serialise inference per process.
_INFER_LOCK = threading.Lock()


def validate_gguf(path: str | Path) -> Path:
    """Return the resolved path or raise :class:`LLMError` explaining what is wrong."""
    text = str(path or "").strip()
    if not text:
        raise LLMError("No GGUF model file selected.", hint="Choose a .gguf file in AI > LLM Settings.")
    file_path = Path(text).expanduser()
    if not file_path.is_file():
        raise LLMError(f"GGUF file not found: {file_path}")
    try:
        with file_path.open("rb") as handle:
            magic = handle.read(4)
    except OSError as exc:
        raise LLMError(f"Cannot read GGUF file {file_path}: {exc}") from exc
    if magic != GGUF_MAGIC:
        raise LLMError(
            f"{file_path.name} is not a valid GGUF file (bad header).",
            hint="Download the model again, or convert it with llama.cpp's convert_hf_to_gguf.py.",
        )
    return file_path.resolve()


def flatten_for_plain_chat(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Reduce canonical messages to plain role/content pairs (no tool_call objects)."""
    flat: list[dict[str, str]] = []
    for message in messages:
        role = message.get("role")
        content = str(message.get("content") or "")
        if role == "tool":
            flat.append({"role": "user", "content": f"Tool result:\n{content}"})
        elif role in ("system", "user", "assistant"):
            flat.append({"role": role, "content": content})
    return flat


def merge_system_into_user(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """For chat templates (e.g. Gemma) that reject the ``system`` role."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    rest = [dict(m) for m in messages if m["role"] != "system"]
    if system:
        if rest and rest[0]["role"] == "user":
            rest[0]["content"] = f"{system}\n\n{rest[0]['content']}"
        else:
            rest.insert(0, {"role": "user", "content": system})
    return rest


class _StreamCloser:
    """Object with ``close()`` so the UI Stop button can interrupt generation."""

    def __init__(self, cancel_event: threading.Event):
        self._cancel_event = cancel_event

    def close(self) -> None:
        self._cancel_event.set()


class GGUFBackend(LLMBackend):
    name = "gguf"
    native_tools = False

    def _model_path(self) -> Path:
        return validate_gguf(self.config.get("gguf_path") or self.config.get("model_name"))

    def _load(self) -> Any:
        path = self._model_path()
        try:
            import llama_cpp  # type: ignore[import-not-found]
        except ImportError as exc:
            raise LLMUnavailableError(
                "llama-cpp-python is not installed, so GGUF models cannot be run.",
                hint="Install it with:  pip install llama-cpp-python   (see README for GPU builds).",
            ) from exc
        n_threads = int(self.config.get("n_threads") or 0) or None
        key = (
            str(path),
            int(self.config.get("n_ctx", 4096)),
            int(self.config.get("n_gpu_layers", 0)),
            n_threads,
            str(self.config.get("chat_format") or ""),
        )
        with _CACHE_LOCK:
            model = _MODEL_CACHE.get(key)
            if model is None:
                _MODEL_CACHE.clear()  # keep at most one model resident in RAM/VRAM
                try:
                    model = llama_cpp.Llama(
                        model_path=str(path),
                        n_ctx=key[1],
                        n_gpu_layers=key[2],
                        n_threads=n_threads,
                        chat_format=key[4] or None,
                        verbose=False,
                    )
                except Exception as exc:
                    raise LLMError(f"Failed to load {path.name}: {exc}") from exc
                _MODEL_CACHE[key] = model
            return model

    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        cancel_event: threading.Event,
        on_chunk: ChunkCallback | None = None,
        set_response: ResponseCallback | None = None,
    ) -> ChatResult:
        model = self._load()
        plain = flatten_for_plain_chat(messages)
        kwargs: dict[str, Any] = {
            "temperature": self._temperature(),
            "max_tokens": self._max_tokens(),
            "stream": True,
        }
        if set_response:
            set_response(_StreamCloser(cancel_event))
        acc = OpenAIStreamAccumulator()
        try:
            with _INFER_LOCK:
                try:
                    iterator = model.create_chat_completion(messages=plain, **kwargs)
                except Exception as exc:
                    if "system" not in str(exc).lower():
                        raise
                    iterator = model.create_chat_completion(messages=merge_system_into_user(plain), **kwargs)
                for chunk in iterator:
                    if cancel_event.is_set():
                        break
                    text = acc.feed(chunk)
                    if text and on_chunk:
                        on_chunk(text)
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(f"GGUF generation failed: {exc}") from exc
        finally:
            if set_response:
                set_response(None)
        return acc.result()

    def list_models(self) -> list[str]:
        return [self._model_path().name]


def unload_models() -> None:
    """Release any cached model (frees RAM/VRAM)."""
    with _CACHE_LOCK:
        _MODEL_CACHE.clear()
