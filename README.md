# YouFreeTest (YFT) — research notes

Isolated lab for YouTube browser-playback research behind **YouFree** (iOS/Android PWA).
This repository is **documentation-only on purpose**: the runnable lab lives on the
deployment server, not in git. Read `AI_HANDOFF.md` first, then
`docs/YT-BROWSER-PLAYBACK.md` for the full technical findings.

* **Live lab:** https://code2it.com/yft/
* **Remote debug bridge:** https://lab.code2it.com/logger.php?s=<session>
* **Deployment (source of truth):** `ubuntu@57.131.147.36:/var/www/yft`
* **Local working copy (dev machine):** `C:\xampp\htdocs\yf-lab\yft-live`

## TL;DR findings

| Path | Verdict |
|---|---|
| `yt-dlp server` (resolve + relay `/yft/media`) | Works but resolve is slow, all bytes cross the VPS, dies when the server YouTube session expires |
| `opaque direct` (browser-side resolve + direct googlevideo) | Works — resolve must happen from the same IP as the player |
| `visionos 2-hop` (ANDROID→visitorData→VISIONOS, `/yft/gv` + `?range=`) | **Best browser path** — full-length, adaptive qualities incl. 2160p+, unlocked ranges |
| `ANDROID adaptive` (removed) | **Prefix-locked** — only the first ~2–4.5 MB decodes; every continuation mechanism fails |

The hybrid "server resolves → client plays the googlevideo URL directly" is
**architecturally impossible**: googlevideo signatures bind `ip=` to the resolving
party. A URL minted server-side 403s from any other IP.
