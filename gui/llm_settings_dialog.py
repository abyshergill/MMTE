from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)
from core.llm_config import LLMConfigManager


class LLMSettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("LLM & Model Settings")
        self.resize(480, 320)
        self.cfg_mgr = LLMConfigManager()
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(10)

        cfg = self.cfg_mgr.config

        # Provider Selector
        self.provider_combo = QComboBox()
        self.provider_combo.addItems(["OpenAI", "Ollama (Local)", "Anthropic", "Custom / Local HTTP"])
        self.provider_combo.setCurrentText(cfg.get("provider", "OpenAI"))
        self.provider_combo.currentTextChanged.connect(self._on_provider_changed)
        form.addRow(QLabel("<b>LLM Provider:</b>"), self.provider_combo)

        # Base Endpoint URL
        self.endpoint_edit = QLineEdit(cfg.get("endpoint_url", ""))
        self.endpoint_edit.setPlaceholderText(
            "Ollama: http://localhost:11434/v1 | OpenAI-compatible local server: http://localhost:1234/v1"
        )
        self.endpoint_edit.setToolTip(
            "Enter an OpenAI-compatible API base URL, or Ollama's native chat URL ending in /api/chat."
        )
        form.addRow(QLabel("<b>Endpoint Base URL:</b>"), self.endpoint_edit)

        # API Key
        self.api_key_edit = QLineEdit(cfg.get("api_key", ""))
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("sk-... (Leave empty for local Ollama)")
        form.addRow(QLabel("<b>API Key:</b>"), self.api_key_edit)

        # Model Name
        self.model_edit = QLineEdit(cfg.get("model_name", ""))
        self.model_edit.setPlaceholderText("Use the exact installed model name, e.g. llama3.1 or qwen2.5:7b")
        form.addRow(QLabel("<b>Model Name:</b>"), self.model_edit)

        # Temperature
        self.temp_spin = QDoubleSpinBox()
        self.temp_spin.setRange(0.0, 2.0)
        self.temp_spin.setSingleStep(0.1)
        self.temp_spin.setValue(float(cfg.get("temperature", 0.7)))
        form.addRow(QLabel("<b>Temperature:</b>"), self.temp_spin)

        # Max Tokens
        self.max_tokens_spin = QSpinBox()
        self.max_tokens_spin.setRange(128, 32768)
        self.max_tokens_spin.setSingleStep(256)
        self.max_tokens_spin.setValue(int(cfg.get("max_tokens", 2048)))
        form.addRow(QLabel("<b>Max Response Tokens:</b>"), self.max_tokens_spin)

        root.addLayout(form)
        root.addSpacing(10)

        # Dialog Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        save_btn = QPushButton("Save Settings")
        save_btn.setStyleSheet("background-color: #2b78e4; color: white; font-weight: bold;")
        save_btn.clicked.connect(self._save_settings)
        btn_row.addWidget(save_btn)

        root.addLayout(btn_row)

    def _on_provider_changed(self, provider: str):
        if provider == "Ollama (Local)":
            if not self.endpoint_edit.text() or "openai.com" in self.endpoint_edit.text():
                self.endpoint_edit.setText("http://localhost:11434/v1")
            if not self.model_edit.text() or self.model_edit.text() == "gpt-4o":
                self.model_edit.setText("llama3.1")
        elif provider == "OpenAI":
            if "localhost" in self.endpoint_edit.text() or not self.endpoint_edit.text():
                self.endpoint_edit.setText("https://api.openai.com/v1")
            if not self.model_edit.text() or self.model_edit.text().startswith(("llama", "qwen", "mistral")):
                self.model_edit.setText("gpt-4o")

    def _save_settings(self):
        new_cfg = {
            "provider": self.provider_combo.currentText(),
            "endpoint_url": self.endpoint_edit.text().strip(),
            "api_key": self.api_key_edit.text().strip(),
            "model_name": self.model_edit.text().strip(),
            "temperature": self.temp_spin.value(),
            "max_tokens": self.max_tokens_spin.value(),
        }
        self.cfg_mgr.save(new_cfg)
        QMessageBox.information(self, "Saved", "LLM Configuration saved successfully!")
        self.accept()