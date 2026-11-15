import httpx

from ..cache import WEEK, Cache
from ..http import get_json
from .match import same_song

URL = "https://itunes.apple.com/search"


class ITunes:
    """30-second previews and Apple Music links from the free iTunes Search API (no key, no login)."""

    def __init__(self, http: httpx.AsyncClient, cache: Cache):
        self._http = http
        self._cache = cache

    async def find(self, title: str, artist: str) -> dict[str, str | None]:
        """`{"preview_url", "apple_music_url", "cover_backup_url"}`, each None when Apple has no match. One request for all."""
        found = await self._cache.get_or_set(
            f"it3:{title}|{artist}".casefold(), WEEK, lambda: self._find(title, artist)
        )
        return {k: v or None for k, v in found.items()}  # "" is cached as "not found"

    async def _find(self, title: str, artist: str) -> dict[str, str]:
        data = await get_json(self._http, URL, term=f"{title} {artist}", media="music", entity="song", limit=5)
        preview = link = art = ""
        for hit in data.get("results", []):
            if same_song(title, artist, hit.get("trackName", ""), [hit.get("artistName", "")]):
                link = link or hit.get("trackViewUrl", "")
                preview = preview or hit.get("previewUrl", "")
                art = art or hit.get("artworkUrl100", "").replace("100x100bb", "600x600bb")  # same file, bigger
        return {"preview_url": preview, "apple_music_url": link, "cover_backup_url": art}