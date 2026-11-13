"""Tiny JSON-backed store for user preferences (toggles in the Utility / View menus)."""

from __future__ import annotations

import json
from pathlib import Path

from core.config import CONFIG_DIR

SETTINGS_FILE = CONFIG_DIR / "app_settings.json"

DEFAULTS: dict[str, object] = {
    "syntax_highlighting": True,       # colourise plain terminal output
    "intercept_editors": True,         # open vi / vim / nano in the built-in editor (SSH)
    "macro_format": "python",          # recorded macros are saved as "python" or Tera Term "ttl"
    "editor_word_wrap": False,
    "editor_font_size": 12,
}


class AppSettings:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else SETTINGS_FILE
        self._data: dict[str, object] = dict(DEFAULTS)
        try:
            if self.path.exists():
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._data.update(loaded)
        except Exception:
            pass

    def get(self, key: str, default=None):
        return self._data.get(key, DEFAULTS.get(key, default))

    def set(self, key: str, value) -> None:
        self._data[key] = value
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        except Exception:
            pass
