import hashlib
from queue import Empty, Queue
import threading

import httpx
from PyQt6.QtCore import QThread, pyqtSignal

from .api import LastFMClient, SpotifyClient, TIMEOUT
from .auth import SpotifyAuth
from .config import CONFIG_FILE, data_directory
from .errors import ServiceError
from .matching import TrackResolver
from .models import SyncState
from .storage import MatchStore
from .sync import SyncEngine


class SyncWorker(QThread):
    track_changed = pyqtSignal(str, str, int, int, str)
    status_changed = pyqtSignal(str, str)
    state_changed = pyqtSignal(str, str)
    playback_changed = pyqtSignal(object)
    matching_required = pyqtSignal(int, object, object)
    poll_interval_changed = pyqtSignal(int)

    def __init__(self, sp, cfg):
        super().__init__()
        self.sp, self.cfg = sp, dict(cfg)
        self._stop_event, self._wake = threading.Event(), threading.Event()
        self._commands = Queue(maxsize=10)

    def choose_match(self, event_id, uri):
        if not self._commands.full():
            self._commands.put_nowait((event_id, uri))
            self._wake.set()

    def stop(self):
        self._stop_event.set()
        self._wake.set()

    def run(self):
        lastfm = store = None
        try:
            lastfm = LastFMClient(self.cfg["lastfm_api_key"])
            namespace = getattr(self.sp, "namespace", self.cfg["spotify_client_id"])
            store = MatchStore(data_directory() / "matches.sqlite3", namespace)

            def report_track(source, candidate):
                self.track_changed.emit(source.title, source.artist, 0,
                                        candidate.track.duration_ms or 0, candidate.artwork)

            engine = SyncEngine(lastfm, self.sp, TrackResolver(self.sp, store), self.cfg["lastfm_user"],
                                stopped=self._stop_event.is_set,
                                on_state=lambda state, message: self.state_changed.emit(state.value, message),
                                on_track=report_track, on_progress=self.playback_changed.emit,
                                on_match=self.matching_required.emit)
            while not self._stop_event.is_set() and engine.state != SyncState.STOPPED:
                self._wake.clear()
                delay = 0
                try:
                    event_id, uri = self._commands.get_nowait()
                except Empty:
                    pass
                else:
                    try:
                        if not engine.choose_match(event_id, uri):
                            self.status_changed.emit("That source track changed; choose its current match", "warn")
                    except ValueError as error:
                        self.status_changed.emit(str(error), "warn")
                    except ServiceError as error:
                        self.status_changed.emit(str(error), "error")
                        delay = engine.retry.failure(error)
                        if error.fatal:
                            engine.stop(str(error))
                if not delay:
                    delay = engine.step()
                self.poll_interval_changed.emit(max(1, int(delay + 0.999)))
                if engine.state != SyncState.STOPPED:
                    # Manual choices must not bypass a service's Retry-After cooldown.
                    self._stop_event.wait(delay)
            if self._stop_event.is_set():
                engine.stop()
        except Exception:
            # Never send raw transport/SQLite exceptions containing credentials or local paths to UI logs.
            self.state_changed.emit("STOPPED", "Local sync service failed; check installation and data-directory permissions")
        finally:
            if lastfm:
                lastfm.close()
            if store:
                store.close()


class AuthWorker(QThread):
    auth_success = pyqtSignal(object)
    auth_failed = pyqtSignal(str)

    def __init__(self, cfg):
        super().__init__()
        self.cfg = dict(cfg)
        self._cancelled = threading.Event()

    def stop(self):
        self._cancelled.set()

    def run(self):
        auth = sp = None
        handed_off = False
        try:
            auth = SpotifyAuth(self.cfg["spotify_client_id"])
            auth.connect(self._cancelled)
            if self._cancelled.is_set():
                return
            sp = SpotifyClient(auth)
            try:
                user = sp.request("GET", "me")
            except ServiceError as error:
                if error.status not in (400, 401):
                    raise
                # A revoked cached grant must return to the browser, not loop forever
                # through the same rejected token on every new session.
                auth.token = None
                auth.connect(self._cancelled)
                user = sp.request("GET", "me")
            if not isinstance(user, dict) or not user.get("id"):
                raise ValueError("Spotify returned an invalid profile")
            sp.namespace = hashlib.sha256((self.cfg["spotify_client_id"] + ":" + user["id"]).encode()).hexdigest()
            if self._cancelled.is_set():
                return
            # Retire the old plaintext login cache only after successful PKCE authorization.
            legacy = CONFIG_FILE.parent / ".lastsync_spotify_cache"
            if legacy.exists():
                legacy.unlink()
            handed_off = True
            self.auth_success.emit(sp)
        except (ServiceError, PermissionError, TimeoutError) as error:
            if not self._cancelled.is_set():
                self.auth_failed.emit(str(error))
        except Exception:
            if not self._cancelled.is_set():
                self.auth_failed.emit("Authorization failed; check OS credential storage and Spotify app settings")
        finally:
            if not handed_off:
                if sp:
                    sp.close()
                if auth:
                    auth.close()


class ArtLoader(QThread):
    art_ready = pyqtSignal(bytes, int)

    def __init__(self, url, generation):
        super().__init__()
        self.url, self.generation = url, generation
        self.cancelled = threading.Event()

    def stop(self):
        self.cancelled.set()

    def run(self):
        if not self.url.startswith("https://"):
            return
        try:
            with httpx.Client(timeout=TIMEOUT) as client:
                with client.stream("GET", self.url) as response:
                    response.raise_for_status()
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        if self.cancelled.is_set() or len(data) + len(chunk) > 2 * 1024 * 1024:
                            return
                        data.extend(chunk)
                    if not self.cancelled.is_set():
                        self.art_ready.emit(bytes(data), self.generation)
        except httpx.HTTPError:
            pass
