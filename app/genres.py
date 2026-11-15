"""Tag cleaning and genre distribution. Pure functions, no I/O."""
import math
import re
from collections import defaultdict
from collections.abc import Iterable

MAX_GENRES = 6

_SEPARATORS = re.compile(r"[\s\-_./]+")
_WORD = re.compile(r"[\w']+")
_DECADE = re.compile(r"\d{2,4}s")

# Tags that describe the listener or the era rather than the music.
# Keys are compact (see _key).
_NOISE = frozenset({
    "favorite", "favorites", "favourite", "favourites", "favoritesongs", "good", "best",
    "bestof", "awesome", "love", "loved", "seenlive", "live", "cover", "mymusic",
    "albumsiown", "spotify", "femalevocalists", "femalevocalist", "malevocalists",
    "malevocalist", "english", "american", "british", "usa", "uk",
})

# Compact key -> canonical display name, for synonyms and spellings we want to merge.
_CANONICAL = {
    "hiphop": "Hip Hop",
    "rnb": "R&B", "r&b": "R&B", "randb": "R&B", "rhythmandblues": "R&B",
    "electronica": "Electronic",
    "synthpop": "Synthpop", "80ssynth": "Synthpop",
    "altrock": "Alternative Rock",
    "dnb": "Drum and Bass", "drumnbass": "Drum and Bass", "drum&bass": "Drum and Bass",
    "drumandbass": "Drum and Bass",
    "rocknroll": "Rock and Roll", "rockandroll": "Rock and Roll",
}

# Word-level Turkish names; anything not listed is kept as is.
_TR = {
    "electronic": "Elektronik", "classical": "Klasik", "jazz": "Caz",
    "alternative": "Alternatif", "experimental": "Deneysel", "instrumental": "Enstrümantal",
    "soundtrack": "Film Müziği", "dance": "Dans", "acoustic": "Akustik",
    "turkish": "Türk", "world": "Dünya", "vocal": "Vokal",
}


def _key(name: str) -> str:
    return _SEPARATORS.sub("", name.casefold())


def group_key(tag: str) -> str:
    """The key under which `blend` groups a tag: spellings and synonyms of one genre share it."""
    key = _key(tag)
    canonical = _CANONICAL.get(key)
    return _key(canonical) if canonical else key


def _title(name: str) -> str:
    return _WORD.sub(lambda m: m.group().capitalize(), name.casefold())


def _localize(name: str, lang: str) -> str:
    if lang != "tr":
        return name
    return _WORD.sub(lambda m: _TR.get(m.group().casefold(), m.group()), name)


def _shares(tags: Iterable[dict]) -> tuple[dict[str, float], dict[str, tuple[int, str]]]:
    """One layer of raw votes -> {group: share of this layer}, plus each group's best spelling."""
    votes: dict[str, int] = defaultdict(int)
    labels: dict[str, tuple[int, str]] = {}  # group -> (votes of best spelling, display)
    for tag in tags:
        name, count = tag.get("name", ""), tag.get("count", 0)
        key = _key(name)
        if count <= 0 or not key or len(key) > 40 or key in _NOISE or _DECADE.fullmatch(key):
            continue
        canonical = _CANONICAL.get(key)
        group = _key(canonical) if canonical else key
        votes[group] += count
        if count > labels.get(group, (0, ""))[0]:
            labels[group] = (count, canonical or _title(name))
    total = sum(votes.values())
    return {g: v / total for g, v in votes.items()}, labels


def strength(tags: Iterable[dict]) -> int:
    """Votes that survive tag cleaning: how much evidence one layer really has."""
    total = 0
    for tag in tags:
        key = _key(tag.get("name", ""))
        if tag.get("count", 0) > 0 and key and len(key) <= 40 and key not in _NOISE and not _DECADE.fullmatch(key):
            total += tag["count"]
    return total


def _blend(layers: Iterable[tuple[float, Iterable[dict]]], limit: int) -> list[tuple[str, str, float]]:
    """Weighted layers of votes -> [(group key, English label, share 0..1)], best first."""
    score: dict[str, float] = defaultdict(float)
    names: dict[str, tuple[int, str]] = {}
    for weight, tags in layers:
        shares, labels = _shares(tags)
        for group, share in shares.items():
            score[group] += weight * share
            if group not in names or labels[group][0] > names[group][0]:
                names[group] = labels[group]
    top = sorted(score.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    total = sum(v for _, v in top)
    return [(g, names[g][1], v / total) for g, v in top]


def blend(
    layers: Iterable[tuple[float, Iterable[dict]]], lang: str = "en", limit: int = MAX_GENRES
) -> list[dict]:
    """Merge weighted layers of MusicBrainz `{name, count}` votes into `[{name, percent, tag}]`.

    Each layer (recording, album, artist...) is normalised on its own, so an artist with
    thousands of votes cannot drown out a song with five. Spellings are merged on a
    separator-insensitive key ("synth-pop" == "synthpop"), noise is dropped, and percentages
    are relative to the genres kept. `tag` is the lowercase English name, which is what the
    genre-based recommendations send back (the displayed `name` may be translated).
    """
    return [
        {"name": _localize(label, lang), "percent": round(100 * share, 1), "tag": label.casefold()}
        for _, label, share in _blend(layers, limit)
    ]


def profile(layers: Iterable[tuple[float, Iterable[dict]]], limit: int = MAX_GENRES) -> dict[str, float]:
    """The same blend as `blend`, as {group key: share 0..1}: the form `similarity` compares."""
    return {g: share for g, _, share in _blend(layers, limit)}


RATIO = 0.5  # each rank counts half as much as the one before: weights 1, 0.5, 0.25, ...


def rank_weights(target: dict[str, float], first: str | None = None) -> dict[str, float]:
    """Importance of each of the target's genres by rank: the biggest share gets 1, the next RATIO,
    the next RATIO**2... `first` (a genre the person clicked) takes the top rank whatever its share."""
    order = sorted(target, key=lambda g: -target[g])
    if first in target:
        order.remove(first)
        order.insert(0, first)
    return {g: RATIO**k for k, g in enumerate(order)}


def similarity(target: dict[str, float], other: dict[str, float], weights: dict[str, float]) -> float:
    """Weighted Euclidean distance D = sum w*(A-B)**2, scaled to a score from 0 to 1:

        score = 1 - D / sum w*(A**2 + B**2) = 2*sum(w*A*B) / sum(w*(A**2 + B**2))

    1 means identical mixes, 0 means no genre in common. A gap in the target's top genre costs the
    most; later ranks only fine-tune. Genres the target does not have weigh less than its last rank."""
    tail = RATIO ** len(weights)
    num = den = 0.0
    for g in set(target) | set(other):
        w, a, b = weights.get(g, tail), target.get(g, 0.0), other.get(g, 0.0)
        num += w * a * b
        den += w * (a * a + b * b)
    return 2 * num / den if den else 0.0


def required_genres(target: dict[str, float], weights: dict[str, float], good: float) -> list[str]:
    """The target's genres a candidate must carry to be able to score at least `good`, most important first.

    A candidate that lacks genre g can at best mirror the rest of the target. With S = sum(w*A**2) over all
    genres and S' = S - w_g*A_g**2, the best such score is sqrt(S'/S) (scale the candidate to the target's own
    shape; this ignores that shares must sum to 1, so it is an upper bound). If that bound is below `good`,
    no candidate without g can be a good match, so g belongs in the search query. A balanced 50/50 target
    needs both genres; a 70/15/10/5 one needs only its first."""
    s = sum(weights[g] * target[g] ** 2 for g in target)
    if not s:
        return []
    order = sorted(target, key=lambda g: -weights[g])
    need = [g for g in order if math.sqrt(max(0.0, 1 - weights[g] * target[g] ** 2 / s)) < good]
    return need or order[:1]