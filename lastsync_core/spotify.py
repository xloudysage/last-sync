"""Authorized Spotify catalog and playback operations."""
import re
import time

import httpx

from .errors import ServiceError
from .models import Candidate, Device, Playback, Track
from .transport import request_json

TRACK_ID = re.compile(r"^[A-Za-z0-9]{22}$")


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
        self.preferred_device = None
        self.catalog_cache = None

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
            data = self.catalog_cache.cached_search(query) if self.catalog_cache else None
            fresh = data is None
            if fresh:
                data = self.request("GET", "search", params={"q": query, "type": "track", "limit": 10})
            try:
                items = data["tracks"]["items"]
                if not isinstance(items, list):
                    raise TypeError
                for item in items:
                    candidate = parse_candidate(item)
                    if candidate:
                        found[candidate.uri] = candidate
            except (KeyError, TypeError, AttributeError):
                raise ServiceError("Spotify", "Spotify: malformed search response") from None
            if fresh and self.catalog_cache:
                self.catalog_cache.cache_search(query, {"tracks": {"items": items}})
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

    def devices(self):
        data = self.request("GET", "me/player/devices")
        if not isinstance(data, dict) or not isinstance(data.get("devices"), list):
            raise ServiceError("Spotify", "Spotify returned an invalid device list")
        return [Device(d.get("id") or "", d.get("name") or "Unnamed device",
                       d.get("type") or "Unknown", d.get("is_active") is True,
                       d.get("is_restricted") is not False)
                for d in data["devices"] if isinstance(d, dict)]

    def active_device(self):
        for device in self.devices():
            if not device.id or device.restricted:
                continue
            if device.id == self.preferred_device or (self.preferred_device is None and device.active):
                return device.id
        return None

    def start(self, uri, device_id):
        # Last.fm supplies no reliable live position. Track Follow starts at zero.
        self.request("PUT", "me/player/play", params={"device_id": device_id},
                     json={"uris": [uri], "position_ms": 0})

    def close(self):
        self.client.close()
