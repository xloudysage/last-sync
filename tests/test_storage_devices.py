import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from lastsync_core.errors import ServiceError
from lastsync_core.models import Track
from lastsync_core.spotify import SpotifyClient
from lastsync_core.storage import MatchStore


class ReleaseStorageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "matches.sqlite3"
        self.now = 100

    def open(self, namespace="user"):
        store = MatchStore(self.path, namespace, clock=lambda: self.now)
        self.addCleanup(store.close)
        return store

    def test_migrates_real_v1_schema_with_manual_record(self):
        track = Track("Song", "Artist")
        candidate = {"uri": "spotify:track:" + "x" * 22, "track": {"title": "Song", "artist": "Artist"}}
        with sqlite3.connect(self.path) as conn:
            conn.execute("""CREATE TABLE matches (namespace TEXT NOT NULL, source TEXT NOT NULL,
                candidate TEXT, confidence REAL NOT NULL, manual INTEGER NOT NULL,
                expires REAL NOT NULL, PRIMARY KEY(namespace, source))""")
            conn.execute("INSERT INTO matches VALUES (?,?,?,?,?,?)",
                         ("user", MatchStore.source_key(track), json.dumps(candidate), 1, 1, 900))
            conn.execute("PRAGMA user_version=1")
        conn.close()
        store = self.open()
        self.assertTrue(store.get(track).manual)
        self.assertEqual(store.get(track).selected.uri, candidate["uri"])
        self.assertEqual(store.db.execute("PRAGMA user_version").fetchone(), (2,))
        self.assertEqual(self.open("other").get(track), None)

    def test_session_isolation_idempotent_end_and_incomplete_runs(self):
        store = self.open()
        first = store.begin_session("Friend", "device-id")
        store.record_start(first)
        self.now += 20
        store.end_session(first, "stopped")
        store.record_start(first)
        store.end_session(first, "failed")
        store.begin_session("FRIEND", None)
        self.open("other").begin_session("Friend", None)
        self.assertEqual(store.db.execute("SELECT count(*) FROM profiles").fetchone(), (2,))
        self.assertEqual(store.db.execute("SELECT ended_at,outcome,confirmed_starts,device_mode FROM sessions "
                                          "ORDER BY id").fetchall(), [(120,"stopped",1,"selected"),
                                                                      (None,None,0,"active"), (None,None,0,"active")])

    def test_search_cache_reused_by_client_and_expires(self):
        store = self.open()
        sp = SpotifyClient(None, client=Mock())
        sp.catalog_cache = store
        sp.request = Mock(return_value={"tracks": {"items": []}})
        source = Track("Song", "Artist")
        sp.search(source)
        count = sp.request.call_count
        sp.catalog_cache = self.open()
        sp.search(source)
        self.assertEqual(sp.request.call_count, count)
        sp.catalog_cache = self.open("other")
        sp.search(source)
        self.assertEqual(sp.request.call_count, 2 * count)
        self.now += 600
        sp.search(source)
        self.assertEqual(sp.request.call_count, 3 * count)

    def test_search_failures_are_never_cached(self):
        store = self.open()
        sp = SpotifyClient(None, client=Mock())
        sp.catalog_cache = store
        sp.request = Mock(return_value={"tracks": {"items": None}})
        with self.assertRaises(ServiceError):
            sp.search(Track("A", "B"))
        self.assertEqual(store.db.execute("SELECT count(*) FROM search_cache").fetchone(), (0,))

    def test_cache_bounds(self):
        store = self.open()
        for n in range(260):
            store.cache_search(str(n), {"tracks": {"items": []}})
        store.cache_search("large", {"extra": "x" * 270000})
        self.assertEqual(store.db.execute("SELECT count(*) FROM search_cache").fetchone(), (256,))
        self.assertIsNone(store.cached_search("0"))


class SpotifyDeviceTests(unittest.TestCase):
    def setUp(self):
        self.sp = SpotifyClient(None, client=Mock())
        self.sp.request = Mock(return_value={"devices": [
            {"id": "phone", "name": "Phone", "is_active": True, "is_restricted": False},
            {"id": "desk", "name": "Desk", "is_active": False, "is_restricted": False},
            {"id": "locked", "is_active": True, "is_restricted": True}, {"id": None}]})

    def test_active_explicit_missing_and_restricted(self):
        self.assertEqual(self.sp.active_device(), "phone")
        for selected, expected in [("desk", "desk"), ("gone", None), ("locked", None)]:
            self.sp.preferred_device = selected
            self.assertEqual(self.sp.active_device(), expected)
        self.assertEqual(self.sp.request.call_count, 4)

    def test_empty_and_invalid_device_response(self):
        self.sp.request.return_value = {"devices": []}
        self.assertEqual(self.sp.devices(), [])
        self.sp.request.return_value = {}
        with self.assertRaises(ServiceError):
            self.sp.devices()
