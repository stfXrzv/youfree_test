# YouTube browser playback — findings

Researched with REA + live browser instrumentation, Oct–Nov 2026.
Goal: real playback (not a YouTube iframe) resolved and streamed in-browser,
with a server relay only as fallback.

## Constraint recap

* YouTube iframe as the player: rejected by product decision.
* `fetch()` of googlevideo from a `code2it.com` page: impossible — ACAO is
  `*.youtube.com` only; responses are opaque and unreadable.
* Resolve therefore runs inside a **sandboxed iframe on a youtube.com origin**
  (real `youtube.com` page hosts `youtubei/v1/player` calls → JSON is
  same-origin-readable there; the sandbox postMessages formats back).
* `ump=1` streams: binary `vnd.yt-ump`, unreadable — dead end.
* HLS/DASH manifests: not present in ANDROID/IOS player responses.

## The three surviving paths

### 1. yt-dlp server (`default`)
`/yft/api/resolve?id=<v>` warms a server yt-dlp resolve; `/yft/media/<id>`
relays upstream bytes (progressive or ffmpeg-muxed). Reliable transport, but
resolve latency is seconds, all traffic crosses the VPS, and the whole path
dies when the server's YouTube session is challenged/expired.

### 2. opaque direct (`current`)
Browser resolve → element `src` = the googlevideo URL itself. The SW
intercepts the media element's requests and rewrites them with `?range=`
windows (redirect for the first request). Real 206s flow between element and
googlevideo — this is why it streams continuously where the opaque-fetch
variant could not.

### 3. visionos 2-hop (`visionos`) — recommended
ANDROID player call harvests `visitorData`; a VISIONOS client call with it
returns adaptive MP4 URLs whose signatures accept `?range=` slicing anywhere
in the file. `/yft/gv` maps the element's `Range` header onto that param
(header itself is stripped by `no-cors`). Verified full-length: 217 s clip,
217 s played, ~2 k decoded frames; qualities up to 4320p60 AV1 solo
(~11 k frames, zero stall). Concurrency limit is memory, not codec.

## ANDROID adaptive — removed, prefix-locked (do not resurrect)

Reproduced mechanically in-browser:

* Only the first chunk (~2–4.5 MB, ≈15–20 s of video) ever decodes
  (max observed ~441 frames on a 217 s clip).
* Every continuation mechanism fails:
  * opaque `no-cors` passthrough → upstream slice arrives as a synthetic
    `200` on a range request → Chrome treats it as a **new resource from
    byte 0** → stream corruption → EOS.
  * redirect + `?range=` → follow-up requests lose the `Range` header →
    same 200-semantics corruption.
  * synthetic `206` over an opaque body → **impossible**: opaque bodies are
    not readable/measurable in JS (`blob.size === 0`); Chrome rejects
    unverifiable `Content-Range`.
  * mid-file `?range=` / header / `sq` / `rn` → 403 in the browser context
    that owns the signature.
* False-positive trap: the element EOS-es while `currentTime` equals the
  moov duration — it *looks* like a finished clip. Only the decoded-frame
  count exposes it.
* itag 18 (360p progressive, full-length, direct) is the only ANDROID
  signature that plays end-to-end — same quality ceiling as the old
  server premux, but instant and serverless. itag 22 is gone (2025).

## Transport laws distilled

1. **IP binding:** resolve and playback must share one IP. Server-resolved
   URLs cannot be played client-side — which is exactly why the relay exists.
2. `?range=` is the only slice mechanism that survives `no-cors`
   (the `Range` header is stripped).
3. Opaque bodies cannot be inspected or repackaged — no synthetic 206.
4. iOS Safari/AVPlayer: QUIC/direct googlevideo is unreliable → bytes must
   transit the SW fetch path (`/yft/gv`), never a redirect.
5. Decoded frames, not `currentTime`, are the truth metric.
6. Stalls are usually bandwidth; death is `qf` and `currentTime` frozen
   >10 s together. Descend the ladder only on real decoder death.

## Quality ladder

`[0(auto), 4320, 2160, 1440, 1080, 720, 480, 360]` — start at the chosen
ceiling, descend only on verified failure, resume position on switch.
