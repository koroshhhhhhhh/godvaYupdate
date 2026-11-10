"""Pinterest (video pins only) via yt-dlp."""
from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import aiohttp
from yt_dlp import YoutubeDL

from soundcloud import EXECUTOR, INFO_MAX_AGE, ScError, first, iso_date, to_int
from soundcloud import run_ffmpeg as _run_ffmpeg

log = logging.getLogger("social")

BRANDS = {"pinterest": "پینترست", "youtube": "یوتیوب"}
EXTRA_OPTS: dict[str, dict] = {}  # per-platform extra yt-dlp options (see custom_site.py)
REWRITERS: dict[str, Callable[[str], str]] = {}  # per-platform URL normalisers (see custom_site.py)
NO_VIDEO = "ویدیویی در این لینک پیدا نشد."
PIN_NO_VIDEO = "این پین ویدیو ندارد. فقط پین‌های ویدیویی پشتیبانی می‌شوند."
TRANSCODE_SEM = asyncio.Semaphore(1)  # one heavy re-encode at a time

PIN_RE = re.compile(
    r"https?://(?:[\w-]+\.)?pinterest\.[a-z.]{2,6}/pin/[^\s<>\"']+|https?://pin\.it/[^\s<>\"']+",
    re.I,
)
YT_RE = re.compile(
    r"https?://(?:(?:www|m|music)\.)?youtube\.com/(?:watch\?(?:[^\s#]*&)?v=|shorts/|live/)[\w-]{6,}[^\s<>\"']*"
    r"|https?://youtu\.be/[\w-]{6,}[^\s<>\"']*",
    re.I,
)
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


async def run_ffmpeg(*args: str) -> None:
    await _run_ffmpeg(*args, low_priority=True)


@contextlib.contextmanager
def _private_cookiefile(opts: dict):
    """yt-dlp rewrites its cookie file when it closes, so parallel jobs must not share one file:
    every job gets a private copy. A missing cookie file is simply ignored."""
    cf = opts.get("cookiefile")
    if not cf:
        yield opts
        return
    if not os.path.isfile(cf):
        yield {k: v for k, v in opts.items() if k != "cookiefile"}
        return
    fd, tmp = tempfile.mkstemp(prefix="ytck_", suffix=".txt")
    os.close(fd)
    try:
        shutil.copyfile(cf, tmp)
        yield {**opts, "cookiefile": tmp}
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def detect(text: Optional[str]) -> Optional[tuple[str, str]]:
    for platform, rx in (("pinterest", PIN_RE), ("youtube", YT_RE)):
        m = rx.search(text or "")
        if m:
            return platform, m.group(0).rstrip(".,;:!?)]}»")
    return None


async def resolve(url: str) -> str:
    """Follow pin.it short links."""
    if "pin.it/" not in url:
        return url
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), headers=_UA) as s:
            async with s.get(url, allow_redirects=True) as r:
                return str(r.url)
    except Exception as exc:
        log.debug("resolve failed for %s: %s", url, exc)
        return url


@dataclass
class MediaInfo:
    platform: str
    url: str
    title: str = ""
    description: str = ""
    author: str = ""
    author_name: str = ""
    date: Optional[str] = None
    likes: Optional[int] = None
    views: Optional[int] = None
    comments: Optional[int] = None
    duration: Optional[float] = None
    heights: list = field(default_factory=list)   # video heights the source offers (e.g. [240, 480, 720])
    ytdlp_info: Optional[dict] = None
    fetched_at: float = 0.0

    @property
    def brand(self) -> str:
        return BRANDS.get(self.platform, self.platform)


@dataclass
class LocalMedia:
    path: Path
    width: Optional[int] = None
    height: Optional[int] = None
    duration: Optional[int] = None
    thumb: Optional[Path] = None


def _classify(text: str, platform: str = "pinterest") -> ScError:
    s = text.lower()
    brand = BRANDS.get(platform, platform)
    if platform == "youtube":
        if "not a bot" in s or "sign in to confirm" in s:
            return ScError("یوتیوب این سرور را ربات تشخیص داد و ورود می‌خواهد. کوکی یوتیوب (youtube_cookies.txt) "
                           "یا پراکسی (YOUTUBE_PROXY) لازم است.")
        if "private video" in s or "members-only" in s or "members only" in s:
            return ScError("این ویدیو خصوصی است یا مخصوص اعضای کانال است.")
        if "age" in s and "confirm" in s or "age-restricted" in s:
            return ScError("این ویدیو محدودیت سنی دارد و بدون کوکی یوتیوب دانلود نمی‌شود.")
        if "unavailable" in s or "removed" in s or "does not exist" in s or "copyright" in s:
            return ScError("این ویدیو در دسترس نیست یا حذف شده است.")
        if "live event" in s or "is live" in s or "premieres in" in s:
            return ScError("پخش زنده یا ویدیوی هنوز منتشرنشده پشتیبانی نمی‌شود.")
    if any(k in s for k in ("no video", "unsupported url", "no formats", "video formats")):
        return ScError(PIN_NO_VIDEO if platform == "pinterest" else NO_VIDEO)
    if "redirection detected" in s or "require login" in s or "log in" in s or "login" in s:
        return ScError("سایت این ویدیو را برای سرور ربات نشان نمی‌دهد: لینک حذف شده، کوکی منقضی شده، "
                       "یا منطقه/IP سرور محدود است.")
    if "410" in s or "gone" in s:
        return ScError("این ویدیو حذف شده یا دیگر در دسترس نیست.")
    if "403" in s or "forbidden" in s:
        return ScError("دسترسی به این ویدیو محدود است (منطقه سرور، سن یا خصوصی بودن).")
    if "404" in s or "not found" in s:
        return ScError("پین پیدا نشد یا حذف شده است." if platform == "pinterest"
                       else "محتوا پیدا نشد یا حذف شده است.")
    if "429" in s or "too many" in s:
        return ScError("تعداد درخواست‌ها زیاد است. کمی بعد دوباره امتحان کنید.")
    return ScError(f"دریافت از {brand} ممکن نشد. دوباره امتحان کنید.")


def _fetch_sync(url: str, platform: str = "pinterest") -> MediaInfo:
    opts = {"quiet": True, "no_warnings": True, "socket_timeout": 20, "retries": 3,
            "skip_download": True, "noplaylist": True, **EXTRA_OPTS.get(platform, {})}
    try:
        with _private_cookiefile(opts) as o, YoutubeDL(o) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise _classify(str(exc), platform) from exc
    if not info:
        raise ScError("اطلاعاتی از این لینک دریافت نشد.")
    fmts = info.get("formats") or []
    if not (info.get("url") or any(f.get("vcodec") not in (None, "none") for f in fmts)):
        raise ScError(PIN_NO_VIDEO if platform == "pinterest" else NO_VIDEO)
    if info.get("is_live"):
        raise ScError("پخش زنده پشتیبانی نمی‌شود.")
    heights = sorted({int(f["height"]) for f in fmts
                      if f.get("height") and f.get("vcodec") not in (None, "none")})
    if info.get("height") and not heights:
        heights = [int(info["height"])]
    ts = info.get("timestamp")
    return MediaInfo(
        platform=platform, url=info.get("webpage_url") or url,
        duration=info.get("duration"), heights=heights,
        title=info.get("title") or "", description=(info.get("description") or "").strip(),
        author=info.get("uploader_id") or info.get("channel") or "",
        author_name=info.get("uploader") or "",
        date=iso_date(first(info.get("upload_date"), ts and time.strftime("%Y%m%d", time.gmtime(ts)))),
        likes=to_int(info.get("like_count")), views=to_int(info.get("view_count")),
        comments=to_int(info.get("comment_count")),
        ytdlp_info=info, fetched_at=time.monotonic(),
    )


async def fetch(platform: str, url: str) -> MediaInfo:
    if platform == "pinterest":
        url = await resolve(url)
    url = REWRITERS.get(platform, lambda u: u)(url)
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(EXECUTOR, _fetch_sync, url, platform)
    except ScError:
        raise
    except Exception as exc:
        log.warning("%s fetch failed: %s", platform, exc)
        raise ScError(f"دریافت از {BRANDS.get(platform, platform)} ممکن نشد. دوباره امتحان کنید.") from exc


ProgressCb = Callable[[int, int, float, Optional[float], float], None]


def _format_for(quality: Optional[int]) -> str:
    """yt-dlp format: the best video up to the chosen height (0/None = best available)."""
    if not quality:
        return "bv*+ba/b"
    q = int(quality)
    return f"bv*[height<={q}]+ba/b[height<={q}]/bv*+ba/b"


def _download_sync(info: MediaInfo, workdir: Path, cb: ProgressCb, quality: Optional[int] = None) -> Path:
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

    opts = {
        "quiet": True, "no_warnings": True, "socket_timeout": 20, "retries": 5,
        "noplaylist": True, "overwrites": True, "noprogress": True,
        "format": _format_for(quality), "format_sort": ["vcodec:h264", "res", "acodec:aac"],
        "merge_output_format": "mp4", "outtmpl": str(workdir / "m_01.%(ext)s"),
        "concurrent_fragment_downloads": 16, "fragment_retries": 5, "progress_hooks": [hook],
        **EXTRA_OPTS.get(info.platform, {}),
    }

    def run(reuse: bool) -> None:
        with _private_cookiefile(opts) as o, YoutubeDL(o) as ydl:
            if reuse and info.ytdlp_info:
                ydl.process_ie_result(copy.deepcopy(info.ytdlp_info), download=True)
            else:
                ydl.extract_info(info.url, download=True)

    reuse = (time.monotonic() - info.fetched_at) < INFO_MAX_AGE
    try:
        run(reuse)
    except Exception as exc:
        if not reuse:
            raise _classify(str(exc), info.platform) from exc
        for p in workdir.glob("m_*"):
            p.unlink(missing_ok=True)
        try:
            run(False)
        except Exception as exc2:
            raise _classify(str(exc2), info.platform) from exc2

    files = [p for p in workdir.glob("m_*") if p.suffix not in {".part", ".ytdl", ".temp"}]
    if not files:
        raise ScError("فایلی دریافت نشد.")
    return max(files, key=lambda p: p.stat().st_size)


async def download(info: MediaInfo, workdir: Path, cb: ProgressCb, quality: Optional[int] = None) -> Path:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(EXECUTOR, _download_sync, info, workdir, cb, quality)


async def _probe(path: Path) -> dict:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    try:
        return json.loads(out or b"{}")
    except json.JSONDecodeError:
        return {}


def _streams(probe: dict):
    v = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in probe.get("streams", []) if s.get("codec_type") == "audio"), {})
    w, h = to_int(v.get("width")), to_int(v.get("height"))
    rot = (v.get("tags") or {}).get("rotate")
    for sd in v.get("side_data_list") or []:
        rot = sd.get("rotation", rot)
    try:
        if rot is not None and abs(int(float(rot))) % 180 == 90:
            w, h = h, w
    except (TypeError, ValueError):
        pass
    dur = first(v.get("duration"), (probe.get("format") or {}).get("duration"))
    try:
        dur = int(float(dur)) if dur else None
    except ValueError:
        dur = None
    return v, a, w, h, dur


async def _thumb(video: Path, out: Path) -> Optional[Path]:
    for seek in ("0.3", "0"):
        try:
            await run_ffmpeg("-ss", seek, "-i", str(video), "-frames:v", "1", "-pix_fmt", "yuvj420p",
                             "-vf", "scale=320:320:force_original_aspect_ratio=decrease",
                             "-q:v", "5", str(out))
            return out
        except RuntimeError:
            continue
    return None


async def prepare(src: Path, workdir: Path) -> LocalMedia:
    """Make the video Telegram-friendly with as little work as possible."""
    v, a, w, h, dur = _streams(await _probe(src))
    if not v:
        raise ScError("فایل ویدیویی معتبر نیست.")
    log.info("video %s: v=%s a=%s pix=%s %sx%s", src.name, v.get("codec_name"),
             a.get("codec_name"), v.get("pix_fmt"), w, h)

    ok = (v.get("codec_name") == "h264" and v.get("pix_fmt") in ("yuv420p", "yuvj420p")
          and a.get("codec_name") in (None, "aac", "mp3"))
    out, changed = src, False
    if ok and src.suffix.lower() == ".mp4":
        thumb_task = asyncio.create_task(_thumb(src, workdir / "t1.jpg"))  # nothing to convert
    else:
        out = workdir / "v1.mp4"
        base = ["-i", str(src), "-map", "0:v:0", "-map", "0:a:0?"]
        done = False
        if ok:
            try:
                await run_ffmpeg(*base, "-c", "copy", "-movflags", "+faststart", str(out))
                done = True
            except RuntimeError as exc:
                log.warning("remux failed: %s", exc)
        if not done:
            try:
                async with TRANSCODE_SEM:
                    await run_ffmpeg(
                        *base, "-vf",
                        "scale=w='min(1280,iw)':h='min(1280,ih)':"
                        "force_original_aspect_ratio=decrease:force_divisible_by=2",
                        "-c:v", "libx264", "-preset", "superfast", "-crf", "25", "-threads", "2",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                        "-movflags", "+faststart", str(out),
                    )
                done = True
            except RuntimeError as exc:
                log.warning("re-encode failed: %s", exc)
        if done:
            changed = True
        else:
            out = src  # last resort: send untouched
        thumb_task = asyncio.create_task(_thumb(out, workdir / "t1.jpg"))

    if changed:  # real size/duration of the file we will send
        _, _, w2, h2, dur2 = _streams(await _probe(out))
        w, h, dur = w2 or w, h2 or h, dur2 or dur
    return LocalMedia(out, w, h, dur, await thumb_task)