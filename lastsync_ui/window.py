import sys
import sqlite3
import time
import os
from pathlib import Path
from PyQt6.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame, QDialog, QMessageBox, QSystemTrayIcon, QMenu, QTabWidget
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QColor, QPainter, QBrush, QIcon, QPixmap, QPen, QFontDatabase
from lastsync_core.config import DEFAULT_CONFIG as _DEFAULT_CONFIG, load_config, save_config, data_directory
from lastsync_core.friends import FriendMonitor, TRACK_FOLLOW, DISCOVERY
from lastsync_core.storage import MatchStore
from .friends import FriendsPanel
from lastsync_core.workers import SyncWorker, AuthWorker, ArtLoader
from lastsync_core.sync import POLL_ACTIVE, POLL_IDLE
from .activity import LogPanel
from .widgets import PulseDot, ArtWidget, ProgressBar
from .dialogs import DeviceDialog, MatchDialog, SettingsDialog
from .theme import C


class LastSyncWindow(QMainWindow):
    BTN_R = 25

    def __init__(self, cfg=None, prompt_settings=True, *, friends_path=None, monitor_factory=FriendMonitor):
        super().__init__()
        config_error = None
        try:
            self._cfg = dict(cfg) if cfg is not None else load_config()
        except Exception as error:
            self._cfg = dict(_DEFAULT_CONFIG)
            config_error = str(error)
        self._sp = self._worker = self._auth_worker = None
        self._device_dialog = None
        self._monitor_factory, self._monitor = monitor_factory, None
        self._retired_monitors = set()
        self._next_session = None
        self._session_user = self._cfg.get("lastfm_user", "")
        self._mode, self._picked, self._hint = TRACK_FOLLOW, None, None
        self._friends_store = None
        try:
            self._friends_store = MatchStore(friends_path or data_directory() / "matches.sqlite3")
            self._friends_store.seed_friends(self._session_user)
        except (OSError, RuntimeError, sqlite3.Error):
            config_error = "Could not open the local Friends database."
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
        self._configure_monitor()
        if config_error:
            self.set_status(config_error)
            self.friends.notify(config_error)
            self.log_panel.add_entry(config_error, "error")
        elif prompt_settings and not self._cfg.get("lastfm_api_key"):
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
        self.setWindowTitle("LastSync 2.0")
        self.setMinimumSize(440, 680)
        self.resize(600, 760)
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
        self.pages = QTabWidget()
        self.pages.setStyleSheet("QTabWidget::pane { border: 0; background: #101411; } "
                                 "QTabBar::tab { padding: 9px 22px; color: #a3aea7; background: #171b18; } "
                                 "QTabBar::tab:selected { color: white; border-bottom: 2px solid #1db954; }")
        self.setCentralWidget(self.pages)
        self.friends = FriendsPanel(self._friends_store, self)
        self.pages.addTab(self.friends, "Friends")
        self.pages.addTab(central, "Player")
        self.friends.roster_changed.connect(self._watch)
        self.friends.play_requested.connect(self._listen)
        self.friends.stop_requested.connect(self.stop_sync)
        self.friends.settings_requested.connect(self._open_settings)
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
        user = self._session_user or "—"
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
        return all((self._session_user, self._cfg.get("spotify_client_id"), self._cfg.get("lastfm_api_key")))

    def _prompt_settings(self):
        QMessageBox.information(self, "Welcome to LastSync",
                                "Enter a Last.fm API key to browse Friends. Spotify playback also needs a Client ID.")
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
            self.friends.matches.clear()
        key_changed = new["lastfm_api_key"] != self._cfg.get("lastfm_api_key")
        if new["lastfm_user"] != self._cfg.get("lastfm_user"):
            self._session_user = new["lastfm_user"]
        self._cfg = new
        if self._friends_store and new["lastfm_user"]:
            self._friends_store.seed_friends(new["lastfm_user"])
            self.friends.reload()
        if key_changed:
            self._configure_monitor()
        else:
            self._watch()
        self._refresh_labels()

    def _refresh_labels(self):
        self.sub_label.setText(f"with  {self._session_user or '—'}")
        self.footer_label.setText(self._footer_text())

    def _configure_monitor(self):
        if self._monitor:
            old, self._monitor = self._monitor, None
            self._retired_monitors.add(old)
            old.stop()
        key = self._cfg.get("lastfm_api_key")
        if key and self._monitor_factory:
            self._monitor = self._monitor_factory(key)
            self._monitor.updated.connect(self._friend_updated)
            self._monitor.finished.connect(self._monitor_finished)
            self._watch()
            self._monitor.start()
        self.friends.notify("" if key else "Open Settings to connect Last.fm.")

    def _monitor_finished(self):
        worker = self.sender()
        self._retired_monitors.discard(worker)
        if worker is self._monitor:
            self._monitor = None
        worker.deleteLater()

    def _watch(self):
        if self._monitor:
            focus = self._session_user if self._syncing and self._mode != DISCOVERY else ""
            self._monitor.watch(self.friends.names(), focus)

    def _friend_updated(self, name):
        if self._monitor is self.sender() and not self._closing:
            self.friends.update_activity(name, self._monitor.activity(name))

    def _listen(self, username, mode, picked, hint):
        request = (username, mode, picked, hint)
        if self._worker or self._auth_worker:
            self.stop_sync()
            self._next_session = request
            self.set_status(f"Switching to {username}")
            return
        self._start_request(request)

    def _start_request(self, request):
        self._session_user, self._mode, self._picked, self._hint = request
        self._refresh_labels()
        self.pages.setCurrentIndex(1)
        self.start_sync()

    def _resume_switch(self):
        if self._next_session and not (self._worker or self._auth_worker or self._closing):
            request, self._next_session = self._next_session, None
            self._start_request(request)

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
        if not self._session_user:
            self.pages.setCurrentIndex(0)
            self.friends.notify("Choose a friend to start listening.")
            return
        if not self._config_is_complete():
            self._prompt_settings()
            return
        self._syncing = True
        self.friends.stop_button.setEnabled(True)
        self._watch()
        self.sync_btn.setText("CANCEL CONNECT")
        self.sync_btn.setEnabled(True)
        self._btn_stop()
        self.pulse.set_active(True)
        self.set_status("Connecting to Spotify")
        self._clear_card("Waiting to follow a track")
        if self._sp is not None:
            self._on_auth_ok(self._sp)
            return
        worker = AuthWorker(self._cfg, choose_output=True)
        self._auth_worker = worker
        worker.device_options.connect(self._show_devices)
        worker.auth_success.connect(self._on_auth_ok)
        worker.auth_failed.connect(self._on_auth_fail)
        worker.finished.connect(self._auth_finished)
        worker.start()

    def _show_devices(self, devices, message):
        if not self._syncing or self._closing or not self._auth_worker:
            return
        if self._device_dialog is None:
            dialog = self._device_dialog = DeviceDialog(self)
            worker = self._auth_worker
            dialog.refresh_requested.connect(lambda: worker.device_command("refresh"))
            dialog.accepted.connect(lambda: worker.device_command("select", dialog.device_id()))
            dialog.rejected.connect(self.stop_sync)
            dialog.open()
        self.set_status("Choose a Spotify playback device")
        self._device_dialog.set_devices(devices, message)

    def _dismiss_devices(self):
        if self._device_dialog is not None:
            dialog, self._device_dialog = self._device_dialog, None
            dialog.hide()
            dialog.deleteLater()

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
        self.log_panel.add_entry(f"{self._mode} · {self._session_user}", "info")
        self.live_badge.setText(self._mode.upper())
        worker = SyncWorker(self._sp, {**self._cfg, "lastfm_user": self._session_user}, monitor=self._monitor,
                            mode=self._mode, picked=self._picked, hint=self._hint)
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
        self._dismiss_devices()
        worker = self._auth_worker
        self._auth_worker = None
        if worker:
            worker.deleteLater()
        if not self._syncing:
            self._reset_button()
        self._resume_switch()

    def _sync_finished(self):
        worker = self._worker
        self._worker = None
        self._syncing = False
        self._tick_timer.stop()
        self._progress_snapshot = None
        if not self._next_session:
            self._close_spotify()
        self.live_badge.hide()
        self.match_btn.hide()
        if worker:
            worker.deleteLater()
        self._reset_button()
        self._resume_switch()

    def _reset_button(self):
        self.friends.stop_button.setEnabled(False)
        self._watch()
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
        self._next_session = None
        self._dismiss_devices()
        self._syncing = False
        self.friends.stop_button.setEnabled(False)
        self._watch()
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
        self.elapsed_lbl.setToolTip("Position reported by your Spotify player")
        self._tick_progress()

    def _on_state(self, state, message):
        if not self._syncing:
            return
        self._state = state
        if self._match_request and state in ("FOLLOWING", "RESOLVING", "STARTING", "RECOVERING"):
            status = "Confirmed on your Spotify device" if state == "FOLLOWING" else message
            self.friends.set_match(self._session_user, self._match_request[1], status)
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
        worker = self._worker
        dialog = MatchDialog(source, candidates, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and self._worker is worker and self._syncing:
            worker.choose_match(event_id, dialog.uri())

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
        self.friends.shutdown()
        if self._monitor:
            self._monitor.stop()
        self.stop_sync()
        self.set_status("Closing background services")
        self._finish_shutdown()

    def _finish_shutdown(self):
        workers = [self._worker, self._auth_worker, self._monitor, *self._retired_monitors,
                   *self._art_loaders, *self.friends.loaders.values()]
        if any(worker and worker.isRunning() for worker in workers):
            QTimer.singleShot(100, self._finish_shutdown)
            return
        self._close_spotify()
        if self._friends_store:
            self._friends_store.close()
            self._friends_store = None
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
