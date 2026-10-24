# YouFreeTest (YFT)

YFT is the isolated YouTube search, streaming, and download test application in
this repository. Its source lives in [`yft/`](yft/). The implementation notes
for a coding agent are in [`AI_HANDOFF.md`](AI_HANDOFF.md).

## Live test

- **Public test URL:** [https://code2it.com/yft/](https://code2it.com/yft/)
- **Local URL:** http://127.0.0.1:8183/yft/

The public URL returned HTTP 200 with the title “YFT — YouFree Test” on
2026-10-24. Earlier browser verification recorded in the source handoff covers
search, playback, offline cache playback, and a completed browser download.
A successful page response alone does not establish that YouTube will resolve
every video; upstream availability and session acceptance can change.

## What it does

- Searches YouTube music videos and displays normalized metadata and thumbnails.
- Resolves a playable source on the server, validates formats, and keeps source
  URLs inside the server.
- Streams through a same-origin YFT relay. Desktop playback can mux separate
  audio/video streams to fragmented MP4 with FFmpeg, without writing a full
  streaming file to disk. Safari, Firefox, and iOS browsers use a single
  progressive HTTPS H.264/AAC MP4 source and relay upstream byte ranges for
  playback and seeking.
- Caches bytes consumed through the player in the browser Cache API. Cached
  playback supports byte ranges and can work offline after the response has
  been cached.
- Downloads only after an explicit user action. The browser stores the real
  media Blob in IndexedDB, reads it back to verify its size, then acknowledges
  the commit so the temporary server file can be deleted.
- Keeps search history and recently played tracks on the device. The server's
  download-device manifest is metadata; it is not proof that a Blob exists.

Streaming cache and saved downloads are separate. A cached stream is not a
downloaded file in IndexedDB.

## Layout

| Path | Purpose |
| --- | --- |
| `yft/index.html`, `yft/styles.css` | YFT page and visual presentation |
| `yft/app.js` | Search, playback, IndexedDB downloads, and client state |
| `yft/sw.js` | Service-worker byte cache and range handling |
| `yft/server.py` | Isolated HTTP API, yt-dlp resolver, FFmpeg relay, and download jobs |
| `yft/ytapi/app.py` | Backend helpers imported by YFT for YouTube configuration and data normalization |
| `yft/requirements.txt` | Python package pins used by the captured source |
| `yft/yft.service`, `yft/yft-pot.service` | Reference systemd units for YFT and the PO-token provider |
| `yft/yft-nginx.conf` | Reference Nginx routes for the VPS-hosted YFT service |
| `yft/yft-local-nginx.conf` | Alternative route to a laptop runtime through a reverse SSH forward |
| `yft/start-local.ps1`, `yft/start-yft.ps1`, `yft/start-public-tunnel.ps1` | Windows local and fallback tunnel helpers |
| `yft/diagnose.py`, `yft/probe_backend.py` | Diagnostic helpers |

The VPS route owns only `/yft/`; the supplied Nginx snippet does not change
`/youfree/`. YFT imports the copied backend module but does not expose its
account or authentication routes. Its server docstring also states that it
does not modify the main YouFree files or database.

## HTTP API

The public paths below are served under `/yft`; Nginx maps API and media
requests to the backend listening on loopback port 8183.

| Method and path | Input | Behavior |
| --- | --- | --- |
| `GET /yft/api/health` | — | Returns service/runtime and auth-session configuration status. |
| `GET /yft/api/search?q=...&limit=20` | Search query | Returns `{ "items": [...] }`; the limit is clamped to 1–20. |
| `GET /yft/api/resolve?id=VIDEO_ID&refresh=1` | YouTube video ID; optional refresh | Returns ID, selected quality/mode (`direct` or `mux`), same-origin `mediaPath`, and expiry seconds. Safari, Firefox, and iOS browsers get a separately cached progressive source. |
| `GET /yft/media/VIDEO_ID` | Optional byte `Range` header | Streams through the YFT relay; it does not return an upstream provider URL. |
| `POST /yft/api/download` | JSON `{ "id", "title", "artist" }` | Starts a temporary download and returns HTTP 202 with `jobId` and state. |
| `GET /yft/api/download/status?id=JOB_ID` | Download job ID | Returns state, progress, error, and selected quality. |
| `GET /yft/api/download/file?id=JOB_ID` | Ready download job ID | Returns the temporary binary file. |
| `POST /yft/api/download/ack` | JSON `{ "jobId": "..." }` | Acknowledges a verified IndexedDB write and removes the server temporary file. |
| `POST /yft/api/device/downloads` | JSON `{ "device": "...", "items": [{ "id": "...", "size": 123 }] }` | Updates an in-memory device manifest; this manifest is metadata only. |

The server also has a loopback-only `/internal/media/<token>/<index>` route for
FFmpeg source relay. It is not the browser-facing API.

## Limits and known behavior

- Stream cache: 300 MB total with a 120 MB per-entry limit.
- Explicit downloads: 500 MB per file, two concurrent workers, and a maximum
  of eight starts per client IP per hour.
- Server temporary downloads expire after 30 minutes if the browser does not
  acknowledge them.
- Chromium desktop source selection tries 1440, 1280, and 1024 pixel targets,
  then falls back to the best available source at or below 1440 pixels. Safari,
  Firefox, and iOS browsers select a progressive HTTPS MP4 at or below 720p so
  the relay can preserve byte-range responses. Format availability depends on
  YouTube and yt-dlp.
- Cold playback includes source resolution and FFmpeg mux initialization. A
  0.5-second cold start was not demonstrated in the recorded testing.
- YouTube can reject or revoke a session. The server reports a session-refresh
  error when its configured session is no longer accepted.

## Runtime and data

This repository contains a sanitized source copy. It does not include the
runtime virtual environments, YouTube cookies, API keys, `.env` files,
account databases, media downloads, caches, or private session data. Provide
runtime configuration outside Git; never commit those files.

The captured service configuration uses a YFT process on `127.0.0.1:8183`
and a separate PO-token provider on `127.0.0.1:4417`. It configures
`YFT_AUTH_POLICY` as either `anonymous-first` or `session-first`. If a
private cookie file is used, provision it out of band with restrictive file
permissions. The included deployment files are reference snapshots and must
be checked against the target host before any deployment.

The local PowerShell helpers expect a separate data root by default at
`../../work/yft-lab`, including a Python virtual environment and a YFT copy
of `ytapi`. That lab data is deliberately not included here. See
[`AI_HANDOFF.md`](AI_HANDOFF.md) before trying to run or deploy the component.

## Recorded verification

The prior local browser run recorded search results and thumbnails, progressing
video playback with decoded audio, byte-cache storage and a correct cached
`206` range, offline playback, recovery after corrupting cached bytes, and a
10,568,234-byte MP4 Blob retained in IndexedDB after reload.

The prior public run recorded playback of a 720×1280 clip through HTTPS,
IndexedDB storage, server acknowledgement and temporary-file cleanup, followed
by offline playback after reload. A later VPS browser report recorded
10,568,235 bytes retained across reload, 46 decoded frames, decoded audio
during offline playback, and server acknowledgement/cleanup. These are
historical results from the source handoff; their browser evidence files were
not part of the sanitized folder copied into this repository.

On 2026-10-24, Safari User-Agent checks for two YouTube videos returned direct
MP4 sources; byte-range requests at the beginning and mid-file returned HTTP
206 with matching `Content-Range`, and remote ffprobe identified H.264 video
and AAC audio. These checks validate the HTTP/media path, not playback on a
physical iPhone.

A fresh desktop Chrome test on 2026-10-24 played the track through the 1080p
live-mux route with no player console error; the only browser console error
was a missing `/favicon.ico` (404). This does not verify Firefox or physical
iPhone playback.

## References

- [MDN Cache API: Cache.put()](https://developer.mozilla.org/en-US/docs/Web/API/Cache/put)
- [yt-dlp JavaScript runtime setup](https://github.com/yt-dlp/yt-dlp/wiki/EJS)
- [YFT implementation handoff](AI_HANDOFF.md)
