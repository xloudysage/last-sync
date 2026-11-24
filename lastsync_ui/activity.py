from datetime import datetime
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QScrollArea
from PyQt6.QtCore import Qt, QTimer
from .theme import C

MAX_LOG_ENTRIES = 60


class LogRow(QWidget):
    _FG = {
        "track":    C["green"],
        "info":     "#999999",
        "warn":     C["warn"],
        "error":    C["error"],
        "auth":     C["error"],
    }

    def __init__(self, text: str, level: str, ts: str, parent=None):
        super().__init__(parent)
        self.setFixedHeight(20)
        fg = self._FG.get(level, self._FG["info"])

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 10, 0)
        lay.setSpacing(7)

        t = QLabel(ts)
        t.setFixedWidth(54)
        t.setStyleSheet("color: #999999; font-family: Courier New; font-size: 8px;")
        t.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        dot = QLabel("●")
        dot.setFixedWidth(8)
        dot.setStyleSheet(f"color: {fg}; font-size: 6px;")
        dot.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        msg = QLabel(text)
        msg.setTextFormat(Qt.TextFormat.PlainText)
        msg.setStyleSheet(f"color: {fg}; font-family: Courier New; font-size: 9px;")
        msg.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        lay.addWidget(t)
        lay.addWidget(dot)
        lay.addWidget(msg, 1)


# ── Log panel ──────────────────────────────────────────────────
class LogPanel(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("logPanel")
        self.setStyleSheet(f"""
            QFrame#logPanel {{
                background-color: {C['bg1']};
                border: 1px solid {C['border']};
                border-radius: 14px;
            }}
        """)
        self.setFixedHeight(106)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 7, 0, 4)
        outer.setSpacing(1)

        hdr = QHBoxLayout()
        hdr.setContentsMargins(12, 0, 12, 0)
        lbl = QLabel("ACTIVITY")
        lbl.setStyleSheet("color: #999999; font-family: Courier New; font-size: 8px; "
                          "font-weight: bold; letter-spacing: 3px; border: none;")
        self._cnt = QLabel("")
        self._cnt.setStyleSheet("color: #999999; font-family: Courier New; font-size: 8px; border: none;")
        hdr.addWidget(lbl)
        hdr.addStretch()
        hdr.addWidget(self._cnt)
        outer.addLayout(hdr)

        inner = QWidget()
        inner.setStyleSheet("background: transparent;")
        self._rows = QVBoxLayout(inner)
        self._rows.setContentsMargins(0, 2, 0, 0)
        self._rows.setSpacing(0)
        self._rows.addStretch()

        scroll = QScrollArea()
        scroll.setWidget(inner)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("background: transparent; border: none;")
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll = scroll
        outer.addWidget(scroll)

        self._entries = []
        self._total   = 0

    def add_entry(self, text: str, level: str = "info"):
        self._total += 1
        ts  = datetime.now().strftime("%H:%M:%S")
        row = LogRow(text, level, ts)
        self._rows.insertWidget(self._rows.count() - 1, row)
        self._entries.append(row)
        while len(self._entries) > MAX_LOG_ENTRIES:
            old = self._entries.pop(0)
            self._rows.removeWidget(old)
            old.deleteLater()
        self._cnt.setText(str(self._total))
        QTimer.singleShot(50, lambda: self._scroll.verticalScrollBar().setValue(
            self._scroll.verticalScrollBar().maximum()
        ))


# ── Reveal button ──────────────────────────────────────────────
