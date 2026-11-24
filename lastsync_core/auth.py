"""PKCE with a bounded, cancellable loopback callback and OS token storage."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import secrets as random_secrets
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import webbrowser

import httpx

from .transport import request_json
from .config import SecretStore
from .errors import ServiceError

REDIRECT_URI = "http://127.0.0.1:8888/callback"
SPOTIFY_SCOPE = "user-modify-playback-state user-read-playback-state"


def pkce_pair():
    verifier = random_secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


def callback_code(target, expected_state):
    parts = urlsplit(target)
    if parts.path != "/callback":
        raise ValueError("Unexpected callback path")
    values = parse_qs(parts.query)
    states = values.get("state", [])
    if len(states) != 1 or not random_secrets.compare_digest(states[0], expected_state):
        raise ValueError("Authorization state mismatch")
    if "error" in values:
        raise PermissionError("Spotify authorization was declined")
    codes = values.get("code", [])
    if len(codes) != 1 or not codes[0]:
        raise ValueError("Missing authorization code")
    return codes[0]


def receive_code(state, authorize_url, cancelled, *, timeout=180, opener=webbrowser.open):
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Callback URLs contain authorization codes.

        def do_GET(self):
            try:
                code = callback_code(self.path, state)
                result["code"] = code
                status, message = 200, b"LastSync authorized. You can close this window."
            except PermissionError:
                result["denied"] = True
                status, message = 403, b"Authorization declined. You can close this window."
            except ValueError:
                status, message = 400, b"Invalid authorization callback."
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(message)))
            self.end_headers()
            try:
                self.wfile.write(message)
            except OSError:
                pass

    class LoopbackServer(HTTPServer):
        allow_reuse_address = False

        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(0.5)  # Slow local clients cannot block cancellation indefinitely.
            return connection, address

        def handle_error(self, request, client_address):
            pass

    try:
        server = LoopbackServer(("127.0.0.1", 8888), Callback)
    except OSError:
        raise ServiceError("Spotify", "Authorization port 8888 is occupied; close the other listener", fatal=True) from None
    with server:
        server.timeout = 0.2
        if cancelled.is_set():
            raise InterruptedError("Authorization cancelled")
        if not opener(authorize_url):
            raise ServiceError("Spotify", "Could not open the system browser for authorization", fatal=True)
        deadline = time.monotonic() + timeout
        while not result and not cancelled.is_set() and time.monotonic() < deadline:
            server.handle_request()
    if cancelled.is_set():
        raise InterruptedError("Authorization cancelled")
    if result.get("denied"):
        raise PermissionError("Spotify authorization was declined")
    if "code" not in result:
        raise TimeoutError("Spotify authorization timed out; press Start to retry")
    return result["code"]


class SpotifyAuth:
    def __init__(self, client_id, store=None, client=None, clock=time.time):
        self.client_id, self.store = client_id, store or SecretStore()
        self.client, self.clock = client or httpx.Client(), clock
        self.account = "spotify-token-" + hashlib.sha256(client_id.encode()).hexdigest()[:24]
        self.token = None
        self.lock = threading.RLock()
        raw = self.store.get(self.account)
        if raw:
            try:
                token = json.loads(raw)
                if isinstance(token, dict) and token.get("refresh_token"):
                    self.token = token
            except ValueError:
                pass

    def _save(self, data, old_refresh=None):
        if not isinstance(data, dict) or not data.get("access_token"):
            raise ServiceError("Spotify", "Spotify: malformed authorization response", fatal=True)
        if not data.get("refresh_token") and old_refresh:
            data["refresh_token"] = old_refresh
        if not data.get("refresh_token"):
            raise ServiceError("Spotify", "Spotify did not return a refresh token; reconnect", fatal=True)
        data["expires_at"] = self.clock() + int(data.get("expires_in", 3600))
        self.store.set(self.account, json.dumps(data))
        self.token = data

    def connect(self, cancelled):
        with self.lock:
            if cancelled.is_set():
                raise InterruptedError("Authorization cancelled")
            if self.token:
                try:
                    self.access_token()
                    return
                except ServiceError as error:
                    if error.status not in (400, 401):
                        raise
            verifier, challenge = pkce_pair()
            state = random_secrets.token_urlsafe(32)
            url = "https://accounts.spotify.com/authorize?" + urlencode({
                "client_id": self.client_id, "response_type": "code", "redirect_uri": REDIRECT_URI,
                "scope": SPOTIFY_SCOPE, "state": state, "code_challenge_method": "S256",
                "code_challenge": challenge,
            })
            code = receive_code(state, url, cancelled)
            if cancelled.is_set():
                raise InterruptedError("Authorization cancelled")
            data = request_json(self.client, "Spotify", "POST", "https://accounts.spotify.com/api/token",
                                data={"grant_type": "authorization_code", "client_id": self.client_id,
                                      "redirect_uri": REDIRECT_URI, "code": code, "code_verifier": verifier})
            self._save(data)

    def access_token(self, force_refresh=False):
        with self.lock:
            if not self.token:
                raise ServiceError("Spotify", "Connect Spotify before starting playback", fatal=True)
            if force_refresh or self.token.get("expires_at", 0) <= self.clock() + 60:
                old_refresh = self.token["refresh_token"]
                data = request_json(self.client, "Spotify", "POST", "https://accounts.spotify.com/api/token",
                                    data={"grant_type": "refresh_token", "client_id": self.client_id,
                                          "refresh_token": old_refresh})
                self._save(data, old_refresh)
            return self.token["access_token"]

    def close(self):
        self.client.close()
