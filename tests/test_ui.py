"""Native Qt smoke and lifecycle tests. No live accounts, network or user settings."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

try:
    from PyQt6.QtCore import QTimer
    from PyQt6.QtGui import QPixmap
    from PyQt6.QtWidgets import QApplication, QLineEdit
    import lastsync
except ImportError:
    lastsync = None

from lastsync_core.models import Candidate, Playback, Ranked, Track


@unittest.skipIf(lastsync is None, "PyQt6 is required for UI tests")
class NativeUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.window = lastsync.LastSyncWindow({"lastfm_user": "friend", "lastfm_api_key": "test",
                                              "spotify_client_id": "test"}, prompt_settings=False, friends_path=":memory:", monitor_factory=None)

    def tearDown(self):
        self.window._worker = self.window._auth_worker = None
        self.window._friends_store.close()
        self.window._closing = True
        self.window._tray.hide()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def test_native_window_renders_preserved_layout(self):
        self.window.show()
        self.app.processEvents()
        pixmap = self.window.grab()
        self.assertFalse(pixmap.isNull())
        self.assertEqual(self.window.windowTitle(), "LastSync 2.0")
        self.assertGreaterEqual(self.window.width(), 440)

    def test_settings_masks_api_key_and_needs_no_client_secret(self):
        dialog = lastsync.SettingsDialog(self.window._cfg)
        self.assertEqual(dialog.lfm_key.echoMode(), QLineEdit.EchoMode.Password)
        self.assertTrue(dialog.is_valid())
        self.assertNotIn("spotify_client_secret", dialog.get_config())
        dialog.deleteLater()

    def test_stop_is_nonblocking_and_retains_running_worker(self):
        class BusyWorker:
            stopped = False
            def stop(self):
                self.stopped = True
            def wait(self):
                raise AssertionError("UI must not wait synchronously for network workers")
        worker = BusyWorker()
        self.window._syncing, self.window._worker = True, worker
        started = time.monotonic()
        self.window.stop_sync()
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertTrue(worker.stopped)
        self.assertIs(self.window._worker, worker)
        self.assertFalse(self.window.sync_btn.isEnabled())

    def test_late_track_signal_after_stop_is_ignored(self):
        self.window.stop_sync()
        self.window._on_track("late", "artist", 0, 1000, "")
        self.assertEqual(self.window.track_label.text(), "Start sync to listen along")

    def test_actual_spotify_progress_and_staleness(self):
        self.window._syncing, self.window._state = True, "FOLLOWING"
        self.window._on_playback(Playback("x", 30000, 180000, False, "d", time.monotonic()))
        self.assertEqual(self.window.elapsed_lbl.text(), "0:30")
        self.window._on_playback(Playback("x", 30000, 180000, True, "d", time.monotonic() - 20))
        self.assertEqual(self.window.elapsed_lbl.text(), "refreshing")

    def test_stale_artwork_does_not_replace_current_track(self):
        self.window._syncing = True
        self.window._art_generation = 3
        self.window._on_art(b"invalid", 2)
        self.assertIsNone(self.window.art_widget._px)

    def test_manual_match_dialog_uses_explicit_selection(self):
        candidate = Candidate("spotify:track:" + "a" * 22, Track("Song", "Artist"))
        dialog = lastsync.MatchDialog(candidate.track, (Ranked(candidate, 0.85, "Ambiguous"),))
        self.assertEqual(dialog.uri(), "")
        dialog.items.setCurrentRow(0)
        self.assertEqual(dialog.uri(), candidate.uri)
        dialog.custom.setText("https://open.spotify.com/track/" + "b" * 22)
        self.assertIn("bbbb", dialog.uri())
        dialog.deleteLater()

    def test_remote_metadata_is_plain_text(self):
        from PyQt6.QtCore import Qt
        self.assertEqual(self.window.track_label.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(self.window.artist_label.textFormat(), Qt.TextFormat.PlainText)

    def test_real_qthread_worker_finishes_after_cooperative_stop(self):
        from PyQt6.QtTest import QSignalSpy
        from lastsync_core import workers
        from lastsync_core.models import Observation, SyncState
        from lastsync_core.storage import MatchStore
        source = Track("A Song", "An Artist")
        class LastFM:
            def recent(self, username):
                return Observation(source, (), 100)
            def close(self):
                pass
        class Spotify:
            starts = 0
            def search(self, track):
                return [Candidate("spotify:track:" + "a" * 22,
                                  Track("A Song", "An Artist", duration_ms=100000))]
            def active_device(self):
                return "device"
            def start(self, uri, device):
                self.starts += 1
            def playback(self):
                return Playback("spotify:track:" + "a" * 22, 1000, 100000, True, "device", time.monotonic())
        spotify = Spotify()
        worker = workers.SyncWorker(spotify, self.window._cfg)
        states = []
        def receive(state, message):
            states.append(state)
            if state == "FOLLOWING":
                worker.stop()
        worker.state_changed.connect(receive)
        with patch.object(workers, "LastFMClient", lambda *a: LastFM()), \
             patch.object(workers, "MatchStore", lambda *a: MatchStore(":memory:")):
            spy = QSignalSpy(worker.finished)
            worker.start()
            completed = spy.wait(5000)
            if not completed:
                worker.stop()
                spy.wait(2000)
            self.assertTrue(completed)
        self.assertIn("FOLLOWING", states)
        self.assertEqual(spotify.starts, 1)
        self.assertFalse(worker.isRunning())
        worker.deleteLater()

    def test_revoked_cached_grant_returns_to_browser_authorization(self):
        from lastsync_core import workers
        from lastsync_core.errors import ServiceError
        class Auth:
            token = {"access_token": "revoked"}
            connections = 0
            def connect(self, cancelled):
                self.connections += 1
            def close(self):
                pass
        auth = Auth()
        class Spotify:
            calls = 0
            def request(self, *args):
                self.calls += 1
                if self.calls == 1:
                    raise ServiceError("Spotify", "revoked", status=401, fatal=True)
                return {"id": "user"}
            def close(self):
                pass
        spotify = Spotify()
        worker = workers.AuthWorker(self.window._cfg)
        successes = []
        worker.auth_success.connect(successes.append)
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(workers, "SpotifyAuth", lambda *a: auth), \
             patch.object(workers, "SpotifyClient", lambda *a: spotify), \
             patch.object(workers, "CONFIG_FILE", Path(folder) / "config.json"):
            worker.run()
        self.assertEqual(auth.connections, 2)
        self.assertIsNone(auth.token)
        self.assertEqual(successes, [spotify])
        worker.deleteLater()

    def test_device_picker_refresh_then_play_on_selected_output(self):
        import sqlite3
        from unittest.mock import Mock
        from PyQt6.QtCore import QThread, Qt
        from PyQt6.QtTest import QTest
        from lastsync_core import workers
        from lastsync_core.models import Device, Observation

        source = Track("Song", "Artist")
        candidate = Candidate("spotify:track:" + "s" * 22, source)
        fetch_threads = []
        spotify = Mock(preferred_device=None)
        spotify.request.return_value = {"id": "test-account"}
        def devices():
            fetch_threads.append(QThread.currentThread())
            if len(fetch_threads) == 1:
                return []
            return [Device("locked", "TV", "TV", True, True),
                    Device("desk", "Office", "Computer", False, False)]
        spotify.devices.side_effect = devices
        spotify.search.return_value = [candidate]
        spotify.active_device.side_effect = lambda: spotify.preferred_device
        spotify.playback.return_value = Playback(candidate.uri, 0, 10000, True, "desk", 0)
        lastfm = Mock()
        lastfm.recent.return_value = Observation(source, (), 100)

        def wait_for(predicate):
            deadline = time.monotonic() + 5
            while not predicate() and time.monotonic() < deadline:
                QTest.qWait(10)
            self.assertTrue(predicate())

        with tempfile.TemporaryDirectory() as folder, \
             patch.object(workers, "SpotifyClient", return_value=spotify), \
             patch.object(workers, "SpotifyAuth"), \
             patch.object(workers, "LastFMClient", return_value=lastfm), \
             patch.object(workers, "data_directory", return_value=Path(folder)), \
             patch.object(workers, "CONFIG_FILE", Path(folder) / "config"):
            self.window.start_sync()
            try:
                wait_for(lambda: self.window._device_dialog is not None)
                dialog = self.window._device_dialog
                self.assertEqual(dialog.items.count(), 1)
                dialog.refresh.click()
                wait_for(lambda: dialog.items.count() == 3)
                self.assertFalse(dialog.items.item(1).flags() & Qt.ItemFlag.ItemIsEnabled)
                self.assertTrue(all(t != self.app.thread() for t in fetch_threads))
                spotify.start.assert_not_called()
                dialog.items.setCurrentRow(2)
                dialog.follow.click()
                wait_for(lambda: self.window._state == "FOLLOWING")
                spotify.start.assert_called_once_with(candidate.uri, "desk")
            finally:
                self.window.stop_sync()
                wait_for(lambda: self.window._auth_worker is None and self.window._worker is None)
            with sqlite3.connect(Path(folder) / "matches.sqlite3") as db:
                self.assertEqual(db.execute("SELECT device_mode,confirmed_starts,outcome FROM sessions").fetchone(),
                                 ("selected", 1, "stopped"))
            db.close()

    def test_cancel_during_device_rate_limit_wakes_worker(self):
        from unittest.mock import Mock
        from PyQt6.QtTest import QSignalSpy
        from lastsync_core import workers
        from lastsync_core.errors import ServiceError
        worker = workers.AuthWorker(self.window._cfg, choose_output=True)
        spotify = Mock()
        spotify.request.return_value = {"id": "id"}
        spotify.devices.side_effect = ServiceError("Spotify", "rate limited", retry_after=60)
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(workers, "SpotifyAuth"), patch.object(workers, "SpotifyClient", return_value=spotify), \
             patch.object(workers, "CONFIG_FILE", Path(folder) / "config"):
            ready = QSignalSpy(worker.device_options)
            done = QSignalSpy(worker.finished)
            success = QSignalSpy(worker.auth_success)
            worker.start()
            try:
                self.assertTrue(ready.wait(3000))
                self.assertIsNone(ready[0][0])
            finally:
                worker.stop()
                self.assertTrue(done.wait(2000) or len(done))
            self.assertFalse(len(success))
            spotify.close.assert_called_once()
        worker.deleteLater()


if __name__ == "__main__":
    unittest.main()
