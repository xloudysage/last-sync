from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QColor, QPainter, QBrush, QPixmap, QPainterPath
from .theme import C


class PulseDot(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(8, 8)
        self._alpha  = 80
        self._active = False
        self._step   = -5
        self._timer  = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)

    def set_active(self, v: bool):
        self._active = v
        self._timer.setInterval(25 if v else 1000)
        if not v:
            self._alpha = 80
            self.update()

    def _tick(self):
        if not self._active:
            return
        self._alpha += self._step
        if self._alpha <= 40:
            self._step = 5
        elif self._alpha >= 255:
            self._step = -5
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        col = QColor(29, 185, 84, self._alpha) if self._active else QColor(50, 50, 50, 120)
        p.setBrush(QBrush(col))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(0, 0, 8, 8)


# ── Album art widget ───────────────────────────────────────────
class ArtWidget(QWidget):
    SIZE = 64

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self._px = None

    def set_pixmap(self, px: QPixmap):
        self._px = px.scaled(
            self.SIZE, self.SIZE,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.update()

    def clear(self):
        self._px = None
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, self.SIZE, self.SIZE, 10, 10)
        p.setClipPath(path)
        if self._px:
            p.drawPixmap(0, 0, self._px)
        else:
            p.fillRect(0, 0, self.SIZE, self.SIZE, QColor(22, 22, 22))
            p.setPen(QColor(45, 45, 45))
            p.setFont(QFont("Courier New", 20))
            p.drawText(0, 0, self.SIZE, self.SIZE, Qt.AlignmentFlag.AlignCenter, "♪")


# ── Progress bar ───────────────────────────────────────────────
class ProgressBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(4)
        self._v = 0.0

    def set_progress(self, v: float):
        self._v = max(0.0, min(1.0, v))
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(28, 28, 28)))
        p.drawRoundedRect(0, 0, w, h, 2, 2)
        fw = int(w * self._v)
        if fw > 1:
            p.setBrush(QBrush(QColor(29, 185, 84)))
            p.drawRoundedRect(0, 0, fw, h, 2, 2)


# ── Log row ────────────────────────────────────────────────────
