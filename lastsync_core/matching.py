import re
from difflib import SequenceMatcher

from .models import Ranked, Resolution, normalize

AUTO_THRESHOLD = 0.91
AMBIGUITY_MARGIN = 0.045
VERSION_WORDS = {
    "live": r"\blive\b", "remix": r"\b(remix|mix)\b", "acoustic": r"\bacoustic\b",
    "instrumental": r"\binstrumental\b", "karaoke": r"\bkaraoke\b", "cover": r"\bcover\b",
    "demo": r"\bdemo\b", "sped up": r"\bsped\s*up\b", "slowed": r"\bslowed\b",
    "radio edit": r"\bradio\s*edit\b", "mono": r"\bmono\b",
}


def versions(title):
    # Restrict 'live' to version annotations; 'Live Forever' is a song title.
    annotated = " ".join(a or b for a, b in re.findall(r"\(([^)]*)\)|\[([^]]*)\]", title))
    annotated += " " + " ".join(re.split(r"\s[-–—]\s", title)[1:])
    marked = normalize(annotated)
    result = {name for name, pattern in VERSION_WORDS.items() if re.search(pattern, marked)}
    # Remix names frequently have no separating punctuation.
    for name in ("remix", "instrumental", "karaoke", "sped up", "slowed"):
        if re.search(VERSION_WORDS[name], normalize(title)):
            result.add(name)
    return result


def title_key(title):
    # Remaster years and featuring credits do not identify a different performance.
    title = re.sub(r"[\(\[][^)\]]*\b(?:remaster(?:ed)?|feat\.?|ft\.?)\b[^)\]]*[\)\]]", "", title,
                   flags=re.IGNORECASE)
    title = re.sub(r"\s[-–—]\s(?:\d{4}\s*)?remaster(?:ed)?(?:\s*\d{4})?.*$", "", title,
                   flags=re.IGNORECASE)
    return normalize(title)


def recording_versions(track):
    result = versions(track.title) | versions(track.album)
    if re.search(r"^live(?:$| (?:at|in|from)\b)", normalize(track.album)):
        result.add("live")
    return result


def similarity(a, b):
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def rank(source, candidate):
    if not candidate.playable:
        return Ranked(candidate, 0, "Unavailable for playback")
    target = candidate.track
    if source.isrc and target.isrc and source.isrc.upper() == target.isrc.upper():
        return Ranked(candidate, 1, "Exact ISRC")
    if recording_versions(source) != recording_versions(target):
        return Ranked(candidate, 0, "Different recording version")
    if versions(source.title) and title_key(source.title) != title_key(target.title):
        return Ranked(candidate, 0, "Different version annotation; confirm manually")
    title_score = similarity(title_key(source.title), title_key(target.title))
    artist_score = similarity(normalize(source.artist), normalize(target.artist))
    # An exact primary artist is allowed when Spotify adds collaborating artists.
    primary = normalize(target.artist.split(",")[0])
    if normalize(source.artist) == primary:
        artist_score = 1
    if title_score < 0.78 or artist_score < 0.72:
        return Ranked(candidate, 0, "Title or artist mismatch")
    weights = [(0.6, title_score), (0.35, artist_score)]
    if source.duration_ms and target.duration_ms:
        difference = abs(source.duration_ms - target.duration_ms)
        if difference > max(15000, source.duration_ms * 0.12):
            return Ranked(candidate, 0, "Duration differs substantially")
        weights.append((0.15, max(0, 1 - difference / 15000)))
    score = sum(w * s for w, s in weights) / sum(w for w, _ in weights)
    return Ranked(candidate, score, "Title, artist and recording version" +
                  (", with duration" if len(weights) == 3 else "; duration unavailable"))


class TrackResolver:
    def __init__(self, spotify, store):
        self.spotify, self.store = spotify, store

    def resolve(self, source):
        cached = self.store.get(source)
        if cached is not None:
            return cached
        ranked = tuple(sorted((rank(source, c) for c in self.spotify.search(source)),
                              key=lambda r: (-r.confidence, r.candidate.uri)))
        selected = None
        if ranked and ranked[0].confidence >= AUTO_THRESHOLD:
            top = ranked[0]
            # Duplicate releases with identical artist/title/duration are equivalent;
            # different recordings at similar confidence need an explicit choice.
            alternatives = [r for r in ranked[1:] if r.confidence >= top.confidence - AMBIGUITY_MARGIN
                            and not self.equivalent(top.candidate, r.candidate)]
            if not alternatives:
                selected = top.candidate
        result = Resolution(selected, ranked, ranked[0].confidence if ranked else 0)
        # Keep ambiguous candidates in memory for the correction dialog, not as a negative cache.
        if selected or not ranked:
            self.store.put(source, result)
        return result

    @staticmethod
    def equivalent(a, b):
        if a.track.isrc and b.track.isrc:
            return a.track.isrc.upper() == b.track.isrc.upper()
        return (a.track.key == b.track.key and a.track.duration_ms is not None
                and b.track.duration_ms is not None
                and abs(a.track.duration_ms - b.track.duration_ms) <= 1500)

    def correct(self, source, uri):
        candidate = self.spotify.track(uri)
        result = Resolution(candidate, (), 1, True)
        self.store.put(source, result)
        return result
