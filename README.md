# Music stack

A self-hosted music setup: Navidrome for listening, an acquisition app
for search and review, and Soulseek downloads through AirVPN. The app processes
private copies with Beets, FFmpeg and Essentia. OpenRouter gives metadata advice;
the user approves publication manually.

This repository contains application source, both Compose projects, pinned
container images, configuration templates, agent tools, and deployment notes.
Music files, databases, credentials, VPN keys, and backups stay outside Git.

## Services

| Service | Purpose | Local address |
| --- | --- | --- |
| Acquisition | Search, downloads, review, library downloads and sharing choices | localhost:4534 |
| Navidrome | Playback and playlists | localhost:4533 |
| Soulseek / slskd | Search, download and share approved audio | localhost:5030 through Gluetun |
| AirVPN / Gluetun | Soulseek network connection and forwarded peer port | WireGuard; private settings |
| Deemix | Existing separate downloader | localhost:6595 |
| Redirects | Old Aurral and Lidarr addresses | localhost:3015 and localhost:8687 |
| Beets maintenance | Optional manual tools container | `tools` Compose profile |

Deemix remains available separately. Acquisition's source pool currently uses
Soulseek and yt-dlp. Free-text yt-dlp search uses YouTube; pasted YouTube,
SoundCloud, Bandcamp and Vimeo URLs are supported.

## Acquisition workflow

1. Ask for music on the Requests page (or through an agent): an album or track
   by artist and name, or an exact link. Everything downloaded starts as a
   request. An album or track is searched on Soulseek, RuTracker and YouTube
   Music at once; the copies that are that record are ranked by quality
   (lossless first; ties go to whoever can send now) and tried one by one. A copy
   that sends nothing for 10 minutes is dropped for the next one. A list that runs
   out is searched again daily for a week. Lossy arrivals, and albums already in
   the library as lossy files, are searched weekly for a better copy for four
   weeks. YouTube is used only with a Premium login (256 kbps), one album at a
   time with pauses between tracks, and can be switched off (`youtube_enabled`).
   A compilation by "Various Artists" is searched by its title.
2. To pick a copy yourself, search, filter results by file type and quality, and
   choose **Get this**: that copy is tried first, then others if it fails.
   Library hints mark existing tracks or possible matches. A few downloads run at
   a time (`download_workers`); the rest wait their turn.
3. Preparation fills what it can: BPM, key, genre, year and mood, plus a missing
   album and cover art from MusicBrainz and the Cover Art Archive. A strong
   MusicBrainz match renames the track only when the recording length agrees
   within 5 seconds, so unlisted remixes keep their own names. Identical copies
   of library tracks are skipped. A copy of the same recording that is clearly
   better (lossless over lossy, or at least 25% higher bitrate) replaces the
   library version, which moves to a trash folder; an equal or worse copy is skipped.
   The same title on the same album is the same recording, whatever the lengths;
   on another album it is only when the lengths agree within 3 seconds, and
   otherwise the new track is added alongside it.
4. With automatic adding on (Requests, separately for requests and inbox
   imports), tracks with nothing to decide go straight into the library. The
   open questions below go to a strong model first (`review_model`, default
   `openai/gpt-6-luna` at high effort, through OpenRouter); it adds, replaces or
   skips each track from the metadata and your rules (best copy only, skip
   instrumentals, keep other versions), and its reason is kept on the job.
   **Needs you** then holds only what the model was unsure of, each with one-click answers:
   - a missing artist or title
   - a file that names a different song than the one searched for
   - one recording over 20 minutes
   - a library version that may be a different recording
   **Recently added** lists the last week's additions. **Undo** takes tracks back
   to Review and restores any version they replaced. Albums, mixed inbox batches,
   filters, tag editing and OpenRouter advice still work as before.
5. Added tracks are published. Tracks left out stay in Review; skipped ones remain private. Navidrome
   scans the accepted library, and the Soulseek share tree gets managed audio
   links. Everything accepted is shared by default; admins can exclude tracks or
   pause all sharing.
6. Library supports playback, genre/key/BPM filters, original file downloads,
   album ZIP exports and Navidrome links. Keys use Camelot notation (8A = A
   minor) and genres one spelling each (Hip-Hop becomes Hip Hop). Admins can
   edit a track's tags from More → Edit tags. Tag edits made with other tools
   are picked up within five minutes.

Missing BPM, key, genre, year and mood are filled during ingestion and, for
existing music, by Library → Tag analysis. MusicBrainz is authoritative: its
first release year and genres are used first, then Discogs (year, styles), then
Last.fm artist tags for genre. When the catalogs have nothing, genre comes from Essentia's Discogs-EffNet model.
Mood (happy, sad, aggressive, relaxed, party, danceable), BPM and key always
come from the audio. Values already in a file are kept, every filled value
records its source, and files over 20 minutes are not analyzed from audio. Key
tags that are not keys are listed under Key → Not a key for manual correction.

An album/playlist URL imports its available audio files. A single full-concert
video remains one file; YouTube chapters are not split into separate tracks.
MusicBrainz suggestions currently match single tracks rather than complete
album releases. Existing album tags are retained and can be corrected in review.

## Layout

- `acquisition/`: FastAPI app, workers, classic frontend, tests and MCP bridge.
- `compose.yaml`: acquisition, Navidrome and the Aurral redirect. Project `aurral`.
- `compose.sources.yaml`: slskd, Gluetun, Deemix, web proxy and optional Beets.
  Project `lidarr`; its network is `lidarr_default`.
- `examples/`: configuration templates with placeholders for secrets.
- `deployment/`: reverse-proxy settings and Cloudflare ingress example.
- `scripts/init-config.sh`: prepares private config files for a new installation.

One copy per song: a finished Soulseek download is moved into staging, the copy
prepared for Review becomes the library file, and the staged download is deleted
once its tracks are in the library. A torrent keeps seeding from its own folder,
so torrent albums keep that copy too. Moves are renames only within one mount, so
the work folders and paths that files move between are configured through the
`/music-storage` mount (`staging_root`, `slskd_download_root`, and
`sharing_source_root` for the library); `/state/ingestion` and `/state/trash` are
symlinks to folders on the music disk. Undo moves a track back to Review; tag
edits made at approval are not restored.

The project and network names (`aurral`, `lidarr`) are historical: this stack
replaced an earlier Aurral + Lidarr setup. The old Aurral fork remains at
https://github.com/mateo19182/aurral.

## Deploying changes

Point `.env` at your private state (see `.env.example`). Validate both projects
before any deployment:

```sh
docker compose config --quiet
docker compose -f compose.sources.yaml config --quiet
```

For a reviewed acquisition change, build and replace that service:

```sh
docker compose build acquisition
docker compose up -d --no-deps acquisition
```

`REDIRECT_CONF` and `DEEMIX_PROXY_CONF` can point the Nginx containers at private
copies of the files in `deployment/` that use your real hostnames.

## New installation

Requires Docker Compose, Linux `/dev/net/tun` for the VPN, and enough disk for
music and private preparation copies. Containers use UID/GID 1000. Prepare the
media root with ownership that permits that user to write:

```text
/mnt/data/music-library/
  library/
  shared/
  incoming/acquisition/
  downloads/slskd/
  downloads/slskd-incomplete/
  downloads/deemix/
```

Run `./scripts/init-config.sh`, then edit the private files under `config/`:

- `acquisition/config.json`: slskd API key, optional OpenRouter key, model and
  site URLs, optional `discogs_token` and `lastfm_api_key` for genre and year
  lookups. Its slskd key must match the key in `slskd/slskd.yml`.
- `slskd/slskd.yml`: Soulseek account, admin credentials and API key. Share only
  `/data/shared`; pending or rejected downloads must stay private.
- `airvpn/airvpn.env`: WireGuard private/preshared keys, addresses and selected
  country. Match the AirVPN forwarded port to `soulseek.listen_port` and
  `FIREWALL_VPN_INPUT_PORTS`. Configure the forwarded port in your AirVPN account.
- `deemix/htpasswd`: create a private Nginx password file before starting its proxy.
  Sign in/configure the existing Deemix application separately.
- `navidrome/navidrome.toml`: folder-based albums and portable musical key tags.

Replace every `CHANGE_ME` value. Keep configuration files private with mode `0600`.
For `deemix/htpasswd`, use mode `0644` so Nginx workers can read it, and keep
its host directory `config/deemix` at mode `0700`.
Public hostname changes also require editing the proxy files and tunnel ingress.
Then start the download project to create the external network, followed by the
player and acquisition project:

```sh
docker compose -f compose.sources.yaml up -d
docker compose up -d --build
curl --fail http://127.0.0.1:4534/api/health
```

Create Navidrome users through its first-run/admin interface. Acquisition uses
their credentials for browser sign-in. Optional `navidrome_scan` credentials in
acquisition config request an immediate scan; otherwise the scheduled scan runs
every five minutes. The OpenRouter key enables automatic metadata advice;
without a key the app still supports manual review.

Genre and mood estimates need Essentia's models (about 23 MB, CC BY-NC-ND 4.0)
in the acquisition state directory. Run `./scripts/fetch-models.sh`; it uses
`ACQUISITION_STATE_DIR` from `.env`, verifies checksums and can be rerun.
Without the models, catalog lookups and BPM/key analysis still work.

Cloudflared runs separately on the host. The ingress example documents the
local targets; tunnel credentials and account configuration are not in this repo.

## Agent access

See [acquisition/AGENTS.md](acquisition/AGENTS.md) for the stdio MCP tools and
[acquisition/mcp-config.example.json](acquisition/mcp-config.example.json) for
client configuration. The same authenticated REST API is described by
`/openapi.json` with Bearer authentication.

A signed-in admin creates a dedicated token with `POST /api/agents` and a JSON
`name`, optionally `"can_approve": true`. Tokens expire after 90 days and can be revoked with `DELETE /api/agents/{id}`.
Store the token in a mode-`0600` credentials file. Agents may search, request music,
import completed files, request advice, inspect reviews, cancel and retry jobs.
They cannot reject reviews, edit tags, change sharing, or mint tokens. They can approve publication (without editing tags) only if the token was minted with `can_approve`; the approval is recorded as `approved_by: agent:<name>`.
A publication retry only resumes an existing human approval.

## Development and verification

Python 3.11 supports the pinned Essentia TensorFlow wheel. Set
`ESSENTIA_MODELS` to a fetched model directory to include the real-model test. FFmpeg, ffprobe and Chromaprint
must be installed for ingestion tests. The Docker image also includes Node 24 for
yt-dlp's YouTube JavaScript support.

```sh
uv sync --project acquisition --frozen --group dev
PYTHONPATH=acquisition acquisition/.venv/bin/pytest -q acquisition/tests
node --check acquisition/app/static/app.js
```

## Hermes and Telegram

The acquisition tools can be exposed to a Telegram bot through a
[Hermes](https://github.com/NousResearch/hermes-agent) gateway. Merge the server in
[deployment/hermes-mcp.example.yaml](deployment/hermes-mcp.example.yaml) into the
existing `mcp_servers` mapping of your Hermes config, preserving other servers and
platform toolsets. An explicit MCP allowlist must also include `music-acquisition`.

Add the workflow in
[deployment/hermes-music-instructions.md](deployment/hermes-music-instructions.md)
to the agent's operating instructions, then start a new conversation with `/new`
to refresh them.

Ask Hermes to find a track or album, compare sources, queue your chosen source,
check download progress, or summarize pending reviews. It can request OpenRouter
advice and report deterministic album/duplicate concerns. Publication remains a
manual action in Acquire. The tool allowlist and backend agent permissions exclude
approval, rejection and sharing changes. Dedicated credentials stay in the
mode-0600 file named by `--credentials`; provision a replacement in Acquire if its
90-day token expires.

The example config leaves the OpenRouter key blank. The default review model is
`meta/muse-spark-1.3-contributor`, independent of Hermes's chat model.

## State and backups

Back up accepted music plus the private acquisition and Navidrome state.
Acquisition state includes the SQLite job/review history, Beets state, and
`sharing.json`. Navidrome state includes users, playlists and playback data.
Back up source configuration and VPN keys separately. Databases require
consistent backups, such as SQLite's backup API or a stopped service.
