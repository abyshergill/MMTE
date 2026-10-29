"""Provider registry + persistent LLM settings."""

import json
import os
import stat

import pytest

from core.llm import providers
from core.llm.settings import DEFAULT_LLM_CONFIG, LLMConfigManager, resolve_api_key, sanitize


@pytest.mark.parametrize(
    "name,key",
    [
        ("OpenAI", "openai"),
        ("openai", "openai"),
        ("Ollama (Local)", "ollama"),
        ("ollama", "ollama"),
        ("Anthropic", "anthropic"),
        ("Claude", "anthropic"),
        ("Custom / Local HTTP", "custom"),
        ("Google Gemini", "gemini"),
        ("Local GGUF File", "gguf"),
        ("gguf", "gguf"),
    ],
)
def test_get_provider_accepts_labels_keys_and_aliases(name, key):
    assert providers.get_provider(name).key == key


def test_get_provider_unknown_returns_none():
    assert providers.get_provider("nonsense") is None
    assert providers.get_provider("") is None
    assert providers.get_provider(None) is None


def test_every_provider_has_a_known_backend_and_unique_key():
    keys = [p.key for p in providers.PROVIDERS]
    assert len(keys) == len(set(keys))
    assert {p.backend for p in providers.PROVIDERS} == {"openai", "ollama", "anthropic", "gguf"}


@pytest.mark.parametrize(
    "config,expected",
    [
        ({"endpoint_url": "http://127.0.0.1:11434"}, "ollama"),
        ({"endpoint_url": "http://localhost:11434/api/chat"}, "ollama"),
        ({"endpoint_url": "http://localhost:11434/v1"}, "custom"),
        ({"endpoint_url": "https://api.anthropic.com"}, "anthropic"),
        ({"gguf_path": "/m/model.gguf"}, "gguf"),
        ({"endpoint_url": "http://10.0.0.5:8000/v1"}, "custom"),
        ({"provider": "Groq"}, "groq"),
    ],
)
def test_resolve_provider_infers_from_legacy_configs(config, expected):
    assert providers.resolve_provider(config).key == expected


@pytest.mark.parametrize(
    "url,local",
    [
        ("http://localhost:11434", True),
        ("http://127.0.0.1:1234/v1", True),
        ("http://192.168.1.20:8080", True),
        ("http://10.1.2.3", True),
        ("http://172.16.0.9", True),
        ("http://172.32.0.9", False),
        ("https://api.openai.com/v1", False),
        ("http://my-box.local:11434", True),
        ("http://8.8.8.8", False),
    ],
)
def test_is_local_endpoint(url, local):
    assert providers.is_local_endpoint(url) is local


def test_is_cloud_distinguishes_hosted_from_local_and_gguf():
    assert providers.is_cloud({"provider": "OpenAI"}) is True
    assert providers.is_cloud({"provider": "Anthropic"}) is True
    assert providers.is_cloud({"provider": "Ollama (Local)", "endpoint_url": "http://localhost:11434"}) is False
    assert providers.is_cloud({"provider": "Ollama (Local)", "endpoint_url": "https://ollama.example.com"}) is True
    assert providers.is_cloud({"provider": "Local GGUF File", "gguf_path": "/x.gguf"}) is False


# --------------------------------------------------------------------------- settings

def test_sanitize_clamps_and_coerces_bad_values():
    cfg = sanitize({"temperature": "9", "max_tokens": "abc", "n_ctx": 1, "tool_mode": "bogus", "provider_profiles": []})
    assert cfg["temperature"] == 2.0
    assert cfg["max_tokens"] == DEFAULT_LLM_CONFIG["max_tokens"]
    assert cfg["n_ctx"] == 512
    assert cfg["tool_mode"] == "auto"
    assert cfg["provider_profiles"] == {}


def test_load_missing_or_corrupt_file_gives_defaults(tmp_path):
    path = tmp_path / "llm.json"
    assert LLMConfigManager(path).config["provider"] == "Ollama (Local)"
    path.write_text("{not json", encoding="utf-8")
    assert LLMConfigManager(path).config == sanitize({})
    path.write_text("[1, 2]", encoding="utf-8")
    assert LLMConfigManager(path).config == sanitize({})


def test_legacy_four_field_config_is_upgraded_in_place(tmp_path):
    path = tmp_path / "llm.json"
    path.write_text(
        json.dumps({"provider": "OpenAI", "api_key": "k", "endpoint_url": "https://api.openai.com/v1", "model_name": "gpt-4o"}),
        encoding="utf-8",
    )
    cfg = LLMConfigManager(path).config
    assert cfg["model_name"] == "gpt-4o"
    assert cfg["gguf_path"] == "" and cfg["tool_mode"] == "auto" and cfg["context_length"] == 8192


def test_save_roundtrip_and_private_permissions(tmp_path):
    path = tmp_path / "sub" / "llm.json"
    mgr = LLMConfigManager(path)
    mgr.save({"provider": "OpenAI", "api_key": "sk-secret", "model_name": "gpt-4o-mini"})
    assert LLMConfigManager(path).config["api_key"] == "sk-secret"
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not [p for p in path.parent.iterdir() if p.suffix == ".tmp"]


def test_each_provider_remembers_its_own_endpoint_key_and_model(tmp_path):
    mgr = LLMConfigManager(tmp_path / "llm.json")
    mgr.save({"provider": "OpenAI", "api_key": "sk-1", "model_name": "gpt-4o", "endpoint_url": "https://api.openai.com/v1"})
    mgr.save({"provider": "Anthropic", "api_key": "ak-2", "model_name": "claude-x", "endpoint_url": "https://api.anthropic.com"})
    assert mgr.profile_for("OpenAI")["api_key"] == "sk-1"
    assert mgr.profile_for("Anthropic")["model_name"] == "claude-x"
    fresh = mgr.profile_for("Groq")
    assert fresh["endpoint_url"] == "https://api.groq.com/openai/v1" and fresh["api_key"] == ""


def test_resolve_api_key_prefers_config_then_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    assert resolve_api_key({"provider": "OpenAI", "api_key": "cfg-key"}) == "cfg-key"
    assert resolve_api_key({"provider": "OpenAI", "api_key": ""}) == "env-key"
    monkeypatch.delenv("OPENAI_API_KEY")
    assert resolve_api_key({"provider": "OpenAI"}) == ""
    assert resolve_api_key({"provider": "Ollama (Local)"}) == ""
