#!/usr/bin/env python3
"""youFree backend — stateless yt-dlp + YouTube Data API + Google OAuth.

Runs on 127.0.0.1:8182 behind nginx at https://colab.code2it.com/ytapi/.
No database. Sessions are HMAC-signed tokens. Media is downloaded by
yt-dlp into a temp dir, proxied to the viewer while it downloads, and
auto-deleted after a short TTL — nothing is stored permanently.
"""
import base64
import datetime
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
YTDLP = os.path.join(BASE, "venv", "bin", "yt-dlp")
COOKIES = os.path.join(BASE, "cookies.txt")  # optional
# yt-dlp egress proxy — Cloudflare WARP (unflagged consumer IP pool).
# Set YTDLP_PROXY="" in .env to disable.
YTDLP_PROXY = os.environ.get("YTDLP_PROXY", "socks5h://127.0.0.1:40000")
PORT = 8182
APP_ORIGIN = "https://code2it.com"
APP_URL = APP_ORIGIN + "/youfree/"

# ---- config (loaded from env file) ----
def _load_env():
    env_path = os.path.join(BASE, ".env")
    if os.path.isfile(env_path):
        for line in open(env_path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

_load_env()
PUBLIC_BASE = os.environ.get("PUBLIC_BASE", "https://api.code2it.com/ytapi")
# OAuth redirect host may differ while the new URI isn't registered in Google
# console yet — set OAUTH_BASE in .env to keep the old callback working.
OAUTH_BASE = os.environ.get("OAUTH_BASE", PUBLIC_BASE)
YT_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
OAUTH_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

_secret_path = os.path.join(BASE, "secret")
if os.path.isfile(_secret_path):
    with open(_secret_path, "rb") as fh:
        SECRET = fh.read().strip()
else:
    SECRET = secrets.token_bytes(32)
    with open(_secret_path, "wb") as fh:
        fh.write(SECRET)
    os.chmod(_secret_path, 0o600)

def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

def _b64d(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))

def sign(payload: str) -> str:
    return _b64e(hmac.new(SECRET, payload.encode(), hashlib.sha256).digest()[:18])

def now() -> int:
    return int(time.time())

# ---- temp media store (download-to-temp + proxy model) ----
TMP = os.path.join(BASE, "tmp")
os.makedirs(TMP, exist_ok=True)

def _cookie_snapshot():
    """Give one yt-dlp process a private copy; yt-dlp rewrites cookie files on exit."""
    if not os.path.isfile(COOKIES):
        return None
    fd, path = tempfile.mkstemp(prefix="ytapi-ytdlp-cookies-", suffix=".txt", dir=TMP)
    os.close(fd)
    try:
        shutil.copyfile(COOKIES, path)
        os.chmod(path, 0o600)
        return path
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise
MEDIA_TTL = 30 * 60              # files live max 30 min
MEDIA_ERROR_RETRY_AFTER = 60      # do not pin a failed resolver result for the media TTL
MEDIA_MAX_BYTES = 3 * 1024**3    # temp dir cap
PROGRESSIVE_WAIT_TIMEOUT = 270   # Stay below the public proxy's 300s read timeout.
MEDIA: dict[str, dict] = {}      # key -> download state
MEDIA_GUARD = threading.Lock()

def media_state(key: str, create_for: tuple | None = None):
    with MEDIA_GUARD:
        st = MEDIA.get(key)
        if (st is not None and st.get("error") and create_for is not None
                and now() >= st.get("retry_after", 0)):
            MEDIA.pop(key, None)
            st = None
        if st is None and create_for is not None:
            vid, fmt = create_for
            st = {
                "vid": vid, "fmt": fmt,
                "path": os.path.join(TMP, key),
                "done": threading.Event(),
                "error": None, "size": 0, "expires": now() + MEDIA_TTL,
                "mime": None, "touched": now(),
            }
            MEDIA[key] = st
            threading.Thread(target=_download_worker, args=(st,), daemon=True).start()
        if st is not None:
            st["touched"] = now()
        return st

def media_sweep():
    while True:
        time.sleep(60)
        try:
            with MEDIA_GUARD:
                keys = list(MEDIA.keys())
            for k in keys:
                st = MEDIA.get(k)
                if not st:
                    continue
                if st["expires"] < now() and now() - st["touched"] > 90:
                    try:
                        os.remove(st["path"])
                    except OSError:
                        pass
                    with MEDIA_GUARD:
                        MEDIA.pop(k, None)
            total = sum(s["size"] for s in list(MEDIA.values()))
            if total > MEDIA_MAX_BYTES:
                for k, st in sorted(list(MEDIA.items()), key=lambda kv: kv[1]["touched"]):
                    try:
                        os.remove(st["path"])
                    except OSError:
                        pass
                    with MEDIA_GUARD:
                        MEDIA.pop(k, None)
                    total -= st["size"]
                    if total <= MEDIA_MAX_BYTES:
                        break
        except Exception:
            pass

def _download_worker(st: dict):
    """yt-dlp downloads straight to st['path'] (no .part juggling so we can
    stream the file while it grows). Retries soften transient bot-checks."""
    vid, fmt = st["vid"], st["fmt"]
    selector = FMT_SELECTORS.get(fmt, FMT_SELECTORS["audio"])
    attempts = 3
    err = "download failed"
    for attempt in range(1, attempts + 1):
        try:
            cmd = [YTDLP, "--no-playlist", "--no-warnings", "--no-part",
                   "--remote-components", "ejs:github",
                   "-f", selector]
            if YTDLP_PROXY:
                cmd += ["--proxy", YTDLP_PROXY]
            cmd += ["-o", st["path"],
                    "--", f"https://www.youtube.com/watch?v={vid}"]
            # Prefer anonymous playback for public videos. Stale login cookies
            # can hide every available format; try them only as a fallback.
            cookie_copy = _cookie_snapshot() if attempt == 2 else None
            if attempt == 2:
                cmd[1:1] = ["--extractor-args", "youtube:player_client=mweb"]
            if cookie_copy:
                cmd[1:1] = ["--cookies", cookie_copy]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            finally:
                if cookie_copy:
                    try:
                        os.remove(cookie_copy)
                    except OSError:
                        pass
            if proc.returncode == 0 and os.path.getsize(st["path"]) > 0:
                st["mime"] = media_content_type(st["path"], fmt) or (
                    "audio/webm" if fmt == "audio" else "video/mp4"
                )
                st["done"].set()
                return
            err = ((proc.stderr or proc.stdout or "").strip().split("\n") or [""])[-1][:300]
        except Exception as e:
            err = str(e)[:300]
        try:
            os.remove(st["path"])
        except OSError:
            pass
        if attempt < attempts:
            time.sleep(2 * attempt)
    st["error"] = err or "download failed"
    st["retry_after"] = now() + MEDIA_ERROR_RETRY_AFTER
    st["done"].set()

def media_size(st: dict) -> int:
    try:
        st["size"] = os.path.getsize(st["path"])
    except OSError:
        pass
    return st["size"]

def media_content_type(path: str, fmt: str) -> str | None:
    try:
        with open(path, "rb") as media:
            header = media.read(32)
    except OSError:
        return None
    if len(header) < 4:
        return None
    if header.startswith(b"\x1a\x45\xdf\xa3"):
        return "audio/webm" if fmt == "audio" else "video/webm"
    if len(header) < 12:
        return None
    if b"ftyp" in header[:16]:
        return "audio/mp4" if fmt == "audio" else "video/mp4"
    if header.startswith(b"OggS"):
        return "audio/ogg" if fmt == "audio" else "video/ogg"
    if header.startswith(b"ID3") or header[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    if header.startswith(b"RIFF"):
        return "audio/wav"
    if header[0] == 0x47:
        return "video/mp2t"
    return "audio/webm" if fmt == "audio" else "video/mp4"

def wait_media_content_type(st: dict, timeout: float = 8.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detected = media_content_type(st["path"], st["fmt"])
        if detected:
            st["mime"] = detected
            return detected
        if st["done"].is_set():
            break
        time.sleep(0.1)
    return st.get("mime") or ("audio/webm" if st["fmt"] == "audio" else "video/mp4")

def touch_media(st: dict):
    st["touched"] = now()

# ---- light rate limit (per-IP token bucket) ----
class RateLimit:
    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._hits: dict[str, list[float]] = {}

    def ok(self, key: str) -> bool:
        hits = [t for t in self._hits.get(key, []) if t > time.time() - 60]
        if len(hits) >= self.per_minute:
            self._hits[key] = hits
            return False
        hits.append(time.time())
        self._hits[key] = hits
        return True

RL_STREAM = RateLimit(40)
RL_API = RateLimit(120)

# ---- helpers ----
def http_get_json(url: str, timeout: int = 15):
    req = urllib.request.Request(url, headers={"User-Agent": "youFree/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))

def http_post_json(url: str, body: dict, timeout: int = 15):
    data = urllib.parse.urlencode(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))

VIDEO_RE = re.compile(r"^[a-zA-Z0-9_-]{11}$")

def video_id_of(raw: str) -> str | None:
    raw = raw.strip()
    if VIDEO_RE.match(raw):
        return raw
    m = re.search(r"(?:v=|youtu\.be/|/shorts/)([a-zA-Z0-9_-]{11})", raw)
    return m.group(1) if m else None

def yt_api(path: str, params: dict):
    params = dict(params)
    params["key"] = YT_API_KEY
    url = "https://www.googleapis.com/youtube/v3/" + path + "?" + urllib.parse.urlencode(params)
    return http_get_json(url)

def _yt_auth_get(url: str, token: str, timeout: int = 20):
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token,
        "User-Agent": "youFree/1.0",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))

def _yt_auth_call(sess: dict, url: str):
    try:
        return _yt_auth_get(url, sess.get("a", ""))
    except urllib.error.HTTPError as e:
        if e.code != 401 or not sess.get("r"):
            raise
        tok = http_post_json("https://oauth2.googleapis.com/token", {
            "refresh_token": sess["r"], "client_id": OAUTH_CLIENT_ID,
            "client_secret": OAUTH_CLIENT_SECRET, "grant_type": "refresh_token",
        })
        sess["a"] = tok["access_token"]
        return _yt_auth_get(url, sess["a"])

def youtube_liked(sess: dict, pages: int = 100):
    """Return liked videos with the time each item was added to Liked Videos."""
    try:
        channels = _yt_auth_call(
            sess,
            "https://www.googleapis.com/youtube/v3/channels?" + urllib.parse.urlencode({
                "part": "contentDetails", "mine": "true", "maxResults": "1",
            }),
        )
        channel_items = channels.get("items") or []
        playlist_id = (((channel_items[0].get("contentDetails") or {}).get("relatedPlaylists") or {}).get("likes")
                       if channel_items else "")
        if playlist_id:
            playlist_items, total, token = [], 0, ""
            for _ in range(pages):
                params = {"part": "snippet,contentDetails", "playlistId": playlist_id, "maxResults": "50"}
                if token:
                    params["pageToken"] = token
                data = _yt_auth_call(
                    sess,
                    "https://www.googleapis.com/youtube/v3/playlistItems?" + urllib.parse.urlencode(params),
                )
                playlist_items.extend(data.get("items", []))
                total = (data.get("pageInfo") or {}).get("totalResults", total)
                token = data.get("nextPageToken") or ""
                if not token:
                    break

            video_ids = [
                str(((item.get("snippet") or {}).get("resourceId") or {}).get("videoId") or
                    (item.get("contentDetails") or {}).get("videoId") or "")
                for item in playlist_items
            ]
            video_details = {}
            valid_ids = [video_id for video_id in video_ids if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id)]
            for start in range(0, len(valid_ids), 50):
                ids = valid_ids[start:start + 50]
                if not ids:
                    continue
                detail_data = _yt_auth_call(
                    sess,
                    "https://www.googleapis.com/youtube/v3/videos?" + urllib.parse.urlencode({
                        "part": "snippet,contentDetails,statistics", "id": ",".join(ids),
                    }),
                )
                video_details.update({item.get("id"): item for item in detail_data.get("items", [])})

            tracks = []
            for item in playlist_items:
                sn = item.get("snippet") or {}
                video_id = str((sn.get("resourceId") or {}).get("videoId") or
                               (item.get("contentDetails") or {}).get("videoId") or "")
                if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
                    continue
                raw_track = video_details.get(video_id) or item
                track = normalize_item(raw_track)
                if track.get("id") != video_id:
                    track = normalize_item(item)
                try:
                    added_at = datetime.datetime.fromisoformat(
                        str(sn.get("publishedAt") or "").replace("Z", "+00:00")
                    )
                    if added_at.tzinfo is None:
                        added_at = added_at.replace(tzinfo=datetime.timezone.utc)
                    track["likedAt"] = int(added_at.timestamp() * 1000)
                except (TypeError, ValueError, OverflowError):
                    pass
                tracks.append(track)
            tracks.sort(key=lambda item: item.get("likedAt", 0), reverse=True)
            return tracks, total or len(tracks)
    except Exception:
        # Keep likes available for accounts where the channel's likes playlist
        # is not exposed; the fallback has no reliable per-like timestamp.
        pass

    # Legacy-compatible fallback for accounts without a readable likes playlist.
    items, total, token = [], 0, ""
    for _ in range(pages):
        params = {"part": "snippet,contentDetails", "myRating": "like", "maxResults": "50"}
        if token:
            params["pageToken"] = token
        data = _yt_auth_call(sess, "https://www.googleapis.com/youtube/v3/videos?" + urllib.parse.urlencode(params))
        total = (data.get("pageInfo") or {}).get("totalResults", total)
        items += data.get("items", [])
        token = data.get("nextPageToken") or ""
        if not token:
            break
    return items, total

def youtube_playlists(sess: dict) -> list[dict]:
    """The user's own YouTube playlists via their OAuth access token.
    Access tokens live ~1h — refresh once on 401."""
    out = []
    token = ""
    for _ in range(100):
        params = {"part": "snippet,contentDetails", "mine": "true", "maxResults": "50"}
        if token:
            params["pageToken"] = token
        url = "https://www.googleapis.com/youtube/v3/playlists?" + urllib.parse.urlencode(params)
        data = _yt_auth_call(sess, url)
        for it in data.get("items", []):
            sn = it.get("snippet") or {}
            thumbs = sn.get("thumbnails") or {}
            pid = it.get("id") or ""
            if not re.fullmatch(r"[A-Za-z0-9_-]+", pid):
                continue
            out.append({
                "id": pid,
                "title": sn.get("title") or "Untitled playlist",
                "count": (it.get("contentDetails") or {}).get("itemCount", 0),
                "thumb": (thumbs.get("medium") or thumbs.get("default") or {}).get("url", ""),
            })
        token = data.get("nextPageToken") or ""
        if not token:
            break
    return out

def youtube_playlist_items(sess: dict, playlist_id: str) -> list[dict]:
    """Load all pages from an account playlist, including private playlists."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", playlist_id):
        raise ApiError(400, "bad playlist id")
    if playlist_id == "LL":
        items, _ = youtube_liked(sess, pages=100)
        return [normalize_item(item) for item in items]
    items, token = [], ""
    for _ in range(200):
        params = {"part": "snippet,contentDetails", "playlistId": playlist_id, "maxResults": "50"}
        if token:
            params["pageToken"] = token
        url = "https://www.googleapis.com/youtube/v3/playlistItems?" + urllib.parse.urlencode(params)
        data = _yt_auth_call(sess, url)
        items.extend(data.get("items", []))
        token = data.get("nextPageToken") or ""
        if not token:
            break
    tracks = [normalize_item(item) for item in items]
    tracks = [track for track in tracks if re.fullmatch(r"[A-Za-z0-9_-]{11}", track.get("id", ""))]
    for start in range(0, len(tracks), 50):
        batch = tracks[start:start + 50]
        try:
            durations = durations_for([track["id"] for track in batch])
            for track in batch:
                track["duration"] = durations.get(track["id"], track["duration"])
        except Exception:
            pass
    return tracks

def youtube_public_playlist_items(playlist_id: str) -> list[dict]:
    """Load all pages from a public playlist using the configured Data API key."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", playlist_id):
        raise ApiError(400, "bad playlist id")
    items, token = [], ""
    for _ in range(200):
        params = {"part": "snippet,contentDetails", "playlistId": playlist_id, "maxResults": "50"}
        if token:
            params["pageToken"] = token
        data = yt_api("playlistItems", params)
        items.extend(data.get("items", []))
        token = data.get("nextPageToken") or ""
        if not token:
            break
    tracks = [normalize_item(item) for item in items]
    tracks = [track for track in tracks if re.fullmatch(r"[A-Za-z0-9_-]{11}", track.get("id", ""))]
    for start in range(0, len(tracks), 50):
        batch = tracks[start:start + 50]
        try:
            durations = durations_for([track["id"] for track in batch])
            for track in batch:
                track["duration"] = durations.get(track["id"], track["duration"])
        except Exception:
            pass
    return tracks

def normalize_item(item: dict) -> dict:
    # `youtube_liked()` returns normalized records for the channel playlist
    # path, but raw YouTube API objects for its legacy fallback. Keep this
    # normalizer safe for both shapes; the `/me/liked` route normalizes again.
    normalized_id = item.get("id") if isinstance(item, dict) else None
    if (
        isinstance(normalized_id, str)
        and re.fullmatch(r"[A-Za-z0-9_-]{11}", normalized_id)
        and all(field in item for field in ("title", "artist", "thumb", "duration"))
    ):
        try:
            duration = max(0, int(float(item.get("duration") or 0)))
        except (TypeError, ValueError, OverflowError):
            duration = 0
        try:
            views = max(0, int(item.get("views") or 0))
        except (TypeError, ValueError, OverflowError):
            views = 0
        result = {
            "id": normalized_id,
            "title": str(item.get("title") or ""),
            "artist": str(item.get("artist") or ""),
            "thumb": str(item.get("thumb") or ""),
            "duration": duration,
            "views": views,
        }
        try:
            liked_at = int(item.get("likedAt") or 0)
            if liked_at > 0:
                result["likedAt"] = liked_at
        except (TypeError, ValueError, OverflowError):
            pass
        return result

    vid = (item.get("id") or {})
    video_id = vid.get("videoId") if isinstance(vid, dict) else str(vid)
    sn = item.get("snippet") or {}
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", str(video_id or "")):
        video_id = (sn.get("resourceId") or {}).get("videoId", "")
    thumbs = sn.get("thumbnails") or {}
    thumb = (thumbs.get("medium") or thumbs.get("high") or thumbs.get("default") or {}).get("url", "")
    cd = (item.get("contentDetails") or {}).get("duration", "")
    dur = 0
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", cd or "")
    if m:
        dur = int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0)
    result = {
        "id": video_id,
        "title": sn.get("title", ""),
        "artist": sn.get("channelTitle", ""),
        "thumb": thumb,
        "duration": dur,
        "views": int((item.get("statistics") or {}).get("viewCount", 0) or 0),
    }
    try:
        liked_at = int(item.get("likedAt") or 0)
        if liked_at > 0:
            result["likedAt"] = liked_at
    except (TypeError, ValueError, OverflowError):
        pass
    return result

def durations_for(ids: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for i in range(0, len(ids), 50):
        data = yt_api("videos", {"part": "contentDetails,statistics", "id": ",".join(ids[i:i + 50])})
        for it in data.get("items", []):
            cd = (it.get("contentDetails") or {}).get("duration", "")
            m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", cd or "")
            out[it["id"]] = int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0) if m else 0
    return out

# ---- yt-dlp ----
FMT_SELECTORS = {
    "audio": "bestaudio[ext=webm]/bestaudio/best[height<=480]/best",
    "video": "bestvideo[height<=720][ext=mp4]/bestvideo[height<=720]/best[height<=480][ext=mp4]/best[height<=480]",
    # Prefer the broadly supported iOS/Safari combination first. Keep the
    # existing fallbacks for sources that do not offer progressive AVC/AAC.
    "progressive": "best[height<=1080][ext=mp4][vcodec^=avc1][acodec^=mp4a]/best[height<=1080][ext=mp4]/best[height<=1080]/best",
}

def proxy_sig(video_id: str, fmt: str, exp: int, fp: str | None = None) -> str:
    payload = f"m|{video_id}|{fmt}|{exp}"
    # New media URLs are bound to the video, format, and expiry only. Mobile
    # browsers can send media requests with a different UA or network address
    # than the request that resolved the URL.
    if fp is not None:
        payload += f"|{fp}"
    return sign(payload)

def viewer_fp(handler) -> str:
    ip = handler.headers.get("X-Forwarded-For", "").split(",")[0].strip() or handler.client_address[0]
    ua = handler.headers.get("User-Agent", "")
    return sign(f"{ip}|{ua}")[:16]


def valid_proxy_sig(sig: str, video_id: str, fmt: str, exp: int, handler) -> bool:
    # Accept already-issued, fingerprinted URLs until they expire, while new
    # URLs keep working across Safari's separate fetch and media request paths.
    current = proxy_sig(video_id, fmt, exp)
    if hmac.compare_digest(sig, current):
        return True
    legacy = proxy_sig(video_id, fmt, exp, viewer_fp(handler))
    return hmac.compare_digest(sig, legacy)

def session_token(email: str, name: str, picture: str, yt_access: str, yt_refresh: str, ttl: int = 30 * 86400) -> str:
    payload = {"e": email, "n": name, "p": picture, "a": yt_access, "r": yt_refresh, "x": now() + ttl}
    raw = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    return raw + "." + sign("s|" + raw)

def read_session(handler) -> dict | None:
    auth = handler.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:]
    if "." not in token:
        return None
    raw, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, sign("s|" + raw)):
        return None
    try:
        payload = json.loads(_b64d(raw))
    except Exception:
        return None
    if payload.get("x", 0) < now():
        return None
    return payload


# ---------- lyrics (lrclib proxy + per-video cache) ----------
LYRICS_DIR = os.path.join(BASE, "lyrics_cache")
os.makedirs(LYRICS_DIR, exist_ok=True)
LYRICS_MISS_TTL = 7 * 86400

def _lrclib(path: str, params: dict):
    url = "https://lrclib.net/api/" + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "youFree/1.0 (https://code2it.com/youfree/)"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (400, 404):
            return None
        raise

def lyrics_meta(title: str, artist: str) -> tuple[str, str]:
    import html as _html
    title, artist = _html.unescape(title), _html.unescape(artist)
    sep = title.find(" - ")
    if sep >= 0:
        a, t = title[:sep].strip(), title[sep + 3:]
    else:
        a, t = re.sub(r"\s*(?:-\s*Topic|VEVO|Music)\s*$", "", artist, flags=re.I).strip(), title
    t = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", t)
    t = re.sub(r"\b(?:feat|ft)\.?\s+.*$", "", t, flags=re.I)
    t = re.sub(r"\s*[-\u2013\u2014|:]\s*$", "", t)
    t = re.sub(r"\s{2,}", " ", t).strip()
    return (a or artist.strip(), t or title.strip())

CAPTIONS_LOCK = threading.Semaphore(1)

def yt_captions(vid: str):
    with CAPTIONS_LOCK:
        return _yt_captions(vid)

CAPTION_MAX = 64


def _caption_split(g):
    text = " ".join(w for _, w in g)
    if len(text) <= CAPTION_MAX or len(g) < 2:
        return [g]
    mid = len(text) / 2
    best, best_score, pos = 1, None, 0
    for i in range(1, len(g)):
        pos += len(g[i - 1][1]) + 1
        score = abs(pos - mid) - (12 if g[i - 1][1].endswith((",", ";", ":")) else 0)
        if best_score is None or score < best_score:
            best, best_score = i, score
    return _caption_split(g[:best]) + _caption_split(g[best:])


def _caption_sentences(flat):
    groups, cur = [], []
    for i, (t, w) in enumerate(flat):
        if cur and t - cur[-1][0] > 1.6:
            groups.append(cur)
            cur = []
        cur.append((t, w))
        if re.search(r"[.!?\u2026]$", w):
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    out = []
    for g in groups:
        out.extend(_caption_split(g))
    merged = []
    for g in out:
        text = " ".join(w for _, w in g)
        if merged and len(text) < 14:
            prev = " ".join(w for _, w in merged[-1])
            if len(prev) + len(text) + 1 <= CAPTION_MAX and g[0][0] - merged[-1][-1][0] < 2.5:
                merged[-1] = merged[-1] + g
                continue
        if merged:
            prev = " ".join(w for _, w in merged[-1])
            if len(prev) < 14 and len(prev) + len(text) + 1 <= CAPTION_MAX and g[0][0] - merged[-1][-1][0] < 2.5:
                merged[-1] = merged[-1] + g
                continue
        merged.append(g)
    return merged


def _caption_text(text, lang):
    text = re.sub(r"\s+", " ", text).strip()
    if lang == "ro":
        text = re.sub(r"\b([smtlnvș])\s(a|ai|am|au|ar|aș|ați|e|o)\b", r"\1-\2", text, flags=re.I)
        text = re.sub(r"\b(te|se|ne|vă|mi|ți|le|li|i)\s(am|ai|a|au|aș|ar|e)\b", r"\1-\2", text, flags=re.I)
        text = re.sub(r"\b(să|și|că|nu|ce|mai)\s(ți|mi|și|i|l)\b(?!-)", r"\1-\2", text, flags=re.I)
        text = re.sub(r"(\w)\s(i|l)\b(?!-)", r"\1-\2", text)
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    return text[:1].upper() + text[1:] if text else text


def _yt_captions(vid: str):
    """Fallback lyrics from YouTube captions (manual subs, else original-language ASR).
    Metadata goes through the proxy; the signed timedtext URL is fetched directly
    because the proxy IP gets 429 on timedtext."""
    import urllib.request
    cmd = [YTDLP, "--no-warnings", "-q", "--skip-download", "--remote-components", "ejs:github", "-J"]
    if YTDLP_PROXY:
        cmd += ["--proxy", YTDLP_PROXY]
    cookie_copy = _cookie_snapshot()
    if cookie_copy:
        cmd[1:1] = ["--cookies", cookie_copy]
    cmd += ["--", f"https://www.youtube.com/watch?v={vid}"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    finally:
        if cookie_copy:
            try:
                os.remove(cookie_copy)
            except OSError:
                pass
    if proc.returncode != 0:
        raise RuntimeError("captions metadata failed")
    info = json.loads(proc.stdout)
    subs = {k: v for k, v in (info.get("subtitles") or {}).items() if k != "live_chat"}
    auto = info.get("automatic_captions") or {}
    lang = (info.get("language") or "").split("-")[0]
    track, source = None, None
    if subs:
        key = next((k for k in subs if lang and k.split("-")[0] == lang), next(iter(subs)))
        track, source = subs[key], "youtube-subs"
    else:
        key = next((k for k in auto if k.endswith("-orig")), None)
        if key:
            track, source = auto[key], "youtube-asr"
    if not track:
        return None
    url = next((f["url"] for f in track if f.get("ext") == "json3"), None)
    if not url:
        return None
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode("utf-8"))
    flat = []
    for ev in data.get("events") or []:
        segs = ev.get("segs")
        if not segs or ev.get("aAppend"):
            continue
        t0 = int(ev.get("tStartMs") or 0)
        ev_words = []
        for sg in segs:
            w = re.sub(r"\[[^\]]*\]", "", (sg.get("utf8") or "").replace("\n", " ")).strip()
            w = w.lstrip(">").strip()
            if w:
                ev_words.append((round((t0 + int(sg.get("tOffsetMs") or 0)) / 1000, 2), w))
        if source == "youtube-subs":
            if ev_words:
                flat.append(("line", ev_words))
        else:
            flat.extend(("word", x) for x in ev_words)
    if source == "youtube-subs":
        groups = [g for _, g in flat]
    else:
        groups = _caption_sentences([x for _, x in flat])
    lines, words = [], []
    for g in groups:
        text = _caption_text(" ".join(w for _, w in g), lang)
        if not text:
            continue
        lines.append((g[0][0], text))
        words.append({"t": g[0][0], "words": [{"w": w, "t": t} for t, w in g]})
    if len(lines) < 3:
        return None
    synced = "\n".join(f"[{int(t // 60):02d}:{t % 60:05.2f}] {x}" for t, x in lines)
    return {"source": source, "synced": synced, "plain": "\n".join(x for _, x in lines), "words": words}


def _lyrics_key(value: str) -> str:
    import unicodedata
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    normalized = "".join(char for char in normalized if unicodedata.category(char) != "Mn")
    normalized = re.sub(r"\b(?:official|music|video|lyrics|audio|hd|4k)\b", " ", normalized, flags=re.I)
    return " ".join(re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE).casefold().split())

def _lyrics_match(record: dict, artist: str, title: str, duration: int) -> bool:
    if not isinstance(record, dict):
        return False
    if _lyrics_key(record.get("artistName", "")) != _lyrics_key(artist):
        return False
    if _lyrics_key(record.get("trackName", "")) != _lyrics_key(title):
        return False
    try:
        candidate_duration = float(record.get("duration") or 0)
    except (TypeError, ValueError):
        candidate_duration = 0
    return not (duration and candidate_duration and abs(candidate_duration - duration) > 2)

def lyrics_lookup(vid: str, title: str, artist: str, duration: int) -> dict:
    cache = os.path.join(LYRICS_DIR, vid + ".json")
    a, t = lyrics_meta(title, artist)
    query = {"artist": _lyrics_key(a), "title": _lyrics_key(t)}
    try:
        with open(cache) as fh:
            hit = json.load(fh)
        if hit.get("version") == 2 and hit.get("query") == query:
            if hit.get("status") == "found" and _lyrics_match(hit.get("matched") or {}, a, t, duration):
                return hit
            if hit.get("status") == "not-found" and now() - hit.get("ts", 0) < LYRICS_MISS_TTL:
                return hit
    except Exception:
        pass
    params = {"artist_name": a, "track_name": t}
    if duration:
        params["duration"] = duration
    lrc_err = None
    found = None
    try:
        direct = _lrclib("get", params)
        if _lyrics_match(direct or {}, a, t, duration) and (direct.get("syncedLyrics") or direct.get("plainLyrics")):
            found = direct
        if not found:
            results = _lrclib("search", {"q": f"{a} {t}"}) or []
            candidates = [record for record in results
                          if _lyrics_match(record, a, t, duration)
                          and (record.get("syncedLyrics") or record.get("plainLyrics"))]
            candidates.sort(key=lambda record: not bool(record.get("syncedLyrics")))
            found = candidates[0] if candidates else None
    except Exception as error:
        lrc_err = error
    if found:
        out = {
            "id": vid, "version": 2, "query": query, "status": "found", "source": "lrclib",
            "syncedLyrics": found.get("syncedLyrics") or "", "plainLyrics": found.get("plainLyrics") or "",
            "matched": {"artistName": found.get("artistName", ""), "trackName": found.get("trackName", ""),
                        "duration": found.get("duration", 0)}, "ts": now(),
        }
    elif lrc_err is not None:
        raise lrc_err
    else:
        # YouTube captions may be unrelated dialogue, machine ASR, or an auto-translation.
        # Returning no match is safer than showing lyrics for a different recording.
        out = {"id": vid, "version": 2, "query": query, "status": "not-found", "ts": now()}
    tmp = cache + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(out, fh, ensure_ascii=False)
    os.replace(tmp, cache)
    return out

class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message

ACCOUNT_DATA_DIR = os.path.join(BASE, "private-data")
ACCOUNT_DATA_DB = os.path.join(ACCOUNT_DATA_DIR, "account_data.sqlite3")
ACCOUNT_DATA_MAX_BYTES = 64 * 1024 * 1024

def account_data_key(sess: dict) -> str:
    email = str(sess.get("e") or "").strip().casefold()
    if not email:
        raise ApiError(401, "account has no verified email")
    return hmac.new(SECRET, b"youfree-account|" + email.encode("utf-8"), hashlib.sha256).hexdigest()

def account_data_connection():
    os.makedirs(ACCOUNT_DATA_DIR, mode=0o700, exist_ok=True)
    os.chmod(ACCOUNT_DATA_DIR, 0o700)
    connection = sqlite3.connect(ACCOUNT_DATA_DB, timeout=15)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS user_data ("
            "account_key TEXT PRIMARY KEY, data_json TEXT NOT NULL, updated_at INTEGER NOT NULL)"
        )
        connection.commit()
        os.chmod(ACCOUNT_DATA_DB, 0o600)
    except Exception:
        connection.close()
        raise
    return connection

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ---------- plumbing ----------
    def _send_json(self, status: int, data, extra_headers: dict | None = None):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _error(self, err):
        if isinstance(err, ApiError):
            self._send_json(err.status, {"detail": err.message})
        elif isinstance(err, urllib.error.HTTPError):
            self._send_json(502, {"detail": f"upstream {err.code}"})
        else:
            self._send_json(500, {"detail": str(err)[:300]})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        try:
            self.route_get()
        except Exception as e:
            self._error(e)

    def do_POST(self):
        try:
            self.route_post()
        except Exception as e:
            self._error(e)

    def do_HEAD(self):
        try:
            path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
            m = re.fullmatch(r"/m/(\d+)/([a-zA-Z0-9_-]+)/([a-z]+)/([a-zA-Z0-9_-]{11})", path)
            if not m:
                self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
            exp, sig, fmt, vid = int(m.group(1)), m.group(2), m.group(3), m.group(4)
            if exp < now() or fmt not in FMT_SELECTORS or not valid_proxy_sig(sig, vid, fmt, exp, self):
                self.send_response(403); self.send_header("Content-Length", "0"); self.end_headers(); return
            st = media_state(f"{vid}.{fmt}", (vid, fmt))
            self.send_response(200)
            self.send_header("Content-Type", wait_media_content_type(st))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", "0")
            self.end_headers()
        except Exception:
            try:
                self.send_response(500); self.send_header("Content-Length", "0"); self.end_headers()
            except Exception:
                pass

    def log_message(self, *a):
        pass

    # ---------- routes ----------
    def route_get(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = urllib.parse.parse_qs(parsed.query)
        q1 = lambda k, d="": (qs.get(k, [d])[0])
        ip = self.headers.get("X-Forwarded-For", "").split(",")[0].strip() or self.client_address[0]

        if path == "/health":
            return self._send_json(200, {"ok": True, "ts": now()})

        if path == "/search":
            if not RL_API.ok(ip):
                raise ApiError(429, "rate limited")
            q = q1("q").strip()
            if not q:
                raise ApiError(400, "missing q")
            limit = min(25, max(1, int(q1("limit", "15") or 15)))
            try:
                data = yt_api("search", {"part": "snippet", "q": q, "type": "video", "videoCategoryId": "10", "maxResults": limit})
                items = [normalize_item(i) for i in data.get("items", [])]
                durs = durations_for([t["id"] for t in items if t["id"]])
                for t in items:
                    t["duration"] = durs.get(t["id"], t["duration"])
            except Exception:
                items = self._search_fallback(q, limit)
            return self._send_json(200, {"items": items})

        if path == "/trending":
            if not RL_API.ok(ip):
                raise ApiError(429, "rate limited")
            country = re.sub(r"[^A-Z]", "", q1("country", "RO").upper()) or "RO"
            data = yt_api("videos", {"part": "snippet,contentDetails,statistics", "chart": "mostPopular",
                                     "videoCategoryId": "10", "regionCode": country, "maxResults": 24})
            return self._send_json(200, {"items": [normalize_item(i) for i in data.get("items", [])]})

        if path in ("/playlist/LL", "/me/liked"):
            sess = read_session(self)
            if not sess:
                raise ApiError(401, "not authenticated")
            liked, _ = youtube_liked(sess)
            return self._send_json(200, {"items": [normalize_item(i) for i in liked]})

        m = re.fullmatch(r"/playlist/([a-zA-Z0-9_-]+)", path)
        if m:
            return self._send_json(200, {"items": youtube_public_playlist_items(m.group(1))})

        m = re.fullmatch(r"/related/([a-zA-Z0-9_-]{11})", path)
        if m:
            data = yt_api("search", {"part": "snippet", "relatedToVideoId": m.group(1), "type": "video", "videoCategoryId": "10", "maxResults": 15})
            return self._send_json(200, {"items": [normalize_item(i) for i in data.get("items", [])]})

        if path == "/lyrics":
            if not RL_API.ok(ip):
                raise ApiError(429, "rate limited")
            vid = q1("id").strip()
            if not re.fullmatch(r"[a-zA-Z0-9_-]{11}", vid):
                raise ApiError(400, "bad id")
            title, artist = q1("title").strip()[:300], q1("artist").strip()[:200]
            try:
                duration = max(0, int(float(q1("duration", "0") or 0)))
            except ValueError:
                duration = 0
            if not title:
                data = yt_api("videos", {"part": "snippet,contentDetails", "id": vid})
                items = [normalize_item(i) for i in data.get("items", [])]
                if not items:
                    raise ApiError(404, "video not found")
                title, artist = items[0].get("title", ""), items[0].get("artist", "")
                duration = duration or int(items[0].get("duration") or 0)
            return self._send_json(200, lyrics_lookup(vid, title, artist, duration),
                                   {"Cache-Control": "public, max-age=86400"})

        if path == "/stream":
            if not RL_STREAM.ok(ip):
                raise ApiError(429, "rate limited")
            vid = video_id_of(q1("id"))
            if not vid:
                raise ApiError(400, "missing/invalid id")
            fmt = q1("fmt", "audio")
            if fmt not in FMT_SELECTORS:
                fmt = "audio"
            exp = now() + MEDIA_TTL
            sig = proxy_sig(vid, fmt, exp)
            proxy = f"{PUBLIC_BASE}/m/{exp}/{sig}/{fmt}/{vid}"
            return self._send_json(200, {"id": vid, "url": proxy, "expiresIn": MEDIA_TTL})

        m = re.fullmatch(r"/m/(\d+)/([a-zA-Z0-9_-]+)/([a-z]+)/([a-zA-Z0-9_-]{11})", path)
        if m:
            exp, sig, fmt, vid = int(m.group(1)), m.group(2), m.group(3), m.group(4)
            if fmt not in FMT_SELECTORS:
                raise ApiError(400, "bad fmt")
            if exp < now():
                raise ApiError(410, "link expired — re-resolve")
            if not valid_proxy_sig(sig, vid, fmt, exp, self):
                raise ApiError(403, "bad signature")
            st = media_state(f"{vid}.{fmt}", (vid, fmt))
            return self._stream_media(st)

        # ---- auth ----
        if path == "/auth/start":
            if not OAUTH_CLIENT_ID:
                raise ApiError(503, "oauth not configured")
            params = {
                "client_id": OAUTH_CLIENT_ID,
                "redirect_uri": OAUTH_BASE + "/auth/callback",
                "response_type": "code",
                "scope": "openid email profile https://www.googleapis.com/auth/youtube.readonly",
                "access_type": "offline",
                "prompt": "consent",
                "state": sign("oauth|" + q1("r")[:200]),
            }
            return self._redirect("https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params))

        if path == "/auth/callback":
            code = q1("code")
            if not code:
                raise ApiError(400, "missing code")
            tok = http_post_json("https://oauth2.googleapis.com/token", {
                "code": code, "client_id": OAUTH_CLIENT_ID, "client_secret": OAUTH_CLIENT_SECRET,
                "redirect_uri": OAUTH_BASE + "/auth/callback", "grant_type": "authorization_code",
            })
            info = http_get_json("https://openidconnect.googleapis.com/v1/userinfo?alt=json&access_token=" + urllib.parse.quote(tok["access_token"]))
            session = session_token(info.get("email", ""), info.get("name", ""), info.get("picture", ""),
                                    tok["access_token"], tok.get("refresh_token", ""))
            return self._redirect(APP_URL + "#yt_auth=" + urllib.parse.quote(session))

        if path == "/me":
            sess = read_session(self)
            if not sess:
                raise ApiError(401, "not authenticated")
            return self._send_json(200, {"email": sess["e"], "name": sess["n"], "picture": sess["p"]})

        if path == "/me/data":
            sess = read_session(self)
            if not sess:
                raise ApiError(401, "not authenticated")
            connection = account_data_connection()
            try:
                row = connection.execute(
                    "SELECT data_json, updated_at FROM user_data WHERE account_key = ?",
                    (account_data_key(sess),),
                ).fetchone()
            finally:
                connection.close()
            if not row:
                return self._send_json(200, {"data": {}, "updatedAt": 0})
            try:
                data = json.loads(row[0])
            except (TypeError, ValueError):
                data = {}
            return self._send_json(200, {"data": data, "updatedAt": row[1]})

        if path == "/me/playlists":
            sess = read_session(self)
            if not sess:
                raise ApiError(401, "not authenticated")
            playlists = youtube_playlists(sess)
            try:
                liked, total = youtube_liked(sess, pages=1)
                thumbs = ((liked[0].get("snippet") or {}).get("thumbnails") or {}) if liked else {}
                playlists.insert(0, {"id": "LL", "title": "Liked videos", "count": total,
                                     "thumb": (thumbs.get("medium") or thumbs.get("default") or {}).get("url", "")})
            except Exception:
                pass
            return self._send_json(200, {"playlists": playlists})

        m = re.fullmatch(r"/me/playlist/([a-zA-Z0-9_-]+)", path)
        if m:
            sess = read_session(self)
            if not sess:
                raise ApiError(401, "not authenticated")
            return self._send_json(200, {"items": youtube_playlist_items(sess, m.group(1))})

        raise ApiError(404, "not found")

    def route_post(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        length = int(self.headers.get("Content-Length") or 0)
        if length > ACCOUNT_DATA_MAX_BYTES:
            raise ApiError(413, "account data exceeds the 64 MiB limit")
        body = {}
        if length:
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                raise ApiError(400, "invalid JSON body")
        if not isinstance(body, dict):
            raise ApiError(400, "expected a JSON object")

        if path == "/me/data":
            sess = read_session(self)
            if not sess:
                raise ApiError(401, "not authenticated")
            data = body.get("data")
            if not isinstance(data, dict):
                raise ApiError(400, "account data must be a JSON object")
            serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            if len(serialized.encode("utf-8")) > ACCOUNT_DATA_MAX_BYTES:
                raise ApiError(413, "account data exceeds the 64 MiB limit")
            updated_at = int(time.time() * 1000)
            connection = account_data_connection()
            try:
                connection.execute(
                    "INSERT INTO user_data(account_key, data_json, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(account_key) DO UPDATE SET data_json=excluded.data_json, updated_at=excluded.updated_at",
                    (account_data_key(sess), serialized, updated_at),
                )
                connection.commit()
            finally:
                connection.close()
            return self._send_json(200, {"ok": True, "updatedAt": updated_at})

        if path == "/auth/refresh":
            sess = read_session(self)
            if not sess or not sess.get("r"):
                raise ApiError(401, "no refresh token")
            tok = http_post_json("https://oauth2.googleapis.com/token", {
                "refresh_token": sess["r"], "client_id": OAUTH_CLIENT_ID,
                "client_secret": OAUTH_CLIENT_SECRET, "grant_type": "refresh_token",
            })
            session = session_token(sess["e"], sess["n"], sess["p"], tok["access_token"], sess["r"])
            return self._send_json(200, {"token": session})

        raise ApiError(404, "not found")

    # ---------- pieces ----------
    def _search_fallback(self, q: str, limit: int) -> list[dict]:
        cmd = [YTDLP, "--flat-playlist", "--no-warnings", "-j"]
        if YTDLP_PROXY:
            cmd += ["--proxy", YTDLP_PROXY]
        cmd += [f"ytsearch{limit}:{q}"]
        cookie_copy = _cookie_snapshot()
        if cookie_copy:
            cmd[1:1] = ["--cookies", cookie_copy]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        finally:
            if cookie_copy:
                try:
                    os.remove(cookie_copy)
                except OSError:
                    pass
        items = []
        for line in proc.stdout.splitlines():
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("id"):
                items.append({"id": e["id"], "title": e.get("title", ""), "artist": e.get("uploader", "") or e.get("channel", ""),
                              "thumb": (e.get("thumbnails") or [{}])[-1].get("url", "") if e.get("thumbnails") else f"https://i.ytimg.com/vi/{e['id']}/mqdefault.jpg",
                              "duration": int(e.get("duration") or 0), "views": e.get("view_count") or 0})
        return items[:limit]

    def _stream_media(self, st: dict):
        """Serve complete progressive files so Safari gets valid MP4 metadata and Range responses."""
        mime = st["mime"] or ("audio/webm" if st["fmt"] == "audio" else "video/mp4")
        rng = self.headers.get("Range")
        start = 0
        end = -1  # unknown until complete
        if rng:
            m = re.match(r"bytes=(\d+)-(\d*)", rng)
            if m:
                start = int(m.group(1))
                if m.group(2):
                    end = int(m.group(2))

        done = st["done"].is_set()
        if not done and st["fmt"] == "progressive":
            done = st["done"].wait(timeout=PROGRESSIVE_WAIT_TIMEOUT)
            if not done:
                raise ApiError(504, "video is still being prepared; retry shortly")
        if done and st["error"]:
            # failure is cached per key; let a fresh sig retry by removing state
            with MEDIA_GUARD:
                MEDIA.pop(f'{st["vid"]}.{st["fmt"]}', None)
            raise ApiError(502, st["error"])
        size = media_size(st) if done else 0

        if done:
            # full file known — normal static serve with ranges
            if start >= size:
                return self._send_json(416, {"detail": "range not satisfiable"})
            final_end = size - 1 if end < 0 or end >= size else end
            length = final_end - start + 1
            self.send_response(206 if start or final_end < size - 1 else 200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(length))
            if start or final_end < size - 1:
                self.send_header("Content-Range", f"bytes {start}-{final_end}/{size}")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Expose-Headers", "Content-Range, Content-Length, Accept-Ranges")
            self.end_headers()
            with open(st["path"], "rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    remaining -= len(chunk)
            return

        # still downloading — wait for the temp file to appear (yt-dlp may
        # still be in extraction), then stream following file growth
        waited = 0.0
        while not os.path.exists(st["path"]) and not st["done"].is_set() and waited < 20:
            time.sleep(0.1)
            waited += 0.1
        if st["error"]:
            with MEDIA_GUARD:
                MEDIA.pop(f'{st["vid"]}.{st["fmt"]}', None)
            raise ApiError(502, st["error"])
        if not os.path.exists(st["path"]):
            raise ApiError(502, "download did not produce a file")
        mime = wait_media_content_type(st)
        if end >= 0 and start == 0:
            end = -1  # ignore end while size unknown
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers", "Content-Range, Content-Length, Accept-Ranges")
        self.end_headers()
        pos = start
        with open(st["path"], "rb") as fh:
            fh.seek(pos)
            stall = 0
            while True:
                avail = media_size(st) - pos
                if avail <= 0:
                    if st["done"].is_set():
                        break
                    time.sleep(0.15)
                    stall += 1
                    if stall > 1200:  # ~3min without progress — dead download
                        break
                    continue
                stall = 0
                chunk = fh.read(min(64 * 1024, avail))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                pos += len(chunk)
                touch_media(st)
                if end >= 0 and pos > end:
                    break

def main():
    # wipe stale temp files from a previous run
    try:
        for f in os.listdir(TMP):
            os.remove(os.path.join(TMP, f))
    except OSError:
        pass
    threading.Thread(target=media_sweep, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"ytapi listening on 127.0.0.1:{PORT}")
    server.serve_forever()

if __name__ == "__main__":
    main()
