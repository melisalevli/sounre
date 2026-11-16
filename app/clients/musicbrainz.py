import re

import httpx

from ..cache import DAY, WEEK, Cache
from ..http import get_json
from ..ratelimit import RateLimiter
from . import coverart

BASE = "https://musicbrainz.org/ws/2"


def _phrase(text: str) -> str:
    """Quote text for a Lucene query so operators and special characters stay literal."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _credit(credit: list[dict]) -> str:
    return "".join(c.get("name", "") + c.get("joinphrase", "") for c in credit)


# Releases that are not music: interviews, audiobooks, spoken word, DJ sets, demos...
_NOT_MUSIC = {"Spokenword", "Interview", "Audiobook", "Audio drama", "DJ-mix", "Mixtape/Street", "Demo"}
_MUSIC_TYPES = {None, "Album", "Single", "EP"}  # None: the field is absent on lookups

# Recordings that are not the real song: karaoke, tribute and backing-track versions.
_JUNK = re.compile(r"karaoke|tribute|made famous|originally performed|in the style of|backing track", re.I)
MIN_MS, MAX_MS = 45_000, 20 * 60_000  # drops skits/intros and long DJ sets

# Narrows the search on MusicBrainz's side. If searches ever come back empty, remove the
# `primarytype` part first; `_is_song` below still filters on our side either way.
_SONG_ONLY = " AND video:false AND status:official AND primarytype:(album OR single OR ep)"


def _is_music(release: dict) -> bool:
    group = release.get("release-group", {})
    return group.get("primary-type") in _MUSIC_TYPES and not _NOT_MUSIC & set(
        group.get("secondary-types") or []
    )


def _best_release(rec: dict) -> dict | None:
    """First music release, preferring plain albums/singles over live and compilation ones."""
    music = [r for r in rec.get("releases", []) if _is_music(r)]
    return min(music, key=lambda r: len(r.get("release-group", {}).get("secondary-types") or []), default=None)


def _is_song(rec: dict) -> bool:
    """A real song: not a video, not skit/DJ-set length, not karaoke or tribute, on a music release."""
    length = rec.get("length")
    if rec.get("video") or (length and not MIN_MS <= length <= MAX_MS):
        return False
    if _JUNK.search(f"{rec.get('title', '')} {_credit(rec.get('artist-credit', []))}"):
        return False
    return _best_release(rec) is not None


def _cover(release: dict) -> str | None:
    if group := release.get("release-group", {}).get("id"):
        return coverart.for_release_group(group)
    if release.get("id"):
        return coverart.for_release(release["id"])
    return None


def _track(rec: dict) -> dict:
    release = _best_release(rec) or (rec.get("releases") or [{}])[0]
    return {
        "id": rec["id"],
        "title": rec["title"],
        "artist": _credit(rec.get("artist-credit", [])),
        "album": release.get("title"),
        "cover_url": _cover(release),
    }


def _tags(entity: dict) -> list[dict]:
    # Genres are the curated subset of tags; prefer them, fall back to raw tags.
    return entity.get("genres") or entity.get("tags") or []


def _lookup_shape(rec: dict) -> dict:
    """What `MusicBrainz.recording()` returns, built from a recording JSON (a lookup or a search hit)."""
    credit = (rec.get("artist-credit") or [{}])[0]
    release = (rec.get("releases") or [{}])[0]
    return {
        **_track(rec),
        "artist_id": credit.get("artist", {}).get("id"),
        "release_id": release.get("id"),
        "tags": _tags(rec),
    }


class MusicBrainz:
    """Cached, rate-limited MusicBrainz client. Methods return plain JSON-able dicts."""

    def __init__(self, http: httpx.AsyncClient, cache: Cache, interval: float):
        self._http = http
        self._cache = cache
        self._limiter = RateLimiter(interval)

    async def _get(self, path: str, **params):
        await self._limiter.wait()
        return await get_json(self._http, f"{BASE}/{path}", fmt="json", **params)

    async def artist(self, name: str) -> str | None:
        """MBID of the artist `name` refers to (exact name preferred), or None."""
        found = await self.artist_candidates(name)
        return found[0] if found else None

    async def artist_candidates(self, name: str, limit: int = 3) -> list[str]:
        """MBIDs of the artists `name` may refer to, best name match first (several artists can share one name,
        so the caller can pick the one people actually listen to)."""
        n = name.strip()
        found = await self._cache.get_or_set(
            f"mb3:artists:{n.casefold()}", WEEK, lambda: self._artists(n)
        )
        return found[:limit]

    async def _artists(self, name: str) -> list[str]:
        data = await self._get("artist", query=f"artist:{_phrase(name)}", limit=10)
        hits = [h for h in data.get("artists", []) if h.get("score", 0) >= 75]
        exact = [h for h in hits if h["score"] >= 90 and h["name"].casefold() == name.casefold()]
        return [h["id"] for h in (exact or hits)]

    async def recordings(self, query: str, kind: str = "song", limit: int = 100) -> list[dict]:
        """Songs containing every word of `query` in the title (kind="song"), the artist
        (kind="artist"), or each word in either one (kind="any", e.g. "adele hello").

        Narrowing to one field makes the Lucene query smaller and the hits more relevant.
        Non-songs (videos, spoken word, karaoke, skits, DJ sets) are filtered out. Results are unranked."""
        q = query.strip()
        return await self._cache.get_or_set(
            f"mb6:search:{kind}:{q.casefold()}:{limit}", DAY, lambda: self._recordings(q, kind, limit)
        )

    async def _recordings(self, q: str, kind: str, limit: int) -> list[dict]:
        if kind == "any":
            lucene = " AND ".join(f"(recording:{_phrase(w)} OR artist:{_phrase(w)})" for w in q.split())
        else:
            field = "artist" if kind == "artist" else "recording"
            lucene = " AND ".join(f"{field}:{_phrase(w)}" for w in q.split())
        data = await self._get("recording", query=f"({lucene}){_SONG_ONLY}", limit=limit)
        songs = []
        for rec in data.get("recordings", []):
            if not _is_song(rec):
                continue
            if "tags" in rec:  # the hit already carries the votes: /analyze and /similar can skip their own lookup
                self._cache.put(f"mb1:rec:{rec['id']}", _lookup_shape(rec), WEEK)
            credit = (rec.get("artist-credit") or [{}])[0]
            songs.append({
                **_track(rec),
                "score": rec.get("score", 0),
                "artist_id": credit.get("artist", {}).get("id"),  # lets "all" find the named artist without another search
            })
        return songs

    async def recording(self, mbid: str) -> dict:
        return await self._cache.get_or_set(
            f"mb1:rec:{mbid}", WEEK, lambda: self._recording(mbid)
        )

    async def _recording(self, mbid: str) -> dict:
        # `inc` is space-separated: httpx encodes a space as "+", which is what MB expects.
        rec = await self._get(f"recording/{mbid}", inc="artist-credits releases genres tags")
        return _lookup_shape(rec)

    async def artist_tags(self, artist_id: str) -> list[dict]:
        return await self._cache.get_or_set(
            f"mb1:artist-tags:{artist_id}", WEEK, lambda: self._artist_tags(artist_id)
        )

    async def _artist_tags(self, artist_id: str) -> list[dict]:
        return _tags(await self._get(f"artist/{artist_id}", inc="genres tags"))