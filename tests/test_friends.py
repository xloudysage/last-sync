import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import replace
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from lastsync_core.errors import ServiceError
from lastsync_core.friends import BEST_EFFORT, DISCOVERY, TRACK_FOLLOW, FriendActivity, FriendMonitor, PositionHint
from lastsync_core.lastfm import LastFMClient, parse_recent
from lastsync_core.models import Candidate, Observation, Playback, Scrobble, Track
from lastsync_core.storage import MatchStore
from lastsync_core.sync import SyncEngine
from lastsync_core.workers import SyncWorker
from lastsync_ui.friends import FriendsPanel
from lastsync_ui.window import LastSyncWindow


A, B = Track("First", "Artist", "One"), Track("Second", "Artist", "Two")
URI = "spotify:track:" + "a" * 22
MATCH = Candidate(URI, Track("First", "Artist", "One", 180000))


class FriendsCoreTests(unittest.TestCase):
    def test_schema_two_upgrades_preserving_sessions_and_corrections(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "matches.sqlite3"
            store = MatchStore(path)
            from lastsync_core.models import Resolution
            store.put(A, Resolution(MATCH, (), 1, True))
            session = store.begin_session("Alice", None)
            store.end_session(session, "stopped")
            with store.db:
                store.db.execute("DROP TABLE friends")
                store.db.execute("DROP TABLE preferences")
                store.db.execute("PRAGMA user_version=2")
            store.close()
            current = MatchStore(path)
            try:
                self.assertEqual(current.db.execute("PRAGMA user_version").fetchone(), (3,))
                self.assertEqual(current.db.execute("SELECT outcome FROM sessions").fetchone(), ("stopped",))
                self.assertTrue(current.get(A).manual)
                current.seed_friends("Alice")
                current.remove_friend("Alice")
                current.seed_friends("Alice")
                self.assertEqual(current.friends(), [])
            finally:
                current.close()

    def test_profile_and_cover_metadata_from_http(self):
        url = "https://lastfm.freetls.fastly.net/i/u/64s/p.png"
        methods = []
        def handler(req):
            methods.append(req.url.params["method"])
            if methods[-1] == "user.getinfo":
                return httpx.Response(200, json={"user":{"image":[{"#text": url}]}})
            return httpx.Response(200, json={"recenttracks":{"track":[{"name":"First", "artist":"Artist",
                "album":"One", "image":[{"#text":url}], "@attr":{"nowplaying":"true"}}]}})
        client = LastFMClient("key", httpx.Client(transport=httpx.MockTransport(handler)))
        self.addCleanup(client.close)
        self.assertEqual(client.recent("alice").current.artwork, url)
        self.assertEqual(client.avatar("alice"), url)
        self.assertEqual(methods, ["user.getrecenttracks", "user.getinfo"])

    def test_empty_poll_preserves_last_known_track_but_no_live_status(self):
        sample = FriendActivity().observed(Observation(A, (), 100), 20)
        sample = sample.observed(Observation(None, (), 105), 25)
        self.assertIsNone(sample.observation.current)
        self.assertEqual((sample.last_track, sample.last_activity), (A, 100))

    def test_reliable_position_requires_same_fresh_event_and_valid_duration(self):
        state = FriendActivity().observed(Observation(A, (), 100), 20)
        hint = PositionHint(A, state.event, 42000, 20)
        self.assertEqual(hint.position(state, MATCH, 22), 44000)
        repeat = state.observed(Observation(A, (Scrobble(A, 102),), 104), 24)
        for invalid in (repeat, replace(state, error="offline"), replace(state, received=0)):
            self.assertIsNone(hint.position(invalid, MATCH, 24))
        self.assertIsNone(hint.position(replace(state, received=60), MATCH, 60))
        self.assertIsNone(replace(hint, position_ms=180000).position(state, MATCH, 22))

    def test_monitor_shares_reads_and_ignores_stale_samples(self):
        now = [10]
        monitor = FriendMonitor("key", clock=lambda: now[0])
        monitor.watch(["ALICE", "Bob"], "Alice")
        self.assertEqual(monitor._next()[0], ("alice","recent"))
        monitor.entries["alice"] = FriendActivity().observed(Observation(A, (), 100), now[0])
        self.assertIs(monitor.recent("Alice"), monitor.recent("ALICE"))
        now[0] = 19
        with self.assertRaises(ServiceError):
            monitor.recent("alice")
        monitor.watch(["Bob"])
        self.assertNotIn("alice", monitor.entries)

    def test_service_rate_limit_blocks_all_profiles_and_cancel_wakes_scheduler(self):
        calls = []
        attempted = threading.Event()
        client = Mock()
        def limited(name):
            calls.append(name)
            attempted.set()
            raise ServiceError("Last.fm", "rate limited", status=429, retry_after=60)
        client.recent.side_effect = limited
        monitor = FriendMonitor("key", client_factory=lambda _: client)
        monitor.watch(["a","b"], "a")
        monitor.start()
        try:
            self.assertTrue(attempted.wait(2))
            deadline = time.monotonic()+2
            while monitor.blocked_until < time.monotonic()+10 and time.monotonic() < deadline:
                time.sleep(.01)
            monitor.watch(["a","b"], "b")
            with self.assertRaises(ServiceError) as err:
                monitor.recent("b")
            self.assertGreater(err.exception.retry_after, 50)
        finally:
            monitor.stop()
            self.assertTrue(monitor.wait(1500))
        self.assertEqual(calls, ["a"])
        client.close.assert_called_once()

    def test_late_response_after_profile_removed_then_readded_is_discarded(self):
        waiting, release = threading.Event(), threading.Event()
        client = Mock()
        def fetch(name):
            waiting.set()
            release.wait(2)
            return Observation(A, (), 100)
        client.recent.side_effect = fetch
        monitor = FriendMonitor("key", client_factory=lambda _: client)
        monitor.watch(["a"])
        monitor.start()
        try:
            self.assertTrue(waiting.wait(2))
            monitor.watch([])
            monitor.watch(["a"])
            release.set()
            time.sleep(.04)
            self.assertIsNone(monitor.activity("a").observation)
        finally:
            monitor.stop()
            release.set()
            self.assertTrue(monitor.wait(1500))

    def test_engine_starts_at_explicit_position_only_once(self):
        source, spotify, resolver = Mock(), Mock(), Mock()
        source.recent.return_value = Observation(A, (), 100)
        from lastsync_core.models import Resolution
        resolver.resolve.return_value = Resolution(MATCH, (), 1)
        spotify.active_device.return_value = "desk"
        spotify.playback.return_value = Playback(URI, 45000, 180000, True, "desk", 0)
        engine = SyncEngine(source, spotify, resolver, "a", start_position=lambda _: 45000)
        for _ in range(4):
            engine.step()
        spotify.start.assert_called_once_with(URI, "desk", 45000)


class FriendsUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.store = MatchStore(":memory:")
        self.addCleanup(self.store.close)
        self.panel = FriendsPanel(self.store)
        self.addCleanup(self.panel.deleteLater)

    def test_add_duplicate_filter_reorder_remove(self):
        p = self.panel
        for name in ["Alice", "Bob", "alice"]:
            p.query.setText(name)
            p.add()
        self.assertEqual(p.names(), ["Alice","Bob"])
        p.edit("down")
        self.assertEqual(self.store.friends(), ["Bob","Alice"])
        p.query.setText("bo")
        self.assertEqual(p.selected(), "Bob")
        p.edit("remove")
        self.assertEqual(p.selected(), "")
        p.query.clear()
        self.assertEqual(p.selected(), "Alice")

    def test_discovery_only_plays_after_explicit_action_and_keeps_selected_row(self):
        p = self.panel
        p.query.setText("Alice")
        p.add()
        calls = []
        p.play_requested.connect(lambda *args: calls.append(args))
        sample = FriendActivity().observed(Observation(A,(Scrobble(B,90),),100),time.monotonic())
        p.update_activity("alice", sample)
        p.mode.setCurrentText(DISCOVERY)
        p.history.setCurrentRow(1)
        p.update_activity("alice", sample)
        self.assertEqual(calls, [])
        p.play.click()
        self.assertEqual(calls, [("Alice",DISCOVERY,B,None)])
        p.update_activity("alice", replace(sample,error="Offline"))
        self.assertEqual(p.listen_state.text(), "Offline")

    def test_best_effort_allows_position_or_fallback_and_rejects_invalid_input(self):
        p = self.panel
        p.query.setText("Alice")
        p.add()
        p.update_activity("alice", FriendActivity().observed(Observation(A,(),100),time.monotonic()))
        p.mode.setCurrentText(BEST_EFFORT)
        calls = []
        p.play_requested.connect(lambda *args: calls.append(args))
        p.position.setText("1:67")
        p.play.click()
        self.assertFalse(calls)
        p.position.setText("1:24")
        p.play.click()
        self.assertEqual(calls[-1][3].position_ms, 84000)
        p.play.click()
        self.assertIsNone(calls[-1][3])

    def test_discovery_worker_confirms_one_selected_track_without_lastfm_calls(self):
        from lastsync_core import workers
        sp = Mock(namespace="account", preferred_device="desk")
        sp.search.return_value = [MATCH]
        sp.active_device.return_value = "desk"
        sp.playback.return_value = Playback(URI, 0,180000,True,"desk",0)
        cfg = {"lastfm_user":"Alice","spotify_client_id":"test","lastfm_api_key":"test"}
        worker = SyncWorker(sp,cfg,mode=DISCOVERY,picked=A)
        with tempfile.TemporaryDirectory() as folder, patch.object(workers,"data_directory",return_value=Path(folder)), \
                patch.object(workers,"LastFMClient",side_effect=AssertionError("unexpected network request")):
            worker.run()
            sp.start.assert_called_once_with(URI,"desk")
            with sqlite3.connect(Path(folder)/"matches.sqlite3") as db:
                self.assertEqual(db.execute("SELECT confirmed_starts,outcome FROM sessions").fetchone(),
                                 (1,"Selected track is playing on Spotify"))
            db.close()

    def test_switch_retires_previous_worker_before_reusing_spotify(self):
        from lastsync_core import workers
        cfg = {"lastfm_user":"Alice","spotify_client_id":"test","lastfm_api_key":"test"}
        sp = Mock(namespace="account", preferred_device="desk")
        sp.search.return_value = [MATCH]
        sp.active_device.return_value = "desk"
        sp.playback.return_value = Playback(URI,0,180000,True,"desk",0)
        win = LastSyncWindow(cfg,prompt_settings=False,friends_path=":memory:",monitor_factory=None)
        def wait_until(check):
            deadline = time.monotonic() + 4
            while not check() and time.monotonic() < deadline:
                QTest.qWait(10)
            self.assertTrue(check())
        with tempfile.TemporaryDirectory() as folder, patch.object(workers,"data_directory",return_value=Path(folder)), \
                patch.object(workers,"LastFMClient") as lastfm:
            lastfm.return_value.recent.return_value = Observation(A,(),100)
            win._sp = sp
            try:
                win._listen("Alice",TRACK_FOLLOW,None,None)
                wait_until(lambda: sp.start.called)
                win._listen("Bob",DISCOVERY,A,None)
                wait_until(lambda: win._worker is None and win._next_session is None)
                self.assertEqual(sp.start.call_count,2)
                with sqlite3.connect(Path(folder)/"matches.sqlite3") as db:
                    rows = db.execute("SELECT username,outcome FROM sessions JOIN profiles "
                                      "ON sessions.profile_id=profiles.id ORDER BY sessions.id").fetchall()
                    self.assertEqual(rows,[("Alice","stopped"),("Bob","Selected track is playing on Spotify")])
                db.close()
            finally:
                win.stop_sync()
                wait_until(lambda: win._worker is None)
                win._friends_store.close()
                win._closing=True
                win._tray.hide()
                win.close()
                win.deleteLater()

    def test_dashboard_monitors_two_profiles_without_spotify_or_duplicate_source_polls(self):
        calls = []
        client = Mock()
        client.avatar.return_value = ""
        def fetch(name):
            calls.append((name, threading.get_ident()))
            return Observation(A if name == "alice" else B, (), time.time())
        client.recent.side_effect = fetch
        cfg = {"lastfm_user":"Alice","lastfm_api_key":"key","spotify_client_id":""}
        win = LastSyncWindow(cfg, prompt_settings=False, friends_path=":memory:",
                             monitor_factory=lambda key: FriendMonitor(key, lambda _: client))
        try:
            win.friends.query.setText("Bob")
            win.friends.add()
            deadline = time.monotonic()+4
            while not all(win.friends.samples.get(n,FriendActivity()).observation for n in ("alice","bob")):
                self.assertLess(time.monotonic(),deadline)
                QTest.qWait(10)
            self.assertEqual(win.friends.track.text(),"Second · Artist · Two")
            self.assertIsNone(win._sp)
            before = list(calls)
            self.assertEqual(win._monitor.recent("Bob").current, B)
            self.assertEqual(before,calls)
            self.assertEqual({thread for _, thread in calls}, {calls[0][1]})
            self.assertNotEqual(calls[0][1],threading.get_ident())
        finally:
            win._monitor.stop()
            self.assertTrue(win._monitor.wait(2000))
            self.app.processEvents()
            win.friends.shutdown()
            win._friends_store.close()
            win._closing=True
            win._tray.hide()
            win.close()
            win.deleteLater()

    def test_stale_last_report_cannot_appear_live(self):
        p = self.panel
        p.query.setText("Alice")
        p.add()
        p.update_activity("alice", FriendActivity().observed(Observation(A,(),100),time.monotonic()-60))
        p._refresh_age()
        self.assertIn("stale",p.listen_state.text().lower())
        self.assertIn("Stale report",p.roster.currentItem().text())
        self.assertIn(A.title,p.track.text())
