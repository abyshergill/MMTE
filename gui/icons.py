"""Crisp, theme-coloured icons drawn with QPainter.

Platform ``QStyle`` icons (``SP_MediaPlay``, ``SP_DialogCloseButton`` ...) differ per OS and are
blurry or nearly invisible on the dark theme.  These are drawn as vectors at the requested size,
so they stay sharp on any DPI and always use the app's colour tokens.
"""

from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import QApplication

from gui.ui_style import TOKENS


def _canvas(size: int) -> tuple[QPixmap, QPainter, float]:
    app = QApplication.instance()
    ratio = app.devicePixelRatio() if app is not None else 1.0
    ratio = max(ratio, 2.0)  # always oversample so the icon is sharp when scaled
    pixmap = QPixmap(int(size * ratio), int(size * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    return pixmap, painter, float(size)


def _pen(color: str, width: float) -> QPen:
    pen = QPen(QColor(color), width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def play_icon(color: str | None = None, size: int = 16) -> QIcon:
    pixmap, p, s = _canvas(size)
    path = QPainterPath()
    path.moveTo(s * 0.30, s * 0.18)
    path.lineTo(s * 0.84, s * 0.50)
    path.lineTo(s * 0.30, s * 0.82)
    path.closeSubpath()
    p.setPen(_pen(color or TOKENS["ok"], s * 0.08))
    p.setBrush(QColor(color or TOKENS["ok"]))
    p.drawPath(path)
    p.end()
    return QIcon(pixmap)


def stop_icon(color: str | None = None, size: int = 16) -> QIcon:
    pixmap, p, s = _canvas(size)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color or TOKENS["err"]))
    p.drawRoundedRect(QRectF(s * 0.22, s * 0.22, s * 0.56, s * 0.56), s * 0.10, s * 0.10)
    p.end()
    return QIcon(pixmap)


def pause_icon(color: str | None = None, size: int = 16) -> QIcon:
    pixmap, p, s = _canvas(size)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color or TOKENS["warn"]))
    p.drawRoundedRect(QRectF(s * 0.24, s * 0.20, s * 0.18, s * 0.60), s * 0.05, s * 0.05)
    p.drawRoundedRect(QRectF(s * 0.58, s * 0.20, s * 0.18, s * 0.60), s * 0.05, s * 0.05)
    p.end()
    return QIcon(pixmap)


def record_icon(color: str | None = None, size: int = 16) -> QIcon:
    pixmap, p, s = _canvas(size)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color or TOKENS["err"]))
    p.drawEllipse(QPointF(s / 2, s / 2), s * 0.30, s * 0.30)
    p.end()
    return QIcon(pixmap)


def cross_icon(color: str | None = None, size: int = 16) -> QIcon:
    pixmap, p, s = _canvas(size)
    p.setPen(_pen(color or TOKENS["err"], s * 0.11))
    a, b = s * 0.28, s * 0.72
    p.drawLine(QPointF(a, a), QPointF(b, b))
    p.drawLine(QPointF(a, b), QPointF(b, a))
    p.end()
    return QIcon(pixmap)


def plus_icon(color: str | None = None, size: int = 16) -> QIcon:
    pixmap, p, s = _canvas(size)
    p.setPen(_pen(color or TOKENS["text"], s * 0.11))
    p.drawLine(QPointF(s / 2, s * 0.22), QPointF(s / 2, s * 0.78))
    p.drawLine(QPointF(s * 0.22, s / 2), QPointF(s * 0.78, s / 2))
    p.end()
    return QIcon(pixmap)


def chevron_icon(direction: str = "up", color: str | None = None, size: int = 16) -> QIcon:
    pixmap, p, s = _canvas(size)
    p.setPen(_pen(color or TOKENS["dim"], s * 0.11))
    if direction == "up":
        pts = [QPointF(s * 0.26, s * 0.62), QPointF(s * 0.50, s * 0.36), QPointF(s * 0.74, s * 0.62)]
    else:
        pts = [QPointF(s * 0.26, s * 0.38), QPointF(s * 0.50, s * 0.64), QPointF(s * 0.74, s * 0.38)]
    p.drawPolyline(pts)
    p.end()
    return QIcon(pixmap)


def pencil_icon(color: str | None = None, size: int = 16) -> QIcon:
    pixmap, p, s = _canvas(size)
    p.setPen(_pen(color or TOKENS["text"], s * 0.09))
    p.drawLine(QPointF(s * 0.24, s * 0.76), QPointF(s * 0.72, s * 0.28))
    p.drawLine(QPointF(s * 0.62, s * 0.22), QPointF(s * 0.78, s * 0.38))
    p.drawLine(QPointF(s * 0.20, s * 0.80), QPointF(s * 0.32, s * 0.78))
    p.end()
    return QIcon(pixmap)


ICON_SIZE = QSize(16, 16)
