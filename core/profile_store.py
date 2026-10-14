import json
from pathlib import Path

class ProfileStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.profiles: dict[str, dict] = self.load()

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
            json.dump(self.profiles, f, indent=2)

    def upsert(self, name: str, data: dict) -> None:
        self.profiles[name] = data
        self.save()

    def delete(self, name: str) -> None:
        self.profiles.pop(name, None)
        self.save()