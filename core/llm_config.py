import json
from pathlib import Path
from core.config import CONFIG_DIR

LLM_CONFIG_FILE = CONFIG_DIR / "llm_config.json"

DEFAULT_LLM_CONFIG = {
    "provider": "OpenAI",
    "api_key": "",
    "endpoint_url": "https://api.openai.com/v1",
    "model_name": "gpt-4o",
    "temperature": 0.7,
    "max_tokens": 2048,
}


class LLMConfigManager:
    def __init__(self, path: Path = LLM_CONFIG_FILE):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.config = self.load()

    def load(self) -> dict:
        if not self.path.exists():
            return DEFAULT_LLM_CONFIG.copy()
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            merged = DEFAULT_LLM_CONFIG.copy()
            if isinstance(data, dict):
                merged.update(data)
            return merged
        except Exception:
            return DEFAULT_LLM_CONFIG.copy()

    def save(self, data: dict) -> None:
        self.config.update(data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=2)

    def get(self, key: str, default=None):
        return self.config.get(key, default)