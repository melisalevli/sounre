from typing import Literal

from pydantic import BaseModel


class Track(BaseModel):
    id: str  # recording MBID
    title: str
    artist: str
    album: str | None = None
    cover_url: str | None = None


class Genre(BaseModel):
    name: str  # shown to the person (translated)
    percent: float
    tag: str = ""  # lowercase English name, sent back for genre-based recommendations


class SearchResponse(BaseModel):
    results: list[Track]


class CoverResponse(BaseModel):
    url: str | None = None  # Apple artwork; None when nothing was found
    preview_url: str | None = None  # 30 s audio sample, so the play button need not wait for /analyze
    apple_music_url: str | None = None


class AnalyzeResponse(BaseModel):
    genres: list[Genre]
    # Which MusicBrainz entity the votes came from; None when no genre data exists.
    source: Literal["recording", "artist"] | None
    cover_url: str | None
    cover_backup_url: str | None  # Apple artwork, used by the page when cover_url fails to load
    spotify_url: str | None  # exact link; None = checked and not on Spotify
    preview_url: str | None  # 30 s audio sample, None when none was found
    apple_music_url: str  # exact song page, or a search link when Apple has no match
    youtube_url: str  # search link


class SimilarResponse(BaseModel):
    recommendations: list[Track]