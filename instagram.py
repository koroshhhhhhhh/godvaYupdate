"""Instagram: reels, videos, photos, carousels (sent as albums) and stories.

* Reels / single videos -> yt-dlp.
* Photos, carousels (photos + videos mixed) and stories -> gallery-dl
  (yt-dlp cannot read Instagram photos).
Each method is the fallback of the other. A logged-in cookie file is used for both.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import aiohttp
from yt_dlp import YoutubeDL

import social
from soundcloud import EXECUTOR, INFO_MAX_AGE, ScError, first, iso_date, to_int

log = logging.getLogger("instagram")

COOKIES: Optional[str] = None
PROXY: Optional[str] = None
VIDEO_EXT = {"mp4", "mov", "webm", "mkv", "m4v"}
IMAGE_EXT = {"jpg", "jpeg", "png", "webp", "heic"}

AUTH_MSG = ("اینستاگرام برای این محتوا ورود (لاگین) می‌خواهد؛ مثلاً استوری، پیج خصوصی "
            "یا محدودیت سرور. کوکی اینستاگرام باید در ربات تنظیم شود.")
AUTH_REJECTED_MSG = ("اینستاگرام نشست (کوکی) فعلی را قبول نکرد. کوکی منقضی شده یا اینستاگرام "
                     "IP سرور را محدود کرده است. کوکی تازه یا پراکسی لازم است.")

IG_RE = re.compile(
    r"https?://(?:www\.)?(?:instagram\.com|instagr\.am)/(?:[\w.]+/)?(?:p|reels?|tv)/[^\s<>\"']+"
    r"|https?://(?:www\.)?instagram\.com/(?:stories|share)/[^\s<>\"']+",
    re.I,
)
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


class AuthRequired(ScError):
    """Instagram wants a logged-in session."""


def configure(cookies_file: Optional[str], proxy: Optional[str] = None) -> None:
    global COOKIES, PROXY
    COOKIES = cookies_file if cookies_file and os.path.isfile(cookies_file) else None
    PROXY = (proxy or "").strip() or None


def detect(text: Optional[str]) -> Optional[tuple[str, str]]:
    m = IG_RE.search(text or "")
    return ("instagram", m.group(0).rstrip(".,;:!?)]}»")) if m else None


@contextlib.contextmanager
def _cookie_copy():
    """Both yt-dlp and gallery-dl may rewrite the cookie file; parallel jobs get a private copy."""
    if not COOKIES:
        yield None
        return
    fd, tmp = tempfile.mkstemp(prefix="igck_", suffix=".txt")
    os.close(fd)
    try:
        shutil.copyfile(COOKIES, tmp)
        yield tmp
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


async def resolve(url: str) -> str:
    """Follow instagram.com/share/... links."""
    if "/share/" not in url:
        return url
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), headers=_UA) as s:
            async with s.get(url, allow_redirects=True) as r:
                final = str(r.url)
        return url if "accounts/login" in final else final
    except Exception as exc:
        log.debug("resolve failed for %s: %s", url, exc)
        return url


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
@dataclass
class IgItem:
    kind: str                       # "video" | "photo"
    url: str = ""
    ext: str = ""


@dataclass
class IgInfo:
    url: str
    mode: str                       # "ytdlp" | "gdl"
    platform: str = "instagram"
    title: str = ""
    description: str = ""
    author: str = ""
    author_name: str = ""
    date: Optional[str] = None
    likes: Optional[int] = None
    views: Optional[int] = None
    comments: Optional[int] = None
    story: bool = False
    items: list = field(default_factory=list)
    ytdlp_info: Optional[dict] = None
    fetched_at: float = 0.0

    @property
    def brand(self) -> str:
        return "اینستاگرام"

    @property
    def photos(self) -> int:
        return sum(1 for i in self.items if i.kind == "photo")

    @property
    def videos(self) -> int:
        return sum(1 for i in self.items if i.kind == "video")

    @property
    def kind_text(self) -> str:
        p, v = self.photos, self.videos
        if p + v == 1 and not self.story:
            return "ویدیو" if v else "عکس"
        parts = []
        if p:
            parts.append(f"{p} عکس")
        if v:
            parts.append(f"{v} ویدیو")
        return ("استوری: " if self.story else "آلبوم: ") + " و ".join(parts)


@dataclass
class LocalItem:
    path: Path
    kind: str
    width: Optional[int] = None
    height: Optional[int] = None
    duration: Optional[int] = None
    thumb: Optional[Path] = None


def _classify(text: str) -> ScError:
    s = text.lower()
    if any(k in s for k in ("authrequired", "login", "log in", "cookies", "401", "403", "checkpoint",
                            "challenge", "rate-limit", "rate limit", "empty media response",
                            "not available", "redirect", "unauthorized", "wait a few minutes",
                            "please wait", "require_login", "feedback_required")):
        return AuthRequired(AUTH_REJECTED_MSG if COOKIES else AUTH_MSG)
    if "private" in s:
        return ScError("این محتوا خصوصی است.")
    if "404" in s or "not found" in s or "notfound" in s:
        return ScError("محتوا پیدا نشد یا حذف شده است.")
    if "429" in s or "too many" in s:
        return ScError("تعداد درخواست‌ها زیاد است. کمی بعد دوباره امتحان کنید.")
    return ScError("دریافت از اینستاگرام ممکن نشد. دوباره امتحان کنید.")


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #
def _ytdlp_opts(ck: Optional[str], **extra) -> dict:
    opts = {"quiet": True, "no_warnings": True, "socket_timeout": 20, "retries": 3}
    if ck:
        opts["cookiefile"] = ck
    if PROXY:
        opts["proxy"] = PROXY
    opts.update(extra)
    return opts


def _ytdlp_fetch_sync(url: str) -> IgInfo:
    try:
        with _cookie_copy() as ck, YoutubeDL(_ytdlp_opts(ck, skip_download=True)) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise _classify(str(exc)) from exc
    if not info:
        raise ScError("اطلاعاتی از این لینک دریافت نشد.")
    entries = [e for e in (info.get("entries") or []) if e] or [info]
    ts = info.get("timestamp")
    return IgInfo(
        url=info.get("webpage_url") or url, mode="ytdlp",
        description=(info.get("description") or "").strip(),
        author=info.get("uploader_id") or info.get("channel") or "",
        author_name=info.get("uploader") or "",
        date=iso_date(first(info.get("upload_date"), ts and time.strftime("%Y%m%d", time.gmtime(ts)))),
        likes=to_int(info.get("like_count")), views=to_int(info.get("view_count")),
        comments=to_int(info.get("comment_count")),
        items=[IgItem("video", url) for _ in entries],
        ytdlp_info=info, fetched_at=time.monotonic(),
    )


def _gdl_fetch_sync(url: str) -> IgInfo:
    with _cookie_copy() as ck:
        cmd = [sys.executable, "-m", "gallery_dl", "-j"]
        if ck:
            cmd += ["--cookies", ck]
        if PROXY:
            cmd += ["--proxy", PROXY]
        cmd.append(url)
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        except subprocess.TimeoutExpired:
            raise ScError("اینستاگرام پاسخ نداد. دوباره امتحان کنید.")
        except FileNotFoundError:
            raise ScError("ماژول gallery-dl نصب نیست.")

    data = []
    try:
        data = json.loads(p.stdout) if p.stdout.strip() else []
    except json.JSONDecodeError:
        log.warning("gallery-dl output is not json: %s", p.stdout[:200])

    items, meta, seen = [], {}, set()
    for msg in data:
        if not (isinstance(msg, list) and len(msg) >= 3 and msg[0] == 3):
            continue
        murl, kw = msg[1], (msg[2] if isinstance(msg[2], dict) else {})
        if not isinstance(murl, str) or not murl.startswith("http"):
            continue
        key = urlparse(murl).path
        if key in seen:
            continue
        seen.add(key)
        ext = str(kw.get("extension") or Path(urlparse(murl).path).suffix.lstrip(".")).lower()
        items.append(IgItem("video" if ext in VIDEO_EXT else "photo", murl, ext))
        meta = meta or kw
    if not items:
        err = (p.stderr or "").strip()
        log.warning("gallery-dl found nothing for %s (exit %s): %s", url, p.returncode, err[-700:] or "(no error text)")
        raise _classify(err or "not found")

    return IgInfo(
        url=first(meta.get("post_url"), url), mode="gdl",
        description=(meta.get("description") or "").strip(),
        author=str(meta.get("username") or ""), author_name=str(meta.get("fullname") or ""),
        date=iso_date(meta.get("date")), likes=to_int(meta.get("likes")),
        story="/stories/" in url, items=items, fetched_at=time.monotonic(),
    )


async def fetch(url: str) -> IgInfo:
    url = await resolve(url)
    loop = asyncio.get_running_loop()
    is_reel = bool(re.search(r"/(?:reels?|tv)/", url))
    methods = [("ytdlp", _ytdlp_fetch_sync), ("gdl", _gdl_fetch_sync)]
    if not is_reel:  # posts / carousels / stories: gallery-dl first
        methods.reverse()
    errors: list[ScError] = []
    for name, fn in methods:
        try:
            return await loop.run_in_executor(EXECUTOR, fn, url)
        except ScError as e:
            log.info("instagram %s failed: %s", name, e.user_message)
            errors.append(e)
        except Exception as exc:
            log.warning("instagram %s crashed: %s", name, exc)
            errors.append(ScError("دریافت از اینستاگرام ممکن نشد."))
    raise next((e for e in errors if isinstance(e, AuthRequired)), errors[0])


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #
ProgressCb = Callable[[int, int, float, Optional[float], float], None]


def _ytdlp_download_sync(info: IgInfo, workdir: Path, cb: ProgressCb) -> list[Path]:
    def hook(d: dict) -> None:
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            if total:
                frac = done / total
            elif d.get("fragment_count"):
                frac = (d.get("fragment_index") or 0) / d["fragment_count"]
            else:
                frac = 0.0
            cb(done, total, d.get("speed") or 0.0, d.get("eta"), max(0.0, min(frac, 0.99)))
        elif d.get("status") == "finished":
            size = d.get("total_bytes") or d.get("downloaded_bytes") or 0
            cb(size, size, 0.0, 0, 1.0)

    def run(reuse: bool) -> None:
        with _cookie_copy() as ck:
            opts = _ytdlp_opts(
                ck, format="bv*+ba/b", format_sort=["vcodec:h264", "res", "acodec:aac"],
                merge_output_format="mp4", outtmpl=str(workdir / "m_%(autonumber)02d.%(ext)s"),
                concurrent_fragment_downloads=16, fragment_retries=5, overwrites=True,
                noprogress=True, progress_hooks=[hook],
            )
            with YoutubeDL(opts) as ydl:
                if reuse and info.ytdlp_info:
                    ydl.process_ie_result(copy.deepcopy(info.ytdlp_info), download=True)
                else:
                    ydl.extract_info(info.url, download=True)

    reuse = (time.monotonic() - info.fetched_at) < INFO_MAX_AGE
    try:
        run(reuse)
    except Exception as exc:
        if not reuse:
            raise _classify(str(exc)) from exc
        for p in workdir.glob("m_*"):
            p.unlink(missing_ok=True)
        try:
            run(False)
        except Exception as exc2:
            raise _classify(str(exc2)) from exc2

    files = sorted(p for p in workdir.glob("m_*") if p.suffix not in {".part", ".ytdl", ".temp"})
    if not files:
        raise ScError("فایلی دریافت نشد.")
    return files


_CTYPE_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
              "video/mp4": "mp4", "video/quicktime": "mov"}


async def _http_download(info: IgInfo, workdir: Path, cb: ProgressCb) -> list[Path]:
    out: list[Path] = []
    n = len(info.items)
    got_total, t0 = 0, time.monotonic()
    timeout = aiohttp.ClientTimeout(total=600, sock_connect=15)
    async with aiohttp.ClientSession(timeout=timeout, headers=_UA) as s:
        for i, it in enumerate(info.items):
            async with s.get(it.url) as r:
                if r.status != 200:
                    raise ScError("دریافت فایل از اینستاگرام ممکن نشد (لینک منقضی شده است).")
                ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
                ext = _CTYPE_EXT.get(ctype) or it.ext or ("mp4" if it.kind == "video" else "jpg")
                path = workdir / f"m_{i + 1:02d}.{ext}"
                total = r.content_length or 0
                got = 0
                with path.open("wb") as f:
                    async for chunk in r.content.iter_chunked(256 * 1024):
                        f.write(chunk)
                        got += len(chunk)
                        got_total += len(chunk)
                        part = (got / total) if total else 0.0
                        elapsed = max(time.monotonic() - t0, 0.001)
                        cb(got_total, 0, got_total / elapsed, None, min((i + part) / n, 0.99))
            out.append(path)
    cb(got_total, got_total, 0.0, 0, 1.0)
    return out


async def download(info: IgInfo, workdir: Path, cb: ProgressCb) -> list[LocalItem]:
    loop = asyncio.get_running_loop()
    if info.mode == "ytdlp":
        paths = await loop.run_in_executor(EXECUTOR, _ytdlp_download_sync, info, workdir, cb)
    else:
        paths = await _http_download(info, workdir, cb)
    return [
        LocalItem(p, "photo" if p.suffix.lower().lstrip(".") in IMAGE_EXT else "video")
        for p in paths
    ]


# --------------------------------------------------------------------------- #
# Prepare for Telegram
# --------------------------------------------------------------------------- #
async def prepare(files: list[LocalItem], workdir: Path) -> list[LocalItem]:
    for i, m in enumerate(files, 1):
        sub = workdir / f"i{i}"
        sub.mkdir(exist_ok=True)
        if m.kind == "video":  # same light-weight preparation as every other video
            lm = await social.prepare(m.path, sub)
            m.path, m.width, m.height, m.duration, m.thumb = lm.path, lm.width, lm.height, lm.duration, lm.thumb
            continue
        _, _, w, h, _ = social._streams(await social._probe(m.path))
        ext = m.path.suffix.lower().lstrip(".")
        too_big = bool(w and h and (w + h > 9500))
        if ext not in ("jpg", "jpeg", "png") or too_big:
            out = sub / "photo.jpg"
            await social.run_ffmpeg(
                "-i", str(m.path), "-frames:v", "1", "-pix_fmt", "yuvj420p",
                "-vf", "scale='min(4000,iw)':'min(4000,ih)':force_original_aspect_ratio=decrease",
                "-q:v", "2", str(out),
            )
            m.path = out
            _, _, w, h, _ = social._streams(await social._probe(out))
        m.width, m.height = w, h
    return files


# --------------------------------------------------------------------------- #
# Cookie self-test (written to the log at startup)
# --------------------------------------------------------------------------- #
async def check_session() -> None:
    """Ask Instagram whether the cookie file is a valid logged-in session from THIS server."""
    if not COOKIES:
        log.warning("Instagram session check skipped: no cookie file")
        return
    import http.cookiejar
    try:
        jar = http.cookiejar.MozillaCookieJar(COOKIES)
        jar.load(ignore_discard=True, ignore_expires=True)
        cookies = {c.name: c.value for c in jar if "instagram" in c.domain}
    except Exception as exc:
        log.warning("Instagram session check: cookie file unreadable (%s)", exc)
        return
    if "sessionid" not in cookies:
        log.warning("Instagram session check: the cookie file has no sessionid (not logged in when exported)")
        return
    headers = dict(_UA)
    headers.update({
        "X-IG-App-ID": "936619743392459", "X-Requested-With": "XMLHttpRequest",
        "X-CSRFToken": cookies.get("csrftoken", ""), "Referer": "https://www.instagram.com/",
        "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
    })
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
            async with s.get("https://www.instagram.com/api/v1/accounts/current_user/?edit=true",
                             proxy=PROXY, allow_redirects=False) as r:
                body = await r.text()
                status = r.status
        data = json.loads(body) if body.strip().startswith("{") else {}
    except Exception as exc:
        log.warning("Instagram session check could not run: %s", exc)
        return
    user = (data.get("user") or {}).get("username")
    if status == 200 and user:
        log.info("Instagram session OK: logged in as @%s", user)
    else:
        log.warning("Instagram session REJECTED from this server (HTTP %s, %s). The cookie is expired, "
                    "or Instagram blocks this server's IP: export a fresh cookie, or set INSTAGRAM_PROXY.",
                    status, data.get("message") or data.get("error_type") or "no details")
