import asyncio
import hashlib

import httpx

from ..cache import DAY, Cache
from ..http import get_json
from . import coverart

BASE = "https://api.listenbrainz.org/1"
BATCH = 50
TAG_POOL = 40  # recordings fetched per genre lookup; one batched metadata request covers them all


def _track(mbid: str, meta: dict | None) -> dict | None:
    meta = meta or {}
    title = (meta.get("recording") or {}).get("name")
    if not title:
        return None
    release = meta.get("release") or {}
    release_id = release.get("caa_release_mbid") or release.get("mbid")
    return {
        "id": mbid,
        "title": title,
        "artist": (meta.get("artist") or {}).get("name", ""),
        "album": release.get("name"),
        "cover_url": coverart.for_release(release_id) if release_id else None,
    }


def _top_track(row: dict) -> dict | None:
    if not (row.get("recording_mbid") and row.get("recording_name")):
        return None
    release_id = row.get("caa_release_mbid") or row.get("release_mbid")
    return {
        "id": row["recording_mbid"],
        "title": row["recording_name"],
        "artist": row.get("artist_name", ""),
        "album": row.get("release_name"),
        "cover_url": coverart.for_release(release_id) if release_id else None,
        "listens": row.get("total_listen_count") or 0,
    }


def _votes(entries: list[dict] | None) -> list[dict]:
    """ListenBrainz tag entries `{tag, count}` -> the `{name, count}` votes `genres.blend` reads."""
    return [{"name": e["tag"], "count": e.get("count", 0)} for e in entries or [] if e.get("tag")]


class ListenBrainz:
    """Genre tags, tag votes and listen counts from open listening data (no API key)."""

    def __init__(self, http: httpx.AsyncClient, cache: Cache):
        self._http = http
        self._cache = cache

    async def tag_pool(self, tags: list[str], operator: str = "OR") -> list[str]:
        """Recording MBIDs carrying all ("AND") or any ("OR") of `tags` (lowercase). One request, no MusicBrainz."""
        key = f"lb1:tags:{operator}:" + "|".join(sorted(tags))
        return await self._cache.get_or_set(key, DAY, lambda: self._tag_pool(tags, operator))

    async def _tag_pool(self, tags: list[str], operator: str) -> list[str]:
        rows = await get_json(
            self._http, f"{BASE}/lb-radio/tags",
            tag=tags, operator=operator, pop_begin=0, pop_end=100, count=TAG_POOL,
        )
        return [r["recording_mbid"] for r in rows if r.get("recording_mbid")]

    async def profiles(self, mbids: list[str]) -> dict[str, dict]:
        """Per recording: `{"track": Track dict, "votes": (recording, release_group, artist)}`, the votes as
        `{name, count}` lists. One batched request for any number of recordings (up to BATCH)."""
        key = "lb1:prof:" + hashlib.sha1(",".join(mbids).encode()).hexdigest()
        return await self._cache.get_or_set(key, DAY, lambda: self._profiles(mbids))

    async def _profiles(self, mbids: list[str]) -> dict[str, dict]:
        meta = await get_json(
            self._http, f"{BASE}/metadata/recording/",
            body={"recording_mbids": mbids[:BATCH], "inc": "artist tag release"},  # inc: one string, not a list
        )
        out = {}
        for mbid in mbids[:BATCH]:
            m = meta.get(mbid)
            if not (track := _track(mbid, m)):
                continue
            tag = (m or {}).get("tag") or {}
            out[mbid] = {
                "track": track,
                "votes": (_votes(tag.get("recording")), _votes(tag.get("release_group")), _votes(tag.get("artist"))),
            }
        return out

    async def top_recordings(self, artist_mbid: str) -> list[dict]:
        """An artist's most-listened songs, best first."""
        return await self._cache.get_or_set(
            f"lb1:top:{artist_mbid}", DAY, lambda: self._top_recordings(artist_mbid)
        )

    async def _top_recordings(self, artist_mbid: str) -> list[dict]:
        rows = await get_json(self._http, f"{BASE}/popularity/top-recordings-for-artist/{artist_mbid}")
        return [t for r in rows if (t := _top_track(r))]

    async def listens(self, mbids: list[str]) -> dict[str, int]:
        """Total listen count per recording MBID (0 when unknown), in concurrent batches of BATCH."""
        parts = await asyncio.gather(*(
            self._cache.get_or_set("lb1:pop:" + ",".join(b), DAY, lambda b=b: self._listens(b))
            for b in (mbids[i:i + BATCH] for i in range(0, len(mbids), BATCH))
        ))
        return {k: v for part in parts for k, v in part.items()}

    async def _listens(self, mbids: list[str]) -> dict[str, int]:
        rows = await get_json(
            self._http, f"{BASE}/popularity/recording", body={"recording_mbids": mbids}
        )
        return {r["recording_mbid"]: r.get("total_listen_count") or 0 for r in rows}