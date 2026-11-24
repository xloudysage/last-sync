import base64
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx

from lastsync_core.api import LastFMClient, SpotifyClient, parse_candidate, parse_recent, request_json, retry_after
from lastsync_core.auth import SpotifyAuth, callback_code, pkce_pair
from lastsync_core.config import CredentialError, SecretStore, load_config, save_config
from lastsync_core.errors import RetryPolicy, ServiceError
from lastsync_core.matching import TrackResolver, rank, versions
from lastsync_core.models import Candidate, Observation, Playback, Scrobble, SyncState, Track, normalize
from lastsync_core.storage import MatchStore
from lastsync_core.sync import SyncEngine

A = Track("A Song", "An Artist")
B = Track("Another Song", "An Artist")
URI_A = "spotify:track:" + "a" * 22
URI_B = "spotify:track:" + "b" * 22
CA = Candidate(URI_A, Track(A.title, A.artist, "Album", 180000))
CB = Candidate(URI_B, Track(B.title, B.artist, "Album", 210000))


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now


class MemoryCredentials:
    def __init__(self):
        self.values = {}

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, value):
        self.values[service, account] = value


class FakeLastFM:
    def __init__(self):
        self.observation = Observation(A, (), 100)
        self.error = None
        self.hook = lambda: None

    def recent(self, username):
        self.hook()
        if self.error:
            raise self.error
        return self.observation


class FakeSpotify:
    def __init__(self, clock):
        self.clock = clock
        self.candidates = [CA, CB]
        self.current = None
        self.device = "device-1"
        self.starts = []
        self.searches = 0
        self.start_error = self.playback_error = None
        self.apply_before_error = False
        self.search_hook = lambda: None

    def search(self, source):
        self.searches += 1
        self.search_hook()
        return self.candidates

    def track(self, uri):
        return next(c for c in self.candidates if c.uri == uri)

    def playback(self):
        if self.playback_error:
            raise self.playback_error
        return self.current

    def active_device(self):
        return self.device

    def start(self, uri, device):
        self.starts.append((uri, device))
        if not self.start_error or self.apply_before_error:
            candidate = next(c for c in self.candidates if c.uri == uri)
            self.current = Playback(uri, 0, candidate.track.duration_ms, True, device, self.clock())
        if self.start_error:
            raise self.start_error


class ParsingTests(unittest.TestCase):
    def test_empty_history_is_idle(self):
        self.assertIsNone(parse_recent({"recenttracks": {"track": []}}, 123).current)

    def test_single_dictionary_and_nowplaying_false(self):
        row = {"name": "A", "artist": {"#text": "B"}, "@attr": {"nowplaying": "false"},
               "date": {"uts": "100"}}
        observation = parse_recent({"recenttracks": {"track": row}}, 200)
        self.assertIsNone(observation.current)
        self.assertEqual(observation.history[0].timestamp, 100)
        self.assertEqual(observation.observed_at, 200)

    def test_nowplaying_does_not_invent_start_time(self):
        observation = parse_recent({"recenttracks": {"track": [
            {"name": "A", "artist": {"#text": "B"}, "@attr": {"nowplaying": "true"}}]}}, 200)
        self.assertEqual(observation.current.title, "A")
        self.assertEqual(observation.history, ())

    def test_malformed_response_is_error_not_idle(self):
        for payload in (None, {}, {"recenttracks": {"track": None}},
                        {"recenttracks": {"track": [{"name": "x"}]}}):
            with self.subTest(payload=payload), self.assertRaises(ServiceError):
                parse_recent(payload, 0)

    def test_api_error_in_http_200(self):
        with self.assertRaises(ServiceError) as caught:
            parse_recent({"error": 29, "message": "secret raw response"}, 0)
        self.assertEqual(caught.exception.retry_after, 30)
        self.assertNotIn("secret", str(caught.exception))

    def test_candidate_tolerates_removed_fields(self):
        candidate = parse_candidate({"uri": URI_A, "name": A.title, "artists": [{"name": A.artist}]})
        self.assertTrue(candidate.playable)
        self.assertIsNone(candidate.track.duration_ms)

    def test_rejects_local_and_unplayable(self):
        item = {"uri": URI_A, "name": A.title, "artists": [{"name": A.artist}], "is_playable": False}
        self.assertFalse(parse_candidate(item).playable)
        self.assertIsNone(parse_candidate({"uri": "spotify:episode:abc"}))

    def test_lastfm_params_are_encoded(self):
        captured = []
        def transport(request):
            captured.append(request)
            return httpx.Response(200, json={"recenttracks": {"track": []}})
        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            LastFMClient("a&b", client).recent("user&method=bad")
        self.assertEqual(captured[0].url.params["user"], "user&method=bad")
        self.assertEqual(captured[0].url.params["method"], "user.getrecenttracks")


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.store = MatchStore(":memory:")
        self.sp = FakeSpotify(Clock())
        self.resolver = TrackResolver(self.sp, self.store)
        self.addCleanup(self.store.close)

    def test_unicode_and_punctuation(self):
        self.assertEqual(normalize("Ａ Song!"), normalize("a song"))
        self.assertNotEqual(normalize("தமிழ்"), "")

    def test_live_title_is_not_live_version(self):
        self.assertEqual(versions("Live Forever"), set())
        self.assertEqual(versions("Live Forever - Live at Wembley"), {"live"})

    def test_wrong_versions_never_autoplay(self):
        for suffix in ("Live", "Acoustic", "Remix", "Instrumental", "Karaoke", "Radio Edit"):
            with self.subTest(suffix=suffix):
                self.assertEqual(rank(A, Candidate(URI_A, Track(A.title + " (" + suffix + ")", A.artist))).confidence, 0)

    def test_wrong_artist_and_duration(self):
        self.assertEqual(rank(A, Candidate(URI_A, Track(A.title, "Someone Else"))).confidence, 0)
        self.assertEqual(rank(Track(A.title, A.artist, duration_ms=100000), CA).confidence, 0)

    def test_different_remixes_are_not_equivalent(self):
        source = Track("A Song (DJ One Remix)", A.artist)
        candidate = Candidate(URI_A, Track("A Song (DJ Two Remix)", A.artist))
        self.assertEqual(rank(source, candidate).confidence, 0)

    def test_live_album_metadata_prevents_studio_mismatch(self):
        self.assertEqual(rank(Track(A.title, A.artist, "Live at Wembley"), CA).confidence, 0)
        candidate = Candidate(URI_A, Track(A.title, A.artist, "Live from London"))
        self.assertEqual(rank(A, candidate).confidence, 0)
        self.assertGreater(rank(Track(A.title, A.artist, "Live Through This"), CA).confidence, 0.99)

    def test_remaster_does_not_change_performance(self):
        self.assertGreater(rank(A, Candidate(URI_A, Track("A Song - 2011 Remaster", A.artist))).confidence, 0.99)

    def test_isrc_is_authoritative(self):
        self.assertEqual(rank(Track("different", "different", isrc="ABC"),
                              Candidate(URI_A, Track("x", "y", isrc="abc"))).confidence, 1)

    def test_high_confidence_match_is_cached(self):
        self.assertEqual(self.resolver.resolve(A).selected.uri, URI_A)
        self.resolver.resolve(A)
        self.assertEqual(self.sp.searches, 1)

    def test_ambiguous_recordings_need_correction(self):
        self.sp.candidates = [CA, Candidate(URI_B, Track(A.title, A.artist, "Other album", 220000))]
        result = self.resolver.resolve(A)
        self.assertIsNone(result.selected)
        self.assertEqual(len(result.candidates), 2)
        self.resolver.correct(A, URI_B)
        self.assertEqual(self.resolver.resolve(A).selected.uri, URI_B)
        self.assertTrue(self.resolver.resolve(A).manual)

    def test_same_performance_on_multiple_albums_is_not_ambiguous(self):
        self.sp.candidates = [CA, Candidate(URI_B, CA.track)]
        self.assertIsNotNone(self.resolver.resolve(A).selected)

    def test_no_match_is_cached_briefly(self):
        clock = Clock()
        store = MatchStore(":memory:", clock=clock)
        self.addCleanup(store.close)
        self.sp.candidates = []
        resolver = TrackResolver(self.sp, store)
        resolver.resolve(A)
        resolver.resolve(A)
        self.assertEqual(self.sp.searches, 1)
        clock.now = 301
        resolver.resolve(A)
        self.assertEqual(self.sp.searches, 2)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.clock, self.lfm = Clock(), FakeLastFM()
        self.sp = FakeSpotify(self.clock)
        self.store = MatchStore(":memory:")
        self.addCleanup(self.store.close)
        self.cancelled, self.states, self.progress, self.matches = False, [], [], []
        self.engine = SyncEngine(self.lfm, self.sp, TrackResolver(self.sp, self.store), "friend",
                                 clock=self.clock, stopped=lambda: self.cancelled,
                                 on_state=lambda *a: self.states.append(a),
                                 on_progress=self.progress.append,
                                 on_match=lambda *a: self.matches.append(a))

    def follow(self):
        self.engine.step()
        self.assertEqual(self.engine.state, SyncState.STARTING)
        self.engine.step()
        self.assertEqual(self.engine.state, SyncState.FOLLOWING)

    def test_play_once_and_confirm(self):
        self.follow()
        for _ in range(20):
            self.clock.now += 2
            self.engine.step()
        self.assertEqual(len(self.sp.starts), 1)
        self.assertEqual(self.progress[-1].position_ms, 0)
        self.assertIn(SyncState.RESOLVING, [state for state, _ in self.states])

    def test_track_change(self):
        self.follow()
        self.lfm.observation = Observation(B, (), 110)
        self.engine.step()
        self.engine.step()
        self.assertEqual([uri for uri, _ in self.sp.starts], [URI_A, URI_B])

    def test_idle_then_same_track_is_new_event(self):
        self.follow()
        self.lfm.observation = Observation(None, (), 110)
        self.engine.step()
        self.lfm.observation = Observation(A, (), 120)
        self.engine.step()
        self.assertEqual(len(self.sp.starts), 2)

    def test_repeat_with_new_scrobble_evidence(self):
        self.follow()
        self.lfm.observation = Observation(A, (Scrobble(A, 105),), 110)
        self.engine.step()
        self.engine.step()
        self.engine.step()
        self.assertEqual(len(self.sp.starts), 2)

    def test_late_scrobble_for_original_play_does_not_restart(self):
        self.follow()
        self.lfm.observation = Observation(A, (Scrobble(A, 95),), 110)
        self.engine.step()
        self.assertEqual(len(self.sp.starts), 1)

    def test_repetition_without_evidence_is_not_guessed(self):
        self.follow()
        self.clock.now = 10000
        self.engine.step()
        self.assertEqual(len(self.sp.starts), 1)

    def test_missing_device_recovers_before_first_command(self):
        self.sp.device = None
        self.engine.step()
        self.assertEqual(self.engine.state, SyncState.RECOVERING)
        self.assertEqual(self.sp.starts, [])
        self.sp.device = "device-2"
        self.engine.step()
        self.assertEqual(len(self.sp.starts), 1)

    def test_device_disconnect_does_not_restart_current_track(self):
        self.follow()
        original = self.sp.current
        self.sp.current = None
        self.engine.step()
        self.assertEqual(self.engine.state, SyncState.RECOVERING)
        self.sp.current = original
        self.engine.step()
        self.assertEqual(len(self.sp.starts), 1)

    def test_manual_track_change_stops_following(self):
        self.follow()
        self.sp.current = Playback(URI_B, 100, 210000, True, "device-1", 0)
        self.engine.step()
        self.assertEqual(self.engine.state, SyncState.STOPPED)
        self.assertEqual(len(self.sp.starts), 1)

    def test_pause_stops_but_natural_end_does_not_restart(self):
        self.follow()
        self.sp.current = Playback(URI_A, 180000, 180000, False, "device-1", 0)
        self.engine.step()
        self.assertNotEqual(self.engine.state, SyncState.STOPPED)
        self.assertEqual(len(self.sp.starts), 1)
        self.sp.current = Playback(URI_A, 10000, 180000, False, "device-1", 0)
        self.engine.step()
        self.assertEqual(self.engine.state, SyncState.STOPPED)

    def test_lastfm_failure_preserves_current_event(self):
        self.follow()
        self.lfm.error = ServiceError("Last.fm", "network")
        self.assertEqual(self.engine.step(), 2)
        self.assertEqual(self.engine.step(), 4)
        self.lfm.error = None
        self.engine.step()
        self.assertEqual(len(self.sp.starts), 1)

    def test_rate_limit_honors_retry_after(self):
        self.lfm.error = ServiceError("Last.fm", "limited", retry_after=120)
        self.assertEqual(self.engine.step(), 120)

    def test_timed_out_applied_command_is_not_resent(self):
        self.sp.start_error = ServiceError("Spotify", "timeout", uncertain=True)
        self.sp.apply_before_error = True
        self.engine.step()
        self.engine.step()
        self.assertEqual(self.engine.state, SyncState.FOLLOWING)
        self.assertEqual(len(self.sp.starts), 1)

    def test_timed_out_unapplied_command_stops_after_confirmation_window(self):
        self.sp.start_error = ServiceError("Spotify", "timeout", uncertain=True)
        self.engine.step()
        self.clock.now = 11
        self.engine.step()
        self.assertEqual(self.engine.state, SyncState.STOPPED)
        self.assertEqual(len(self.sp.starts), 1)

    def test_explicit_device_rejection_can_retry(self):
        self.sp.start_error = ServiceError("Spotify", "no device", status=404)
        self.engine.step()
        self.sp.start_error = None
        self.engine.step()
        self.engine.step()
        self.assertEqual(self.engine.state, SyncState.FOLLOWING)

    def test_no_match_never_plays_unrelated_music(self):
        self.sp.candidates = [CB]
        for _ in range(10):
            self.engine.step()
        self.assertEqual(self.sp.starts, [])
        self.assertEqual(self.sp.searches, 1)
        self.assertEqual(self.engine.state, SyncState.RESOLVING)

    def test_stop_during_network_read_prevents_playback(self):
        self.lfm.hook = lambda: setattr(self, "cancelled", True)
        self.engine.step()
        self.assertEqual(self.sp.starts, [])
        self.assertEqual(self.engine.state, SyncState.STOPPED)

    def test_stop_during_resolution_prevents_playback(self):
        self.sp.search_hook = lambda: setattr(self, "cancelled", True)
        self.engine.step()
        self.assertEqual(self.sp.starts, [])

    def test_stale_manual_correction_is_rejected(self):
        self.follow()
        previous = self.engine.event_id
        self.lfm.observation = Observation(B, (), 110)
        self.engine.step()
        self.assertFalse(self.engine.choose_match(previous, URI_A))

    def test_manual_correction_can_resolve_ambiguous_track(self):
        self.sp.candidates = [CA, Candidate(URI_B, Track(A.title, A.artist, "Other album", 210000))]
        self.engine.step()
        self.assertEqual(self.sp.starts, [])
        self.assertTrue(self.engine.choose_match(self.engine.event_id, URI_B))
        self.engine.step()
        self.assertEqual(self.sp.starts[0][0], URI_B)


class PersistenceTests(unittest.TestCase):
    def test_album_context_keeps_manual_corrections_separate(self):
        store = MatchStore(":memory:")
        self.addCleanup(store.close)
        resolver = TrackResolver(FakeSpotify(Clock()), store)
        first = Track(A.title, A.artist, "First Album")
        resolver.correct(first, URI_A)
        self.assertIsNotNone(store.get(first))
        self.assertIsNone(store.get(Track(A.title, A.artist, "Second Album")))

    def test_database_migrates_and_reopens(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "matches.db"
            store = MatchStore(path, "user-a")
            resolver = TrackResolver(FakeSpotify(Clock()), store)
            resolver.correct(A, URI_A)
            self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 1)
            store.close()
            store = MatchStore(path, "user-a")
            self.assertTrue(store.get(A).manual)
            store.close()
            other = MatchStore(path, "user-b")
            self.assertIsNone(other.get(A))
            other.close()

    def test_future_schema_is_not_modified(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "future.db"
            with sqlite3.connect(path) as db:
                db.execute("PRAGMA user_version=99")
            db.close()
            with self.assertRaises(RuntimeError):
                MatchStore(path)

    def test_legacy_config_moves_credentials_out_of_json(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text(json.dumps({"lastfm_user": "friend", "lastfm_api_key": "private-key",
                                        "spotify_client_id": "client", "spotify_client_secret": "obsolete"}))
            store = SecretStore(MemoryCredentials())
            cfg = load_config(path, store)
            self.assertEqual(cfg["lastfm_api_key"], "private-key")
            self.assertEqual(store.get("lastfm-api-key"), "private-key")
            self.assertNotIn("private-key", path.read_text())
            self.assertNotIn("obsolete", path.read_text())
            self.assertEqual(load_config(path, store), cfg)

    def test_failed_credential_write_preserves_legacy_file(self):
        class FailingStore:
            def set(self, *args):
                raise CredentialError("unavailable")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            original = '{"lastfm_api_key":"key"}'
            path.write_text(original)
            with self.assertRaises(CredentialError):
                load_config(path, FailingStore())
            self.assertEqual(path.read_text(), original)


class AuthAndTransportTests(unittest.TestCase):
    def test_connect_exchanges_pkce_code_and_persists_tokens(self):
        captured = []
        def handler(request):
            captured.append(request)
            return httpx.Response(200, json={"access_token": "access", "refresh_token": "refresh",
                                            "expires_in": 3600})
        store = SecretStore(MemoryCredentials())
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            auth = SpotifyAuth("client", store, client, clock=lambda: 1000)
            with patch("lastsync_core.auth.receive_code", return_value="authorization-code") as callback:
                auth.connect(threading.Event())
            params = parse_qs(captured[0].content.decode())
            self.assertEqual(params["grant_type"], ["authorization_code"])
            verifier = params["code_verifier"][0]
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            self.assertIn(challenge, callback.call_args.args[1])
            self.assertNotIn("client_secret", params)
            self.assertEqual(auth.access_token(), "access")
            self.assertEqual(json.loads(store.get(auth.account))["refresh_token"], "refresh")

    def test_cancelled_authorization_makes_no_network_requests(self):
        store = SecretStore(MemoryCredentials())
        with httpx.Client(transport=httpx.MockTransport(lambda request: self.fail("network after cancel"))) as client:
            auth = SpotifyAuth("client", store, client)
            cancelled = threading.Event()
            cancelled.set()
            with self.assertRaises(InterruptedError):
                auth.connect(cancelled)

    def test_callback_listener_cancels_and_closes(self):
        from lastsync_core.auth import receive_code
        cancelled, closed = threading.Event(), []
        class Server:
            def __init__(self, address, handler):
                self.address = address
            def __enter__(self):
                return self
            def __exit__(self, *args):
                closed.append(True)
            def handle_request(self):
                cancelled.set()
        with patch("lastsync_core.auth.HTTPServer", Server):
            with self.assertRaises(InterruptedError):
                receive_code("state", "https://accounts.spotify.com/authorize", cancelled,
                             opener=lambda url: True)
        self.assertEqual(closed, [True])

    def test_pkce_challenge(self):
        verifier, challenge = pkce_pair()
        self.assertTrue(43 <= len(verifier) <= 128)
        self.assertEqual(challenge, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode())

    def test_callback_rejects_csrf_and_duplicate_values(self):
        for target in ("/callback?code=x", "/callback?state=bad&code=x", "/other?state=s&code=x",
                       "/callback?state=s&state=s&code=x", "/callback?state=s&code=x&code=y"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                callback_code(target, "s")
        self.assertEqual(callback_code("/callback?state=s&code=valid", "s"), "valid")
        with self.assertRaises(PermissionError):
            callback_code("/callback?state=s&error=access_denied", "s")

    def test_pkce_token_refresh_keeps_rotating_credentials_secure(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"access_token": "new", "expires_in": 3600})
        store = SecretStore(MemoryCredentials())
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            auth = SpotifyAuth("client", store, client, clock=lambda: 1000)
            auth.token = {"access_token": "expired", "refresh_token": "refresh", "expires_at": 0}
            self.assertEqual(auth.access_token(), "new")
            self.assertEqual(auth.token["refresh_token"], "refresh")
            self.assertNotIn(b"client_secret", requests[0].content)
            self.assertEqual(json.loads(store.get(auth.account))["access_token"], "new")

    def test_401_refreshes_once(self):
        class Auth:
            def access_token(self, force_refresh=False):
                return "new" if force_refresh else "old"
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(401 if len(requests) == 1 else 200, json={"id": "user"})
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            sp = SpotifyClient(Auth(), client)
            self.assertEqual(sp.request("GET", "me"), {"id": "user"})
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1].headers["Authorization"], "Bearer new")

    def test_transport_error_redacts_url_and_marks_uncertain_write(self):
        def handler(request):
            raise httpx.ReadTimeout("contains-private-token", request=request)
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(ServiceError) as caught:
                request_json(client, "Spotify", "PUT", "https://example.invalid/?secret=private")
        self.assertTrue(caught.exception.uncertain)
        self.assertNotIn("private", str(caught.exception))

    def test_429_has_retry_after_and_no_automatic_request_replay(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(429, headers={"Retry-After": "90"}, json={"error": "secret"})
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(ServiceError) as caught:
                request_json(client, "Spotify", "GET", "https://example.invalid")
        self.assertEqual(caught.exception.retry_after, 90)
        self.assertEqual(len(requests), 1)
        self.assertFalse(caught.exception.fatal)

    def test_retry_backoff_is_service_specific_and_bounded(self):
        retry = RetryPolicy()
        self.assertEqual([retry.failure(ServiceError("Last.fm", "x")) for _ in range(8)],
                         [2, 4, 8, 16, 32, 60, 60, 60])
        self.assertEqual(retry.failure(ServiceError("Spotify", "x")), 2)
        retry.success("Last.fm")
        self.assertEqual(retry.failure(ServiceError("Last.fm", "x")), 2)


if __name__ == "__main__":
    unittest.main()
