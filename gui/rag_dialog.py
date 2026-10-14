from pathlib import Path
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.rag_manager import AVAILABLE_EMBEDDING_MODELS, RAGMemoryManager
from core.application_knowledge import APPLICATION_MEMORY_NAME


class RAGManagementDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Project RAG Memory & Embedding Manager")
        self.resize(850, 580)
        self.rag_mgr = RAGMemoryManager()
        self.pending_files: list[str] = []
        self.browse_files_btn: QPushButton | None = None
        self.save_btn: QPushButton | None = None
        self.delete_btn: QPushButton | None = None
        self._build_ui()

    def _build_ui(self):
        layout = QHBoxLayout(self)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left Side - List of RAG Memories
        left_container = QVBoxLayout()
        left_container.addWidget(QLabel("<b>Saved Project RAG Memories</b>"))

        self.rag_list = QListWidget()
        self.rag_list.currentTextChanged.connect(self._on_select_memory)
        left_container.addWidget(self.rag_list)

        btn_row = QHBoxLayout()
        new_btn = QPushButton("+ New RAG Memory")
        new_btn.clicked.connect(self._clear_fields)
        self.delete_btn = QPushButton("Delete")
        self.delete_btn.setStyleSheet("background-color: #8a2d3b; color: white;")
        self.delete_btn.clicked.connect(self._delete_memory)
        btn_row.addWidget(new_btn)
        btn_row.addWidget(self.delete_btn)
        left_container.addLayout(btn_row)

        left_widget = QWidget()
        left_widget.setLayout(left_container)
        splitter.addWidget(left_widget)

        # Right Side - RAG Config, Embedding Model, Job Assignment & File Ingestion
        right_container = QVBoxLayout()

        # RAG Name
        right_container.addWidget(QLabel("<b>RAG Memory Identifier:</b>"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. project_1_rag or firmware_test_rag")
        right_container.addWidget(self.name_edit)

        # Embedding Model Selector
        right_container.addWidget(QLabel("<b>Select Embedding Model:</b>"))
        self.embed_combo = QComboBox()
        self.embed_combo.addItems(AVAILABLE_EMBEDDING_MODELS)
        right_container.addWidget(self.embed_combo)

        # Assigned Job / System Role
        right_container.addWidget(QLabel("<b>Assigned Job / Agent Role:</b>"))
        self.job_edit = QLineEdit()
        self.job_edit.setPlaceholderText(
            "e.g. Firmware Testing Engineer for Project 1 - analyzes serial logs"
        )
        right_container.addWidget(self.job_edit)

        # Add Data: Text input & File Ingestion
        right_container.addWidget(
            QLabel("<b>Add Knowledge (Manual Text or Append Context):</b>")
        )
        self.context_edit = QTextEdit()
        self.context_edit.setPlaceholderText(
            "Type or paste context/specifications here to add to this memory..."
        )
        right_container.addWidget(self.context_edit)

        # File Attachment Controls
        file_row = QHBoxLayout()
        self.browse_files_btn = QPushButton("📁 Browse & Append Files (.txt, .log, .py, .md)...")
        self.browse_files_btn.clicked.connect(self._browse_files)
        file_row.addWidget(self.browse_files_btn)
        right_container.addLayout(file_row)

        self.files_label = QLabel("Ingested Files: None")
        self.files_label.setWordWrap(True)
        self.files_label.setStyleSheet("color: #888888; font-size: 11px;")
        right_container.addWidget(self.files_label)

        # Save / Append Button
        self.save_btn = QPushButton("💾 Save / Append Data to RAG Memory")
        self.save_btn.setStyleSheet(
            "background-color: #2b78e4; color: white; font-weight: bold; padding: 6px;"
        )
        self.save_btn.clicked.connect(self._save_memory)
        right_container.addWidget(self.save_btn)

        right_widget = QWidget()
        right_widget.setLayout(right_container)
        splitter.addWidget(right_widget)

        splitter.setSizes([260, 590])
        layout.addWidget(splitter)

        self._refresh_list()

    def _refresh_list(self):
        self.rag_list.clear()
        for name in self.rag_mgr.list_names():
            self.rag_list.addItem(name)

    def _on_select_memory(self, name: str):
        if not name:
            return
        data = self.rag_mgr.get(name)
        if data:
            self.name_edit.setText(data.get("name", ""))

            # Set model in combo box if present
            model = data.get("embedding_model", "")
            idx = self.embed_combo.findText(model)
            if idx >= 0:
                self.embed_combo.setCurrentIndex(idx)

            self.job_edit.setText(data.get("assigned_job", ""))
            built_in = bool(data.get("built_in")) or name == APPLICATION_MEMORY_NAME
            if built_in:
                self.context_edit.setPlainText("\n\n".join(data.get("text_entries", [])))
            else:
                self.context_edit.clear()  # Clear new input field for appending

            files = data.get("file_sources", [])
            entries_count = len(data.get("text_entries", []))
            file_names = [Path(f).name for f in files]
            self.files_label.setText(
                f"{'Built-in application guide' if built_in else f'Existing Entries: {entries_count} block(s)'}\n"
                f"Files: {', '.join(file_names) if file_names else 'None'}"
            )
            self.pending_files = []
            self._set_builtin_read_only(built_in)

    def _set_builtin_read_only(self, read_only: bool) -> None:
        self.name_edit.setReadOnly(read_only)
        self.job_edit.setReadOnly(read_only)
        self.context_edit.setReadOnly(read_only)
        self.embed_combo.setEnabled(not read_only)
        if self.browse_files_btn:
            self.browse_files_btn.setEnabled(not read_only)
        if self.save_btn:
            self.save_btn.setEnabled(not read_only)
        if self.delete_btn:
            self.delete_btn.setEnabled(not read_only)

    def _clear_fields(self):
        self._set_builtin_read_only(False)
        self.rag_list.clearSelection()
        self.name_edit.clear()
        self.job_edit.clear()
        self.context_edit.clear()
        self.files_label.setText("Ingested Files: None")
        self.pending_files = []

    def _browse_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Select Text/Log/Code Files for RAG Memory",
            "",
            "Text & Code Files (*.txt *.log *.md *.py *.json *.yaml *.csv);;All Files (*.*)",
        )
        if files:
            self.pending_files.extend(files)
            file_names = [Path(f).name for f in self.pending_files]
            self.files_label.setText(f"Pending to add: {', '.join(file_names)}")

    def _save_memory(self):
        name = self.name_edit.text().strip()
        model = self.embed_combo.currentText()
        job = self.job_edit.text().strip()
        new_text = self.context_edit.toPlainText().strip()

        if not name:
            QMessageBox.warning(
                self, "Validation Error", "RAG Memory Name cannot be empty."
            )
            return

        self.rag_mgr.create_or_update(
            name=name,
            embedding_model=model,
            assigned_job=job,
            new_text=new_text,
            new_file_paths=self.pending_files,
        )

        self._refresh_list()
        self.context_edit.clear()
        self.pending_files = []
        self._on_select_memory(name)
        QMessageBox.information(
            self, "Success", f"RAG Memory '{name}' updated successfully!"
        )

    def _delete_memory(self):
        name = self.name_edit.text().strip()
        if not name or not self.rag_mgr.get(name):
            return

        if (
            QMessageBox.question(
                self, "Confirm Delete", f"Delete RAG Memory '{name}'?"
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.rag_mgr.delete(name)
            self._clear_fields()
            self._refresh_list()