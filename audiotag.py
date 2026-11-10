"""Change the title, artist and cover of an audio file without re-encoding it.

* mp3 / m4a (aac, alac) / flac keep their format and their sound untouched (`-c copy`).
* any other format (ogg, opus, wav ...) is converted to a 320k mp3, like the rest of the bot does.
* the cover is embedded in the file and a small JPEG thumbnail is prepared for Telegram.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("audiotag")

MAX_INPUT_MB = 20           # the official Telegram server lets a bot download files up to 20 MB


class AudioTagError(Exception):
    """Error with a message that is safe to show to the user."""

    def __init__(self, user_message: str):
        super().__init__(user_message)
        self.user_message = user_message


@dataclass
class AudioInfo:
    title: str
    artist: str
    duration: int           # seconds
    codec: str
    has_cover: bool
    ext: str                # extension of the result: mp3 / m4a / flac
    copy: bool              # True = audio stream is copied, False = re-encoded to mp3


@dataclass
class Built:
    path: Path
    cover_applied: bool     # False when the cover had to be dropped because ffmpeg refused it


def safe_filename(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", s)
    return re.sub(r"\s+", " ", s).strip(" .")[:120] or "track"


def clean_text(s: str, limit: int = 200) -> str:
    return re.sub(r"\s+", " ", s or "").strip()[:limit]


async def _run(cmd: list[str]) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await proc.communicate()
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} exit {proc.returncode}: " + err.decode(errors="ignore")[-600:])
    return out


async def _ffmpeg(*args: str) -> None:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args]
    if shutil.which("nice"):
        cmd = ["nice", "-n", "10", *cmd]
    await _run(cmd)


def _num(v) -> Optional[float]:
    try:
        x = float(v)
        return x if x > 0 else None
    except (TypeError, ValueError):
        return None


async def probe(path: Path) -> AudioInfo:
    try:
        out = await _run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)])
        data = json.loads(out or b"{}")
    except (RuntimeError, OSError, ValueError):
        data = {}
    streams = data.get("streams") or []
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not audio:
        raise AudioTagError("این فایل آهنگ نیست یا قابل خواندن نیست.")

    tags: dict[str, str] = {}
    for src in ((data.get("format") or {}).get("tags") or {}, audio.get("tags") or {}):
        for k, v in src.items():
            tags.setdefault(str(k).lower(), str(v))
    has_cover = any(s.get("codec_type") == "video" and (s.get("disposition") or {}).get("attached_pic") == 1
                    for s in streams)

    codec = str(audio.get("codec_name") or "").lower()
    if codec == "mp3":
        ext, copy = "mp3", True
    elif codec in ("aac", "alac"):
        ext, copy = "m4a", True
    elif codec == "flac":
        ext, copy = "flac", True
    else:
        ext, copy = "mp3", False
    dur = _num(audio.get("duration")) or _num((data.get("format") or {}).get("duration")) or 0
    return AudioInfo(
        title=clean_text(tags.get("title", "")),
        artist=clean_text(tags.get("artist", "") or tags.get("album_artist", "")),
        duration=int(round(dur)),
        codec=codec, has_cover=has_cover, ext=ext, copy=copy,
    )


async def prepare_cover(image: Path, workdir: Path, name: str = "cover") -> tuple[Path, Path]:
    """Any picture -> (cover jpg for embedding, thumbnail jpg for Telegram: 320px, under 200 KB)."""
    cover, thumb = workdir / f"{name}.jpg", workdir / f"{name}_thumb.jpg"
    try:
        await asyncio.gather(
            _ffmpeg("-i", str(image), "-frames:v", "1", "-pix_fmt", "yuvj420p",
                    "-vf", "scale='min(1400,iw)':'min(1400,ih)':force_original_aspect_ratio=decrease",
                    "-q:v", "2", str(cover)),
            _ffmpeg("-i", str(image), "-frames:v", "1", "-pix_fmt", "yuvj420p",
                    "-vf", "scale=320:320:force_original_aspect_ratio=decrease",
                    "-q:v", "5", str(thumb)),
        )
        for q in ("12", "20", "28"):          # Telegram thumbnails must stay under 200 KB
            if thumb.stat().st_size <= 190_000:
                break
            await _ffmpeg("-i", str(cover), "-frames:v", "1", "-pix_fmt", "yuvj420p",
                          "-vf", "scale=320:320:force_original_aspect_ratio=decrease",
                          "-q:v", q, str(thumb))
    except (RuntimeError, OSError) as exc:
        raise AudioTagError("این تصویر قابل خواندن نیست. یک عکس دیگر بفرستید.") from exc
    if not cover.is_file() or cover.stat().st_size == 0 or not thumb.is_file() or thumb.stat().st_size == 0:
        raise AudioTagError("این تصویر قابل خواندن نیست. یک عکس دیگر بفرستید.")
    return cover, thumb


async def extract_cover(src: Path, workdir: Path) -> Optional[tuple[Path, Path]]:
    """The cover embedded in the audio file -> (cover jpg, thumbnail jpg), or None."""
    raw = workdir / "cover_old.raw.jpg"
    try:
        await _ffmpeg("-i", str(src), "-map", "0:v:0", "-frames:v", "1", str(raw))
        return await prepare_cover(raw, workdir, "cover_old")
    except (RuntimeError, OSError, AudioTagError):
        return None


async def build(src: Path, workdir: Path, info: AudioInfo, title: str, artist: str,
                cover_mode: str, cover: Optional[Path]) -> Built:
    """cover_mode: "keep" (leave the embedded cover as it is), "new" (use `cover`) or "remove"."""
    outdir = workdir / "out"
    outdir.mkdir(exist_ok=True)
    base = f"{artist} - {title}" if artist else title
    out = outdir / f"{safe_filename(base)}.{info.ext}"

    def command(with_cover: bool) -> list[str]:
        args = ["-i", str(src)]
        use_new = with_cover and cover_mode == "new" and cover is not None
        if use_new:
            args += ["-i", str(cover)]
        args += ["-map", "0:a:0"]
        mapped_cover = False
        if use_new:
            args += ["-map", "1:v:0", "-c:v", "copy"]
            mapped_cover = True
        elif with_cover and cover_mode == "keep" and info.has_cover:
            args += ["-map", "0:v:0", "-c:v", "copy"]
            mapped_cover = True
        args += ["-c:a", "copy"] if info.copy else ["-c:a", "libmp3lame", "-b:a", "320k"]
        args += ["-map_metadata", "0", "-metadata", f"title={title}", "-metadata", f"artist={artist}"]
        if mapped_cover:
            args += ["-disposition:v:0", "attached_pic",
                     "-metadata:s:v:0", "title=Cover", "-metadata:s:v:0", "comment=Cover (front)"]
        if info.ext == "mp3":
            args += ["-id3v2_version", "3", "-write_id3v1", "1"]
        args.append(str(out))
        return args

    wants_cover = (cover_mode == "new" and cover is not None) or (cover_mode == "keep" and info.has_cover)
    applied = wants_cover
    try:
        await _ffmpeg(*command(True))
    except RuntimeError as exc:
        if not wants_cover:
            raise AudioTagError("ساخت فایل ممکن نشد. آهنگ دیگری امتحان کنید.") from exc
        log.warning("tagging with cover failed (%s); retrying without cover", exc)
        applied = False
        try:
            await _ffmpeg(*command(False))
        except RuntimeError as exc2:
            raise AudioTagError("ساخت فایل ممکن نشد. آهنگ دیگری امتحان کنید.") from exc2
    if not out.is_file() or out.stat().st_size == 0:
        raise AudioTagError("ساخت فایل ممکن نشد. آهنگ دیگری امتحان کنید.")
    return Built(path=out, cover_applied=applied if wants_cover else True)
