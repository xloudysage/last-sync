import sys
import time
import os
import os
from datetime import datetime
from pathlib import Path
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QLabel, QPushButton, QFrame, QDialog,
    QLineEdit, QMessageBox, QSystemTrayIcon, QMenu, QScrollArea, QListWidget
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import (
    QFont, QColor, QPainter, QBrush, QIcon, QPixmap, QPen,
    QFontDatabase, QPainterPath
)
from lastsync_core.config import DEFAULT_CONFIG as _DEFAULT_CONFIG, load_config, save_config
from lastsync_core.workers import SyncWorker, AuthWorker, ArtLoader
from lastsync_core.sync import POLL_ACTIVE, POLL_IDLE

MAX_LOG_ENTRIES = 60

# ── Palette ────────────────────────────────────────────────────
C = {
    "green":       "#1db954",
    "green_hi":    "#1ed760",
    "green_lo":    "#17a349",
    "green_dim":   "#0c2e17",
    "green_text":  "#153320",
    "bg":          "#0a0a0a",
    "bg1":         "#0d0d0d",
    "bg2":         "#111111",
    "bg3":         "#161616",
    "border":      "#1e1e1e",
    "border2":     "#252525",
    "border3":     "#303030",
    "text":        "#e8e8e8",
    "text_dim":    "#aaaaaa",
    "text_mid":    "#666666",
    "text_muted":  "#999999",
    "text_ghost":  "#aaaaaa",
    "warn":        "#b08020",
    "error":       "#9b3a3a",
}


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
class RevealButton(QPushButton):
    def __init__(self, field: QLineEdit, parent=None):
        super().__init__("👁", parent)
        self._field = field
        self._shown = False
        self.setFixedSize(28, 28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet("""
            QPushButton { background: transparent; border: none; color: #999999; font-size: 12px; }
            QPushButton:hover { color: #777777; }
        """)
        self.clicked.connect(self._toggle)

    def _toggle(self):
        self._shown = not self._shown
        self._field.setEchoMode(
            QLineEdit.EchoMode.Normal if self._shown else QLineEdit.EchoMode.Password
        )


# ── Settings dialog ────────────────────────────────────────────
class SettingsDialog(QDialog):
    _FIELD = f"""
        QLineEdit {{
            background: {C['bg3']}; border: 1px solid {C['border2']};
            border-radius: 8px; color: #dddddd;
            padding: 9px 38px 9px 12px;
            font-family: Courier New; font-size: 11px;
            selection-background-color: {C['green']};
        }}
        QLineEdit:focus {{ border-color: {C['green']}; }}
        QLineEdit:hover {{ border-color: {C['border3']}; }}
    """
    _FIELD_ERR = _FIELD.replace(f"border: 1px solid {C['border2']}", "border: 1px solid #7a2a2a")

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setFixedWidth(400)
        self.setStyleSheet(f"QDialog {{ background: {C['bg']}; }}")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 26, 26, 26)
        lay.setSpacing(0)

        hdr = QLabel("SETTINGS")
        hdr.setStyleSheet(f"color: {C['text']}; font-family: Courier New; "
                          "font-size: 11px; font-weight: bold; letter-spacing: 5px;")
        lay.addWidget(hdr)
        lay.addSpacing(22)

        def section(txt):
            l = QLabel(txt)
            l.setStyleSheet("color: #999999; font-family: Courier New; font-size: 8px; letter-spacing: 2px;")
            lay.addWidget(l)
            lay.addSpacing(6)

        def field_row(ph, pwd=False):
            container = QWidget()
            container.setStyleSheet("background: transparent;")
            hl = QHBoxLayout(container)
            hl.setContentsMargins(0, 0, 0, 0)
            hl.setSpacing(0)
            f = QLineEdit()
            f.setPlaceholderText(ph)
            f.setStyleSheet(self._FIELD)
            f.setMinimumHeight(40)
            hl.addWidget(f, 1)
            if pwd:
                f.setEchoMode(QLineEdit.EchoMode.Password)
                rb = RevealButton(f)
                hl.addWidget(rb)
                hl.setAlignment(rb, Qt.AlignmentFlag.AlignVCenter)
            return container, f

        section("LAST.FM")
        w1, self.lfm_user = field_row("username")
        w2, self.lfm_key  = field_row("api key", pwd=True)
        lay.addWidget(w1)
        lay.addSpacing(8)
        lay.addWidget(w2)
        lay.addSpacing(20)

        section("SPOTIFY")
        w3, self.sp_id  = field_row("client id",     pwd=True)
        lay.addWidget(w3)
        lay.addSpacing(8)
        lay.addSpacing(8)

        hint = QLabel("Spotify Premium required. PKCE authorization opens your browser. Credentials use OS storage.")
        hint.setStyleSheet("color: #999999; font-family: Courier New; font-size: 8px;")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        lay.addSpacing(22)

        self.lfm_user.setText(cfg.get("lastfm_user", ""))
        self.lfm_key.setText(cfg.get("lastfm_api_key", ""))
        self.sp_id.setText(cfg.get("spotify_client_id", ""))

        btn_row = QHBoxLayout()
        cancel  = QPushButton("Cancel")
        save    = QPushButton("SAVE")
        for b in (cancel, save):
            b.setMinimumHeight(38)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.setStyleSheet(f"""
            QPushButton {{ background: {C['bg3']}; color: {C['text_muted']}; border: 1px solid {C['border']};
                border-radius: 8px; font-family: Courier New; font-size: 9px; }}
            QPushButton:hover {{ color: #888888; border-color: {C['border3']}; }}
        """)
        save.setStyleSheet(f"""
            QPushButton {{ background: {C['green']}; color: #000000; border: none;
                border-radius: 8px; font-family: Courier New; font-size: 9px;
                font-weight: bold; letter-spacing: 2px; }}
            QPushButton:hover {{ background: {C['green_hi']}; }}
        """)
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self._on_save)
        btn_row.addWidget(cancel)
        btn_row.addWidget(save)
        lay.addLayout(btn_row)

        self._fields = {
            "lastfm_user":           self.lfm_user,
            "lastfm_api_key":        self.lfm_key,
            "spotify_client_id":     self.sp_id,
        }

    def _on_save(self):
        any_empty = False
        for f in self._fields.values():
            if not f.text().strip():
                f.setStyleSheet(self._FIELD_ERR)
                any_empty = True
            else:
                f.setStyleSheet(self._FIELD)
        if not any_empty:
            self.accept()

    def get_config(self) -> dict:
        return {k: f.text().strip() for k, f in self._fields.items()}

    def is_valid(self) -> bool:
        return all(f.text().strip() for f in self._fields.values())


# ── Main window ────────────────────────────────────────────────
class MatchDialog(QDialog):
    def __init__(self, source, candidates, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Choose Spotify match")
        self.setStyleSheet("QDialog { background: #111111; } QLabel { color: #dddddd; } "
                          "QListWidget, QLineEdit { background: #161616; color: #dddddd; padding: 8px; } "
                          "QPushButton { background: #1db954; color: black; padding: 8px; border-radius: 6px; }")
        self.setMinimumSize(520, 300)
        layout = QVBoxLayout(self)
        label = QLabel(f"{source.title} - {source.artist}")
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        layout.addWidget(label)
        self.items = QListWidget()
        self.candidates = list(candidates)
        for ranked in self.candidates:
            track = ranked.candidate.track
            self.items.addItem(f"{ranked.confidence:.0%} | {track.title} - {track.artist} | {track.album} | {ranked.reason}")
        layout.addWidget(self.items)
        self.custom = QLineEdit()
        self.custom.setPlaceholderText("Or paste a Spotify track link / URI")
        layout.addWidget(self.custom)
        note = QLabel("Your choice is saved for this recording and Spotify account.")
        layout.addWidget(note)
        buttons = QHBoxLayout()
        cancel, save = QPushButton("Cancel"), QPushButton("Use this track")
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self._save)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)

    def uri(self):
        if self.custom.text().strip():
            return self.custom.text().strip()
        row = self.items.currentRow()
        return self.candidates[row].candidate.uri if row >= 0 else ""

    def _save(self):
        if self.uri():
            self.accept()


class LastSyncWindow(QMainWindow):
    BTN_R = 25

    def __init__(self, cfg=None, prompt_settings=True):
        super().__init__()
        config_error = None
        try:
            self._cfg = dict(cfg) if cfg is not None else load_config()
        except Exception as error:
            self._cfg = dict(_DEFAULT_CONFIG)
            config_error = str(error)
        self._sp = self._worker = self._auth_worker = None
        self._art_loaders = set()
        self._art_generation = 0
        self._syncing = self._closing = False
        self._state = "IDLE"
        self._match_request = None
        self._progress_snapshot = None
        self._track_start = 0.0
        self._duration_ms = 0
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(250)
        self._tick_timer.timeout.connect(self._tick_progress)
        self._init_fonts()
        self._init_ui()
        self._init_tray()
        if config_error:
            self.set_status(config_error)
            self.log_panel.add_entry(config_error, "error")
        elif prompt_settings and not self._config_is_complete():
            QTimer.singleShot(300, self._prompt_settings)

    def _init_fonts(self):
        fams        = QFontDatabase.families()
        if not fams and sys.platform == "win32":
            # Qt offscreen / isolated desktop sessions may not enumerate system fonts.
            fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
            for name in ("segoeui.ttf", "georgia.ttf", "cour.ttf", "seguisym.ttf"):
                QFontDatabase.addApplicationFont(str(fonts / name))
            fams = QFontDatabase.families()
            if "Segoe UI" in fams:
                QApplication.instance().setFont(QFont("Segoe UI", 10))
        self._serif = "Georgia"     if "Georgia"     in fams else "DejaVu Serif"
        self._mono  = "Courier New" if "Courier New" in fams else "DejaVu Sans Mono"

    def _f(self, family, size, bold=False):
        return QFont(family, size, QFont.Weight.Bold if bold else QFont.Weight.Normal)

    def _init_ui(self):
        self.setWindowTitle("LastSync 1.1")
        self.setMinimumSize(440, 680)
        self.resize(440, 680)
        self.setStyleSheet(f"QMainWindow {{ background-color: {C['bg']}; }} QLabel {{ background: transparent; }}")

        # Window icon
        px = QPixmap(64, 64)
        px.fill(Qt.GlobalColor.transparent)
        ip = QPainter(px)
        ip.setRenderHint(QPainter.RenderHint.Antialiasing)
        ip.setBrush(QBrush(QColor(29, 185, 84)))
        ip.setPen(Qt.PenStyle.NoPen)
        ip.drawRoundedRect(4, 4, 56, 56, 14, 14)
        ip.setPen(QPen(QColor(0, 0, 0)))
        ip.setFont(QFont(self._mono, 16, QFont.Weight.Bold))
        ip.drawText(px.rect(), Qt.AlignmentFlag.AlignCenter, "LS")
        ip.end()
        self.setWindowIcon(QIcon(px))

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(24, 24, 24, 18)
        root.setSpacing(0)

        # ── Header ──────────────────────────────────────────────
        hdr = QHBoxLayout()
        hdr.setSpacing(0)

        badge = QLabel("LS")
        badge.setFixedSize(32, 32)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setStyleSheet(f"""
            background: {C['green']}; color: #000000; border-radius: 8px;
            font-family: Courier New; font-size: 10px; font-weight: bold;
        """)

        title_col = QVBoxLayout()
        title_col.setSpacing(1)
        title_col.setContentsMargins(10, 0, 0, 0)
        app_lbl = QLabel("LASTSYNC")
        app_lbl.setFont(self._f(self._mono, 11, bold=True))
        app_lbl.setStyleSheet(f"color: {C['text']}; letter-spacing: 5px;")
        self.sub_label = QLabel(f"with  {self._cfg.get('lastfm_user', '—')}")
        self.sub_label.setFont(self._f(self._mono, 8))
        self.sub_label.setStyleSheet("color: #999999; letter-spacing: 1px;")
        title_col.addWidget(app_lbl)
        title_col.addWidget(self.sub_label)

        gear_btn = QPushButton("⚙")
        gear_btn.setFixedSize(30, 30)
        gear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        gear_btn.setToolTip("Settings")
        gear_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: #999999; border: none; font-size: 14px; border-radius: 7px; }}
            QPushButton:hover {{ color: #888888; background: {C['bg3']}; }}
        """)
        gear_btn.clicked.connect(self._open_settings)

        self.pulse = PulseDot()

        hdr.addWidget(badge)
        hdr.addLayout(title_col, 1)
        hdr.addWidget(gear_btn)
        hdr.addSpacing(8)
        hdr.addWidget(self.pulse)
        hdr.setAlignment(self.pulse, Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(hdr)

        root.addSpacing(20)

        # ── Now-playing card ─────────────────────────────────────
        card = QFrame()
        card.setObjectName("npCard")
        card.setStyleSheet(f"""
            QFrame#npCard {{
                background-color: {C['bg2']};
                border: 1px solid {C['border']};
                border-radius: 18px;
            }}
        """)
        cv = QVBoxLayout(card)
        cv.setContentsMargins(20, 18, 20, 16)
        cv.setSpacing(0)

        # Card header
        ch = QHBoxLayout()
        np_lbl = QLabel("NOW PLAYING")
        np_lbl.setFont(self._f(self._mono, 7, bold=True))
        np_lbl.setStyleSheet("color: #999999; letter-spacing: 4px;")
        self.live_badge = QLabel("TRACK FOLLOW")
        self.live_badge.setFont(self._f(self._mono, 7, bold=True))
        self.live_badge.setStyleSheet(f"color: {C['green']}; letter-spacing: 2px;")
        self.live_badge.hide()
        ch.addWidget(np_lbl)
        ch.addStretch()
        ch.addWidget(self.live_badge)
        cv.addLayout(ch)

        cv.addSpacing(14)

        # Art + text row — horizontal, art on the left
        art_row = QHBoxLayout()
        art_row.setSpacing(14)
        art_row.setContentsMargins(0, 0, 0, 0)

        self.art_widget = ArtWidget()
        art_row.addWidget(self.art_widget, 0, Qt.AlignmentFlag.AlignTop)

        text_col = QVBoxLayout()
        text_col.setSpacing(3)
        text_col.setContentsMargins(0, 0, 0, 0)

        self.track_label = QLabel("Start sync to listen along")
        self.track_label.setFont(self._f(self._serif, 14, bold=True))
        self.track_label.setStyleSheet(f"color: {C['text']};")
        self.track_label.setWordWrap(True)
        self.track_label.setMaximumWidth(300)
        self.track_label.setMaximumHeight(62)

        self.artist_label = QLabel("")
        self.artist_label.setFont(self._f(self._serif, 11))
        self.artist_label.setStyleSheet(f"color: {C['text_muted']};")
        self.artist_label.setWordWrap(True)
        self.artist_label.setMaximumWidth(300)
        self.artist_label.setMaximumHeight(28)

        # Small inline badge shown when track not on Spotify
        self.unavail_badge = QLabel("Choose a match; playback unchanged")
        self.unavail_badge.setFont(self._f(self._mono, 7))
        self.unavail_badge.setStyleSheet(f"""
            color: {C['warn']}; background: #1a1200;
            border: 1px solid #2e2000; border-radius: 4px;
            padding: 2px 7px;
        """)
        self.unavail_badge.hide()

        text_col.addWidget(self.track_label)
        text_col.addWidget(self.artist_label)
        text_col.addSpacing(5)
        text_col.addWidget(self.unavail_badge)
        text_col.addStretch()

        art_row.addLayout(text_col, 1)
        cv.addLayout(art_row)

        cv.addSpacing(16)

        self.progress_bar = ProgressBar()
        cv.addWidget(self.progress_bar)
        cv.addSpacing(6)

        time_row = QHBoxLayout()
        self.elapsed_lbl  = QLabel("")
        self.duration_lbl = QLabel("")
        for lbl in (self.elapsed_lbl, self.duration_lbl):
            lbl.setFont(self._f(self._mono, 8))
            lbl.setStyleSheet(f"color: {C['text_muted']};")
        time_row.addWidget(self.elapsed_lbl)
        time_row.addStretch()
        time_row.addWidget(self.duration_lbl)
        cv.addLayout(time_row)

        root.addWidget(card)
        root.addSpacing(10)

        # ── Status row ───────────────────────────────────────────
        sr = QHBoxLayout()
        self.status_label = QLabel("Ready")
        self.status_label.setFont(self._f(self._mono, 8))
        self.status_label.setStyleSheet(f"color: {C['text_ghost']}; letter-spacing: 1px;")

        self.poll_label = QLabel("")
        self.poll_label.setFont(self._f(self._mono, 8))
        self.poll_label.setStyleSheet(f"color: {C['border']}; font-family: Courier New;")
        self.poll_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.poll_label.setToolTip("Sync check frequency")

        sr.addWidget(self.status_label, 1)
        sr.addWidget(self.poll_label)
        root.addLayout(sr)

        root.addSpacing(8)

        # ── Log ──────────────────────────────────────────────────
        self.log_panel = LogPanel()
        root.addWidget(self.log_panel)

        root.addSpacing(14)

        # ── Sync button ──────────────────────────────────────────
        self.sync_btn = QPushButton("START SYNC")
        self.sync_btn.setFixedHeight(48)
        self.sync_btn.setFont(self._f(self._mono, 10, bold=True))
        self.sync_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sync_btn.setShortcut("Return")
        self._btn_start()
        self.sync_btn.clicked.connect(self.toggle_sync)
        root.addWidget(self.sync_btn)
        self.match_btn = QPushButton("CHOOSE SPOTIFY MATCH")
        self.match_btn.setMinimumHeight(32)
        self.match_btn.setStyleSheet("color: #bbbbbb; background: #161616; border-radius: 8px;")
        self.match_btn.clicked.connect(self._choose_match)
        self.match_btn.hide()
        root.addWidget(self.match_btn)
        for label in (self.track_label, self.artist_label, self.status_label, self.sub_label):
            label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        self.status_label.setMinimumHeight(32)

        root.addSpacing(10)

        # ── Footer ───────────────────────────────────────────────
        self.footer_label = QLabel(self._footer_text())
        self.footer_label.setFont(self._f(self._mono, 7))
        self.footer_label.setStyleSheet("color: #888888; letter-spacing: 1px;")
        self.footer_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.footer_label)

    def _footer_text(self):
        user = self._cfg.get("lastfm_user", "—")
        return f"polls {POLL_ACTIVE}s active · {POLL_IDLE}s idle  ·  last.fm/{user}"

    # ── Tray ──
    def _init_tray(self):
        self._tray = QSystemTrayIcon(self.windowIcon(), self)
        menu = QMenu()
        menu.setStyleSheet(f"""
            QMenu {{ background: {C['bg3']}; color: #aaaaaa; font-family: Courier New;
                font-size: 10px; border: 1px solid {C['border']}; border-radius: 6px; }}
            QMenu::item:selected {{ background: {C['border']}; }}
        """)
        menu.addAction("Show LastSync").triggered.connect(self._show_window)
        menu.addSeparator()
        menu.addAction("Quit").triggered.connect(self._begin_shutdown)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(
            lambda r: self._show_window()
            if r == QSystemTrayIcon.ActivationReason.Trigger else None
        )
        self._tray.setToolTip("LastSync · idle")
        self._tray.show()

    def _show_window(self):
        self.showNormal()
        self.activateWindow()

    # ── Button styles ──
    def _btn_start(self):
        r = self.BTN_R
        self.sync_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C['green']}; color: #000000; border: none;
                border-radius: {r}px; letter-spacing: 4px;
            }}
            QPushButton:hover   {{ background: {C['green_hi']}; }}
            QPushButton:pressed {{ background: {C['green_lo']}; }}
            QPushButton:disabled {{ background: {C['green_dim']}; color: {C['green_text']}; }}
        """)

    def _btn_stop(self):
        r = self.BTN_R
        self.sync_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C['bg3']}; color: {C['text_dim']};
                border: 1px solid #222222; border-radius: {r}px; letter-spacing: 4px;
            }}
            QPushButton:hover   {{ background: #1c1c1c; border-color: {C['border3']}; }}
            QPushButton:pressed {{ background: {C['bg']}; }}
        """)

    def _btn_loading(self):
        r = self.BTN_R
        self.sync_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C['green_dim']}; color: {C['green_text']}; border: none;
                border-radius: {r}px; letter-spacing: 4px;
            }}
        """)

    # ── Config ──
    def _config_is_complete(self):
        return all(self._cfg.get(k, "").strip() for k in _DEFAULT_CONFIG)

    def _prompt_settings(self):
        QMessageBox.information(self, "Welcome to LastSync",
                                "Fill in your credentials in Settings to get started.")
        self._open_settings()

    def _open_settings(self):
        if self._worker or self._auth_worker:
            QMessageBox.information(self, "Settings", "Stop the current session before changing settings.")
            return
        dlg = SettingsDialog(self._cfg, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new = dlg.get_config()
        try:
            save_config(new)
        except Exception:
            QMessageBox.warning(self, "Settings not saved", "OS credential storage or the settings file is unavailable.")
            return
        if new["spotify_client_id"] != self._cfg.get("spotify_client_id"):
            self._close_spotify()
        self._cfg = new
        self._refresh_labels()

    def _refresh_labels(self):
        self.sub_label.setText(f"with  {self._cfg.get('lastfm_user', '—')}")
        self.footer_label.setText(self._footer_text())

    # ── Progress ──
    def _tick_progress(self):
        playback = self._progress_snapshot
        if playback is None or not playback.duration_ms:
            return
        age = max(0, time.monotonic() - playback.observed_monotonic)
        if age > 10:
            self.elapsed_lbl.setText("refreshing")
            return
        position = min(playback.duration_ms, playback.position_ms + (int(age * 1000) if playback.playing else 0))
        self.progress_bar.set_progress(position / playback.duration_ms)
        self.elapsed_lbl.setText(self._ms(position))

    @staticmethod

    def _ms(ms: int) -> str:
        s = ms // 1000
        return f"{s // 60}:{s % 60:02d}"

    # ── Sync ──
    def toggle_sync(self):
        self.stop_sync() if self._syncing else self.start_sync()

    def start_sync(self):
        if self._worker or self._auth_worker or self._closing:
            return
        if not self._config_is_complete():
            self._prompt_settings()
            return
        self._syncing = True
        self.sync_btn.setText("CANCEL CONNECT")
        self.sync_btn.setEnabled(True)
        self._btn_stop()
        self.pulse.set_active(True)
        self.set_status("Connecting to Spotify")
        self._clear_card("Waiting to follow a track")
        if self._sp is not None:
            self._on_auth_ok(self._sp)
            return
        worker = AuthWorker(self._cfg)
        self._auth_worker = worker
        worker.auth_success.connect(self._on_auth_ok)
        worker.auth_failed.connect(self._on_auth_fail)
        worker.finished.connect(self._auth_finished)
        worker.start()

    def _on_auth_ok(self, sp):
        if not self._syncing or self._closing:
            sp.close()
            sp.auth.close()
            return
        self._sp = sp
        self.sync_btn.setText("STOP SYNC")
        self.sync_btn.setEnabled(True)
        self._btn_stop()
        self.set_status("Connected; polling Last.fm")
        self.log_panel.add_entry("Track Follow started; source position unknown", "info")
        worker = SyncWorker(self._sp, self._cfg)
        self._worker = worker
        worker.track_changed.connect(self._on_track)
        worker.status_changed.connect(self._on_status)
        worker.state_changed.connect(self._on_state)
        worker.playback_changed.connect(self._on_playback)
        worker.matching_required.connect(self._on_match)
        worker.poll_interval_changed.connect(self._on_poll_interval)
        worker.finished.connect(self._sync_finished)
        worker.start()
        self._tick_timer.start()

    def _auth_finished(self):
        worker = self._auth_worker
        self._auth_worker = None
        if worker:
            worker.deleteLater()
        if not self._syncing:
            self._reset_button()

    def _sync_finished(self):
        worker = self._worker
        self._worker = None
        self._syncing = False
        self._tick_timer.stop()
        self._progress_snapshot = None
        self._close_spotify()
        self.live_badge.hide()
        self.match_btn.hide()
        if worker:
            worker.deleteLater()
        self._reset_button()

    def _reset_button(self):
        self.pulse.set_active(False)
        self.sync_btn.setEnabled(not self._closing)
        self.sync_btn.setText("START SYNC")
        self._btn_start()

    def _on_auth_fail(self, msg):
        if not self._syncing:
            return
        self._syncing = False
        self._reset_button()
        self.set_status(msg)
        self.log_panel.add_entry(msg, "auth")

    def stop_sync(self):
        self._syncing = False
        self._tick_timer.stop()
        self.pulse.set_active(False)
        self.live_badge.hide()
        self.match_btn.hide()
        self._match_request = None
        self._art_generation += 1
        for loader in self._art_loaders:
            loader.stop()
        for worker in (self._worker, self._auth_worker):
            if worker:
                worker.stop()
        self._clear_card("Start sync to listen along")
        self.poll_label.setText("")
        self.set_status("Stopped; Spotify playback is unchanged")
        self._tray.setToolTip("LastSync - idle")
        if self._worker or self._auth_worker:
            self.sync_btn.setText("STOPPING")
            self.sync_btn.setEnabled(False)
        else:
            self._reset_button()

    def _clear_card(self, placeholder=""):
        self.track_label.setText(placeholder)
        self.track_label.setToolTip("")
        self.artist_label.setText("")
        self.artist_label.setToolTip("")
        self.art_widget.clear()
        self.unavail_badge.hide()
        self.progress_bar.set_progress(0)
        self.elapsed_lbl.setText("")
        self.duration_lbl.setText("")
        self._track_start = 0.0
        self._duration_ms = 0
        self._progress_snapshot = None

    # ── Handlers ──
    def _on_track(self, track, artist, pos_ms, dur_ms, art_url):
        if not self._syncing:
            return
        self.track_label.setText(track)
        self.track_label.setToolTip(track)
        self.artist_label.setText(artist)
        self.unavail_badge.hide()
        self.match_btn.setText("CORRECT SPOTIFY MATCH")
        self.match_btn.show()
        self.duration_lbl.setText(self._ms(dur_ms) if dur_ms else "")
        self.log_panel.add_entry(f"{track} - {artist}", "track")
        self._tray.setToolTip(f"LastSync - {track} - {artist}")
        self._art_generation += 1
        for loader in self._art_loaders:
            loader.stop()
        self.art_widget.clear()
        if art_url and len(self._art_loaders) < 2:
            loader = ArtLoader(art_url, self._art_generation)
            self._art_loaders.add(loader)
            loader.art_ready.connect(self._on_art)
            loader.finished.connect(lambda current=loader: self._art_finished(current))
            loader.start()

    def _on_art(self, raw, generation):
        if self._syncing and generation == self._art_generation:
            pixmap = QPixmap()
            if pixmap.loadFromData(raw):
                self.art_widget.set_pixmap(pixmap)

    def _art_finished(self, loader):
        self._art_loaders.discard(loader)
        loader.deleteLater()

    def _on_playback(self, playback):
        if not self._syncing or self._state == "IDLE":
            return
        self._progress_snapshot = playback
        self.duration_lbl.setText(self._ms(playback.duration_ms) if playback.duration_ms else "")
        self.elapsed_lbl.setToolTip("Your Spotify playback position; the source position is unknown")
        self._tick_progress()

    def _on_state(self, state, message):
        if not self._syncing:
            return
        self._state = state
        self.live_badge.setVisible(state == "FOLLOWING")
        self.set_status(message)
        self.log_panel.add_entry(f"{state}: {message}", "error" if state == "STOPPED" else "info")
        if state == "IDLE":
            self._clear_card("Last.fm reports no current track")
            self.match_btn.hide()
            self._match_request = None
        if state == "STOPPED":
            self._tick_timer.stop()

    def _on_match(self, event_id, source, candidates):
        if not self._syncing:
            return
        self._match_request = (event_id, source, candidates)
        self._art_generation += 1
        self._clear_card(source.title)
        self.artist_label.setText(source.artist)
        self.match_btn.setText("CHOOSE SPOTIFY MATCH")
        self.match_btn.show()
        self.unavail_badge.setVisible(bool(candidates))

    def _choose_match(self):
        if self._match_request is None or self._worker is None:
            return
        event_id, source, candidates = self._match_request
        dialog = MatchDialog(source, candidates, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and self._worker:
            self._worker.choose_match(event_id, dialog.uri())

    def _on_status(self, message, level):
        if self._syncing:
            self.set_status(message)
            self.log_panel.add_entry(message, level)

    def _on_poll_interval(self, interval: int):
        self.poll_label.setText(f"↻ {interval}s")
        color = "#1a3d22" if interval == POLL_ACTIVE else C["border"]
        self.poll_label.setStyleSheet(
            f"color: {color}; font-family: Courier New; font-size: 8px;"
        )

    def set_status(self, msg: str):
        self.status_label.setText(msg)
        self.status_label.setToolTip(msg)

    def _close_spotify(self):
        if self._sp:
            self._sp.close()
            self._sp.auth.close()
            self._sp = None

    def _begin_shutdown(self):
        if self._closing:
            return
        self._closing = True
        self.stop_sync()
        self.set_status("Closing background services")
        self._finish_shutdown()

    def _finish_shutdown(self):
        workers = [self._worker, self._auth_worker, *self._art_loaders]
        if any(worker and worker.isRunning() for worker in workers):
            QTimer.singleShot(100, self._finish_shutdown)
            return
        self._close_spotify()
        self._tray.hide()
        QApplication.instance().quit()

    def closeEvent(self, event):
        if QSystemTrayIcon.isSystemTrayAvailable() and not self._closing:
            self.hide()
            event.ignore()
        elif not self._closing:
            event.ignore()
            self._begin_shutdown()
        else:
            event.accept()


def main(argv=None):
    if sys.version_info < (3, 12):
        raise SystemExit("LastSync requires Python 3.12 or newer")
    argv = list(sys.argv if argv is None else argv)
    check = "--check" in argv
    if check:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication(argv)
    app.setStyle("Fusion")
    app.setQuitOnLastWindowClosed(False)
    window = LastSyncWindow(dict(_DEFAULT_CONFIG), prompt_settings=False) if check else LastSyncWindow()
    window.show()
    if check:
        app.processEvents()
        if window.grab().isNull():
            return 1
        window._tray.hide()
        window._closing = True
        window.close()
        print("LastSync native startup check passed (no accounts or network used)")
        return 0
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
