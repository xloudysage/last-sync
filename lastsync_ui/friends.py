"""Friends roster, Last.fm activity detail, and explicit listening choices."""
from collections import OrderedDict
from datetime import datetime
import sqlite3
import time

from PyQt6.QtCore import QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import (
    QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QVBoxLayout, QWidget,
)

from lastsync_core.friends import BEST_EFFORT, DISCOVERY, TRACK_FOLLOW, FriendActivity, PositionHint
from lastsync_core.workers import ArtLoader


def timestamp(value):
    try:
        return datetime.fromtimestamp(value).astimezone().strftime("%d %b, %H:%M:%S")
    except (ValueError, OverflowError, OSError):
        return "unknown time"


def label(text=""):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    return widget


class FriendsPanel(QWidget):
    roster_changed = pyqtSignal()
    play_requested = pyqtSignal(str, str, object, object)
    stop_requested = pyqtSignal()
    settings_requested = pyqtSignal()

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self.samples, self.matches, self.rows = {}, {}, {}
        self.image_cache = OrderedDict()
        self.loaders = {}
        self.wanted = {}
        self.closed = False
        self.stale_names = set()
        self.hint_identity = None
        self.setStyleSheet("""
            QWidget { color: #ecefed; font-family: 'Segoe UI'; font-size: 13px; }
            QListWidget, QLineEdit, QComboBox { background: #171b19; border: 1px solid #39423d;
                border-radius: 6px; padding: 5px; }
            QListWidget::item { padding: 5px; }
            QListWidget::item:selected { background: #244631; color: white; }
            QPushButton { background: #232925; border: 1px solid #38433c; padding: 5px 10px; border-radius: 6px; }
            QPushButton:hover { background: #303c34; }
            QPushButton:disabled { color: #828982; }
            QPushButton#listen:enabled { background: #1db954; color: #07150b; font-weight: 600; }
            QLabel#section { color: #91a498; font-size: 12px; }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 12, 18, 14)
        header = QHBoxLayout()
        title = label("Friends")
        title.setStyleSheet("font-size: 24px; font-weight: 600;")
        header.addWidget(title, 1)
        settings = QPushButton("Settings")
        settings.clicked.connect(self.settings_requested)
        header.addWidget(settings)
        layout.addLayout(header)
        tools = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setPlaceholderText("Search friends or add a Last.fm username")
        self.query.setClearButtonEnabled(True)
        self.query.textChanged.connect(self._filter)
        self.query.returnPressed.connect(self.add)
        add = QPushButton("Add")
        add.clicked.connect(self.add)
        tools.addWidget(self.query, 1)
        tools.addWidget(add)
        layout.addLayout(tools)
        self.notice = label()
        self.notice.hide()
        layout.addWidget(self.notice)
        self.roster = QListWidget()
        self.roster.setAccessibleName("Followed Last.fm profiles")
        self.roster.setMinimumHeight(90)
        self.roster.setIconSize(QSize(34, 34))
        self.roster.currentItemChanged.connect(self._detail)
        self.roster.verticalScrollBar().valueChanged.connect(self._images)
        layout.addWidget(self.roster, 2)
        order = QHBoxLayout()
        self.up, self.down, self.remove = [QPushButton(t) for t in ("Move up", "Move down", "Remove")]
        self.up.clicked.connect(lambda: self.edit("up"))
        self.down.clicked.connect(lambda: self.edit("down"))
        self.remove.clicked.connect(lambda: self.edit("remove"))
        for b in (self.up, self.down, self.remove):
            order.addWidget(b)
        order.addStretch()
        layout.addLayout(order)
        identity = QHBoxLayout()
        self.avatar, self.cover = label(), label()
        for image, size in ((self.avatar, 40), (self.cover, 58)):
            image.setFixedSize(size, size)
            image.setAlignment(Qt.AlignmentFlag.AlignCenter)
            image.setStyleSheet("background: #202924; border-radius: 6px; color: #b9c9bc;")
        identity.addWidget(self.avatar)
        info = QVBoxLayout()
        self.name = label()
        self.name.setStyleSheet("font-size: 17px; font-weight: 600;")
        self.listen_state = label()
        info.addWidget(self.name)
        info.addWidget(self.listen_state)
        identity.addLayout(info, 1)
        identity.addWidget(self.cover)
        layout.addLayout(identity)
        self.track, self.when, self.match = label(), label(), label()
        for w in (self.track, self.when, self.match):
            layout.addWidget(w)
        self.mode = QComboBox()
        self.mode.addItems([TRACK_FOLLOW, BEST_EFFORT, DISCOVERY])
        self.mode.currentTextChanged.connect(self._detail)
        form = QFormLayout()
        form.addRow("Listening mode", self.mode)
        layout.addLayout(form)
        self.position = QLineEdit()
        self.position.setPlaceholderText("Optional verified source position, e.g. 1:24")
        self.position_time = None
        self.position.textChanged.connect(self._position_changed)
        layout.addWidget(self.position)
        self.history = QListWidget()
        self.history.setAccessibleName("Choose a reported track to play")
        self.history.currentRowChanged.connect(self._enable_play)
        layout.addWidget(self.history, 1)
        buttons = QHBoxLayout()
        self.play = QPushButton("Listen along")
        self.play.setObjectName("listen")
        self.play.clicked.connect(self._play)
        self.stop_button = QPushButton("Stop session")
        self.stop_button.clicked.connect(self.stop_requested)
        self.stop_button.setEnabled(False)
        buttons.addWidget(self.play, 1)
        buttons.addWidget(self.stop_button)
        layout.addLayout(buttons)
        self.reload()
        self.age_timer = QTimer(self)
        self.age_timer.setInterval(5000)
        self.age_timer.timeout.connect(self._refresh_age)
        self.age_timer.start()

    def _refresh_age(self):
        now = time.monotonic()
        stale = {n for n in self.rows if self.samples.get(n.casefold(), FriendActivity()).stale(now)}
        for name in (stale ^ self.stale_names) & self.rows.keys():
            self._row(name)
            if name == self.selected():
                self._detail()
        self.stale_names = stale

    def names(self):
        return list(self.rows)

    def selected(self):
        item = self.roster.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item and not item.isHidden() else ""

    def notify(self, msg):
        self.notice.setText(msg)
        self.notice.setVisible(bool(msg))

    def _position_changed(self, value):
        self.position_time = time.monotonic() if value.strip() else None

    def reload(self, select=None):
        previous = select or self.selected()
        self.roster.blockSignals(True)
        self.rows.clear()
        self.roster.clear()
        for name in self.store.friends() if self.store else []:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.roster.addItem(item)
            self.rows[name] = item
            self._row(name)
        self.roster.blockSignals(False)
        if previous in self.rows:
            self.roster.setCurrentItem(self.rows[previous])
        self._filter()

    def _filter(self):
        query = self.query.text().strip().casefold()
        for username, item in self.rows.items():
            item.setHidden(query not in username.casefold())
        if not self.selected():
            self.roster.setCurrentItem(next((i for i in self.rows.values() if not i.isHidden()), None))
        self._detail()

    def add(self):
        name = self.query.text().strip()
        if not self.store:
            self.notify("Local friend storage is unavailable.")
            return
        try:
            self.store.add_friend(name)
        except (ValueError, sqlite3.Error) as exc:
            self.notify(str(exc) if isinstance(exc, ValueError) else "Could not save this friend.")
            return
        self.query.clear()
        self.notify("")
        self.reload(next((n for n in self.store.friends() if n.casefold() == name.casefold()), name))
        self.roster_changed.emit()

    def edit(self, action):
        name = self.selected()
        if not name or not self.store:
            return
        try:
            if action == "remove":
                self.store.remove_friend(name)
                self.samples.pop(name.casefold(), None)
                self.matches.pop(name.casefold(), None)
            else:
                self.store.move_friend(name, -1 if action == "up" else 1)
        except sqlite3.Error:
            self.notify("Could not update the friend list.")
            return
        self.reload(name)
        self.roster_changed.emit()

    def _row(self, name):
        data = self.samples.get(name.casefold(), FriendActivity())
        obs = data.observation
        track = data.last_track
        status = "Unavailable" if data.error else ("Reported now playing" if obs and obs.current else
                 ("Last heard" if track else ("Idle" if obs else "Waiting")))
        if not data.error and data.stale(time.monotonic()):
            status = "Stale report"
        item = self.rows[name]
        if not data.avatar and not item.icon().isNull():
            item.setIcon(QIcon())
        item.setText(f"{name}   ·   {status}" + (f"\n{track.title} — {track.artist}" if track else ""))
        item.setToolTip(item.text())

    def update_activity(self, key, sample):
        name = next((n for n in self.rows if n.casefold() == key), None)
        if name is None:
            return
        self.samples[key] = sample
        self._row(name)
        if self.selected() == name:
            self._detail()
        else:
            self._images()

    def set_match(self, name, track, status):
        self.matches[name.casefold()] = (track, status)
        if self.selected().casefold() == name.casefold():
            self._detail()

    def _detail(self, *_):
        name = self.selected()
        sample = self.samples.get(name.casefold(), FriendActivity())
        obs = sample.observation
        current = obs.current if obs else None
        recent = obs.history if obs else ()
        track = sample.last_track
        mode = self.mode.currentText()
        identity = (name, sample.event)
        if self.hint_identity != identity:
            self.position.clear()
            self.hint_identity = identity
        self.position.setVisible(bool(name) and mode == BEST_EFFORT)
        self.history.setVisible(bool(name) and mode == DISCOVERY)
        self.name.setText(name or ("No matching friends" if self.rows else "Add your first Last.fm profile"))
        self.listen_state.setText(sample.error or ("Last.fm reports now playing" if current else
                                  "No current track reported" if obs else "Waiting for Last.fm" if name else ""))
        if not sample.error and sample.stale(time.monotonic()):
            self.listen_state.setText("Last.fm report is stale")
        self.track.setText(" · ".join(filter(None, [track.title, track.artist, track.album])) if track else "")
        times = []
        if obs:
            times.append("Last checked " + timestamp(obs.observed_at))
        if recent:
            times.append("Last scrobble " + timestamp(recent[0].timestamp))
        if track and not current and (not recent or recent[0].track != track):
            times.append("Last observed " + timestamp(sample.last_activity))
        self.when.setText("   ·   ".join(times))
        prev, match = self.matches.get(name.casefold(), (None, ""))
        self.match.setText(("Spotify · " + (match if prev == track and match else "Not matched yet")) if track else "")
        choices = ([(current, None)] if current else []) + [(s.track, s.timestamp) for s in recent]
        if track and not any(t == track for t, _ in choices):
            choices.insert(0, (track, sample.last_activity))
        old = self.history.currentItem()
        selected = old.data(Qt.ItemDataRole.UserRole) if old else None
        self.history.blockSignals(True)
        self.history.clear()
        for item_track, stamp in choices:
            item = QListWidgetItem(f"{item_track.title} — {item_track.artist}  ·  " +
                                   (timestamp(stamp) if stamp is not None else "reported now playing"))
            item.setData(Qt.ItemDataRole.UserRole, (item_track, stamp))
            self.history.addItem(item)
            if selected == (item_track, stamp):
                self.history.setCurrentItem(item)
        if choices and self.history.currentRow() < 0:
            self.history.setCurrentRow(0)
        self.history.blockSignals(False)
        self.play.setText("Play selected track" if mode == DISCOVERY else "Listen along")
        self._enable_play()
        names = self.names()
        self.up.setEnabled(bool(name) and name != names[0])
        self.down.setEnabled(bool(name) and name != names[-1])
        self.remove.setEnabled(bool(name))
        self.avatar.setText(name[:2].upper())
        self.cover.setText("♪" if track else "")
        self.wanted = {self.avatar: sample.avatar, self.cover: track.artwork if track else ""}
        self._images()

    def _enable_play(self, *_):
        self.play.setEnabled(bool(self.selected()) and
                             (self.mode.currentText() != DISCOVERY or self.history.currentItem() is not None))

    def _play(self):
        name, mode = self.selected(), self.mode.currentText()
        if not name:
            return
        sample = self.samples.get(name.casefold(), FriendActivity())
        hint = picked = None
        if mode == BEST_EFFORT and self.position.text().strip():
            parts = self.position.text().strip().split(":")
            if (len(parts) != 2 or any(not p.isascii() or not p.isdigit() for p in parts)
                    or len(parts[0]) > 4 or len(parts[1]) != 2 or int(parts[1]) >= 60):
                self.notify("Enter the source player's current position as minutes:seconds.")
                return
            if not sample.observation or not sample.observation.current or sample.error or time.monotonic()-sample.received > 8:
                self.notify("Wait for a fresh now-playing report, or clear the position to follow from zero.")
                return
            hint = PositionHint(sample.observation.current, sample.event,
                                (int(parts[0])*60 + int(parts[1]))*1000, self.position_time)
            self.position.clear()
        if mode == DISCOVERY:
            item = self.history.currentItem()
            if not item:
                return
            picked = item.data(Qt.ItemDataRole.UserRole)[0]
        self.notify("")
        self.play_requested.emit(name, mode, picked, hint)

    def _images(self, *_):
        if self.closed:
            return
        visible = self.roster.viewport().rect()
        targets = list(self.wanted.items())
        for name, item in self.rows.items():
            if not item.isHidden() and self.roster.visualItemRect(item).intersects(visible):
                sample = self.samples.get(name.casefold())
                if sample and sample.avatar:
                    targets.append((item, sample.avatar))
        for widget, url in targets:
            if not url:
                continue
            cached = self.image_cache.get(url)
            if cached and (cached[0] is not None or time.monotonic() < cached[1]):
                self.image_cache.move_to_end(url)
                if cached[0] is not None:
                    if isinstance(widget, QListWidgetItem):
                        widget.setIcon(QIcon(cached[0]))
                    else:
                        widget.setPixmap(cached[0].scaled(widget.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                                          Qt.TransformationMode.SmoothTransformation))
            elif url not in self.loaders and len(self.loaders) < 2:
                worker = ArtLoader(url, 0)
                self.loaders[url] = worker
                worker.art_ready.connect(lambda raw, _, key=url: self._image_ready(key, raw))
                worker.finished.connect(lambda key=url: self._image_done(key))
                worker.start()

    def _image_ready(self, key, raw):
        px = QPixmap()
        if not self.closed and px.loadFromData(raw):
            self.image_cache[key] = (px.scaled(96, 96, Qt.AspectRatioMode.KeepAspectRatio,
                                               Qt.TransformationMode.SmoothTransformation), 0)

    def _image_done(self, key):
        self.loaders.pop(key).deleteLater()
        if not self.image_cache.get(key, (None,))[0]:
            self.image_cache[key] = (None, time.monotonic() + 300)
        while len(self.image_cache) > 96:
            self.image_cache.popitem(last=False)
        self._images()

    def shutdown(self):
        self.closed = True
        self.age_timer.stop()
        for worker in self.loaders.values():
            worker.stop()
