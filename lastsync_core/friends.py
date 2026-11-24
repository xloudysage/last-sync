"""One Last.fm request scheduler shared by the Friends dashboard and the player."""
from dataclasses import dataclass, replace
import threading
import time

from PyQt6.QtCore import QThread, pyqtSignal

from .errors import ServiceError
from .lastfm import LastFMClient
from .models import Observation, Track


@dataclass(frozen=True)
class FriendActivity:
    observation: Observation | None = None
    avatar: str = ""
    error: str = ""
    fatal: bool = False
    received: float = 0
    # Incremented when observed track identity or repeat evidence changes.
    event: int = 0
    event_since: float = 0
    last_track: Track | None = None
    last_activity: float = 0

    def stale(self, now):
        return self.observation is not None and now - self.received > 45

    def observed(self, sample, now):
        before = self.observation
        previous = before.current if before else None
        changed = sample.current != previous
        if not changed and sample.current and before:
            previous_times = {s.timestamp for s in before.history if s.track.key == sample.current.key}
            changed = any(s.track.key == sample.current.key and s.timestamp > self.event_since
                          and s.timestamp not in previous_times for s in sample.history)
        latest = sample.history[0] if sample.history else None
        track = sample.current or (latest.track if latest else None)
        when = sample.observed_at if sample.current else (latest.timestamp if latest else 0)
        if when < self.last_activity and not sample.current:
            track, when = self.last_track, self.last_activity
        return replace(self, observation=sample, error="", fatal=False, received=now,
                       last_track=track, last_activity=when,
                       event=self.event + int(changed), event_since=sample.observed_at if changed else self.event_since)


class FriendMonitor(QThread):
    updated = pyqtSignal(str)

    def __init__(self, key, client_factory=LastFMClient, clock=time.monotonic):
        super().__init__()
        self.key, self.factory, self.clock = key, client_factory, clock
        self.lock = threading.Condition()
        self.entries = {}
        self.due = {}
        self.failures = {}
        self.focus = ""
        self.blocked_until = 0
        self.closed = False

    def watch(self, names, focus=""):
        wanted = {n.casefold() for n in names if n}
        focus = focus.casefold()
        if focus:
            wanted.add(focus)
        with self.lock:
            for name in self.entries.keys() - wanted:
                del self.entries[name]
                for job in ("recent", "avatar"):
                    self.due.pop((name, job), None)
                    self.failures.pop((name, job), None)
            for name in sorted(wanted - self.entries.keys()):
                self.entries[name] = FriendActivity()
                self.due[name, "recent"] = self.due[name, "avatar"] = 0
            if focus and focus != self.focus and not self.entries[focus].error:
                self.due[focus, "recent"] = 0
            self.focus = focus
            self.lock.notify_all()

    def activity(self, name):
        with self.lock:
            return self.entries.get(name.casefold(), FriendActivity())

    def recent(self, username):
        with self.lock:
            item = self.entries.get(username.casefold())
            now = self.clock()
            if item and not item.error and item.observation and now - item.received <= 8:
                return item.observation
            raise ServiceError("Last.fm", item.error if item and item.error else "Waiting for fresh Last.fm activity",
                               fatal=bool(item and item.fatal), retry_after=max(0, self.blocked_until-now))

    def stop(self):
        with self.lock:
            self.closed = True
            self.lock.notify_all()

    def _next(self):
        with self.lock:
            while not self.closed:
                if not self.due:
                    self.lock.wait()
                    continue
                now = self.clock()
                # Reserve foreground play checks even during the first scan of a large roster.
                focus = (self.focus, "recent")
                if focus in self.due and self.due[focus] <= now:
                    job = focus
                else:
                    job = min(self.due, key=lambda j: (self.due[j], j[1] != "recent", j[0]))
                wait = max(self.blocked_until, self.due[job]) - now
                if wait <= 0:
                    return job, self.entries[job[0]]
                self.lock.wait(wait)
        return None

    def run(self):
        client = self.factory(self.key)
        try:
            while work := self._next():
                (name, job), entry = work
                error = None
                try:
                    result = client.recent(name) if job == "recent" else client.avatar(name)
                except ServiceError as exc:
                    error = exc
                except Exception:
                    error = ServiceError("Last.fm", "Last.fm returned unreadable activity")
                now = self.clock()
                with self.lock:
                    pause = error.retry_after if error else 0
                    if error and error.status == 429 and not pause:
                        pause = 30
                    self.blocked_until = now + max(0.35, pause)
                    if self.closed or self.entries.get(name) is not entry:
                        continue
                    if error:
                        count = min(6, self.failures.get((name, job), 0) + 1)
                        self.failures[name, job] = count
                        delay = 300 if error.fatal else max(2 ** count, pause)
                        if job == "recent":
                            self.entries[name] = replace(entry, error=str(error), fatal=error.fatal)
                    else:
                        self.failures.pop((name, job), None)
                        if job == "recent":
                            self.entries[name] = entry.observed(result, now)
                            delay = 2 if name == self.focus else (12 if result.current else 35)
                        else:
                            self.entries[name] = replace(entry, avatar=result)
                            delay = 6 * 3600
                    self.due[name, job] = now + delay
                self.updated.emit(name)
        finally:
            client.close()


TRACK_FOLLOW = "Track Follow"
BEST_EFFORT = "Best-Effort Sync"
DISCOVERY = "Discovery"


@dataclass(frozen=True)
class PositionHint:
    """Source position explicitly supplied by the listener for one observed play."""
    track: Track
    event: int
    position_ms: int
    sampled: float

    def position(self, activity, candidate, now):
        age = now - self.sampled
        if (not activity.observation or activity.observation.current != self.track or
                activity.error or activity.event != self.event or not 0 <= age <= 30 or
                now - activity.received > 8 or self.position_ms < 0):
            return None
        pos = self.position_ms + int(age * 1000)
        if not candidate.track.duration_ms or pos >= candidate.track.duration_ms:
            return None
        return pos


class PickedTrack:
    """Discovery pins the explicit choice even if the friend's source changes."""
    def __init__(self, track):
        self.sample = Observation(track, (), time.time())

    def recent(self, username):
        return self.sample
