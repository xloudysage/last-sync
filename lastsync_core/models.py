from dataclasses import dataclass, field
from enum import Enum
import unicodedata
import re


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.sub(r"[^\w\s]", " ", value).split())


@dataclass(frozen=True)
class Track:
    title: str
    artist: str
    album: str = ""
    duration_ms: int | None = None
    isrc: str = ""
    artwork: str = field(default="", compare=False)

    @property
    def key(self):
        return normalize(self.title), normalize(self.artist)


@dataclass(frozen=True)
class Scrobble:
    track: Track
    timestamp: int  # Last.fm's timestamp, never an observation timestamp.


@dataclass(frozen=True)
class Observation:
    current: Track | None
    history: tuple[Scrobble, ...]
    observed_at: float


@dataclass(frozen=True)
class Candidate:
    uri: str
    track: Track
    artwork: str = ""
    playable: bool = True


@dataclass(frozen=True)
class Ranked:
    candidate: Candidate
    confidence: float
    reason: str


@dataclass(frozen=True)
class Resolution:
    selected: Candidate | None
    candidates: tuple[Ranked, ...]
    confidence: float = 0
    manual: bool = False


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    type: str
    active: bool
    restricted: bool


@dataclass(frozen=True)
class Playback:
    uri: str
    position_ms: int
    duration_ms: int
    playing: bool
    device_id: str
    observed_monotonic: float


class SyncState(str, Enum):
    IDLE = "IDLE"
    DETECTING = "DETECTING"
    RESOLVING = "RESOLVING"
    STARTING = "STARTING"
    FOLLOWING = "FOLLOWING"
    RECOVERING = "RECOVERING"
    STOPPED = "STOPPED"
