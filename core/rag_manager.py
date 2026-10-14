import json
import hashlib
import math
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests

from core.application_knowledge import (
    APPLICATION_EMBEDDING_MODEL,
    APPLICATION_KNOWLEDGE_CHUNKS,
    APPLICATION_MEMORY_NAME,
)
from core.config import CONFIG_DIR

RAG_FILE = CONFIG_DIR / "rag_memories.json"

AVAILABLE_EMBEDDING_MODELS = [
    "openai/text-embedding-3-small",
    "openai/text-embedding-3-large",
    "local/all-MiniLM-L6-v2",
    "ollama/nomic-embed-text",
    "ollama/mxbai-embed-large",
    "ollama/qwen3-embedding:0.6b",
]


class RAGMemoryManager:
    def __init__(self, path: Path = RAG_FILE):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.memories: dict[str, dict] = self.load()

    def load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(self.memories, f, indent=2)

    def create_or_update(
        self,
        name: str,
        embedding_model: str,
        assigned_job: str,
        new_text: str = "",
        new_file_paths: list[str] = None,
    ) -> dict:
        if new_file_paths is None:
            new_file_paths = []

        memory = self.memories.get(
            name,
            {
                "name": name,
                "embedding_model": embedding_model,
                "assigned_job": assigned_job,
                "text_entries": [],
                "file_sources": [],
            },
        )

        # Update metadata
        memory["embedding_model"] = embedding_model
        memory["assigned_job"] = assigned_job

        # Append new manual text entry if provided
        if new_text.strip():
            memory["text_entries"].append(new_text.strip())

        # Ingest and append contents of files
        for fpath in new_file_paths:
            path_obj = Path(fpath)
            if path_obj.exists() and fpath not in memory["file_sources"]:
                try:
                    content = path_obj.read_text(encoding="utf-8", errors="ignore")
                    memory["text_entries"].append(
                        f"--- FILE: {path_obj.name} ---\n{content}"
                    )
                    memory["file_sources"].append(fpath)
                except Exception as e:
                    print(f"Error reading file {fpath}: {e}")

        self.memories[name] = memory
        self.save()
        return memory

    def delete(self, name: str) -> None:
        self.memories.pop(name, None)
        self.save()

    def get(self, name: str) -> dict | None:
        return self.memories.get(name)

    def list_names(self) -> list[str]:
        return sorted(list(self.memories.keys()))

    def ensure_application_memory(self) -> dict:
        """Install/update the built-in application handbook as a selectable RAG memory."""
        memory = self.memories.get(APPLICATION_MEMORY_NAME, {})
        memory.update(
            {
                "name": APPLICATION_MEMORY_NAME,
                "embedding_model": APPLICATION_EMBEDDING_MODEL,
                "assigned_job": "Application-only AI assistant engineer for Modern Multi Terminal",
                "text_entries": list(APPLICATION_KNOWLEDGE_CHUNKS),
                "file_sources": ["core/application_knowledge.py"],
                "built_in": True,
            }
        )
        self.memories[APPLICATION_MEMORY_NAME] = memory
        self.save()
        return memory

    @staticmethod
    def _ollama_embed_url(endpoint_url: str) -> str:
        endpoint = (endpoint_url or "http://localhost:11434").strip().rstrip("/")
        parsed = urlsplit(endpoint)
        path = parsed.path.rstrip("/")
        for suffix in ("/api/chat", "/chat/completions", "/embeddings", "/v1"):
            if path.endswith(suffix):
                path = path[: -len(suffix)]
                break
        return urlunsplit(parsed._replace(path=f"{path}/api/embed"))

    @staticmethod
    def _embedding_model_name(model_name: str | None) -> str:
        model = (model_name or APPLICATION_EMBEDDING_MODEL).strip()
        if model.startswith("ollama/"):
            return model[len("ollama/"):]
        return model

    def _embed_texts(self, texts: list[str], endpoint_url: str, model_name: str) -> list[list[float]]:
        if not texts:
            return []
        response = requests.post(
            self._ollama_embed_url(endpoint_url),
            json={"model": self._embedding_model_name(model_name), "input": texts},
            headers={"Content-Type": "application/json"},
            timeout=(5, 90),
        )
        response.raise_for_status()
        data = response.json()
        vectors = data.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise ValueError("Ollama returned an unexpected embedding count")
        return [[float(value) for value in vector] for vector in vectors]

    @staticmethod
    def _content_hash(entries: list[str], model_name: str) -> str:
        serialized = json.dumps([model_name, entries], ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _cosine_similarity(left: list[float], right: list[float]) -> float:
        if not left or len(left) != len(right):
            return 0.0
        dot = sum(a * b for a, b in zip(left, right))
        left_norm = math.sqrt(sum(a * a for a in left))
        right_norm = math.sqrt(sum(b * b for b in right))
        if not left_norm or not right_norm:
            return 0.0
        return dot / (left_norm * right_norm)

    @staticmethod
    def _lexical_scores(query: str, entries: list[str]) -> list[float]:
        query_terms = {word.lower() for word in query.split() if len(word) > 2}
        if not query_terms:
            return [0.0] * len(entries)
        scores = []
        for entry in entries:
            terms = {word.lower() for word in entry.split() if len(word) > 2}
            scores.append(len(query_terms & terms) / math.sqrt(len(query_terms) * max(len(terms), 1)))
        return scores

    def retrieve_relevant(
        self,
        name: str,
        query: str,
        endpoint_url: str,
        top_k: int = 5,
    ) -> tuple[str, str]:
        """Retrieve semantically relevant entries; fall back to lexical ranking offline."""
        memory = self.get(name)
        if not memory:
            return "", "memory not found"
        entries = [str(item) for item in memory.get("text_entries", []) if str(item).strip()]
        if not entries:
            return "", "memory is empty"
        model_name = str(memory.get("embedding_model") or APPLICATION_EMBEDDING_MODEL)
        content_hash = self._content_hash(entries, model_name)
        vectors = memory.get("embeddings")
        vector_model = memory.get("embedding_hash")
        mode = "Ollama embeddings"
        try:
            if vector_model != content_hash or not isinstance(vectors, list) or len(vectors) != len(entries):
                vectors = self._embed_texts(entries, endpoint_url, model_name)
                memory["embeddings"] = vectors
                memory["embedding_hash"] = content_hash
                memory["embedding_dimensions"] = len(vectors[0]) if vectors else 0
                self.save()
            query_vector = self._embed_texts([query], endpoint_url, model_name)[0]
            scores = [self._cosine_similarity(query_vector, vector) for vector in vectors]
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            mode = f"keyword fallback ({type(exc).__name__})"
            scores = self._lexical_scores(query, entries)

        ranked = sorted(enumerate(scores), key=lambda item: item[1], reverse=True)[:max(top_k, 1)]
        relevant = [(entries[index], score) for index, score in ranked if score > 0]
        if not relevant:
            relevant = [(entries[index], score) for index, score in ranked[: min(2, len(ranked))]]
        context = "\n\n".join(
            f"[Knowledge {index + 1}; relevance={score:.3f}]\n{text}"
            for index, (text, score) in enumerate(relevant)
        )
        return context, mode