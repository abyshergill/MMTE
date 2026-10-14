from __future__ import annotations

from core.application_knowledge import (
    APPLICATION_EMBEDDING_MODEL,
    APPLICATION_KNOWLEDGE_CHUNKS,
    APPLICATION_MEMORY_NAME,
)
from core.rag_manager import RAGMemoryManager


def test_application_memory_is_seeded_and_uses_qwen_embedding(tmp_path):
    manager = RAGMemoryManager(tmp_path / "rag.json")

    memory = manager.ensure_application_memory()

    assert memory["name"] == APPLICATION_MEMORY_NAME
    assert memory["embedding_model"] == APPLICATION_EMBEDDING_MODEL
    assert memory["built_in"] is True
    assert memory["text_entries"] == APPLICATION_KNOWLEDGE_CHUNKS
    assert APPLICATION_MEMORY_NAME in manager.list_names()


def test_retrieve_relevant_persists_vectors_and_returns_matching_chunk(monkeypatch, tmp_path):
    manager = RAGMemoryManager(tmp_path / "rag.json")
    manager.create_or_update(
        name="test memory",
        embedding_model="ollama/qwen3-embedding:0.6b",
        assigned_job="test",
        new_text="SSH opens an interactive PTY shell channel.",
    )
    memory = manager.get("test memory")
    memory["text_entries"].append("RAG performs semantic retrieval over application instructions.")
    embed_calls = []

    def fake_embed(texts, endpoint_url, model_name):
        embed_calls.append((texts, endpoint_url, model_name))
        if len(texts) == 1:
            return [[0.0, 1.0]]
        return [[1.0, 0.0], [0.0, 1.0]]

    monkeypatch.setattr(manager, "_embed_texts", fake_embed)

    context, mode = manager.retrieve_relevant(
        "test memory", "how does application knowledge retrieval work?", "http://127.0.0.1:11434", top_k=1
    )

    assert "RAG performs semantic retrieval" in context
    assert mode == "Ollama embeddings"
    assert len(embed_calls) == 2
    assert memory["embedding_dimensions"] == 2

    # Corpus vectors are persisted and reused; only the new query needs embedding.
    manager.retrieve_relevant("test memory", "semantic retrieval", "http://127.0.0.1:11434", top_k=1)
    assert len(embed_calls) == 3
    assert len(embed_calls[-1][0]) == 1


def test_retrieve_falls_back_to_keyword_search_when_ollama_is_offline(monkeypatch, tmp_path):
    import requests

    manager = RAGMemoryManager(tmp_path / "rag.json")
    manager.create_or_update(
        name="project",
        embedding_model="ollama/qwen3-embedding:0.6b",
        assigned_job="test",
        new_text="SSH authentication uses Paramiko and interactive PTY sessions.",
    )
    manager.create_or_update(
        name="project",
        embedding_model="ollama/qwen3-embedding:0.6b",
        assigned_job="test",
        new_text="RAG memory stores semantic vector embeddings.",
    )

    def offline(*_args, **_kwargs):
        raise requests.ConnectionError("Ollama unavailable")

    monkeypatch.setattr(manager, "_embed_texts", offline)

    context, mode = manager.retrieve_relevant(
        "project", "how does SSH use an interactive PTY?", "http://127.0.0.1:11434", top_k=1
    )

    assert "SSH authentication" in context
    assert "keyword fallback" in mode


def test_ollama_embedding_url_normalizes_chat_and_v1_urls():
    assert RAGMemoryManager._ollama_embed_url("http://127.0.0.1:11434/") == "http://127.0.0.1:11434/api/embed"
    assert RAGMemoryManager._ollama_embed_url("http://127.0.0.1:11434/api/chat") == "http://127.0.0.1:11434/api/embed"
    assert RAGMemoryManager._ollama_embed_url("http://localhost:1234/v1") == "http://localhost:1234/api/embed"
