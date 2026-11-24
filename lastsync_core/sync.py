"""Deterministic Track Follow state machine; no Qt, sleeps or wall-clock guessing."""
import time
from .errors import RetryPolicy, ServiceError
from .models import SyncState

POLL_ACTIVE, POLL_IDLE = 2, 5


class SyncEngine:
    def __init__(self, lastfm, spotify, resolver, username, *, clock=time.monotonic,
                 stopped=lambda: False, on_state=lambda *a: None,
                 on_track=lambda *a: None, on_progress=lambda *a: None,
                 on_match=lambda *a: None):
        self.lastfm, self.spotify, self.resolver = lastfm, spotify, resolver
        self.username, self.clock, self.cancelled = username, clock, stopped
        self.on_state, self.on_track = on_state, on_track
        self.on_progress, self.on_match = on_progress, on_match
        self.state = SyncState.IDLE
        self.message = "Ready"
        self.retry = RetryPolicy()
        self.source = self.resolution = self.pending = self.owned = None
        self.event_id = 0
        self.first_observed_at = 0
        self.last_scrobble = None
        self.no_match_until = 0
        self.missing_since = None

    def transition(self, state, message):
        if (state, message) != (self.state, self.message):
            self.state, self.message = state, message
            self.on_state(state, message)

    def stop(self, message="Stopped; Spotify playback is unchanged"):
        self.transition(SyncState.STOPPED, message)

    def _call(self, service, operation):
        result = operation()
        self.retry.success(service)
        if self.cancelled():
            self.stop()
        return result

    def _new_event(self, observation):
        current = observation.current
        newest = next((s for s in observation.history if s.track.key == current.key), None)
        repeated = (self.source is not None and current.key == self.source.key and newest is not None
                    and newest.timestamp > self.first_observed_at
                    and newest.timestamp != self.last_scrobble)
        changed = self.source is None or self.source.key != current.key or repeated
        if changed:
            self.event_id += 1
            self.source, self.resolution, self.pending = current, None, None
            self.first_observed_at = observation.observed_at
            self.last_scrobble = newest.timestamp if newest else None
            self.no_match_until = 0
        return changed

    def _check_control(self, playback):
        if self.owned is None or self.pending is not None:
            return True
        if playback is None or not playback.device_id:
            self.transition(SyncState.RECOVERING, "Spotify device disconnected; waiting without restarting")
            return True
        uri, device = self.owned
        if playback.uri != uri or (device and playback.device_id != device):
            self.stop("Playback or device changed in Spotify; press Start to follow again")
            return False
        ended = playback.duration_ms > 0 and playback.position_ms >= playback.duration_ms - 1500
        if not playback.playing and not ended:
            self.stop("Playback paused in Spotify; press Start to follow again")
            return False
        return True

    def step(self):
        if self.cancelled() or self.state == SyncState.STOPPED:
            self.stop(self.message if self.state == SyncState.STOPPED else "Stopped")
            return POLL_IDLE
        try:
            if self.source is None:
                self.transition(SyncState.DETECTING, "Checking Last.fm reported now-playing")
            observation = self._call("Last.fm", lambda: self.lastfm.recent(self.username))
            if self.cancelled():
                return POLL_IDLE
            # Inspect local changes before issuing commands for a newly observed source track.
            playback = self._call("Spotify", self.spotify.playback) if self.owned or self.pending else None
            if self.cancelled() or not self._check_control(playback):
                return POLL_IDLE
            if observation.current is None:
                self.source = self.resolution = self.pending = None
                self.transition(SyncState.IDLE, "Last.fm reports no current track")
                if playback:
                    self.on_progress(playback)
                return POLL_IDLE
            changed = self._new_event(observation)
            if changed:
                self.on_match(self.event_id, self.source, ())  # Invalidate stale correction dialogs.
            if self.pending:
                candidate, device, sent_at = self.pending
                if playback and playback.uri == candidate.uri and playback.playing:
                    self.pending = None
                    self.owned = (candidate.uri, playback.device_id or device)
                    self.transition(SyncState.FOLLOWING, "Following track; source position unknown")
                    self.on_track(self.source, candidate)
                    self.on_progress(playback)
                elif self.clock() - sent_at >= 10:
                    # A timed-out command may have been applied. Never resend blindly.
                    self.stop("Playback start could not be confirmed; check Spotify, then press Start")
                else:
                    self.transition(SyncState.STARTING, "Waiting for Spotify playback confirmation")
                return POLL_ACTIVE
            if not changed and self.resolution and self.resolution.selected and self.owned:
                if playback and playback.uri == self.owned[0]:
                    self.transition(SyncState.FOLLOWING, "Following track; source position unknown")
                    self.on_progress(playback)
                return POLL_ACTIVE
            if self.resolution is None or (not self.resolution.selected and self.clock() >= self.no_match_until):
                self.transition(SyncState.RESOLVING, "Matching title, artist and recording version")
                self.resolution = self._call("Spotify", lambda: self.resolver.resolve(self.source))
                if self.cancelled():
                    return POLL_IDLE
                if not self.resolution.selected:
                    self.no_match_until = self.clock() + 300
                    self.on_match(self.event_id, self.source, self.resolution.candidates)
                    self.transition(SyncState.RESOLVING, "No confident match; choose a Spotify track")
                    return POLL_ACTIVE
            if not self.resolution.selected:
                return POLL_ACTIVE
            device = self._call("Spotify", self.spotify.active_device)
            if self.cancelled():
                return POLL_IDLE
            if not device:
                self.transition(SyncState.RECOVERING, "Open Spotify and play once to activate a device")
                return POLL_IDLE
            candidate = self.resolution.selected
            self.transition(SyncState.STARTING, "Starting matched track from the beginning")
            self.pending = (candidate, device, self.clock())
            try:
                if not self.cancelled():
                    self._call("Spotify", lambda: self.spotify.start(candidate.uri, device))
            except ServiceError as error:
                if not error.uncertain:
                    self.pending = None  # Explicit rejection is safe to retry after backoff.
                raise
            return POLL_ACTIVE
        except ServiceError as error:
            if self.cancelled():
                self.stop()
                return POLL_IDLE
            if error.fatal:
                self.stop(str(error))
                return POLL_IDLE
            delay = self.retry.failure(error)
            self.transition(SyncState.RECOVERING, f"{error}; retry in {delay:g}s")
            return delay

    def choose_match(self, event_id, uri):
        if event_id != self.event_id or self.source is None or self.state == SyncState.STOPPED:
            return False
        result = self.resolver.correct(self.source, uri)
        if self.cancelled():
            return False
        self.resolution = result
        self.pending = None
        self.owned = None  # Explicit user correction authorizes replacing the current selection.
        self.transition(SyncState.RESOLVING, "Manual match saved; waiting to start playback")
        return True
