import json
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path

from .models import Candidate, Resolution, Track, normalize


class MatchStore:
    """One connection per worker. Migrations are transactional and versioned."""

    def __init__(self, path, namespace="default", clock=time.time):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5)
        self.clock, self.namespace = clock, namespace
        try:
            self.db.execute("PRAGMA foreign_keys=ON")
            self._migrate()
        except Exception:
            self.db.close()
            raise

    def _migrate(self):
        with self.db:
            # Serialize schema discovery as well as updates across app instances.
            self.db.execute("BEGIN IMMEDIATE")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version > 2:
                raise RuntimeError("Match database is newer than this application")
            if version < 1:
                self.db.execute("""CREATE TABLE matches (
                    namespace TEXT NOT NULL, source TEXT NOT NULL, candidate TEXT,
                    confidence REAL NOT NULL, manual INTEGER NOT NULL, expires REAL NOT NULL,
                    PRIMARY KEY(namespace, source))""")
            if version < 2:
                self.db.execute("""CREATE TABLE profiles (
                    id INTEGER PRIMARY KEY, namespace TEXT NOT NULL, user_key TEXT NOT NULL,
                    username TEXT NOT NULL, UNIQUE(namespace, user_key))""")
                self.db.execute("""CREATE TABLE sessions (
                    id INTEGER PRIMARY KEY, profile_id INTEGER NOT NULL REFERENCES profiles(id),
                    started_at REAL NOT NULL, ended_at REAL, outcome TEXT, confirmed_starts INTEGER NOT NULL DEFAULT 0,
                    device_mode TEXT NOT NULL CHECK(device_mode IN ('selected','active')))""")
                self.db.execute("CREATE INDEX sessions_profile_time ON sessions(profile_id, started_at)")
                self.db.execute("""CREATE TABLE search_cache (
                    namespace TEXT NOT NULL, query TEXT NOT NULL, response TEXT NOT NULL,
                    expires REAL NOT NULL, PRIMARY KEY(namespace, query))""")
                self.db.execute("CREATE INDEX search_expiry ON search_cache(expires)")
                self.db.execute("PRAGMA user_version=2")

    def begin_session(self, username, selected_device):
        username = username.strip()
        with self.db:
            profile_id = self.db.execute("""INSERT INTO profiles(namespace,user_key,username)
                VALUES (?,?,?) ON CONFLICT(namespace,user_key) DO UPDATE SET username=excluded.username
                RETURNING id""", (self.namespace, username.casefold(), username)).fetchone()[0]
            return self.db.execute("""INSERT INTO sessions(profile_id,started_at,device_mode)
                VALUES (?,?,?)""", (profile_id, self.clock(), "selected" if selected_device else "active")).lastrowid

    def record_start(self, session):
        with self.db:
            self.db.execute("UPDATE sessions SET confirmed_starts=confirmed_starts+1 WHERE id=? AND ended_at IS NULL",
                            (session,))

    def end_session(self, session, outcome):
        with self.db:
            self.db.execute("UPDATE sessions SET ended_at=max(started_at,?),outcome=? WHERE id=? AND ended_at IS NULL",
                            (self.clock(), outcome, session))

    def cached_search(self, query):
        row = self.db.execute("SELECT response FROM search_cache WHERE namespace=? AND query=? AND expires>?",
                              (self.namespace, query, self.clock())).fetchone()
        if row:
            try:
                value = json.loads(row[0])
                if isinstance(value["tracks"]["items"], list):
                    return value
            except (ValueError, TypeError, KeyError):
                pass
        return None

    def cache_search(self, query, response):
        payload = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
        if len(payload.encode("utf-8")) > 256 * 1024:
            return
        now = self.clock()
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO search_cache VALUES (?,?,?,?)",
                            (self.namespace, query, payload, now + 600))
            self.db.execute("DELETE FROM search_cache WHERE expires<=?", (now,))
            self.db.execute("""DELETE FROM search_cache WHERE rowid IN
                (SELECT rowid FROM search_cache ORDER BY expires DESC,rowid DESC LIMIT -1 OFFSET 256)""")

    @staticmethod
    def source_key(track):
        return json.dumps([*track.key, normalize(track.album), track.duration_ms, track.isrc.upper()],
                          ensure_ascii=False)

    def get(self, track):
        key = self.source_key(track)
        row = self.db.execute("SELECT candidate,confidence,manual,expires FROM matches "
                              "WHERE namespace=? AND source=?", (self.namespace, key)).fetchone()
        if row is None or row[3] < self.clock():
            return None
        try:
            data = json.loads(row[0]) if row[0] else None
            candidate = Candidate(**{**data, "track": Track(**data["track"])}) if data else None
            return Resolution(candidate, (), row[1], bool(row[2]))
        except (TypeError, ValueError, KeyError):
            self.delete(track)
            return None

    def put(self, track, resolution):
        ttl = 10 * 365 * 86400 if resolution.manual else (30 * 86400 if resolution.selected else 300)
        data = json.dumps(asdict(resolution.selected), ensure_ascii=False) if resolution.selected else None
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO matches VALUES (?,?,?,?,?,?)",
                            (self.namespace, self.source_key(track), data, resolution.confidence,
                             int(resolution.manual), self.clock() + ttl))
            # Bound automatic mappings; retain explicit user corrections.
            self.db.execute("DELETE FROM matches WHERE manual=0 AND expires<?", (self.clock(),))
            self.db.execute("""DELETE FROM matches WHERE rowid IN (
                SELECT rowid FROM matches WHERE manual=0 ORDER BY expires DESC LIMIT -1 OFFSET 5000)""")

    def delete(self, track):
        with self.db:
            self.db.execute("DELETE FROM matches WHERE namespace=? AND source=?",
                            (self.namespace, self.source_key(track)))

    def close(self):
        self.db.close()
