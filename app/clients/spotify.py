import logging

import httpx

from ..cache import WEEK, Cache
from ..errors import AppError
from ..http import get_json
from .match import same_song

log = logging.getLogger(__name__)
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"


def _plain(text: str) -> str:
    return text.replace('"', " ")


class Spotify:
    """Optional: confirms a song exists on Spotify and returns its exact link.

    Client-credentials flow, so visitors never log in. One lookup per analysed song (cached).
    """

    def __init__(self, http: httpx.AsyncClient, cache: Cache, client_id: str, client_secret: str):
        self._http = http
        self._cache = cache
        self._auth = (client_id, client_secret)

    async def track_url(self, title: str, artist: str) -> str | None:
        key = f"sp1:{title}|{artist}".casefold()
        found = await self._cache.get_or_set(key, WEEK, lambda: self._find(title, artist))
        return found or None  # "" is cached as "not on Spotify"

    async def _token(self) -> str:
        return await self._cache.get_or_set("sp1:token", 3000, self._fetch_token)  # lives 3600 s

    async def _fetch_token(self) -> str:
        try:
            resp = await self._http.post(TOKEN_URL, data={"grant_type": "client_credentials"}, auth=self._auth)
        except httpx.HTTPError as exc:
            log.warning("spotify token error: %r", exc)
            raise AppError(502, "upstream") from exc
        if resp.is_error:
            log.warning("spotify token returned %s", resp.status_code)
            raise AppError(502, "upstream")
        return resp.json()["access_token"]

    async def _find(self, title: str, artist: str) -> str:
        data = await get_json(
            self._http, f"{API}/search",
            headers={"Authorization": f"Bearer {await self._token()}"},
            q=f'track:"{_plain(title)}" artist:"{_plain(artist)}"', type="track", limit=5,
        )
        for item in data.get("tracks", {}).get("items", []):
            if same_song(title, artist, item["name"], [a["name"] for a in item["artists"]]):
                return item["external_urls"]["spotify"]
        return ""