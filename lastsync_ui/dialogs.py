from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QDialog, QLineEdit, QListWidget, QListWidgetItem
from PyQt6.QtCore import Qt, pyqtSignal
from .theme import C


class DeviceDialog(QDialog):
    refresh_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Spotify playback device")
        self.setMinimumWidth(380)
        self.setStyleSheet(f"""
            QDialog {{ background: {C['bg']}; }}
            QLabel {{ color: {C['text']}; }}
            QListWidget {{ color: {C['text']}; background: {C['bg3']}; border: 1px solid {C['border3']}; }}
            QListWidget::item {{ padding: 7px; }}
            QListWidget::item:selected {{ background: {C['green_dim']}; }}
            QListWidget::item:disabled {{ color: #999; }}
            QPushButton {{ color: {C['text']}; background: {C['bg3']}; border-radius: 6px; padding: 8px 12px; }}
            QPushButton:disabled {{ color: #888; }}
            QPushButton#follow:enabled {{ background: {C['green']}; color: black; }}
        """)
        layout = QVBoxLayout(self)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.items = QListWidget()
        self.items.setAccessibleName("Playback devices")
        layout.addWidget(self.items)
        actions = QHBoxLayout()
        self.refresh = QPushButton("Refresh")
        cancel = QPushButton("Cancel")
        self.follow = QPushButton("Start following")
        self.follow.setObjectName("follow")
        self.follow.setDefault(True)
        self.refresh.clicked.connect(self._refresh)
        cancel.clicked.connect(self.reject)
        self.follow.clicked.connect(self.accept)
        actions.addWidget(self.refresh)
        actions.addStretch()
        actions.addWidget(cancel)
        actions.addWidget(self.follow)
        layout.addLayout(actions)

    def set_devices(self, devices, message):
        self.items.clear()
        ready = devices is not None
        self.refresh.setEnabled(ready)
        self.follow.setEnabled(ready)
        self.status.setText(message or "Choose where Spotify should play this session.")
        if not ready:
            return
        self.items.addItem("Use the active Spotify device")
        for device in devices:
            available = device.id and not device.restricted
            state = "unavailable" if not available else ("active" if device.active else "")
            item = QListWidgetItem(" · ".join(filter(None, (device.name, device.type, state))))
            item.setData(Qt.ItemDataRole.UserRole, device.id)
            if not available:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled & ~Qt.ItemFlag.ItemIsSelectable)
            self.items.addItem(item)
        self.items.setCurrentRow(0)

    def device_id(self):
        return self.items.currentItem().data(Qt.ItemDataRole.UserRole)

    def _refresh(self):
        self.set_devices(None, "Checking Spotify for devices…")
        self.refresh_requested.emit()


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
        w1, self.lfm_user = field_row("default username (optional)")
        w2, self.lfm_key  = field_row("api key", pwd=True)
        lay.addWidget(w1)
        lay.addSpacing(8)
        lay.addWidget(w2)
        lay.addSpacing(20)

        section("SPOTIFY")
        w3, self.sp_id  = field_row("client id for playback",     pwd=True)
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
        for f in (self.lfm_key,):
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
        return bool(self.lfm_key.text().strip())


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
