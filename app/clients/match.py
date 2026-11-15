def same_song(title: str, artist: str, found_title: str, found_artists: list[str]) -> bool:
    """Loose check that a third-party search hit is the song we asked for."""
    a, b = title.casefold(), found_title.casefold()
    want = artist.casefold()
    return (a in b or b in a) and any(n.casefold() in want or want in n.casefold() for n in found_artists)