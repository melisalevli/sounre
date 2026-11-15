<p align="center">
  <img src="app/static/sounre.png" alt="Sounre logo" width="160" />
</p>

<h1 align="center">Sounre</h1>

<p align="center">
  <a href="https://sounre.onrender.com">sounre.onrender.com</a>
</p>

<p align="center">
  <em>Search a song, see what it's made of, find what sounds like it.</em>
</p>

---

## What is Sounre?

Sounre is a small web app that shows the genre mix of a song.

Most songs aren't one genre. A track can be 56% synthpop, 37% new wave and 6% pop. You type in a song or an artist, pick the right result, and Sounre draws its genres as a pie chart next to a spinning CD of the cover. From there you can click a genre to find songs where that genre is strongest, or press **Find similar** to get songs whose genre mix is closest to the one you're looking at.

### How to use it

1. **Pick what to search.** Choose *Song*, *Artist* or *Song and/or artist* above the search field.
2. **Search.** Type and press the button. A short list of results appears.
3. **Choose a result.** The page shows the genre pie chart, the cover, and the genres with their percentages.
4. **Follow a genre.** Click a genre to get songs where that genre has the biggest share.
5. **Find similar.** Press the button to get songs with the closest overall mix.
6. **Listen.** Play the 30-second preview, or open the song on Spotify, Apple Music or YouTube.
7. **Switch language.** The button in the header toggles Turkish and English, including genre names.

If the server can't be reached, the page falls back to a few sample songs so the interface still works.

---

## Getting started

**Requirements:** Python 3.12 or newer.

```bash
git clone https://github.com/melisalevli/sounre.git
cd sounre
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export SOUNRE_CONTACT="https://your-site.example"
uvicorn app.main:create_app --factory --reload
```

Then open http://localhost:8000.

`SOUNRE_CONTACT` is required. MusicBrainz asks every client to identify itself with an email or a website, and Sounre puts this value in its User-Agent. A website keeps your inbox private.

---

## How it works

### Stack

| Piece | Choice |
| --- | --- |
| Backend | [FastAPI](https://fastapi.tiangolo.com) on [uvicorn](https://www.uvicorn.org) |
| Upstream calls | [httpx](https://www.python-httpx.org) |
| Cache | [diskcache](https://grantjenks.com/docs/diskcache/) (SQLite on disk, values stored as JSON) |
| Frontend | One static HTML file with plain CSS and JavaScript, no framework and no build step |
| Data | MusicBrainz, ListenBrainz, Cover Art Archive, iTunes Search API |
| Hosting | Render (free plan) |

There is no database server, no user accounts, and no API key. One process serves both the API and the page.

### Where the data comes from

- **MusicBrainz** for genre tags. They are crowd-sourced, so the percentages reflect community opinion, not measurement.
- **ListenBrainz** for listen counts and similar-song candidates.
- **Cover Art Archive** for covers, with the **iTunes Search API** as a backup and for previews and Apple Music links.
- **Spotify** is optional. Without credentials the Spotify button is a search link; with them it links to the exact track.

### The idea in four lines

- **Genre mix.** A song's tag votes (weight 3) are blended with its artist's tag votes (weight 1). Spellings are merged ("synth-pop" and "synthpop" count as one), noise like "favorites" is dropped, and the top six genres become percentages.
- **Similarity.** Two genre mixes are compared with a weighted score from 0 to 1. The biggest genre of the song you picked counts most.
- **Politeness.** MusicBrainz allows one request per second, so every call to it goes through a single limiter and every answer is cached.
- **Protection.** Each visitor IP gets a points budget per minute, and the server cancels work the browser has already abandoned.

The full write-up, with the flows, request and response formats, and the maths, is in [TECHNICAL.md](TECHNICAL.md).

### API

All routes live under `/api/v1`.

| Route | Returns |
| --- | --- |
| `GET /search?query=&kind=song\|artist\|all` | Matching songs, most listened first |
| `GET /analyze?mbid=&lang=en\|tr` | Genre percentages plus cover, preview and streaming links |
| `GET /cover?title=&artist=` | Cover, preview and Apple Music link, so the page needn't wait for `/analyze` |
| `GET /similar?mbid=&g=tag:percent&g=...` | Songs with the most similar genre mix |
| `GET /similar-genres?mbid=&genre=` | Songs where one genre has the biggest share |

### Settings

| Variable | Default | What it does |
| --- | --- | --- |
| `SOUNRE_CONTACT` | none (required) | Email or URL sent to MusicBrainz in the User-Agent |
| `SOUNRE_CACHE_DIR` | `.cache` | Where the SQLite cache lives |
| `SOUNRE_IP_BUDGET` | `120` | Points per minute per visitor IP |
| `SOUNRE_PROXY` | `none` | Where the visitor's IP comes from: `none`, `cloudflare`, or `forwarded` |
| `SOUNRE_PROXY_HOPS` | `1` | With `forwarded`: which `X-Forwarded-For` entry, counted from the right, is the visitor (Render: `2`) |
| `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET` | none | Optional, for exact Spotify links |

---

## Design

The interface is dark and quiet, so the music is what stands out. The palette is a warm, limited set: cream text on walnut and bark browns, with a single pale blue accent. Labels are small, uppercase and letter-spaced, and buttons are simple pills or outlined ghosts.

The genre mix is a pie chart and the cover is a spinning CD, because a song is easier to read as a shape than as a list of numbers. Motion shows state: things ease in, and the CD spins while a preview plays.

Text uses each device's own system font, so no fonts are downloaded from anywhere.

---

## Privacy

Sounre has no accounts, no cookies, and no user database. It does not log visitor IPs or searches (only upstream failures), and the per-IP budgets live in memory and disappear on restart. Your host's own logs may still record IPs.

Search text is sent to MusicBrainz, ListenBrainz and Apple. Your browser loads covers from Cover Art Archive and Apple and previews from Apple, so those hosts see the visitor's IP.

---

## Deployment: Render

Sounre runs on the **free plan on Render**, configured by `render.yaml`.

1. Push the repo to GitHub.
2. In Render choose **New → Blueprint** and pick the repo.
3. Enter `SOUNRE_CONTACT` (the site's URL) when asked.

The free plan sleeps when idle, so the first request after a quiet spell can take up to a minute, and its disk is ephemeral, so the cache starts empty after a restart. Both are fine for a cache. Keep it at one worker, because the limiter and the budgets live in the process.

---

## Known limits

- Genre data is only as good as MusicBrainz's community tags, and obscure songs may have few or none.
- The weights and the similarity score are design choices, not a scientific measure of how songs sound.
- The repo has no automated tests; I removed them to keep it minimal.
