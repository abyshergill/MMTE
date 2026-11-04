"""Small, crisp "x" close button used on terminal tabs and in the session list.

The default Qt tab close icon is a fixed bitmap that looks blurry and nearly invisible on a
dark theme.  This button paints its own anti-aliased cross, so it stays sharp at any DPI,
is muted until hovered, and turns into a clear red target when the pointer is over it.
"""

from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QAbstractButton

from gui.ui_style import TOKENS


class CloseButton(QAbstractButton):
    """Round hover target with a drawn cross.  Emits ``clicked`` like any button."""

    def __init__(self, parent=None, size: int = 18, tooltip: str = "Close terminal"):
        super().__init__(parent)
        self._size = int(size)
        self.setFixedSize(self._size, self._size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip)

    def sizeHint(self) -> QSize:
        return QSize(self._size, self._size)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)

        if self.isDown():
            background, cross = QColor(TOKENS["err"]).darker(130), QColor("#ffffff")
        elif self.underMouse():
            background, cross = QColor(TOKENS["err"]), QColor("#ffffff")
        else:
            background, cross = QColor(0, 0, 0, 0), QColor(TOKENS["mute"])

        if background.alpha():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(background)
            painter.drawEllipse(rect)

        pen = QPen(cross, 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        centre = rect.center()
        reach = rect.width() * 0.22
        painter.drawLine(QPointF(centre.x() - reach, centre.y() - reach), QPointF(centre.x() + reach, centre.y() + reach))
        painter.drawLine(QPointF(centre.x() - reach, centre.y() + reach), QPointF(centre.x() + reach, centre.y() - reach))
        painter.end()

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)
