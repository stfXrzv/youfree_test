# AI_HANDOFF — instructions for the next agent

## What this repo is (and is not)

This repo holds **research documentation only**. Do not add source code here.
The YFT frontend (`app.js`, `index.html`, `styles.css`, `sw.js`, `lab.html`,
`itag18.html`) is deployed directly to the VPS and intentionally not versioned —
per owner request, this repo must never contain the frontend or any cookies /
secrets / session material.

## Where the real code lives

* Deploy: `scp` to `ubuntu@57.131.147.36`, then `sudo cp` into `/var/www/yft`
  (files are owned by `www-data`; direct write is denied — stage via `/tmp`).
* Local dev copy: `C:\xampp\htdocs\yf-lab\yft-live`.
* **Always bump the cache-buster** in `index.html` (`app.js?v=N`, `styles.css?v=N`,
  and the `sw.js` registration URL) on every deploy, or the service worker /
  HTTP cache will keep serving stale code and you will debug ghosts.
* `*.bak-pre-sandbox` on the server are pre-sandbox snapshots of the old
  single-player version — restore points only.

## Lab architecture (current)

Three-pane comparison test (`?v=N` cache-buster, `?debug=<session>` for remote
logging). One quality selector, per-pane seek/time, checkbox-linked
synchronization (seek/play/pause between checked panes), winner arbiter with
audio-follows-winner. Panes:

1. **yt-dlp server** — `/yft/api/resolve` → `/yft/media/<id>` relay.
2. **opaque direct** — sandboxed-iframe YouTube resolve (real `youtube.com`
   origin → readable JSON), element plays the googlevideo URL directly; the
   service worker rewrites range behavior via `?range=` params.
3. **visionos 2-hop** — ANDROID resolve harvests `visitorData`, VISIONOS
   re-resolve produces adaptive URLs fetched through `/yft/gv` (SW injects the
   `?range=` param, which is the only slice mechanism that survives `no-cors`).

An `ANDROID adaptive` pane existed and was **removed after proof** — see the
findings doc before re-adding anything like it.

## Test-methodology rules (learned the hard way)

* `video.ended` / `currentTime === duration` **does not mean success**. A
  prefix-locked stream EOS-es after the first chunk while `currentTime` shows
  the moov duration. Validate with `getVideoPlaybackQuality().totalVideoFrames`
  — require `frames >= duration * 8` before declaring completion.
* The watchdog must treat early `ended` as a rung failure and descend the
  quality ladder, not as a finish.
* Node/curl probes of googlevideo URLs are **unreliable evidence**: signatures
  are IP-bound, so a URL minted in a browser 403s from Node. Judge transport
  results only inside the same browser context that resolved the URL.
* Opaque (`no-cors`) responses cannot be read, measured, or repackaged in JS —
  no synthetic 206s, no blob inspection. `blob.size === 0` always.
* Keep the browser window open after tests unless the user asks otherwise;
  use a desktop viewport for the desktop check.

## Debugging tools

* Remote bridge: open `https://code2it.com/yft/?debug=<session>`, then poll
  `https://lab.code2it.com/logger.php?s=<session>` (GET) / POST commands such as
  `run:`, `test`, `sel:<host>`, `playpanes`, `seek:<t>`.
* `sw.js` posts `[gv]` diagnostics to the page console when `?debug` is active —
  they show the incoming Range header and the `?range=` param it was mapped to.

## Hard rules from the owner

* No YouTube iframe as the actual player ("nu s-ar mai numi youfree").
* Interface in English; dark quality dropdown; max quality 4320p.
* Video boxes must hug the real aspect ratio — no black bars, no crop.
* Never commit: frontend files, cookies, YouTube session material, secrets.
