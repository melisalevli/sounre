from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from ..analyzer import analyze, search, similar_by_mix, top_by_genre
from ..clients.itunes import ITunes
from ..clients.listenbrainz import ListenBrainz
from ..clients.musicbrainz import MusicBrainz
from ..clients.spotify import Spotify
from ..errors import AppError
from ..schemas import AnalyzeResponse, CoverResponse, SearchResponse, SimilarResponse
from .guard import until_disconnect

router = APIRouter(prefix="/api/v1")

# The browser keeps answers for a while, so going back to a song or re-running a search costs no request at all
# (the server cache then only serves what the browser has not seen). Empty answers are not cached: they are
# usually "an upstream hiccup" or "nothing found yet", and both deserve another try.
SEARCH_TTL, ANALYZE_TTL, SIMILAR_TTL = 300, 600, 600


def _browser_cache(response: Response, seconds: int, worth_keeping: bool) -> None:
    if worth_keeping:
        response.headers["Cache-Control"] = f"public, max-age={seconds}"


def _mb(request: Request) -> MusicBrainz:
    return request.app.state.mb


def _lb(request: Request) -> ListenBrainz:
    return request.app.state.lb


def _it(request: Request) -> ITunes:
    return request.app.state.it


def _sp(request: Request) -> Spotify | None:
    return request.app.state.sp


MB = Annotated[MusicBrainz, Depends(_mb)]
LB = Annotated[ListenBrainz, Depends(_lb)]
IT = Annotated[ITunes, Depends(_it)]
SP = Annotated[Spotify | None, Depends(_sp)]


@router.get("/search", response_model=SearchResponse)
async def search_tracks(
    request: Request,
    response: Response,
    query: Annotated[str, Query(min_length=2, max_length=100)],
    mb: MB,
    lb: LB,
    kind: Literal["song", "artist", "all"] = "song",
):
    results = await until_disconnect(request, search(mb, lb, query, kind))
    _browser_cache(response, SEARCH_TTL, bool(results))
    return {"results": results}


@router.get("/analyze", response_model=AnalyzeResponse)
async def analyze_track(
    request: Request, response: Response, mbid: UUID, mb: MB, it: IT, sp: SP, lang: Literal["en", "tr"] = "en"
):
    result = await until_disconnect(request, analyze(mb, it, sp, str(mbid), lang))
    _browser_cache(response, ANALYZE_TTL, bool(result["genres"]))
    return result


@router.get("/cover", response_model=CoverResponse)
async def cover(
    request: Request, response: Response, it: IT,
    title: Annotated[str, Query(min_length=1, max_length=200)], artist: Annotated[str, Query(max_length=200)] = "",
):
    """Apple artwork, preview and link on their own, so the page need not wait for the (slow) analysis.
    Same cached lookup as /analyze (the cache shares one iTunes request between the two)."""
    try:
        found = await until_disconnect(request, it.find(title, artist))
    except AppError:
        found = {}
    url, preview = found.get("cover_backup_url"), found.get("preview_url")
    _browser_cache(response, ANALYZE_TTL, bool(url or preview))
    return {"url": url, "preview_url": preview, "apple_music_url": found.get("apple_music_url")}


def _parse_genres(items: list[str]) -> list[tuple[str, float]]:
    """`["synthpop:30.5", ...]` -> [("synthpop", 30.5)]; malformed entries are dropped."""
    out = []
    for raw in items[:8]:
        tag, _, pct = raw.rpartition(":")
        tag = tag.strip().casefold()
        try:
            share = float(pct)
        except ValueError:
            continue
        if tag and len(tag) <= 40 and share > 0:
            out.append((tag, share))
    return out


@router.get("/similar", response_model=SimilarResponse)
async def similar_tracks(
    request: Request, response: Response, lb: LB, mbid: UUID,
    g: Annotated[list[str], Query()],  # the song's genre mix as repeated "tag:percent"
    title: Annotated[str, Query(max_length=200)] = "", artist: Annotated[str, Query(max_length=200)] = "",
):
    """"Find similar": songs whose genre mix is closest to this one's, most similar first.
    The page already holds the mix, so no analysis is repeated here."""
    genres = _parse_genres(g)
    recommendations = (
        await until_disconnect(request, similar_by_mix(lb, str(mbid), genres, title, artist)) if genres else []
    )
    _browser_cache(response, SIMILAR_TTL, bool(recommendations))
    return {"recommendations": recommendations}


@router.get("/similar-genres", response_model=SimilarResponse)
async def similar_genres(
    request: Request, response: Response, lb: LB, mbid: UUID,
    genre: Annotated[str, Query(min_length=1, max_length=40)],  # the clicked genre (lowercase English tag)
    title: Annotated[str, Query(max_length=200)] = "", artist: Annotated[str, Query(max_length=200)] = "",
):
    """A clicked genre: the songs where that genre has the biggest share, biggest first."""
    recommendations = await until_disconnect(
        request, top_by_genre(lb, str(mbid), genre.strip().casefold(), title, artist)
    )
    _browser_cache(response, SIMILAR_TTL, bool(recommendations))
    return {"recommendations": recommendations}