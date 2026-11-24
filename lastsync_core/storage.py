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
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > 1:
            self.db.close()
            raise RuntimeError("Match database is newer than this application")
        with self.db:
            if version < 1:
                self.db.execute("""CREATE TABLE matches (
                    namespace TEXT NOT NULL, source TEXT NOT NULL, candidate TEXT,
                    confidence REAL NOT NULL, manual INTEGER NOT NULL, expires REAL NOT NULL,
                    PRIMARY KEY(namespace, source))""")
                self.db.execute("PRAGMA user_version=1")

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
