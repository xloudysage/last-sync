"""Compatibility imports for v1.1 service clients."""
from .lastfm import LastFMClient, parse_recent
from .spotify import SpotifyClient, parse_candidate
from .transport import TIMEOUT, request_json, retry_after
