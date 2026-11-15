import asyncio
import logging

import httpx

from .errors import AppError

log = logging.getLogger(__name__)


async def get_json(http: httpx.AsyncClient, url: str, *, body=None, headers=None, **params):
    """GET (or POST when `body` is given) and decode JSON, mapping upstream failures to AppError."""
    for attempt in range(2):  # one retry: MusicBrainz answers 503 when briefly rate-limited
        try:
            resp = await (http.get(url, params=params, headers=headers) if body is None
                          else http.post(url, params=params, json=body, headers=headers))
        except httpx.HTTPError as exc:
            log.warning("upstream error %s: %r", url, exc)
            raise AppError(502, "upstream") from exc
        if resp.status_code in (429, 503) and attempt == 0:
            await asyncio.sleep(1.5)
            continue
        break
    if resp.status_code == 404:
        raise AppError(404, "not_found")
    if resp.is_error:
        log.warning("upstream %s returned %s", url, resp.status_code)
        raise AppError(502, "upstream")
    return resp.json()