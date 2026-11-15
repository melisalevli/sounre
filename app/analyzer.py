"""Orchestrates MusicBrainz, ListenBrainz and (optionally) Spotify into search and analysis results."""
import asyncio
import logging
from collections import defaultdict
from urllib.parse import quote

from .clients.itunes import ITunes
from .clients.listenbrainz import ListenBrainz
from .clients.musicbrainz import MusicBrainz
from .clients.spotify import Spotify
from .errors import AppError
from .genres import blend, group_key, profile, rank_weights, required_genres, similarity, strength

log = logging.getLogger(__name__)
MAX_RESULTS = 8
MAX_RECOMMENDATIONS = 5
# Most specific votes count most: recording, then artist (the album layer was dropped to save a request).
LAYERS = (("recording", 3.0), ("artist", 1.0))
# A recording with this many (cleaned) votes is evidence enough on its own: the artist lookup
# (one rate-limited MusicBrainz request) is skipped. None disables the shortcut.
RICH_VOTES: int | None = 5
# Genre-based recommendations (see `similar_by_mix`, `top_by_genre`); tuning knobs.
POOL_TAGS = 3        # the song's strongest genres used to collect candidates
MIN_LISTENS = 20     # candidates almost nobody has played are usually junk
MIN_EVIDENCE = 3     # tag votes a candidate needs before its genre mix means anything
GOOD_SIM = 0.85      # "find similar": a genre is required in the query when a song without it cannot reach this score
MIN_POOL = 10        # fewer candidates than this: relax the query (drop the least important required genre)
MIN_SIM = 0.4        # below this the genre mixes have little in common: better no suggestion than a wrong one


def _sig(t: dict) -> tuple[str, str]:
    """Same song, whichever recording/release it came from."""
    return "".join(c for c in t["title"].casefold() if c.isalnum()), t["artist"].casefold()


def _unique(tracks: list[dict]) -> list[dict]:
    seen, out = set(), []
    for t in tracks:
        if (sig := _sig(t)) not in seen:
            seen.add(sig)
            out.append(t)
    return out


def rank(tracks: list[dict], listens: dict[str, int]) -> list[dict]:
    """Most popular song first, one entry per song.

    A song is usually many recordings (album, single, live, remaster) and listens are split
    between them, so songs are ranked by the *sum* over their recordings, and each song is shown
    as its own most-listened recording. Search relevance only breaks ties (e.g. no listen data).
    """
    return [t for _, t in ranked(tracks, listens)]


def ranked(tracks: list[dict], listens: dict[str, int]) -> list[tuple[int, dict]]:
    """`rank` with each song's total listens kept, so lists from different sources can be merged by popularity."""
    total: dict[tuple, int] = defaultdict(int)
    for t in tracks:
        total[_sig(t)] += listens.get(t["id"], 0)
    ordered = sorted(
        tracks, key=lambda t: (total[_sig(t)], listens.get(t["id"], 0), t.get("score", 0)), reverse=True
    )
    return [(total[_sig(t)], t) for t in _unique(ordered)]


async def _listens(lb: ListenBrainz, tracks: list[dict]) -> dict[str, int]:
    try:
        return await lb.listens([t["id"] for t in tracks])
    except AppError:
        return {}


async def _top_songs(lb: ListenBrainz, artist_id: str | None) -> list[dict]:
    if not artist_id:
        return []
    try:
        return await lb.top_recordings(artist_id)
    except AppError as exc:
        log.warning("artist top songs failed for %s: %s", artist_id, exc.key)
        return []


async def _artist_songs(mb: MusicBrainz, lb: ListenBrainz, query: str) -> list[tuple[int, dict]]:
    """What artist mode shows: the artist's most-listened songs, with their listen totals."""
    try:
        artist_id = await mb.artist(query)
    except AppError as exc:
        log.warning("artist lookup failed for %r: %s", query, exc.key)
        artist_id = None
    top = await _top_songs(lb, artist_id)
    if top:
        return ranked(top, {t["id"]: t.get("listens", 0) for t in top})
    try:  # same fallback as artist mode: recordings credited to the artist, ranked by listens
        tracks = await mb.recordings(query, "artist")
    except AppError:
        return []
    return ranked(tracks, await _listens(lb, tracks))


async def _title_songs(mb: MusicBrainz, lb: ListenBrainz, query: str) -> list[tuple[int, dict]]:
    """What song mode shows: songs titled like the query (several words: each in title or artist)."""
    try:
        tracks = await mb.recordings(query, "song" if len(query.split()) == 1 else "any")
    except AppError:
        return []
    return ranked(tracks, await _listens(lb, tracks))


async def search(mb: MusicBrainz, lb: ListenBrainz, query: str, kind: str = "song") -> list[dict]:
    """kind="artist": that artist's most-listened songs. kind="song": songs titled like the query.
    kind="all": the artist-mode list and the song-mode list merged (one entry per song) and
    ranked by listens. Always most-listened first.

    Costs: song = one MusicBrainz request plus ListenBrainz; artist = one MusicBrainz request (the artist)
    plus one ListenBrainz request; all = both of those side by side (two MusicBrainz requests)."""
    if kind == "all":
        by_artist, by_title = await asyncio.gather(_artist_songs(mb, lb, query), _title_songs(mb, lb, query))
        best: dict[tuple, tuple[int, dict]] = {}  # one entry per song: the higher of its two scores
        for score, t in by_artist + by_title:
            if _sig(t) not in best or score > best[_sig(t)][0]:
                best[_sig(t)] = (score, t)
        merged = sorted(best.values(), key=lambda p: p[0], reverse=True)
        return [t for _, t in merged][:MAX_RESULTS]

    if kind == "artist":
        try:
            if artist_id := await mb.artist(query):
                if top := await lb.top_recordings(artist_id):
                    # same ranking as song search: listens of one song's recordings are summed
                    return rank(top, {t["id"]: t.get("listens", 0) for t in top})[:MAX_RESULTS]
        except AppError:  # the shortcut is optional: fall back to ranking recordings credited to the artist
            pass

    if kind == "song" and len(query.split()) > 1:
        kind = "any"  # some words probably name the artist: skip the narrow search that would come back thin
    tracks = await mb.recordings(query, kind)
    if kind == "song" and not tracks:
        tracks = await mb.recordings(query, "any")
    return rank(tracks, await _listens(lb, tracks))[:MAX_RESULTS]


async def analyze(
    mb: MusicBrainz, it: ITunes, sp: Spotify | None, mbid: str, lang: str
) -> dict:
    """Genres, cover, Spotify link and preview. Recommendations are deliberately not part of this:
    ListenBrainz radio is the slowest upstream call and would hold the genres back (see `similar`)."""
    rec = await mb.recording(mbid)  # often already cached by the search that listed this song
    (source, genres), spotify_url, apple = await asyncio.gather(
        _genres(mb, rec, lang), _spotify_url(sp, rec), _apple(it, rec)
    )
    return {
        "genres": genres,
        "source": source,
        "cover_url": rec["cover_url"],
        "spotify_url": spotify_url,
        "youtube_url": "https://www.youtube.com/results?search_query=" + quote(f"{rec['title']} {rec['artist']}"),
        **apple,
    }


async def _soft(lookup) -> list[dict]:
    """Artist votes only refine the result, so their failure must not fail the analysis."""
    try:
        return await lookup
    except AppError:
        return []


async def _genres(mb: MusicBrainz, rec: dict, lang: str) -> tuple[str | None, list[dict]]:
    """Blend recording and artist votes, so songs with few or no tags still get percentages."""
    if RICH_VOTES is not None and strength(rec["tags"]) >= RICH_VOTES:
        artist = []  # well-tagged recording: no further MusicBrainz request
    elif rec["artist_id"]:
        artist = await _soft(mb.artist_tags(rec["artist_id"]))
    else:
        artist = []
    votes = (rec["tags"], artist)
    genres = blend([(w, v) for (_, w), v in zip(LAYERS, votes)], lang)
    source = next((name for (name, _), v in zip(LAYERS, votes) if blend([(1, v)])), None)
    return source, genres


async def _spotify_url(sp: Spotify | None, rec: dict) -> str | None:
    """Exact link if Spotify has the song, None if it does not. Without credentials, a search link."""
    search_url = "https://open.spotify.com/search/" + quote(f"{rec['title']} {rec['artist']}")
    if sp is None:
        return search_url
    try:
        return await sp.track_url(rec["title"], rec["artist"])
    except AppError:  # Spotify down or rate-limited: do not claim the song is missing
        return search_url


async def _apple(it: ITunes, rec: dict) -> dict:
    """Preview, Apple Music link and backup cover, best effort: a failure means no play button, a search link, no backup."""
    try:
        found = await it.find(rec["title"], rec["artist"])
    except AppError:
        found = {}
    return {
        "preview_url": found.get("preview_url"),
        "cover_backup_url": found.get("cover_backup_url"),
        "apple_music_url": found.get("apple_music_url")
        or "https://music.apple.com/search?term=" + quote(f"{rec['title']} {rec['artist']}"),
    }


async def _pool(lb: ListenBrainz, mbid: str, tags: list[str], operator: str = "OR") -> list[str]:
    try:
        return [i for i in await lb.tag_pool(tags, operator) if i != mbid]
    except AppError as exc:
        log.warning("genre pool failed for %s %s: %s", operator, tags, exc.key)
        return []


async def _candidates(lb: ListenBrainz, mbid: str, pairs: list[tuple[str, float]], target: dict[str, float],
                      weights: dict[str, float]) -> list[str]:
    """Candidate recordings for "find similar", chosen by the same math that scores them (see
    `genres.required_genres`): the query asks for the genres a good match cannot lack (AND), so the
    candidates are already close. If that returns too few, the least important required genre is dropped
    and it is asked again. Last resort: any of the strongest genres (OR)."""
    tag_of: dict[str, str] = {}
    for tag, _ in sorted(pairs, key=lambda p: -p[1]):
        tag_of.setdefault(group_key(tag), tag.casefold())
    need = required_genres(target, weights, GOOD_SIM)
    steps = [([tag_of[g] for g in need[:k]], "AND") for k in range(len(need), 0, -1)]
    steps.append(([t for t, _ in sorted(pairs, key=lambda p: -p[1])[:POOL_TAGS]], "OR"))
    best: list[str] = []
    for tags, operator in steps:
        ids = await _pool(lb, mbid, tags, operator)
        if len(ids) >= MIN_POOL:
            return ids
        if len(ids) > len(best):
            best = ids
    return best


async def _mixes(lb: ListenBrainz, ids: list[str]) -> list[tuple[dict, dict[str, float], int]]:
    """(track, genre mix, tag votes) of every usable candidate. Listen counts only filter out junk
    recordings (MIN_LISTENS); they never take part in any ordering.

    The mix is built exactly like the pie chart (`_genres`): recording votes (weight 3) plus artist votes
    (weight 1), the artist skipped when the recording alone has RICH_VOTES. So the percentages used for
    ranking are the ones the person sees when opening that song. The album layer is deliberately left out:
    the pie chart does not use it, and a song that is 100 % alternative rock on screen must not rank as an
    indie folk song because its album carries that tag."""
    async def profiles() -> dict[str, dict]:
        try:
            return await lb.profiles(ids)
        except AppError:
            return {}

    async def listens() -> dict[str, int]:
        try:
            return await lb.listens(ids)
        except AppError:
            return {}

    found, plays = await asyncio.gather(profiles(), listens())
    out = []
    for i in ids:
        if not (p := found.get(i)):
            continue
        recording, _, artist_votes = p["votes"]
        rich = RICH_VOTES is not None and strength(recording) >= RICH_VOTES
        votes = (recording, [] if rich else artist_votes)
        evidence = sum(strength(v) for v in votes)
        if plays.get(i, 0) < MIN_LISTENS or evidence < MIN_EVIDENCE:
            continue
        if mix := profile([(w, v) for (_, w), v in zip(LAYERS, votes)]):
            out.append((p["track"], mix, evidence))
    return out


def _finish(tracks: list[dict], title: str, artist: str) -> list[dict]:
    """Drop the song itself and duplicate recordings of one song (the first, i.e. best, one is kept)."""
    me = _sig({"title": title, "artist": artist}) if title else None
    return _unique([t for t in tracks if _sig(t) != me])[:MAX_RECOMMENDATIONS]


async def similar_by_mix(
    lb: ListenBrainz, mbid: str, genres: list[tuple[str, float]], title: str = "", artist: str = ""
) -> list[dict]:
    """"Find similar" button. Input: the song's genre percentages. Candidates' genre mixes are compared with
    the song's by `genres.similarity` (rank-weighted: the biggest genre counts most, each next one half as
    much) and listed most similar first. Nothing else takes part in the order."""
    mix: dict[str, float] = defaultdict(float)
    for tag, pct in genres:
        mix[group_key(tag)] += pct
    total = sum(mix.values())
    if not total:
        return []
    target = {g: v / total for g, v in mix.items()}
    weights = rank_weights(target)
    ids = await _candidates(lb, mbid, genres, target, weights)
    if not ids:
        return []
    scored = [(similarity(target, m, weights), t) for t, m, _ in await _mixes(lb, ids)]
    scored = [x for x in scored if x[0] >= MIN_SIM]
    scored.sort(key=lambda x: x[0], reverse=True)
    return _finish([t for _, t in scored], title, artist)


async def top_by_genre(lb: ListenBrainz, mbid: str, genre: str, title: str = "", artist: str = "") -> list[dict]:
    """A clicked genre. Input: that genre only. Songs where it has the biggest share come first (a song
    that is 100 % pop before one that is 60 % pop). Equal shares: more tag votes first, so the better
    documented song wins; listen counts play no part."""
    key = group_key(genre)
    ids = await _pool(lb, mbid, [genre.casefold()])
    if not ids:
        return []
    ranked_ = [(m[key], votes, t) for t, m, votes in await _mixes(lb, ids) if m.get(key, 0) > 0]
    ranked_.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return _finish([t for _, _, t in ranked_], title, artist)