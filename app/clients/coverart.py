BASE = "https://coverartarchive.org"


def for_release_group(mbid: str) -> str:
    return f"{BASE}/release-group/{mbid}/front-500"


def for_release(mbid: str) -> str:
    return f"{BASE}/release/{mbid}/front-500"
