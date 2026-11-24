"""Official service adapters. All calls execute in background workers."""
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import re

import httpx

from .errors import ServiceError
from .models import Candidate, Observation, Playback, Scrobble, Track

TIMEOUT = httpx.Timeout(8, connect=4)
TRACK_ID = re.compile(r"^[A-Za-z0-9]{22}$")


def retry_after(value):
    try:
        return max(0, float(value))
    except (ValueError, TypeError):
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0


def request_json(client, service, method, url, **kwargs):
    try:
        response = client.request(method, url, timeout=TIMEOUT, **kwargs)
    except httpx.RequestError:
        raise ServiceError(service, f"{service}: network unavailable; will retry",
                           uncertain=method != "GET") from None
    code = response.status_code
    if code >= 400:
        messages = {
            401: "authorization expired; reconnect in Settings",
            403: "access denied; check Premium, scopes and app allowlist",
            404: "resource or active playback device unavailable",
            429: "rate limited; waiting before retry",
        }
        detail = messages.get(code, "service unavailable" if code >= 500 else "request rejected")
        raise ServiceError(service, f"{service}: {detail}", status=code,
                           retry_after=retry_after(response.headers.get("Retry-After")),
                           fatal=code in (400, 401, 403),
                           uncertain=method != "GET" and code >= 500)
    if code == 204 or not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        raise ServiceError(service, f"{service}: invalid JSON response",
                           uncertain=method != "GET") from None


def text(value):
    return value.get("#text", value.get("name", "")) if isinstance(value, dict) else str(value or "")


def parse_recent(payload, observed_at):
    if not isinstance(payload, dict):
        raise ServiceError("Last.fm", "Last.fm: malformed recent-tracks response")
    if "error" in payload:
        code = str(payload["error"])
        raise ServiceError("Last.fm", {
            "6": "Last.fm: username not found or invalid request",
            "10": "Last.fm: invalid API key",
            "26": "Last.fm: API key suspended",
            "29": "Last.fm: rate limited",
        }.get(code, "Last.fm: service temporarily unavailable"),
            fatal=code in ("6", "10", "26"), retry_after=30 if code == "29" else 0)
    try:
        rows = payload["recenttracks"]["track"]
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            raise TypeError
        current, history = None, []
        for row in rows:
            track = Track(str(row["name"]), text(row["artist"]), text(row.get("album", "")))
            if not track.title.strip() or not track.artist.strip():
                raise ValueError
            if str(row.get("@attr", {}).get("nowplaying", "")).casefold() == "true":
                if current is None:
                    current = track
            elif row.get("date", {}).get("uts") is not None:
                history.append(Scrobble(track, int(row["date"]["uts"])))
        return Observation(current, tuple(history), observed_at)
    except (KeyError, ValueError, TypeError, AttributeError):
        raise ServiceError("Last.fm", "Last.fm: malformed recent-tracks response") from None


class LastFMClient:
    def __init__(self, api_key, client=None):
        self.api_key = api_key
        self.client = client or httpx.Client()

    def recent(self, username):
        payload = request_json(self.client, "Last.fm", "GET", "https://ws.audioscrobbler.com/2.0/",
                               params={"method": "user.getrecenttracks", "user": username,
                                       "api_key": self.api_key, "format": "json", "limit": 5})
        return parse_recent(payload, time.time())

    def close(self):
        self.client.close()


def parse_candidate(item):
    if not isinstance(item, dict):
        return None
    uri = item.get("uri", "")
    if not uri.startswith("spotify:track:") or not TRACK_ID.fullmatch(uri.split(":")[-1]):
        return None
    artists = item.get("artists") or []
    if not item.get("name") or not artists:
        return None
    images = item.get("album", {}).get("images") or []
    image = next((i.get("url", "") for i in reversed(images) if i.get("url", "").startswith("https://")), "")
    duration = item.get("duration_ms")
    return Candidate(uri, Track(item["name"], ", ".join(a.get("name", "") for a in artists),
                               item.get("album", {}).get("name", ""),
                               duration if isinstance(duration, int) and duration > 0 else None,
                               item.get("external_ids", {}).get("isrc", "")), image,
                     item.get("is_playable") is not False and not item.get("is_local", False))


class SpotifyClient:
    def __init__(self, auth, client=None, clock=time.monotonic):
        self.auth = auth
        self.client = client or httpx.Client()
        self.clock = clock

    def request(self, method, path, **kwargs):
        for attempt in range(2):
            token = self.auth.access_token(force_refresh=bool(attempt))
            try:
                return request_json(self.client, "Spotify", method,
                                    "https://api.spotify.com/v1/" + path,
                                    headers={"Authorization": "Bearer " + token}, **kwargs)
            except ServiceError as error:
                if error.status != 401 or attempt:
                    raise

    def search(self, source):
        queries = []
        if source.isrc:
            queries.append("isrc:" + source.isrc)
        # Quotes in user metadata must not alter the query's field structure.
        clean = lambda s: s.replace('"', ' ').strip()
        queries += [f'track:"{clean(source.title)}" artist:"{clean(source.artist)}"',
                    f'{clean(source.title)} {clean(source.artist)}']
        found = {}
        for query in queries:
            data = self.request("GET", "search", params={"q": query, "type": "track", "limit": 10})
            try:
                for item in data["tracks"]["items"]:
                    candidate = parse_candidate(item)
                    if candidate:
                        found[candidate.uri] = candidate
            except (KeyError, TypeError):
                raise ServiceError("Spotify", "Spotify: malformed search response") from None
        return list(found.values())

    def track(self, value):
        value = value.strip().split("?")[0].rstrip("/").split("/")[-1].split(":")[-1]
        if not TRACK_ID.fullmatch(value):
            raise ValueError("Use a Spotify track link or spotify:track URI")
        candidate = parse_candidate(self.request("GET", "tracks/" + value))
        if not candidate or not candidate.playable:
            raise ValueError("This Spotify track is unavailable for playback")
        return candidate

    def playback(self):
        data = self.request("GET", "me/player")
        if not data:
            return None
        try:
            item = data.get("item") or {}
            return Playback(item.get("uri", ""), int(data.get("progress_ms") or 0),
                            int(item.get("duration_ms") or 0), bool(data.get("is_playing")),
                            (data.get("device") or {}).get("id") or "", self.clock())
        except (TypeError, ValueError, AttributeError):
            raise ServiceError("Spotify", "Spotify: malformed playback response") from None

    def active_device(self):
        data = self.request("GET", "me/player/devices") or {}
        return next((d["id"] for d in data.get("devices", [])
                     if d.get("is_active") and not d.get("is_restricted") and d.get("id")), None)

    def start(self, uri, device_id):
        # Last.fm supplies no reliable live position. Track Follow starts at zero.
        self.request("PUT", "me/player/play", params={"device_id": device_id},
                     json={"uris": [uri], "position_ms": 0})

    def close(self):
        self.client.close()
