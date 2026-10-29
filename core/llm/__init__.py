"""Provider-agnostic LLM layer (Ollama, OpenAI-compatible APIs, Anthropic, local GGUF)."""

from core.llm.agent import run_chat, stream_plain, trim_history  # noqa: F401
from core.llm.base import ChatResult, LLMBackend  # noqa: F401
from core.llm.errors import LLMError, LLMUnavailableError  # noqa: F401
from core.llm.factory import create_backend, list_models, test_connection  # noqa: F401
from core.llm.providers import PROVIDERS, ProviderSpec, get_provider, resolve_provider  # noqa: F401
from core.llm.settings import DEFAULT_LLM_CONFIG, LLMConfigManager, resolve_api_key  # noqa: F401
