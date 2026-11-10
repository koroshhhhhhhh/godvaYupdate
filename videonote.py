"""Convert an ordinary video into a Telegram video message (the round "video note").

Telegram's rules for a video note: square picture, at most 640x640, at most 60 seconds,
H.264 video + AAC audio in an MP4 container. Everything here is done with ffmpeg:
  * the picture is cropped to a centred square and scaled down to 640x640 at most,
  * only the first 60 seconds are kept,
  * the sound is kept when the video has any.
"""
from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

MAX_SECONDS = 60          # Telegram limit for video messages
MAX_SIDE = 640            # Telegram limit: 640x640
MODE = "fit"             # "fit" = the whole picture stays visible (zoomed out), "crop" = centred square crop
BACKGROUND = "blur"      # fit mode: what fills the empty space - "blur" (blurred copy of the video) or "black"
CRF = 17                 # video quality: lower = better and bigger (17 is visually near-lossless)
MAXRATE, BUFSIZE = "5000k", "10000k"   # upper limit of the bitrate, keeps a 60 s clip around 35 MB at most
ZOOM = 1.15              # >1 = tighter centre crop, so the picture looks bigger inside the circle (1.0 = off)
MAX_INPUT_MB = 200        # biggest source video we are willing to fetch and convert


class VideoNoteError(Exception):
    """Error with a message that is safe to show to the user."""

    def __init__(self, user_message: str):
        super().__init__(user_message)
        self.user_message = user_message


@dataclass
class VideoNote:
    path: Path
    duration: int     # seconds
    side: int         # width = height, pixels
    trimmed: bool     # True when the source was longer than MAX_SECONDS


async def _probe(path: Path) -> dict:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        return json.loads(out or b"{}")
    except (OSError, ValueError):
        return {}


def _video_stream(probe: dict) -> Optional[dict]:
    return next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"
                 and s.get("disposition", {}).get("attached_pic") != 1), None)


def _num(value) -> Optional[float]:
    try:
        v = float(value)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _fps(stream: dict) -> Optional[float]:
    raw = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or ""
    try:
        a, _, b = raw.partition("/")
        return float(a) / float(b or 1)
    except (ValueError, ZeroDivisionError):
        return None


def _duration(probe: dict, stream: dict) -> Optional[float]:
    return _num(stream.get("duration")) or _num((probe.get("format") or {}).get("duration"))


async def _ffmpeg(*args: str) -> None:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args]
    if shutil.which("nice"):  # keep the bot responsive while ffmpeg works
        cmd = ["nice", "-n", "10", *cmd]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await proc.communicate()
    except asyncio.CancelledError:  # job cancelled: do not leave ffmpeg running
        with_kill = proc.returncode is None
        if with_kill:
            proc.kill()
            await proc.wait()
        raise
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg exit {proc.returncode}: " + err.decode(errors="ignore")[-600:])


async def convert(src: Path, workdir: Path) -> VideoNote:
    """Make a video message out of `src`. Raises VideoNoteError for problems the user can understand."""
    probe = await _probe(src)
    stream = _video_stream(probe)
    if not stream:
        raise VideoNoteError("این فایل ویدیو نیست یا قابل خواندن نیست.")
    src_dur = _duration(probe, stream)
    fps = _fps(stream)

    tail = ""
    if fps and fps > 30.5:
        tail += ",fps=30"
    tail += ",format=yuv420p"
    S = MAX_SIDE
    if MODE == "crop":
        # centred square crop (tighter than the full square when ZOOM > 1), always scaled to SxS
        side_expr = f"trunc(min(iw,ih)/{ZOOM:.3f}/2)*2"
        graph = f"[0:v:0]crop='{side_expr}':'{side_expr}',scale={S}:{S}:flags=lanczos,setsar=1{tail}[v]"
    else:
        # the whole picture is scaled to fit inside the square, nothing is cut off
        fg = f"scale={S}:{S}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos"
        if BACKGROUND == "blur":
            graph = (f"[0:v:0]split=2[a][b];"
                     f"[a]scale={S}:{S}:force_original_aspect_ratio=increase,crop={S}:{S},boxblur=25:3[bg];"
                     f"[b]{fg}[fg];"
                     f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1{tail}[v]")
        else:
            graph = (f"[0:v:0]{fg}[fg];"
                     f"color=c=black:s={S}x{S}[bg];"
                     f"[bg][fg]overlay=(W-w)/2:(H-h)/2:shortest=1,setsar=1{tail}[v]")

    out = workdir / "videonote.mp4"
    try:
        await _ffmpeg(
            "-i", str(src), "-t", str(MAX_SECONDS),
            "-filter_complex", graph, "-map", "[v]", "-map", "0:a:0?",
            "-c:v", "libx264", "-preset", "fast", "-crf", str(CRF), "-profile:v", "high",
            "-maxrate", MAXRATE, "-bufsize", BUFSIZE,
            "-c:a", "aac", "-b:a", "128k", "-ac", "2",
            "-movflags", "+faststart", str(out),
        )
    except RuntimeError as exc:
        raise VideoNoteError("تبدیل این ویدیو ممکن نشد. ویدیوی دیگری امتحان کنید.") from exc

    if not out.is_file() or out.stat().st_size == 0:
        raise VideoNoteError("تبدیل این ویدیو ممکن نشد. ویدیوی دیگری امتحان کنید.")

    res = await _probe(out)
    vs = _video_stream(res) or {}
    side = int(vs.get("width") or 0) or MAX_SIDE
    dur = _duration(res, vs) or min(src_dur or MAX_SECONDS, MAX_SECONDS)
    return VideoNote(
        path=out,
        duration=max(1, min(MAX_SECONDS, int(round(dur)))),
        side=side,
        trimmed=bool(src_dur and src_dur > MAX_SECONDS + 0.5),
    )
