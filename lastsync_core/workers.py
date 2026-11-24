import hashlib
from queue import Empty, Full, Queue
import threading
import time

import httpx
from PyQt6.QtCore import QThread, pyqtSignal

from .lastfm import LastFMClient
from .spotify import SpotifyClient
from .transport import TIMEOUT
from .auth import SpotifyAuth
from .config import CONFIG_FILE, data_directory
from .errors import ServiceError
from .matching import TrackResolver
from .models import SyncState
from .storage import MatchStore
from .sync import SyncEngine
from .diagnostics import log
from .friends import BEST_EFFORT, DISCOVERY, TRACK_FOLLOW, PickedTrack


class SyncWorker(QThread):
    track_changed = pyqtSignal(str, str, int, int, str)
    status_changed = pyqtSignal(str, str)
    state_changed = pyqtSignal(str, str)
    playback_changed = pyqtSignal(object)
    matching_required = pyqtSignal(int, object, object)
    poll_interval_changed = pyqtSignal(int)

    def __init__(self, sp, cfg, *, monitor=None, mode=TRACK_FOLLOW, picked=None, hint=None):
        super().__init__()
        self.sp, self.cfg = sp, dict(cfg)
        self.monitor, self.mode, self.picked, self.hint = monitor, mode, picked, hint
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
        session = None
        outcome = "failed"
        try:
            if self.mode == DISCOVERY:
                if self.picked is None:
                    raise ValueError("Discovery requires a selected track")
                source = PickedTrack(self.picked)
            elif self.monitor is not None:
                source = self.monitor
            else:
                lastfm = source = LastFMClient(self.cfg["lastfm_api_key"])
            username = self.cfg["lastfm_user"]
            namespace = getattr(self.sp, "namespace", self.cfg["spotify_client_id"])
            store = MatchStore(data_directory() / "matches.sqlite3", namespace)
            self.sp.catalog_cache = store
            session = store.begin_session(self.cfg["lastfm_user"], getattr(self.sp, "preferred_device", None))
            log.info("Session started")

            def report_track(source, candidate):
                store.record_start(session)
                self.track_changed.emit(source.title, source.artist, 0,
                                        candidate.track.duration_ms or 0, candidate.artwork)

            def start_position(candidate):
                if self.mode == BEST_EFFORT and self.hint and self.monitor:
                    return self.hint.position(self.monitor.activity(username), candidate, time.monotonic())
                return None

            engine = SyncEngine(source, self.sp, TrackResolver(self.sp, store), username,
                                start_position=start_position, single_track=self.mode == DISCOVERY,
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
                outcome = "stopped"
            else:
                outcome = engine.message
        except Exception:
            log.error("Sync worker failed")
            # Never send raw transport/SQLite exceptions containing credentials or local paths to UI logs.
            self.state_changed.emit("STOPPED", "Local sync service failed; check installation and data-directory permissions")
        finally:
            if lastfm:
                lastfm.close()
            if store:
                self.sp.catalog_cache = None
                try:
                    if session is not None:
                        store.end_session(session, outcome)
                    log.info("Session closed")
                except Exception:
                    log.error("Session close could not be recorded")
                    self.status_changed.emit("Could not save session end to local history", "error")
                finally:
                    store.close()


class AuthWorker(QThread):
    auth_success = pyqtSignal(object)
    auth_failed = pyqtSignal(str)
    device_options = pyqtSignal(object, str)

    def __init__(self, cfg, *, choose_output=False):
        super().__init__()
        self.cfg = dict(cfg)
        self._cancelled = threading.Event()
        self.choose_output = choose_output
        self._device_commands = Queue(maxsize=1)

    def device_command(self, action, device_id=None):
        try:
            self._device_commands.put_nowait((action, device_id))
        except Full:
            pass

    def stop(self):
        self._cancelled.set()
        self.device_command("cancel")

    def _choose_device(self, sp):
        while not self._cancelled.is_set():
            try:
                devices = sp.devices()
                message = "" if devices else "Open Spotify on the device you want, then refresh."
            except ServiceError as error:
                if error.fatal:
                    raise
                devices = []
                message = str(error)
                if error.retry_after > 0:
                    self.device_options.emit(None, f"Spotify asked us to wait {error.retry_after:g}s.")
                    if self._cancelled.wait(error.retry_after):
                        return False
                    continue
            self.device_options.emit(devices, message)
            action, device_id = self._device_commands.get()
            if self._cancelled.is_set() or action == "cancel":
                return False
            if action == "select" and (device_id is None or any(
                    d.id and d.id == device_id and not d.restricted for d in devices)):
                sp.preferred_device = device_id
                return True
        return False

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
            if self.choose_output and not self._choose_device(sp):
                return
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
