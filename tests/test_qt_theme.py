from __future__ import annotations
from unittest.mock import MagicMock

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPalette, QColor

from core.qt_theme import is_dark_mode, apply_system_theme, _dark_palette


def test_is_dark_mode_respects_qt_dark_scheme(qapp):
    fake_hints = MagicMock()
    fake_hints.colorScheme.return_value = Qt.ColorScheme.Dark
    original = qapp.styleHints
    qapp.styleHints = lambda: fake_hints
    try:
        assert is_dark_mode(qapp) is True
    finally:
        qapp.styleHints = original


def test_is_dark_mode_respects_qt_light_scheme(qapp):
    fake_hints = MagicMock()
    fake_hints.colorScheme.return_value = Qt.ColorScheme.Light
    original = qapp.styleHints
    qapp.styleHints = lambda: fake_hints
    try:
        assert is_dark_mode(qapp) is False
    finally:
        qapp.styleHints = original


def test_is_dark_mode_falls_back_to_palette_lightness(qapp):
    fake_hints = MagicMock()
    fake_hints.colorScheme.return_value = Qt.ColorScheme.Unknown
    original_hints = qapp.styleHints
    qapp.styleHints = lambda: fake_hints

    dark_pal = QPalette()
    dark_pal.setColor(QPalette.ColorRole.Window, QColor(10, 10, 10))
    original_pal = qapp.palette()
    qapp.setPalette(dark_pal)
    try:
        assert is_dark_mode(qapp) is True
    finally:
        qapp.styleHints = original_hints
        qapp.setPalette(original_pal)


def test_dark_palette_has_readable_contrast():
    pal = _dark_palette()
    window = pal.color(QPalette.ColorRole.Window)
    text = pal.color(QPalette.ColorRole.WindowText)
    # Dark background, light text -- basic sanity check, not a full contrast ratio.
    assert window.lightness() < 100
    assert text.lightness() > 150


def test_apply_system_theme_sets_fusion_style(qapp):
    apply_system_theme(qapp)
    assert qapp.style().objectName().lower() == "fusion"


def test_apply_system_theme_returns_bool(qapp):
    result = apply_system_theme(qapp)
    assert isinstance(result, bool)
