"""Backward-compatible import path; the implementation lives in core.llm.settings."""

from core.llm.settings import (  # noqa: F401
    DEFAULT_LLM_CONFIG,
    LLM_CONFIG_FILE,
    LLMConfigManager,
    resolve_api_key,
    sanitize,
)
