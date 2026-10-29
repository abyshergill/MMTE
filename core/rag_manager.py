import hashlib
import json
import math
import os
import re
import tempfile
from collections import Counter
from pathlib import Path

import requests

from core.llm import embeddings as _embeddings
from core.llm.errors import LLMError

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


CHUNK_CHARS = 1200
CHUNK_OVERLAP = 150
_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_STOP_WORDS = frozenset(
    "the and for are but not you all can had her was one our out has his how its may new now see two way who "
    "did get let say she too use with this that from have what when where which does into your about".split()
)


def chunk_text(text: str, max_chars: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into retrieval-sized pieces, preferring paragraph/line boundaries."""
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            window = text[start:end]
            cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(". "))
            if cut > max_chars // 2:
                end = start + cut + 1
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


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
        """Write atomically so a crash mid-write cannot corrupt every saved memory."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), prefix=".rag.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.memories, f, indent=2)
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

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
        name = name.strip()
        if name == APPLICATION_MEMORY_NAME:
            raise ValueError(f"'{APPLICATION_MEMORY_NAME}' is the built-in application guide and cannot be modified.")

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
                    pieces = chunk_text(content) or [""]
                    for number, piece in enumerate(pieces, start=1):
                        suffix = f" (part {number}/{len(pieces)})" if len(pieces) > 1 else ""
                        memory["text_entries"].append(f"--- FILE: {path_obj.name}{suffix} ---\n{piece}")
                    memory["file_sources"].append(fpath)
                except Exception as e:
                    print(f"Error reading file {fpath}: {e}")

        self.memories[name] = memory
        self.save()
        return memory

    def delete(self, name: str) -> None:
        if name == APPLICATION_MEMORY_NAME:
            return  # the built-in guide is re-created on start-up anyway; never delete it
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
        return _embeddings.ollama_embed_url(endpoint_url)

    @staticmethod
    def _embedding_model_name(model_name: str | None) -> str:
        return _embeddings.split_model(model_name or APPLICATION_EMBEDDING_MODEL)[1]

    def _embed_texts(self, texts: list[str], endpoint_url: str, model_name: str) -> list[list[float]]:
        return _embeddings.embed_texts(texts, endpoint_url, model_name)

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
    def _tokens(text: str) -> list[str]:
        return [t for t in _TOKEN_RE.findall(text.lower()) if len(t) > 2 and t not in _STOP_WORDS]

    @staticmethod
    def _lexical_scores(query: str, entries: list[str]) -> list[float]:
        """BM25 ranking; the offline fallback when no embedding server is reachable."""
        query_terms = set(RAGMemoryManager._tokens(query))
        if not query_terms or not entries:
            return [0.0] * len(entries)
        docs = [Counter(RAGMemoryManager._tokens(entry)) for entry in entries]
        lengths = [sum(doc.values()) for doc in docs]
        avg_len = (sum(lengths) / len(lengths)) or 1.0
        count = len(entries)
        scores = []
        for doc, length in zip(docs, lengths):
            score = 0.0
            for term in query_terms:
                freq = doc.get(term, 0)
                if not freq:
                    continue
                containing = sum(1 for other in docs if term in other)
                idf = math.log(1 + (count - containing + 0.5) / (containing + 0.5))
                score += idf * (freq * 2.2) / (freq + 1.2 * (0.25 + 0.75 * length / avg_len))
            scores.append(score)
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
        mode = f"{_embeddings.provider_label(model_name)} embeddings"
        try:
            if vector_model != content_hash or not isinstance(vectors, list) or len(vectors) != len(entries):
                vectors = self._embed_texts(entries, endpoint_url, model_name)
                memory["embeddings"] = vectors
                memory["embedding_hash"] = content_hash
                memory["embedding_dimensions"] = len(vectors[0]) if vectors else 0
                self.save()
            query_vector = self._embed_texts([query], endpoint_url, model_name)[0]
            scores = [self._cosine_similarity(query_vector, vector) for vector in vectors]
        except (requests.RequestException, LLMError, ValueError, KeyError, TypeError) as exc:
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