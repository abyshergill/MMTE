from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.llm.factory import list_models, test_connection
from core.llm.errors import LLMError
from core.llm.providers import get_provider, provider_labels
from core.llm.settings import LLMConfigManager, TOOL_MODES


class LLMSettingsDialog(QDialog):
    """Pick a provider (Ollama, hosted API, OpenAI-compatible server or a local GGUF
    file), its model and the generation options."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("LLM & Model Settings")
        self.resize(560, 560)
        self.cfg_mgr = LLMConfigManager()
        self._drafts: dict[str, dict] = {}
        self._current_label = ""
        self._build_ui()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(10)
        cfg = self.cfg_mgr.config

        self.provider_combo = QComboBox()
        self.provider_combo.addItems(provider_labels())
        spec = get_provider(cfg.get("provider"))
        self._current_label = spec.label if spec else provider_labels()[0]
        self.provider_combo.setCurrentText(self._current_label)
        form.addRow(QLabel("<b>LLM Provider:</b>"), self.provider_combo)

        self.endpoint_edit = QLineEdit()
        form.addRow(QLabel("<b>Endpoint Base URL:</b>"), self.endpoint_edit)

        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow(QLabel("<b>API Key:</b>"), self.api_key_edit)

        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.fetch_btn = QPushButton("Fetch models")
        self.fetch_btn.setToolTip("Ask the provider which models are available")
        self.fetch_btn.clicked.connect(self._fetch_models)
        model_row = QHBoxLayout()
        model_row.addWidget(self.model_combo, 1)
        model_row.addWidget(self.fetch_btn)
        model_holder = QWidget()
        model_holder.setLayout(model_row)
        model_row.setContentsMargins(0, 0, 0, 0)
        form.addRow(QLabel("<b>Model Name:</b>"), model_holder)

        # --- Local GGUF -----------------------------------------------------
        self.gguf_edit = QLineEdit()
        self.gguf_edit.setPlaceholderText("Path to a .gguf model file")
        self.gguf_browse = QPushButton("Browse...")
        self.gguf_browse.clicked.connect(self._browse_gguf)
        gguf_row = QHBoxLayout()
        gguf_row.addWidget(self.gguf_edit, 1)
        gguf_row.addWidget(self.gguf_browse)
        self.gguf_holder = QWidget()
        self.gguf_holder.setLayout(gguf_row)
        gguf_row.setContentsMargins(0, 0, 0, 0)
        self.gguf_label = QLabel("<b>GGUF File:</b>")
        form.addRow(self.gguf_label, self.gguf_holder)

        self.n_ctx_spin = QSpinBox()
        self.n_ctx_spin.setRange(512, 262144)
        self.n_ctx_spin.setSingleStep(512)
        self.n_ctx_spin.setValue(int(cfg.get("n_ctx", 4096)))
        self.n_ctx_label = QLabel("<b>GGUF Context (n_ctx):</b>")
        form.addRow(self.n_ctx_label, self.n_ctx_spin)

        self.gpu_layers_spin = QSpinBox()
        self.gpu_layers_spin.setRange(-1, 1024)
        self.gpu_layers_spin.setValue(int(cfg.get("n_gpu_layers", 0)))
        self.gpu_layers_spin.setToolTip("0 = CPU only, -1 = offload every layer to the GPU")
        self.gpu_layers_label = QLabel("<b>GPU Layers:</b>")
        form.addRow(self.gpu_layers_label, self.gpu_layers_spin)

        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(0, 512)
        self.threads_spin.setValue(int(cfg.get("n_threads", 0)))
        self.threads_spin.setToolTip("0 = auto-detect")
        self.threads_label = QLabel("<b>CPU Threads:</b>")
        form.addRow(self.threads_label, self.threads_spin)

        self.chat_format_edit = QLineEdit(str(cfg.get("chat_format", "")))
        self.chat_format_edit.setPlaceholderText("Optional, e.g. chatml, llama-3, gemma (blank = read from the GGUF)")
        self.chat_format_label = QLabel("<b>Chat Format:</b>")
        form.addRow(self.chat_format_label, self.chat_format_edit)
        self._gguf_rows = [
            self.gguf_label, self.gguf_holder, self.n_ctx_label, self.n_ctx_spin,
            self.gpu_layers_label, self.gpu_layers_spin, self.threads_label, self.threads_spin,
            self.chat_format_label, self.chat_format_edit,
        ]

        # --- Generation options ----------------------------------------------
        self.temp_spin = QDoubleSpinBox()
        self.temp_spin.setRange(0.0, 2.0)
        self.temp_spin.setSingleStep(0.1)
        self.temp_spin.setValue(float(cfg.get("temperature", 0.7)))
        form.addRow(QLabel("<b>Temperature:</b>"), self.temp_spin)

        self.max_tokens_spin = QSpinBox()
        self.max_tokens_spin.setRange(128, 131072)
        self.max_tokens_spin.setSingleStep(256)
        self.max_tokens_spin.setValue(int(cfg.get("max_tokens", 2048)))
        form.addRow(QLabel("<b>Max Response Tokens:</b>"), self.max_tokens_spin)

        self.context_spin = QSpinBox()
        self.context_spin.setRange(512, 1048576)
        self.context_spin.setSingleStep(1024)
        self.context_spin.setValue(int(cfg.get("context_length", 8192)))
        self.context_spin.setToolTip("Ollama context window (num_ctx). Larger values need more memory.")
        self.context_label = QLabel("<b>Ollama Context Window:</b>")
        form.addRow(self.context_label, self.context_spin)

        self.tool_mode_combo = QComboBox()
        self.tool_mode_combo.addItems(list(TOOL_MODES))
        self.tool_mode_combo.setCurrentText(str(cfg.get("tool_mode", "auto")))
        self.tool_mode_combo.setToolTip(
            "auto: native tool calling when supported, otherwise JSON-in-prompt\n"
            "native / prompt: force one protocol\noff: the assistant never operates the app"
        )
        form.addRow(QLabel("<b>Tool Calling:</b>"), self.tool_mode_combo)

        self.embed_edit = QLineEdit(str(cfg.get("embedding_endpoint_url", "")))
        self.embed_edit.setPlaceholderText("http://localhost:11434  (Ollama server used for RAG embeddings)")
        form.addRow(QLabel("<b>Embedding Server:</b>"), self.embed_edit)

        self.redact_check = QCheckBox("Mask passwords / tokens before terminal output is sent to a cloud model")
        self.redact_check.setChecked(bool(cfg.get("redact_secrets", True)))
        form.addRow(QLabel("<b>Privacy:</b>"), self.redact_check)

        root.addLayout(form)
        self.hint_label = QLabel("")
        self.hint_label.setWordWrap(True)
        self.hint_label.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(self.hint_label)
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(self.status_label)
        root.addSpacing(6)

        btn_row = QHBoxLayout()
        test_btn = QPushButton("Test Connection")
        test_btn.clicked.connect(self._test_connection)
        btn_row.addWidget(test_btn)
        btn_row.addStretch(1)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        save_btn = QPushButton("Save Settings")
        save_btn.setStyleSheet("background-color: #2b78e4; color: white; font-weight: bold;")
        save_btn.clicked.connect(self._save_settings)
        btn_row.addWidget(save_btn)
        root.addLayout(btn_row)

        # Load the saved values for the active provider, then react to changes.
        self._load_profile(self._current_label, from_config=True)
        self.provider_combo.currentTextChanged.connect(self._on_provider_changed)

    # ------------------------------------------------------------ provider switching
    def _collect_profile(self) -> dict:
        return {
            "endpoint_url": self.endpoint_edit.text().strip(),
            "api_key": self.api_key_edit.text().strip(),
            "model_name": self.model_combo.currentText().strip(),
            "gguf_path": self.gguf_edit.text().strip(),
        }

    def _load_profile(self, label: str, from_config: bool = False):
        spec = get_provider(label)
        if not spec:
            return
        if label in self._drafts:
            profile = self._drafts[label]
        elif from_config and get_provider(self.cfg_mgr.config.get("provider")) == spec:
            profile = {k: self.cfg_mgr.config.get(k, "") for k in ("endpoint_url", "api_key", "model_name", "gguf_path")}
        else:
            profile = self.cfg_mgr.profile_for(label)
        self.endpoint_edit.setText(profile.get("endpoint_url", ""))
        self.api_key_edit.setText(profile.get("api_key", ""))
        self.model_combo.clear()
        self.model_combo.setCurrentText(profile.get("model_name", ""))
        self.gguf_edit.setText(profile.get("gguf_path", ""))

        is_gguf = spec.backend == "gguf"
        is_ollama = spec.backend == "ollama"
        for widget in self._gguf_rows:
            widget.setVisible(is_gguf)
        self.endpoint_edit.setEnabled(not is_gguf)
        self.fetch_btn.setEnabled(True)
        self.context_spin.setVisible(is_ollama)
        self.context_label.setVisible(is_ollama)
        self.api_key_edit.setEnabled(not is_gguf)
        env = f" or set {spec.api_key_env}" if spec.api_key_env else ""
        self.api_key_edit.setPlaceholderText(
            "Not needed for this provider" if not spec.needs_key else f"Paste your API key{env}"
        )
        self.endpoint_edit.setPlaceholderText(spec.default_endpoint or "Not used for local GGUF files")
        self.hint_label.setText(spec.notes)

    def _on_provider_changed(self, label: str):
        if self._current_label:
            self._drafts[self._current_label] = self._collect_profile()
        self._current_label = label
        self.status_label.setText("")
        self._load_profile(label)

    # ---------------------------------------------------------------- actions
    def _browse_gguf(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select GGUF model", self.gguf_edit.text(), "GGUF models (*.gguf);;All Files (*)")
        if path:
            self.gguf_edit.setText(path)
            if not self.model_combo.currentText().strip():
                self.model_combo.setCurrentText(path.replace("\\", "/").rsplit("/", 1)[-1])

    def _current_config(self) -> dict:
        profile = self._collect_profile()
        return {
            "provider": self.provider_combo.currentText(),
            **profile,
            "temperature": self.temp_spin.value(),
            "max_tokens": self.max_tokens_spin.value(),
            "n_ctx": self.n_ctx_spin.value(),
            "n_gpu_layers": self.gpu_layers_spin.value(),
            "n_threads": self.threads_spin.value(),
            "chat_format": self.chat_format_edit.text().strip(),
            "context_length": self.context_spin.value(),
            "tool_mode": self.tool_mode_combo.currentText(),
            "embedding_endpoint_url": self.embed_edit.text().strip(),
            "redact_secrets": self.redact_check.isChecked(),
        }

    def _fetch_models(self):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            models = list_models(self._current_config())
        except LLMError as exc:
            self.status_label.setText(f"Could not list models: {exc}")
            return
        except Exception as exc:
            self.status_label.setText(f"Could not list models: {exc}")
            return
        finally:
            QApplication.restoreOverrideCursor()
        current = self.model_combo.currentText().strip()
        self.model_combo.clear()
        self.model_combo.addItems(models)
        self.model_combo.setCurrentText(current or (models[0] if models else ""))
        self.status_label.setText(f"{len(models)} model(s) found.")

    def _test_connection(self):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            ok, message = test_connection(self._current_config())
        finally:
            QApplication.restoreOverrideCursor()
        self.status_label.setText(("OK: " if ok else "Problem: ") + message)

    def _save_settings(self):
        new_cfg = self._current_config()
        spec = get_provider(new_cfg["provider"])
        if spec and spec.backend == "gguf" and not new_cfg["gguf_path"]:
            QMessageBox.warning(self, "GGUF file required", "Choose a .gguf model file first.")
            return
        if not new_cfg["model_name"] and spec and spec.backend != "gguf":
            QMessageBox.warning(self, "Model required", "Enter or select a model name.")
            return
        self.cfg_mgr.save(new_cfg)
        QMessageBox.information(self, "Saved", "LLM configuration saved successfully!")
        self.accept()
