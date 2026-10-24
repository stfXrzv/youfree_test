# AI Handoff — YouFreeTest / YFT

## Goal and scope

This repository contains a sanitized source copy of the YFT test application
under `yft/`. The live test URL recorded by the previous project is
[https://code2it.com/yft/](https://code2it.com/yft/). On 2026-10-24, that URL
returned HTTP 200 and the page title “YFT — YouFree Test”.

Keep YFT isolated from the main YouFree application. Its Nginx snippet owns
only `/yft/`; do not modify `/youfree/`, the main YouFree files, or its
database when working on this component. The source server imports
`yft/ytapi/app.py` as a library for configuration and normalization. YFT
does not expose the imported application's account/authentication routes.

## Repository contents and excluded runtime state

`yft/` contains the page, client, service worker, isolated Python server,
the required backend helper module, diagnostic scripts, local launch scripts,
and captured systemd/Nginx reference files.

This public repository must contain source only. Do not add:

- `ytapi/.env`, API keys, OAuth secrets, cookies, or private YouTube sessions;
- copied account databases, runtime virtual environments, or credentials;
- `__pycache__`, caches, downloaded media, logs, browser profiles, or private
  verification captures.

The folder's `.gitignore` blocks common secret/runtime filenames, but still
review staged files before every commit. Supply secrets and session files
outside Git. A session file, if used, should be mode 0600 on Linux and must
never be served to the frontend.

## Component map

- `yft/index.html`: app shell and element IDs consumed by the client.
- `yft/styles.css`: responsive layout and player/search presentation.
- `yft/app.js`: search, player lifecycle, device history, Cache API, IndexedDB,
  explicit download flow, and service-worker interaction.
- `yft/sw.js`: same-origin media request interception, byte caching, range
  responses, cache invalidation, and offline playback.
- `yft/server.py`: HTTP API; YFT-specific search/resolve; yt-dlp invocation;
  source validation; FFmpeg relay; and temporary download jobs.
- `yft/ytapi/app.py`: imported configuration and YouTube data helpers. Its
  routes are not mounted by YFT's HTTP handler.
- `yft/requirements.txt`: yt-dlp and Python dependency pins.
- `yft/yft.service`, `yft/yft-pot.service`: captured systemd unit files.
- `yft/yft-nginx.conf`: VPS route snapshot; the app service listens on
  loopback port 8183 and the PO-token provider on 4417.
- `yft/yft-local-nginx.conf`: alternative Nginx route to a laptop reverse
  forward on port 18183.
- `yft/start-local.ps1`: starts only the local Python service.
- `yft/start-public-tunnel.ps1`: starts the optional reverse SSH forward.
- `yft/start-yft.ps1`: starts the local service and optional tunnel.
- `yft/diagnose.py`, `yft/probe_backend.py`: troubleshooting helpers.

## Request flow

1. Search calls `GET /yft/api/search`. The YFT handler normalizes the query,
   caps the limit at 20, and returns normalized video items.
2. Resolve calls `GET /yft/api/resolve`. The server invokes yt-dlp, checks
   candidate formats, and returns the selected quality/mode plus a same-origin
   `mediaPath`. Never return raw provider/CDN URLs to the browser.
3. Playback requests `GET /yft/media/{videoId}`. If no usable cached bytes are
   available, the YFT server resolves and relays sources through a private
   loopback route. FFmpeg muxes audio/video to fragmented MP4 and writes the
   stream directly to the client response; streaming does not create a full
   media file on disk.
4. The service worker stores only response bytes actually consumed by the
   player. It can answer later byte-range requests and remove corrupt cache
   entries before a fresh resolve. Cached stream bytes are not a saved download.
5. An explicit Download action POSTs the track metadata to
   `/yft/api/download`. The client polls status, downloads the real binary
   body, stores a Blob in IndexedDB, reads it back to verify its size, and only
   then POSTs `/yft/api/download/ack`. The server deletes the temporary file
   after acknowledgement; unacknowledged jobs expire after 30 minutes.
6. The client reports verified device downloads to
   `/yft/api/device/downloads`. The server stores only an in-memory manifest;
   browser IndexedDB remains the source of truth for the actual Blob.

## HTTP contract

All paths below are public paths under `/yft`. Nginx strips the prefix when
proxying to the backend.

| Method and route | Contract |
| --- | --- |
| `GET /api/health` | JSON with `ok`, service name, runtime, auth policy, and whether a session file is configured. |
| `GET /api/search?q=QUERY&limit=20` | `{ "items": [...] }`; missing query returns 400; limit is clamped to 1–20. |
| `GET /api/resolve?id=VIDEO_ID[&refresh=1]` | `{ "id", "quality", "mode", "mediaPath", "expiresIn" }`. IDs must be 11-character YouTube IDs. |
| `GET /media/VIDEO_ID` | Relayed media bytes; forwards browser byte-range semantics through Nginx. |
| `POST /api/download` | JSON track object with `id`, optional `title`, `artist`; HTTP 202 returns `{ "jobId", "state": "queued" }`. |
| `GET /api/download/status?id=JOB_ID` | `{ "state", "progress", "error", "quality" }`. |
| `GET /api/download/file?id=JOB_ID` | Binary body for a ready job. |
| `POST /api/download/ack` | JSON `{ "jobId": "..." }`; returns `{ "ok", "tempDeleted" }` after removing the temp file. |
| `POST /api/device/downloads` | JSON `{ "device": "uuid", "items": [{ "id": "videoId", "size": 123 }] }`; returns `{ "ok", "verifiedCount" }`. |

The server also handles `/internal/media/{randomToken}/{index}` for FFmpeg.
It accepts only loopback clients and a live in-memory source token; it is not a
browser-facing API.

## Configuration and runtime

The server reads these `YFT_*` variables:

- `YFT_HOST` (default `127.0.0.1`) and `YFT_PORT` (default `8183`);
- `YFT_ROOT`, `YFT_STATIC_ROOT`, `YFT_YTAPI_ROOT`, and `YFT_TMP`;
- `YFT_FFMPEG`, `YFT_COOKIES`, and `YFT_YTDLP_PROXY`;
- `YFT_AUTH_POLICY`: `anonymous-first` or `session-first`;
- `YFT_SEARCH_API` and `YFT_POT_PROVIDER_URL`.

The copied `ytapi/app.py` also reads `YOUTUBE_API_KEY`,
`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `PUBLIC_BASE`,
`OAUTH_BASE`, and `YTDLP_PROXY`. Values and private files are deliberately
not in this repo. The captured unit expects its own Python environment; the
Windows launch helpers expect an external data root (default
`../../work/yft-lab`) containing a `.venv` and private `ytapi` runtime
copy. This repo alone is not a complete live runtime.

The prior VPS snapshot used a separate PO-token provider, FFmpeg, yt-dlp's
JavaScript runtime support, and an IPv4 resolver path. Confirm the installed
versions and current service layout on the target before deployment. The
checked-in service and Nginx files are snapshots, not a command to overwrite a
host's current configuration.

## Operational limits and invariants

- 25-minute resolver/media cache TTL; explicit download job TTL is 30 minutes.
- 500 MB maximum explicit download, at most two concurrent workers, and eight
  download starts per client IP per hour.
- Browser stream cache target: 300 MB total and 120 MB per entry.
- Keep resolved provider URLs server-side. Avoid logging query strings,
  cookies, or source URLs.
- Copy cookie files to temporary mode-0600 files for yt-dlp, then remove those
  copies after each resolver attempt; preserve the configured source session.
- A reported browser download is valid only after verifying the actual Blob in
  IndexedDB. The server manifest alone is not proof of file data.
- A request to `/yft/` belongs to YFT. The component's Nginx snippet must not
  change the main `/youfree/` application.

## Verification history and known limits

The source README records local tests for search, thumbnails, decoded playback
audio, byte-cache storage, correct cached HTTP 206 ranges, offline playback,
corrupt-cache recovery, IndexedDB persistence, and history persistence. It
records a public HTTPS test of a 720×1280 video with IndexedDB commit,
acknowledgement, temporary-file deletion, and offline playback. A later VPS
browser report records a 10,568,235-byte saved Blob after reload, 46 decoded
frames, decoded audio during offline playback, and server-side cleanup.
Those evidence files were not copied here, so treat these as historical
handoff results rather than current automated test output.

The public page was reachable on 2026-10-24. YouTube sessions and upstream
format availability can change. A stale or revoked session can cause a
resolver failure; refresh the session through the authorized process, not by
committing a cookie file. Cold starts still include yt-dlp resolution and
FFmpeg setup; a 0.5-second cold-start target was not reached in recorded
testing.

## Change checklist for the next AI

1. Read the relevant source and this handoff before editing.
2. Keep changes scoped to `yft/` unless the user explicitly requests a shared
   YouFree change.
3. Preserve the separation between stream cache and saved IndexedDB Blobs.
4. Never commit secrets or runtime data; inspect the exact staged file list.
5. For streaming changes, verify decoded playback, HTTP byte ranges, and offline
   cache behavior. For download changes, verify IndexedDB readback, ack, and
   server-temp cleanup.
6. Check live behavior separately from source checks; the historical live test
   URL is [https://code2it.com/yft/](https://code2it.com/yft/).
