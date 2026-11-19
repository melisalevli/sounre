# Sounre: technical reference

This document describes how Sounre works end to end: the architecture, each request flow, the request and response formats, the calculations, and the reasons behind the design choices.

## 1. System overview

```
 Browser (one static HTML file)
    |  GET /api/v1/...            (JSON)
    v
 +-----------------------------------------------------------+
 |  uvicorn (1 worker) -> Starlette/FastAPI app              |
 |    Guard middleware    per-IP point budget, security hdrs |
 |    GZip middleware                                        |
 |    router /api/v1      thin routes, validation            |
 |    analyzer.py         orchestration + ranking logic      |
 |    genres.py           pure maths (no I/O)                |
 |    clients/*           MusicBrainz, ListenBrainz, iTunes, |
 |                        Spotify (optional), Cover Art URLs |
 |    cache.py            read-through cache, single-flight  |
 |    ratelimit.py        1 req/s limiter for MusicBrainz    |
 +-----------------------------------------------------------+
    |  httpx (one shared AsyncClient, 10 s timeout)
    v
 MusicBrainz   ListenBrainz   iTunes Search   Spotify (optional)
```

Cover images come from Cover Art Archive. The server builds only their URLs, and the browser fetches them. Audio previews are played by the browser directly from Apple.

**Layering.** Routes validate input and shape output. `analyzer.py` decides what to fetch and in what order. Each module in `clients/` talks to one upstream and returns plain JSON-able dicts. `genres.py` contains pure functions with no I/O.

**Single worker.** The MusicBrainz limiter, the per-IP budgets and the single-flight table are held in process memory. Two workers would double the allowed MusicBrainz rate and split the budgets. Running more than one worker requires moving these three components to Redis; the cache and limiter are small, isolated classes.

## 2. Request lifecycle

Middleware stack, outermost first:

1. **`Guard`** (`abuse.py`), a pure ASGI middleware. It determines the visitor IP, deducts points from that IP's bucket, answers `429` if the bucket is empty, and adds security headers to every response (including the 429 and static files).
2. **`GZipMiddleware`**, applied to bodies of 1024 bytes or more.
3. **Router and static files.** `/api/v1/*` routes, `/healthz`, and `StaticFiles` mounted at `/` last, so API paths take precedence.

`Guard` is implemented as pure ASGI instead of Starlette's `BaseHTTPMiddleware` because the latter wraps `receive`, and `api/guard.py` relies on `request.is_disconnected()` to detect abandoned requests.

Exceptions are mapped in `main.py`: `AppError(status, key)` becomes `{"detail": <localized message>}` with that status, and `ClientGone` becomes an empty `499`.

## 3. Endpoint reference

All routes are `GET` under `/api/v1`. `lang` is `en` or `tr`; messages and genre names follow it. Interactive docs (`/docs`, `/openapi.json`) are disabled.

### 3.1 `/search`

| Param | Rules |
| --- | --- |
| `query` | required, 2 to 100 characters |
| `kind` | `song` (default), `artist` or `all` |

```json
{
  "results": [
    {
      "id": "a1b2c3d4-0000-4000-8000-000000000001",
      "title": "Example Song",
      "artist": "Example Artist",
      "album": "Example Album",
      "cover_url": "https://coverartarchive.org/release-group/<mbid>/front-500"
    }
  ]
}
```

Returns up to 10 results (`MAX_RESULTS`), most listened first. `id` is the MusicBrainz recording ID (MBID). `album` and `cover_url` may be `null`.

### 3.2 `/analyze`

| Param | Rules |
| --- | --- |
| `mbid` | required, must be a UUID |
| `lang` | `en` (default) or `tr` |

```json
{
  "genres": [
    { "name": "Synthpop", "percent": 56.2, "tag": "synthpop" },
    { "name": "New Wave", "percent": 37.5, "tag": "new wave" },
    { "name": "Pop", "percent": 6.2, "tag": "pop" }
  ],
  "source": "recording",
  "cover_url": "https://coverartarchive.org/release-group/<mbid>/front-500",
  "cover_backup_url": "https://is1-ssl.mzstatic.com/.../600x600bb.jpg",
  "spotify_url": "https://open.spotify.com/search/Example%20Song%20Example%20Artist",
  "preview_url": "https://audio-ssl.itunes.apple.com/.../preview.m4a",
  "apple_music_url": "https://music.apple.com/.../example",
  "youtube_url": "https://www.youtube.com/results?search_query=Example%20Song%20Example%20Artist"
}
```

- `genres`: up to 6 entries (`MAX_GENRES`). `name` is translated when `lang=tr`. `tag` is always the lowercase English name and is the value the page sends back to `/similar` and `/similar-genres`. Percentages are rounded to one decimal, so they can total 99.9 or 100.1.
- `source`: `"recording"` or `"artist"`, indicating the first layer that had usable votes, or `null` when no genre data exists. In that case the page shows its "no genre data" message.
- `spotify_url`: a search link when no credentials are configured. With credentials, the exact track link, or `null` meaning the track was checked and is not on Spotify. If Spotify is unavailable, the search link is returned instead of reporting the song as missing.
- `preview_url`, `cover_backup_url`: `null` when Apple has no match. `apple_music_url` falls back to an Apple search link.

### 3.3 `/cover`

Query: `title` (1 to 200 characters), `artist` (optional, up to 200).

```json
{ "url": "https://is1-ssl.mzstatic.com/.../600x600bb.jpg", "preview_url": "https://audio-ssl.itunes.apple.com/.../preview.m4a", "apple_music_url": "https://music.apple.com/.../example" }
```

Any field can be `null`. Errors are swallowed and returned as nulls. The endpoint lets the page show the cover and enable the play button without waiting for `/analyze`, which is slower. It uses the same cached iTunes lookup as `/analyze`, so the two share one upstream request.

### 3.4 `/similar`

| Param | Rules |
| --- | --- |
| `mbid` | required UUID (the current song, so it can be excluded) |
| `g` | required, repeated: `tag:percent`, for example `g=synthpop:56.2&g=new%20wave:37.5`. At most 8 entries are read; malformed entries, tags over 40 characters and non-positive shares are dropped |
| `title`, `artist` | optional, used to drop other recordings of the same song |

```json
{ "recommendations": [ { "id": "…", "title": "…", "artist": "…", "album": "…", "cover_url": "…" } ] }
```

Returns up to 5 songs (`MAX_RECOMMENDATIONS`), most similar first. The page already holds the song's genre mix, so the server does not re-analyse it.

### 3.5 `/similar-genres`

Query: `mbid` (UUID), `genre` (1 to 40 characters, lowercase English tag), optional `title` and `artist`. The response has the same shape as `/similar`. Songs where the genre has the largest share come first.

### 3.6 `/healthz`

Returns `{"ok": true}`. It is not budgeted and is used by Render's health check.

### 3.7 Errors

| Status | When | Body |
| --- | --- | --- |
| 404 | upstream reports that the recording does not exist | `{"detail": "Track not found."}` (or Turkish) |
| 422 | invalid input (bad UUID, missing `g`, query too short) | FastAPI's default validation body, which is not localized |
| 429 | per-IP budget used up | `{"detail": "Too many requests…"}` plus a `Retry-After` header in seconds |
| 499 | the client disconnected before the answer was ready | empty |
| 502 | an upstream failed or timed out | `{"detail": "A music data service is unavailable…"}` |
| 503 | the MusicBrainz queue is full | `{"detail": "The service is busy right now…"}` |

## 4. Flow: search

All three kinds end the same way: recordings are grouped into songs, ranked by total listens, and the top 10 are kept.

**Recordings and songs.** MusicBrainz stores each recording (album cut, single, live, remaster) separately, and listens are split between them. The *song signature* is the title with version suffixes removed (`(Live at …)`, `- 2011 Remaster`, acoustic, demo, radio edit, mono/stereo and similar), reduced to lower-case letters and digits only, combined with the lower-cased artist. Recordings with the same signature count as one song.

**`rank`:** for every signature, the listens of all its recordings are summed. Recordings are sorted by (song total, the recording's own listens, MusicBrainz relevance score), highest first. The first recording of each song is kept, which is its most-listened version.

**`kind=song`**
1. A single word is searched against titles. Several words are searched against title *or* artist (`any`), because the additional words likely name the artist.
2. If a title-only search returns nothing, it is retried with `any`.
3. ListenBrainz is queried for listen counts of the hits (one batched request, up to 50 IDs per batch), then the results are ranked.

**`kind=artist`**
1. `mb.artist_candidates(query)` runs a MusicBrainz artist search. A hit needs a score of 75 or more; exact, case-insensitive name matches with a score of 90 or more are preferred. Up to 3 candidates are kept, because several artists can share one name.
2. `lb.top_recordings(artist_mbid)` runs for every candidate in parallel and returns ListenBrainz's most-listened songs. Karaoke, tribute and similar titles are dropped, and the artist whose top five songs have the most listens is selected. The results are then ranked.
3. If any step fails or finds nothing, the search falls back to recordings credited to that artist (a MusicBrainz search on the artist field), ranked by listens.

**`kind=all`**: the artist path and the title path run at the same time. Results are merged by song signature, keeping the higher listen total, sorted, and cut to 10. This costs two MusicBrainz requests.

**MusicBrainz query construction.** Each word is wrapped in quotes (`_phrase` escapes backslashes and quotes, so user input cannot inject Lucene operators), and the words are joined with `AND`. A suffix restricts results to official, non-video releases whose primary type is album, single or EP.

**Filtering out non-songs (`_is_song`).** A hit is dropped if it is a video, is shorter than 45 s or longer than 20 min, has karaoke, tribute or "in the style of" in the title or artist, or has no music release (interviews, audiobooks, spoken word, DJ sets and demos are excluded). When several releases exist, the one with the fewest secondary types (live, compilation) is preferred.

**Cache pre-filling.** If a search hit already carries genre tags, it is stored in the cache as the recording lookup for that MBID (`mb1:rec:<mbid>`), so a following `/analyze` can skip its own MusicBrainz request.

**Cover pre-loading on the page.** After results are displayed, the page calls `/cover` for the top three songs (staggered, and on hover after 150 ms) so covers, previews and links are cached before a result is selected.

## 5. Flow: analyze

```
/analyze?mbid=…&lang=…
  rec = mb.recording(mbid)           # cached; may already be filled by the search hit
  then, in parallel:
    _genres(rec)    -> (source, genres)
    _spotify_url()  -> exact link, search link, or null
    _apple()        -> preview, cover backup, Apple Music link
  response = genres + source + cover + links + YouTube search link
```

**Genre layers.** Two layers are used: the recording's votes (weight 3) and the artist's votes (weight 1). An album layer was dropped to save one MusicBrainz request per song.

**Shortcut.** If the recording alone has at least `RICH_VOTES = 5` usable votes (`strength()` counts votes remaining after cleaning), the artist lookup is skipped, so a well-tagged song costs one MusicBrainz request instead of two. Otherwise `artist_tags` is fetched; if that fails, it is ignored (`_soft`), because artist votes only refine the result.

**Failure behaviour.** The recording lookup is the only fatal step. Spotify, Apple and artist tags fail softly: the response is still returned, with fewer fields.

## 6. Genre calculations (`genres.py`)

### 6.1 Cleaning

For each tag `{name, count}`:
- The *key* is the lower-cased name with spaces, hyphens, underscores, dots and slashes removed, so "synth-pop", "Synth Pop" and "synthpop" produce the same key.
- Dropped: counts of 0 or less, keys longer than 40 characters, decade tags such as `80s` or `1990s`, and a noise list (favorites, best, seen live, spotify, american, female vocalists, and so on).
- A canonical table merges synonyms: `hiphop`, `rnb`/`randb`, `electronica` to Electronic, `altrock`, `dnb`, `rocknroll`, and so on.
- The display name is the spelling with the most votes, title-cased.

### 6.2 Blend

Each layer is first converted into shares that sum to 1 (its own votes divided by its own total). As a result, an artist with thousands of votes does not outweigh a song with five. Then:

```
score(genre) = sum over layers of  weight * share_in_layer(genre)
keep the top 6 scores, divide by their sum, multiply by 100
```

**Worked example.** Recording votes: synth-pop 2, new wave 1, favorites 5 (noise). Artist votes: new wave 20, pop 10, synthpop 10.

- The recording has 3 usable votes, below 5, so the artist layer is fetched.
- Recording shares: synthpop 2/3, new wave 1/3. Artist shares: new wave 0.5, pop 0.25, synthpop 0.25.
- Scores: synthpop 3 × 2/3 + 0.25 = 2.25; new wave 3 × 1/3 + 0.5 = 1.5; pop 0.25. Total 4.0.
- Result: Synthpop 56.2, New Wave 37.5, Pop 6.2 (rounded to one decimal, so they sum to 99.9).

With `lang=tr`, only a short word list is translated (Electronic to Elektronik, Jazz to Caz, and so on); all other names are unchanged.

### 6.3 Similarity score

Each song's mix is converted into shares that sum to 1 (`A` for the target, `B` for a candidate). Each of the target's genres receives a weight by rank: the largest gets 1, the next 0.5, then 0.25, and so on (`RATIO = 0.5`). A genre the target does not have receives a weight smaller than its last rank.

```
D     = sum  w * (A - B)^2                    weighted squared distance
score = 1 - D / sum w * (A^2 + B^2)
      = 2 * sum(w*A*B) / sum(w * (A^2 + B^2))
```

The two forms are equal because `D = sum w(A^2 + B^2) - 2 sum wAB`. The score is 1 for identical mixes and 0 when no genre is shared. A difference in the main genre has the largest effect on the score.

Using the example target (synthpop 0.5625, new wave 0.375, pop 0.0625):
- a candidate with 50% synthpop, 30% new wave, 20% pop scores 0.983;
- a candidate with 90% synthpop, 10% rock scores 0.844;
- a candidate with 100% rock scores 0.

### 6.4 Candidate selection (`required_genres`)

Scoring every song is not feasible, so ListenBrainz is queried only for songs that could score well.

A candidate that lacks genre `g` can at best copy the rest of the target's shape. With `S = sum(w * A^2)` over all genres and `S' = S - w_g * A_g^2`, the best possible score without `g` is `sqrt(S'/S)`. If that bound is below `GOOD_SIM = 0.85`, no candidate without `g` can be a good match, so `g` must appear in the query.

For the example target, the bounds are synthpop 0.429, new wave 0.905, pop 0.999, so only synthpop is required. A balanced 50/50 target requires both genres; a 70/15/10/5 target requires only its first.

Derivation: the candidate is scaled to `c` times the target's shape on the other genres. The score is then `2cS' / (S + c^2 S')`, which peaks at `c = sqrt(S/S')` with value `sqrt(S'/S)`. The derivation does not account for shares having to sum to 1, so the result is an upper bound.

## 7. Flow: similar songs

```
/similar -> similar_by_mix
  1. target mix  = tags from g, merged by group_key, normalised to sum 1
  2. weights     = rank_weights(target)
  3. candidates  = _candidates(...)      # ListenBrainz tag pool, up to 40 IDs per query
  4. mixes       = _mixes(candidates)    # profiles + listens, both batched, in parallel
  5. score each, drop below MIN_SIM, sort descending
  6. _finish: drop the song itself and duplicate recordings, keep 5
```

**Step 3.** A list of queries is built: the required genres joined with `AND`, then the same list with the least important genre dropped, and so on down to one genre, and finally `OR` over the three strongest genres. The queries run in order, and the first one that returns at least `MIN_POOL = 10` IDs is used (otherwise the largest pool seen). The song itself is removed.

**Step 4.** One batched ListenBrainz request returns metadata and tag votes for up to 50 recordings; another returns listen counts. A candidate is kept only if it has at least `MIN_LISTENS = 20` listens (which filters junk recordings) and at least `MIN_EVIDENCE = 3` tag votes. Its mix is built the same way as the pie chart (recording weight 3, artist weight 1, artist skipped when the recording has 5 or more votes), so the score is computed from the same percentages shown when that song is opened.

**Listen counts do not affect ordering.** They only filter junk recordings; the order is determined by similarity alone.

**Step 5.** `MIN_SIM = 0.4`. Below this value the mixes share little, and returning no suggestion is preferred over returning a wrong one.

**`/similar-genres`** is simpler. ListenBrainz is asked for recordings with that single tag (`OR`), mixes are built the same way, and songs where the genre's share is above 0 are kept and sorted by (share, tag votes) descending. The results are finished the same way as in `/similar`. A song that is 100% pop ranks above one that is 60% pop; ties go to the song with more tag votes.

**No MusicBrainz requests.** Both similar routes use only ListenBrainz, so they never wait in the 1-request-per-second queue.

## 8. Caching

`Cache` is a read-through cache on diskcache (SQLite) with a size limit of 256 MB. Values are stored as JSON (the default pickle serialization has a known deserialization flaw, and JSON covers all values this app stores).

| Key | Holds | TTL |
| --- | --- | --- |
| `mb3:artists:<name>` | candidate artist MBIDs (up to 10 hits; the first 3 are used), `[]` for "no such artist" | 7 days |
| `mb6:search:<kind>:<query>:<limit>` | filtered recordings | 1 day |
| `mb1:rec:<mbid>` | recording, tags, artist ID | 7 days |
| `mb1:artist-tags:<id>` | artist tag votes | 7 days |
| `lb1:tags:<op>:<tags>` | ID pool for a genre query | 1 day |
| `lb2:prof:<sha1 of IDs>` | metadata and tag votes of a batch | 1 day |
| `lb1:top:<artist>` | an artist's top songs | 1 day |
| `lb1:pop:<IDs>` | listen counts | 1 day |
| `it3:<title>\|<artist>` | preview, Apple link, backup cover, or `""` | 7 days |
| `sp1:<title>\|<artist>` | Spotify link, or `""` | 7 days |
| `sp1:token` | Spotify access token | 3000 s (token lifetime is 3600 s) |

**Single-flight.** Concurrent misses for the same key share one upstream call. `/analyze` and `/similar` both need the same recording, so together they cost one MusicBrainz request instead of two.

**Cancellation.** Each caller waiting on a lookup is counted, and the lookup is wrapped in `asyncio.shield` so that one caller leaving does not cancel it for the others. When the last waiting caller leaves, the lookup is cancelled, which also frees a queued slot in the rate limiter.

**Failures are not cached.** Exceptions propagate before the value is stored. Empty "not found" results are stored as the sentinel `""`.

**Browser caching.** Routes add `Cache-Control: public, max-age=N` (search 300 s; analyze, cover and similar 600 s), but only when the answer is non-empty, because empty answers usually result from an upstream failure and should be retried.

## 9. Rate limiting and abuse protection

### 9.1 MusicBrainz limiter (`ratelimit.py`)

MusicBrainz allows about one request per second per IP. The limiter spaces calls at least 1.05 s apart. Waiters take an `asyncio.Lock` in FIFO order, and each reserves `next = max(now, previous_next) + interval` and sleeps until its slot. The queue is capped at 10 (`MAX_QUEUE`): the 11th concurrent caller receives `AppError(503, "busy")` immediately instead of waiting without bound. Without the cap, one visitor issuing many different searches could delay every other request by minutes and cause the server's IP to be banned.

### 9.2 Per-IP budget (`abuse.py`)

Each visitor IP has a token bucket with capacity `SOUNRE_IP_BUDGET` (default 120 points), refilled continuously at budget/60 points per second (2 per second by default). Costs: search 4, analyze 4, similar 3, similar-genres 3, cover 1. With the defaults, this allows about 30 searches per minute.

When a bucket has too few points, the response is `429` with `Retry-After = ceil((cost - points) / rate)` seconds. Buckets are stored as `(points, last_seen)`. When there are more than 10,000 entries, full buckets (equivalent to no bucket) are dropped. Static files and `/healthz` cost nothing.

### 9.3 Identifying the visitor

Behind a proxy, the socket address is the proxy's. `SOUNRE_PROXY` selects where the real IP is read from:

| Mode | Source | When it is safe |
| --- | --- | --- |
| `none` | socket address | local or directly exposed |
| `cloudflare` | `CF-Connecting-IP` | only if Cloudflare is the sole way in (otherwise any client can send the header) |
| `forwarded` | the Nth entry from the right of `X-Forwarded-For` | when `SOUNRE_PROXY_HOPS` matches the number of trusted proxies |

Proxies append to `X-Forwarded-For`, so entries on the left are visitor-controlled and can be forged, while entries on the right were added by infrastructure. On Render the chain is `visitor-supplied…, real visitor, Cloudflare edge`, so hops = 2. If the chain is shorter than expected, the code falls back to the socket address.

### 9.4 Cancelling abandoned work (`api/guard.py`)

Starlette continues running a handler after the client disconnects, which would keep a place in the MusicBrainz queue. `until_disconnect` runs the work as a task and checks `request.is_disconnected()` every 0.25 s. If the client has left, it cancels the task and raises `ClientGone` (answered as 499). Work that finishes within one poll interval is never checked. The page also aborts the previous request with an `AbortController` whenever the user searches or selects something new.

### 9.5 Headers

Every response carries `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, a strict-origin referrer policy, a permissions policy, and a minimal CSP (`frame-ancestors 'none'; base-uri 'none'; object-src 'none'; form-action 'self'`). A full `script-src` CSP is not set because the page uses an inline script and inline `style` attributes, and such a policy has not been tested. Uvicorn runs with `--no-server-header`.

## 10. Upstream calls and resilience

All upstream traffic uses one shared `httpx.AsyncClient` with a User-Agent of `Sounre/0.1 (<contact>)`, a 10 s timeout, and redirects followed.

`get_json` handles every call. On `429` or `503`, it waits 1.5 s and retries once (MusicBrainz answers 503 when briefly rate-limited). A `404` becomes `AppError(404)`. Any other error status, timeout or network failure becomes `AppError(502)`, and details are written to the log and not sent to the client.

| Upstream | Used for | Notes |
| --- | --- | --- |
| MusicBrainz `/ws/2` | recording search and lookup, artist search, tags | rate limited; the only fatal dependency for `/analyze` |
| ListenBrainz | listen counts (batched POST), top songs per artist, tag pool (`lb-radio/tags`), batched metadata and votes | no key required |
| iTunes Search | preview, Apple Music link, backup cover | the hit must pass `same_song`, a loose title-contains and artist-contains check |
| Spotify | exact track link | optional; client-credentials flow; token cached |
| Cover Art Archive | cover URLs (`front-500`) | built by string; fetched by the browser |

## 11. Frontend

The frontend is one HTML file with inline CSS and JavaScript and no build step. The flow:

1. **Search.** Calls `/search?query=&kind=&lang=` after aborting the previous search.
2. **Result selection.** Aborts the previous selection, then calls `/cover` and `/analyze` in parallel. `/cover` returns quickly, so the cover and play button appear first, and `/analyze` fills in the genres.
3. **Pie chart.** An SVG ring with one arc per genre, drawn with `stroke-dasharray` around the CD. The CD is a spinning element that uses the cover. It spins by default (faster while the cover loads). After play is pressed for a song, it spins only while the preview plays.
4. **Genre click.** Calls `/similar-genres` with the genre's `tag`. **Find similar** calls `/similar` with `g=tag:percent` for every genre.
5. **Language.** The default follows the browser language: Turkish if it starts with `tr`, otherwise English. Switching the language reloads the current song so genre names are returned translated. The language and the search mode (`song`, `artist` or `all`) are stored in the browser's `localStorage`; if storage is unavailable, the defaults are used.
6. **Offline fallback.** Only when the page is opened from disk (`file://`) and a search fails, the page shows built-in sample songs and states that they are samples. When served over HTTP, a failure shows an error message.
7. **Cover order.** Apple's artwork from `/cover` loads first because it is fast. If Apple has no cover or it fails to load, the page loads the Cover Art Archive image. If neither loads, it tries the cover URLs from `/analyze`.

The background consists of blurred shapes, plus SVG wave paths that are drawn once and slid sideways by CSS animation, and a fine noise layer.

All text from the API is inserted with `textContent`, so upstream strings cannot inject HTML.

## 12. Deployment and operations

- **Start command:** `uvicorn app.main:create_app --factory --host 0.0.0.0 --port $PORT --workers 1 --no-server-header`. The app factory reads settings from environment variables.
- **Render free plan** via `render.yaml`: health check on `/healthz`, cache in `/tmp/sounre-cache` (ephemeral, so the cache starts cold after a restart), `SOUNRE_PROXY=forwarded`, `SOUNRE_PROXY_HOPS=2`.
- **Secrets:** `SOUNRE_CONTACT` is entered in the dashboard and is not committed; `.env` is git-ignored.
- **Cost:** no component is paid for. No upstream bills per call. According to Render's published material (not tested), a free service without a card on file is suspended rather than charged when it exceeds its limits.

## 13. Design decisions and trade-offs

- **FastAPI plus one static file** instead of a SPA framework: there is no build step, the whole app runs in one process, and the interface is small enough that a framework would add more than it removes.
- **Server-side upstream calls** instead of calling MusicBrainz from the browser: one shared cache and one rate limiter protect the free services, and the contact information stays out of every visitor's requests.
- **Layered votes with per-layer normalisation** so sparse songs still receive a sensible mix, instead of one flat pool in which heavily tagged artists dominate.
- **A similarity score based on a weighted squared distance** instead of an opaque model, with candidate selection derived from the same calculation, so the expensive step only examines songs that can score well.
- **Soft failure.** Everything except the recording lookup degrades to fewer fields instead of returning an error.
- **In-memory limiter and budgets.** Simple and correct for one process; they reset on restart and cannot be shared across workers.

## 14. Limitations

- Genre quality depends on community tags, and obscure songs may have none (`source: null`).
- The weights (3 and 1), `RATIO`, and the thresholds (`MIN_SIM`, `GOOD_SIM`, `MIN_LISTENS`) are hand-picked and not tuned against a ground truth.
- The upper-bound argument in 6.4 assumes shares need not sum to 1, so it is looser than the true bound. This is safe but may require more genres than strictly necessary.
- This version has no automated tests (the earlier suite was removed to keep the project minimal). Tests around `genres.py` and the limiter are the first candidates to restore.
- There is no strict `script-src` CSP. It would require moving the inline script to a separate file.
- Scaling beyond one worker requires moving the cache, the MusicBrainz limiter and the budgets to Redis.

## 15. Common questions

- **Why one worker?** The MusicBrainz limiter, per-IP budgets and single-flight table are held in memory. More workers would break the one-request-per-second guarantee.
- **How is MusicBrainz's limit respected?** A FIFO limiter spaces calls 1.05 s apart, a cache absorbs repeats, single-flight merges simultaneous identical requests, and the queue cap turns overload into a fast 503.
- **What happens when a visitor closes the tab?** The server polls the connection every 0.25 s and cancels the task, which releases its slot in the queue.
- **How is the IP limit kept reliable behind a proxy?** The IP is counted from the right of `X-Forwarded-For`, because entries appended by proxies cannot be forged by the visitor.
- **How does similarity work, and why this formula?** It is a weighted squared distance rescaled to 0 to 1, weighted by rank so the main genre dominates. It is equivalent to `2·ΣwAB / Σw(A²+B²)`.
- **Why are similar songs not ranked by popularity?** Ranking by popularity would recommend only hits. Listens are used only to remove junk recordings.
- **What happens when an upstream is down?** A request is retried once on 429 or 503. After that, the response is a 502 with a localized message, or fewer fields when the dependency is optional.
- **What changes at scale?** The cache, limiter and budgets move to Redis. Other items: a lockfile with hashes, tests, a full CSP, and metrics.