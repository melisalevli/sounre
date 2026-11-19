<p align="center">
  <img src="app/static/sounre.png" alt="Sounre logo" width="160" />
</p>

<h1 align="center">Sounre</h1>

<p align="center">
  <a href="https://sounre.onrender.com">sounre.com</a>
</p>

<p align="center">
  <em>Search a song, see what it's made of, find what sounds like it.</em>
</p>

---

## What is Sounre?

Sounre shows the genre mix of a song, for example 56% synthpop, 37% new wave, 6% pop. The mix is displayed as a pie chart ring around a CD of the cover.

- Search by song, artist, or both.
- Clicking a genre lists songs where that genre has the largest share.
- **Find similar** lists songs with the closest overall genre mix.
- A 30-second preview plays in the page. Links open the song on Spotify, Apple Music or YouTube.
- The header button switches between Turkish and English, including genre names. The default language follows the browser language: Turkish if it starts with `tr`, otherwise English.

---

## Design

**Name.** soun(d) % gen(re). *Soun* comes from sound, *re* from genre, and % refers to the genre percentages.

**Symbol.** The logo is a swan, which can turn into a ballerina or an eighth note.

**Palette.** Based on Swan Lake: pale lake blue, cream and deep brown.

| Role | Color | Hex |
| --- | --- | --- |
| Text, swan | cream | `#F2F0E9` |
| Accent, lake | pale blue | `#8FAFC0` |
| Background | walnut | `#4A3A31` |
| Surfaces | bark | `#523F33` |
| Muted | cork | `#7A6A5F` |

Genre slices use cream and blue tints plus warm neutrals (`#C5D4DA`, `#6F8C9C`, `#B8A99A`, `#D9CFC0`).

**Interface.** Dark background, small uppercase letter-spaced labels, pill and outlined buttons, system fonts (no font downloads). The background consists of blurred shapes, sliding waves and fine noise.

**Visualization.** A pie chart ring shows the genre mix around a spinning CD that shows the cover. The CD spins by default. After a preview is started for a song, it spins only while the preview plays.

---

## Getting started

Requires Python 3.12 or newer.

```bash
git clone https://github.com/melisalevli/sounre.git
cd sounre
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export SOUNRE_CONTACT="your-email@example.com"   # or a website: "https://your-site.example"
uvicorn app.main:create_app --factory --reload
```

The app runs at http://localhost:8000.

`SOUNRE_CONTACT` is required. MusicBrainz requires clients to identify themselves with an email or website, and the value is sent in the User-Agent.

---

## How it works

| Piece | Choice |
| --- | --- |
| Backend | [FastAPI](https://fastapi.tiangolo.com) on [uvicorn](https://www.uvicorn.org) |
| Upstream calls | [httpx](https://www.python-httpx.org) |
| Cache | [diskcache](https://grantjenks.com/docs/diskcache/) (SQLite, values stored as JSON) |
| Frontend | One static HTML file, plain CSS and JavaScript, no build step |
| Hosting | Render |

There is no database server, user account system or required API key. One process serves the API and the page.

### Data sources

- **MusicBrainz:** genre tags (crowd-sourced, so percentages reflect community opinion).
- **ListenBrainz:** listen counts and similar-song candidates.
- **iTunes Search API:** cover, 30-second preview, Apple Music link.
- **Cover Art Archive:** fallback cover when Apple has none.
- **Spotify (optional):** exact track link when credentials are set, otherwise a search link.

### Method

- **Genre mix:** song tag votes (weight 3) are blended with artist tag votes (weight 1). Spellings are merged, noise tags are dropped, and the top six genres become percentages.
- **Similarity:** a weighted score from 0 to 1, in which the largest genre of the selected song counts most.
- **Rate limiting:** MusicBrainz requests go through a single limiter at one request per second, and all answers are cached.
- **Abuse protection:** each IP has a points budget per minute, and work abandoned by the browser is cancelled.

Formats and formulas are described in [TECHNICAL.md](TECHNICAL.md).

### API

All routes are under `/api/v1`.

| Route | Returns |
| --- | --- |
| `GET /search?query=&kind=song\|artist\|all` | Matching songs, most listened first |
| `GET /analyze?mbid=&lang=en\|tr` | Genre percentages, cover, preview, streaming links |
| `GET /cover?title=&artist=` | Cover, preview, Apple Music link |
| `GET /similar?mbid=&g=tag:percent&g=...` | Songs with the most similar genre mix |
| `GET /similar-genres?mbid=&genre=` | Songs where one genre has the largest share |

### Settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `SOUNRE_CONTACT` | none (required) | Email or URL sent to MusicBrainz in the User-Agent |
| `SOUNRE_CACHE_DIR` | `.cache` | Cache location |
| `SOUNRE_IP_BUDGET` | `120` | Points per minute per visitor IP |
| `SOUNRE_PROXY` | `none` | Source of the visitor IP: `none`, `cloudflare`, `forwarded` |
| `SOUNRE_PROXY_HOPS` | `1` | With `forwarded`: position of the visitor in `X-Forwarded-For`, counted from the right (Render: `2`) |
| `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET` | none | Optional, enable exact Spotify links |

---

## Privacy

The app has no accounts, cookies or user database. The language and search mode are stored in the browser's localStorage. Per-IP budgets are kept in memory and reset on restart.

Search text is sent to MusicBrainz, ListenBrainz and Apple. The browser loads covers and previews from Apple (and covers from Cover Art Archive when used), so those hosts see the visitor's IP.

---

## Deployment

Deployment is configured by `render.yaml`. The repository is pushed to GitHub, deployed in Render through **New → Blueprint**, and `SOUNRE_CONTACT` is set to the site URL.

- The service sleeps when idle, and the first request afterwards can take up to a minute.
- The disk is ephemeral, so the cache is empty after a restart.
- Only one worker is used, because the rate limiter and budgets live in the process.

---

## Known limits

- Genre data depends on MusicBrainz community tags, so obscure songs may have few or none.
- The weights and the similarity score are design choices and do not measure how songs sound.
