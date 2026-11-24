"""Last.fm reported listening activity."""
import time
from urllib.parse import urlsplit

import httpx

from .errors import ServiceError
from .models import Observation, Scrobble, Track
from .transport import request_json


def text(value):
    return value.get("#text", value.get("name", "")) if isinstance(value, dict) else str(value or "")


def image_url(images):
    if isinstance(images, list):
        for image in reversed(images):
            url = image.get("#text") if isinstance(image, dict) else None
            if isinstance(url, str) and url.startswith("https://"):
                try:
                    if urlsplit(url).hostname in {"lastfm.freetls.fastly.net", "userserve-ak.last.fm"}:
                        return url
                except ValueError:
                    pass
    return ""


def check_response(payload):
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
            fatal=code in ("6", "10", "26"), status=429 if code == "29" else None,
            retry_after=30 if code == "29" else 0)


def parse_recent(payload, observed_at):
    check_response(payload)
    try:
        rows = payload["recenttracks"]["track"]
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            raise TypeError
        current, history = None, []
        for row in rows:
            track = Track(str(row["name"]), text(row["artist"]), text(row.get("album", "")),
                          artwork=image_url(row.get("image")))
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

    def avatar(self, username):
        payload = request_json(self.client, "Last.fm", "GET", "https://ws.audioscrobbler.com/2.0/",
                               params={"method": "user.getinfo", "user": username,
                                       "api_key": self.api_key, "format": "json"})
        check_response(payload)
        return image_url((payload.get("user") or {}).get("image"))

    def close(self):
        self.client.close()
