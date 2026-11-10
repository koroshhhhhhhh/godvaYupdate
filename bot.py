"""Telegram layer (aiogram 3): confirmation flow, progress updates, sending."""
from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import secrets
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Optional

import aiofiles
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InputFile,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
    ReplyParameters,
)

import audiotag as at
import custom_site
import instagram as ig
import social
import soundcloud as sc
import ui
import videonote as vn
import youtube  # noqa: F401  (registers the YouTube yt-dlp options)

log = logging.getLogger("bot")


@dataclass
class Settings:
    token: str
    max_concurrent_jobs: int = 3
    max_jobs_per_user: int = 2
    max_file_mb: int = 49
    bot_api_url: str = ""
    progress_interval_private: float = 1.2
    progress_interval_group: float = 3.0
    pending_ttl: int = 600
    max_playlist_tracks: int = 0
    max_playlist_tracks_group: int = 0
    playlist_gap_private: float = 0.7
    playlist_gap_group: float = 3.5
    instagram_cookies: str = ""
    instagram_proxy: str = ""


@dataclass
class Job:
    token: str
    owner_id: Optional[int]
    chat_id: int
    thread_id: Optional[int]
    link_msg_id: int
    msg: Message
    url: str
    is_group: bool
    created: float
    prefetch: Optional[asyncio.Task] = None
    platform: str = "soundcloud"
    quality: Optional[int] = None      # chosen video height (0 = best); None = not asked
    stop: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class VnWait:
    """A user who sent /videomsg and now owes us a video."""
    prompt: Message
    created: float


@dataclass
class EditSession:
    """One /edit conversation: audio -> card (title / artist / cover) -> busy (building and sending)."""
    msg: Message                 # the message the user interacts with: the prompt first, then the card
    owner: int
    touched: float
    phase: str = "audio"         # "audio" | "card" | "busy"
    awaiting: str = ""           # "", "title", "artist" or "cover"
    ask: Optional[Message] = None
    workdir: Optional[Path] = None
    src: Optional[Path] = None
    src_msg: Optional[Message] = None
    info: Optional[at.AudioInfo] = None
    title: str = ""
    artist: str = ""
    cover_mode: str = "keep"     # "keep" | "new" | "remove"
    cover: Optional[Path] = None
    thumb: Optional[Path] = None       # Telegram thumbnail of the new cover
    old_thumb: Optional[Path] = None   # Telegram thumbnail of the cover that is already in the file
    cover_n: int = 0


ED_TTL = 900  # seconds without any action before an unfinished /edit is dropped


class ProgressFile(InputFile):
    """Streams a file to Telegram and reports the bytes really sent."""

    def __init__(self, path: Path, state: ui.ProgressState, chunk_size: int = 512 * 1024):
        super().__init__(filename=path.name, chunk_size=chunk_size)
        self.path = path
        self.state = state

    async def read(self, bot: Bot):
        async with aiofiles.open(self.path, "rb") as f:
            while True:
                chunk = await f.read(self.chunk_size)
                if not chunk:
                    break
                self.state.on_upload_chunk(len(chunk))
                yield chunk


class _CountingFile(io.FileIO):
    """A file that reports every written chunk (used to show the download progress)."""

    def __init__(self, path: Path, on_chunk):
        super().__init__(path, "wb")
        self._on_chunk = on_chunk

    def write(self, data):
        n = super().write(data)
        self._on_chunk(n or 0)
        return n


async def safe_edit(msg: Message, text: str, markup=None) -> float:
    """Edit a message. Returns seconds to wait if Telegram asked us to slow down."""
    try:
        await msg.edit_text(text, reply_markup=markup)
    except TelegramRetryAfter as e:
        return float(e.retry_after)
    except TelegramBadRequest:
        pass  # not modified / message gone
    except TelegramAPIError as e:
        log.debug("edit failed: %s", e)
    return 0.0


async def safe_delete(msg: Message) -> None:
    with contextlib.suppress(TelegramAPIError):
        await msg.delete()


# platforms where the user picks the video quality, and the heights offered
QUALITY_CHOICES = {"site": (480, 720), "youtube": (360, 480, 720, 1080)}


def quality_options(platform: str, heights: list) -> list:
    """Only the qualities the video really has; 0 = 'best available' when none of them fits."""
    wanted = QUALITY_CHOICES[platform]
    if not heights:
        return list(wanted)
    top = max(heights)
    return [q for q in wanted if top >= q * 0.9] or [0]


class SoundCloudBot:
    def __init__(self, bot: Bot, settings: Settings):
        self.bot = bot
        self.s = settings
        self.pending: dict[str, Job] = {}
        self.active: dict[int, int] = {}
        self.active_playlists: set[int] = set()
        self.running: dict[str, Job] = {}
        self.vn_wait: dict[tuple[int, int], VnWait] = {}   # (chat id, user id) -> waiting for a video
        self.ed: dict[tuple[int, int], EditSession] = {}   # (chat id, user id) -> /edit conversation
        self._official: Optional[Bot] = None   # talks to the OFFICIAL Telegram server (used by /edit)
        self.sem = asyncio.Semaphore(settings.max_concurrent_jobs)
        self.tasks: set[asyncio.Task] = set()
        self.router = Router()
        r = self.router
        r.message.register(self.on_start, CommandStart())
        r.message.register(self.on_help, Command("help"))
        r.message.register(self.on_videomsg, Command("videomsg", "videonote"))
        r.message.register(self.on_vn_video, self._vn_wait_filter)   # before on_text: videos may carry a caption
        r.message.register(self.on_edit, Command("edit"))
        r.message.register(self.on_ed_audio, self._ed_audio_filter)   # before on_text
        r.message.register(self.on_ed_input, self._ed_input_filter)
        r.message.register(self.on_text, F.text | F.caption)
        r.callback_query.register(self.on_ed_cb, F.data.startswith("ed:"))
        r.callback_query.register(self.on_vn_cancel, F.data.startswith("vn:"))
        r.callback_query.register(self.on_confirm, F.data.startswith("dl:"))
        r.callback_query.register(self.on_help_cb, F.data.startswith("hp:"))

    # ------------------------------------------------------------------ #
    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def _delete_later(self, msg: Message, delay: float) -> None:
        await asyncio.sleep(delay)
        await safe_delete(msg)

    # ------------------------------------------------------------------ #
    async def on_start(self, m: Message) -> None:
        await m.answer(ui.START_TEXT, reply_markup=ui.start_keyboard())

    async def on_help(self, m: Message) -> None:
        await m.answer(ui.HELP_MAIN, reply_markup=ui.help_keyboard())

    async def on_help_cb(self, cb: CallbackQuery) -> None:
        op = (cb.data or "").split(":")[-1]
        msg = cb.message if isinstance(cb.message, Message) else None
        if op == "hdr":  # the header button: nothing to open
            await cb.answer("یکی از برنامه‌های زیر را انتخاب کنید.")
            return
        if msg is None:
            await cb.answer()
            return
        pages = {
            "m": (ui.HELP_MAIN, ui.help_keyboard()),
            "sc": (ui.HELP_SOUNDCLOUD, ui.help_back_keyboard()),
            "pin": (ui.HELP_PINTEREST, ui.help_back_keyboard()),
            "ig": (ui.HELP_INSTAGRAM, ui.help_back_keyboard()),
            "yt": (ui.HELP_YOUTUBE, ui.help_back_keyboard()),
        }
        if op in pages:
            text, kb = pages[op]
            await cb.answer()
            await safe_edit(msg, text, kb)
            return
        await cb.answer()

    @staticmethod
    def _extract_target(m: Message) -> Optional[tuple[str, str]]:
        texts = [m.text or m.caption or ""]
        texts += [e.url for e in (m.entities or m.caption_entities or [])
                  if e.type == "text_link" and e.url]
        for t in texts:
            u = sc.find_url(t)
            if u:
                return "soundcloud", u
        for t in texts:
            r = ig.detect(t) or social.detect(t) or custom_site.detect(t)
            if r:
                return r
        return None

    async def on_text(self, m: Message) -> None:
        if m.from_user and m.from_user.is_bot and not m.sender_chat:
            return
        target = self._extract_target(m)
        platform, url = target if target else ("soundcloud", None)
        is_private = m.chat.type == ChatType.PRIVATE
        if not url:
            if is_private and m.text and not m.text.startswith("/"):
                await m.answer(ui.SEND_LINK_HINT)
            return

        token = secrets.token_hex(5)
        owner = None if m.sender_chat else (m.from_user.id if m.from_user else None)
        auto = platform == "site" and not custom_site.ASK_CONFIRM  # start without asking
        try:
            if auto:
                msg = await m.reply(f"<b>{escape(custom_site.SITE_NAME)}</b>\n\nدر حال آماده‌سازی...")
            else:
                kb = ui.cancel_keyboard(token) if platform in QUALITY_CHOICES else ui.confirm_keyboard(token)
                msg = await m.reply(ui.confirm_text(None, platform), reply_markup=kb)
        except TelegramAPIError as e:
            log.warning("cannot reply in chat %s: %s", m.chat.id, e)
            return

        job = Job(
            token=token, owner_id=owner, chat_id=m.chat.id,
            thread_id=m.message_thread_id if m.is_topic_message else None,
            link_msg_id=m.message_id, msg=msg, url=url, is_group=not is_private,
            created=time.time(), platform=platform,
        )
        self.pending[token] = job
        # Read the info while the user is still deciding -> download starts instantly.
        job.prefetch = self.spawn(self._prefetch(job))
        if auto:
            key = job.owner_id or job.chat_id
            if self.active.get(key, 0) >= self.s.max_jobs_per_user:
                self.pending.pop(token, None)
                job.prefetch.cancel()
                await safe_edit(msg, ui.error_text(ui.BUSY))
                self.spawn(self._delete_later(msg, 10))
            else:
                self.pending.pop(token, None)
                self.spawn(self.run_job(job))

    async def _prefetch(self, job: Job):
        try:
            if job.platform == "soundcloud":
                res = await sc.fetch(job.url)
            elif job.platform == "instagram":
                res = await ig.fetch(job.url)
            else:
                res = await social.fetch(job.platform, job.url)
        except sc.ScError as e:
            if self.pending.pop(job.token, None):  # still waiting for the user's decision
                await safe_edit(job.msg, ui.error_text(e.user_message))
                self.spawn(self._delete_later(job.msg, 20))
                return None
            raise  # user already confirmed; run_job reports it
        if isinstance(res, sc.PlaylistInfo):
            text = ui.playlist_confirm_text(res, self._cap(job))
            kb = ui.confirm_keyboard(job.token)
        elif isinstance(res, social.MediaInfo) and job.platform in QUALITY_CHOICES:
            text = ui.media_confirm_text(res, choose_quality=True)
            kb = ui.quality_keyboard(job.token, quality_options(job.platform, res.heights))
        elif isinstance(res, (social.MediaInfo, ig.IgInfo)):
            text = ui.media_confirm_text(res)
            kb = ui.confirm_keyboard(job.token)
        else:
            text = ui.confirm_text(res[1])
            kb = ui.confirm_keyboard(job.token)
        if job.token in self.pending:
            await safe_edit(job.msg, text, kb)
        return res

    def _cap(self, job: Job) -> int:
        n = self.s.max_playlist_tracks_group if job.is_group else self.s.max_playlist_tracks
        return n if n > 0 else 10**9  # 0 = unlimited

    # ------------------------------------------------------------------ #
    async def on_confirm(self, cb: CallbackQuery) -> None:
        try:
            _, choice, token = (cb.data or "").split(":")
        except ValueError:
            await cb.answer()
            return
        if choice == "s":  # stop a running playlist
            run = self.running.get(token)
            if not run:
                await cb.answer(ui.EXPIRED, show_alert=True)
            elif run.owner_id is not None and cb.from_user.id != run.owner_id:
                await cb.answer(ui.NOT_YOURS, show_alert=True)
            else:
                run.stop.set()
                await cb.answer(ui.STOPPING)
            return
        job = self.pending.get(token)
        if not job:
            await cb.answer(ui.EXPIRED, show_alert=True)
            if isinstance(cb.message, Message):
                with contextlib.suppress(TelegramAPIError):
                    await cb.message.edit_reply_markup(reply_markup=None)
            return
        if job.owner_id is not None and cb.from_user.id != job.owner_id:
            await cb.answer(ui.NOT_YOURS, show_alert=True)
            return

        if choice.startswith("q") and choice[1:].isdigit():  # quality chosen = confirmed
            job.quality = int(choice[1:])
        elif choice == "n":
            self.pending.pop(token, None)
            if job.prefetch:
                job.prefetch.cancel()
            await cb.answer()
            await safe_edit(job.msg, f"<b>{ui.CANCELLED}</b>")
            self.spawn(self._delete_later(job.msg, 5))
            return

        key = job.owner_id or job.chat_id
        if self.active.get(key, 0) >= self.s.max_jobs_per_user:
            await cb.answer(ui.BUSY, show_alert=True)
            return
        self.pending.pop(token, None)
        await cb.answer()
        self.spawn(self.run_job(job))

    # ------------------------------------------------------------------ #
    async def _progress_loop(self, msg: Message, st: ui.ProgressState, interval: float, token: str) -> None:
        last = ""
        while True:
            text = ui.progress_text(st)
            if text != last:
                wait = await safe_edit(msg, text, ui.stop_keyboard(token) if st.pl_total else None)
                if wait:
                    interval = min(interval * 1.5, 10.0)
                    await asyncio.sleep(wait)
                    continue
                last = text
            await asyncio.sleep(interval)

    async def run_job(self, job: Job) -> None:
        key = job.owner_id or job.chat_id
        self.active[key] = self.active.get(key, 0) + 1
        st = ui.ProgressState()
        interval = self.s.progress_interval_group if job.is_group else self.s.progress_interval_private
        updater = asyncio.create_task(self._progress_loop(job.msg, st, interval, job.token))
        workdir = Path(tempfile.mkdtemp(prefix="scbot_"))
        error: Optional[str] = None
        summary: Optional[str] = None
        delivered = False
        try:
            res = await job.prefetch
            if res is None:
                raise sc.ScError(ui.GENERIC_ERROR)
            if isinstance(res, sc.PlaylistInfo):
                summary = await self._run_playlist(job, res, st, workdir)
            elif isinstance(res, social.MediaInfo):
                if self.sem.locked():
                    st.set_stage("queued")
                async with self.sem:
                    await self._deliver_media(job, st, workdir, res)
                delivered = True
            elif isinstance(res, ig.IgInfo):
                if self.sem.locked():
                    st.set_stage("queued")
                async with self.sem:
                    await self._deliver_ig(job, st, workdir, res)
                delivered = True
            else:
                if self.sem.locked():
                    st.set_stage("queued")
                async with self.sem:
                    await self._deliver_track(job, st, workdir, job.url, res, extra_desc=True)
                delivered = True
        except sc.ScError as e:
            error = e.user_message
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("job failed: %s", job.url)
            error = ui.GENERIC_ERROR
        finally:
            updater.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await updater
            await asyncio.to_thread(shutil.rmtree, workdir, True)
            self.active[key] = max(0, self.active.get(key, 1) - 1)

        if summary:
            await safe_delete(job.msg)
            with contextlib.suppress(TelegramAPIError):
                await self.bot.send_message(
                    job.chat_id, summary, message_thread_id=job.thread_id,
                    reply_parameters=ReplyParameters(message_id=job.link_msg_id,
                                                     allow_sending_without_reply=True),
                )
        elif delivered:
            await safe_delete(job.msg)
        elif error:
            await safe_edit(job.msg, ui.error_text(error))
            self.spawn(self._delete_later(job.msg, 30))

    async def _deliver_track(self, job: Job, st: ui.ProgressState, workdir: Path, url: str,
                             res, extra_desc: bool) -> None:
        """Full pipeline for one track: cover || download -> tag -> upload."""
        st.set_stage("info")
        info, meta, fetched_at = res
        st.title, st.artists = meta.title, meta.artists_text

        cover_task = asyncio.create_task(sc.prepare_cover(meta, workdir))  # parallel with download
        try:
            st.set_stage("download")
            src = await sc.download(info, url, workdir, fetched_at, st.on_download)

            st.set_stage("process")
            cover, thumb = await cover_task
            audio = await sc.build_audio(src, cover, meta, workdir)
            size = audio.stat().st_size
            if size > self.s.max_file_mb * 1024 * 1024:
                raise sc.ScError(ui.TOO_BIG)

            st.set_stage("upload")
            st.total = size
            await self._send(job, meta, audio, thumb, st, extra_desc)
        finally:
            if not cover_task.done():
                cover_task.cancel()

    async def _run_playlist(self, job: Job, pl: sc.PlaylistInfo, st: ui.ProgressState,
                            workdir: Path) -> str:
        key = job.owner_id or job.chat_id
        if key in self.active_playlists:
            raise sc.ScError(ui.PL_BUSY)
        self.active_playlists.add(key)
        self.running[job.token] = job
        entries = pl.entries[: self._cap(job)]
        gap = self.s.playlist_gap_group if job.is_group else self.s.playlist_gap_private
        st.pl_title, st.pl_total = pl.title, len(entries)
        ok, failed, handled = 0, [], 0
        try:
            for i, e in enumerate(entries, 1):
                if job.stop.is_set():
                    break
                st.pl_index = i
                st.title, st.artists = e.get("title") or "", ""
                tdir = workdir / f"t{i}"
                tdir.mkdir(exist_ok=True)
                try:
                    if self.sem.locked():  # single tracks of other users interleave fairly
                        st.set_stage("queued")
                    async with self.sem:
                        st.set_stage("info")
                        res = await sc.fetch(e["url"])
                        if isinstance(res, sc.PlaylistInfo):
                            raise sc.ScError(ui.GENERIC_ERROR)
                        await self._deliver_track(job, st, tdir, e["url"], res, extra_desc=False)
                    ok += 1
                except sc.ScError:
                    failed.append(e.get("title") or e["url"])
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("playlist track failed: %s", e.get("url"))
                    failed.append(e.get("title") or e["url"])
                finally:
                    handled += 1
                    await asyncio.to_thread(shutil.rmtree, tdir, True)
                if i < len(entries) and not job.stop.is_set():
                    await asyncio.sleep(gap)
        finally:
            self.active_playlists.discard(key)
            self.running.pop(job.token, None)
        stopped = job.stop.is_set() and handled < len(entries)
        return ui.playlist_summary(pl.title, ok, failed, stopped, pl.total - len(entries))

    async def _deliver_media(self, job: Job, st: ui.ProgressState, workdir: Path,
                             info: social.MediaInfo) -> None:
        """Pinterest video: download -> make Telegram-friendly -> send."""
        st.brand, st.media = info.brand, True
        st.title, st.artists = ui.media_title(info), ""
        st.set_stage("download")
        src = await social.download(info, workdir, st.on_download, job.quality)

        st.set_stage("process")
        media = await social.prepare(src, workdir)
        if media.path.stat().st_size > self.s.max_file_mb * 1024 * 1024:
            raise sc.ScError(ui.TOO_BIG)

        st.set_stage("upload")
        st.total = media.path.stat().st_size
        await self._send_video(job, info, media, st)

    async def _send_video(self, job: Job, info: social.MediaInfo, m: social.LocalMedia,
                          st: ui.ProgressState) -> None:
        plain = info.platform == "site"  # custom site: only the video, nothing else
        cap, truncated = ("", False) if plain else ui.media_caption(info)
        reply = ReplyParameters(message_id=job.link_msg_id, allow_sending_without_reply=True)
        kb = None if plain else ui.link_keyboard(info.url, info.brand)
        for attempt in range(6):
            st.done, st.frac, st.t0 = 0, 0.0, time.monotonic()
            try:
                await self.bot.send_video(
                    chat_id=job.chat_id, message_thread_id=job.thread_id,
                    video=ProgressFile(m.path, st), duration=m.duration, width=m.width,
                    height=m.height, thumbnail=FSInputFile(m.thumb) if m.thumb else None,
                    supports_streaming=True, caption=cap or None, reply_parameters=reply,
                    reply_markup=kb, request_timeout=900,
                )
                break
            except TelegramRetryAfter as e:
                if attempt == 5:
                    raise
                await asyncio.sleep(e.retry_after + 1)
        if truncated and info.description:
            with contextlib.suppress(TelegramAPIError):
                await self.bot.send_message(
                    job.chat_id, f"<blockquote expandable>{escape(info.description)[:3800]}</blockquote>",
                    message_thread_id=job.thread_id, reply_parameters=reply)

    async def _with_flood_retry(self, factory, attempts: int = 6):
        for attempt in range(attempts):
            try:
                return await factory()
            except TelegramRetryAfter as e:
                if attempt == attempts - 1:
                    raise
                await asyncio.sleep(e.retry_after + 1)

    async def _deliver_ig(self, job: Job, st: ui.ProgressState, workdir: Path, info: ig.IgInfo) -> None:
        """Instagram: download -> prepare -> send (several items go out as an album)."""
        st.brand, st.media = info.brand, True
        st.title, st.artists = ui.media_title(info), ""
        st.set_stage("download")
        files = await ig.download(info, workdir, st.on_download)

        st.set_stage("process")
        files = await ig.prepare(files, workdir)
        limit = self.s.max_file_mb * 1024 * 1024
        if any(f.path.stat().st_size > limit for f in files):
            raise sc.ScError(ui.TOO_BIG)

        st.set_stage("upload")
        st.total = sum(f.path.stat().st_size for f in files)
        await self._send_ig(job, info, files, st)

    async def _send_ig(self, job: Job, info: ig.IgInfo, files: list, st: ui.ProgressState) -> None:
        cap, truncated = ui.media_caption(info)
        reply = ReplyParameters(message_id=job.link_msg_id, allow_sending_without_reply=True)
        common = dict(chat_id=job.chat_id, message_thread_id=job.thread_id,
                      reply_parameters=reply, request_timeout=900)

        def reset() -> None:
            st.done, st.frac, st.t0 = 0, 0.0, time.monotonic()

        if len(files) == 1:
            m = files[0]
            kb = ui.link_keyboard(info.url, info.brand)

            async def send_one():
                reset()
                if m.kind == "video":
                    return await self.bot.send_video(
                        video=ProgressFile(m.path, st), duration=m.duration, width=m.width,
                        height=m.height, thumbnail=FSInputFile(m.thumb) if m.thumb else None,
                        supports_streaming=True, caption=cap, reply_markup=kb, **common)
                try:
                    return await self.bot.send_photo(
                        photo=ProgressFile(m.path, st), caption=cap, reply_markup=kb, **common)
                except TelegramBadRequest:  # odd dimensions etc. -> send the original file
                    reset()
                    return await self.bot.send_document(
                        document=ProgressFile(m.path, st), caption=cap, reply_markup=kb, **common)

            await self._with_flood_retry(send_one)
        else:
            for ci in range(0, len(files), 10):  # one Telegram album holds up to 10 items
                chunk = files[ci:ci + 10]

                async def send_album(chunk=chunk, ci=ci):
                    reset()
                    media = []
                    for j, m in enumerate(chunk):
                        c = cap if (ci == 0 and j == 0) else None
                        if m.kind == "video":
                            media.append(InputMediaVideo(
                                media=ProgressFile(m.path, st), duration=m.duration, width=m.width,
                                height=m.height, thumbnail=FSInputFile(m.thumb) if m.thumb else None,
                                supports_streaming=True, caption=c))
                        else:
                            media.append(InputMediaPhoto(media=ProgressFile(m.path, st), caption=c))
                    return await self.bot.send_media_group(media=media, **common)

                await self._with_flood_retry(send_album)
                if ci + 10 < len(files):
                    await asyncio.sleep(1.5)

        if truncated and info.description:
            with contextlib.suppress(TelegramAPIError):
                await self.bot.send_message(
                    job.chat_id, f"<blockquote expandable>{escape(info.description)[:3800]}</blockquote>",
                    message_thread_id=job.thread_id, reply_parameters=reply)

    async def _send(self, job: Job, meta: sc.TrackMeta, audio: Path,
                    thumb: Optional[Path], st: ui.ProgressState, extra_desc: bool = True) -> None:
        cap, truncated = ui.caption(meta)
        reply = ReplyParameters(message_id=job.link_msg_id, allow_sending_without_reply=True)
        for attempt in range(6):
            st.done, st.frac = 0, 0.0
            st.t0 = time.monotonic()
            try:
                await self.bot.send_audio(
                    chat_id=job.chat_id,
                    message_thread_id=job.thread_id,
                    audio=ProgressFile(audio, st),
                    title=meta.title[:255],
                    performer=meta.artists_text[:255],
                    duration=int(meta.duration) or None,
                    thumbnail=FSInputFile(thumb) if thumb else None,
                    caption=cap,
                    reply_parameters=reply,
                    reply_markup=ui.link_keyboard(meta.url),
                    request_timeout=900,
                )
                break
            except TelegramRetryAfter as e:
                if attempt == 5:
                    raise
                await asyncio.sleep(e.retry_after + 1)
        if extra_desc and truncated and meta.description:
            text = escape(meta.description)[:3800]
            with contextlib.suppress(TelegramAPIError):
                await self.bot.send_message(
                    job.chat_id, f"<blockquote expandable>{text}</blockquote>",
                    message_thread_id=job.thread_id, reply_parameters=reply,
                )


    # ------------------------------------------------------------------ #
    # Video -> video message (round video)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _vn_source(m: Message):
        """The video-like file of a message (video, GIF-animation or a video sent as a file), else None."""
        if m.video:
            return m.video
        if m.animation:
            return m.animation
        d = m.document
        if d and (d.mime_type or "").lower().startswith("video/"):
            return d
        return None

    def _vn_wait_filter(self, m: Message) -> bool:
        """True only for a video from a user who is waiting for the conversion; everything else goes on."""
        if not m.from_user or m.sender_chat:
            return False
        if (m.chat.id, m.from_user.id) not in self.vn_wait:
            return False
        return self._vn_source(m) is not None

    async def on_videomsg(self, m: Message) -> None:
        if not m.from_user or m.sender_chat:
            return
        key = (m.chat.id, m.from_user.id)
        old = self.vn_wait.pop(key, None)
        if old:
            await safe_delete(old.prompt)
        text = ui.VN_ASK if m.chat.type == ChatType.PRIVATE else ui.VN_ASK_GROUP
        try:
            prompt = await m.reply(text, reply_markup=ui.vn_cancel_keyboard(m.from_user.id))
        except TelegramAPIError as e:
            log.warning("cannot reply in chat %s: %s", m.chat.id, e)
            return
        self.vn_wait[key] = VnWait(prompt=prompt, created=time.time())

    async def on_vn_cancel(self, cb: CallbackQuery) -> None:
        try:
            _, _, uid = (cb.data or "").split(":")
            uid = int(uid)
        except ValueError:
            await cb.answer()
            return
        if cb.from_user.id != uid:
            await cb.answer(ui.NOT_YOURS, show_alert=True)
            return
        msg = cb.message if isinstance(cb.message, Message) else None
        wait = self.vn_wait.get((msg.chat.id, uid)) if msg else None
        if not msg or not wait or wait.prompt.message_id != msg.message_id:
            await cb.answer(ui.EXPIRED, show_alert=True)
            if msg:
                with contextlib.suppress(TelegramAPIError):
                    await msg.edit_reply_markup(reply_markup=None)
            return
        self.vn_wait.pop((msg.chat.id, uid), None)
        await cb.answer()
        await safe_edit(msg, f"<b>{ui.VN_CANCELLED}</b>")
        self.spawn(self._delete_later(msg, 5))

    async def on_vn_video(self, m: Message) -> None:
        src = self._vn_source(m)
        key = (m.chat.id, m.from_user.id) if m.from_user else None
        wait = self.vn_wait.get(key) if key else None
        if not src or not wait:
            return
        owner = m.from_user.id
        # Problems the user can fix by sending something else: keep waiting for the next video.
        if self.active.get(owner, 0) >= self.s.max_jobs_per_user:
            with contextlib.suppress(TelegramAPIError):
                await m.reply(ui.BUSY)
            return
        if src.file_size and src.file_size > vn.MAX_INPUT_MB * 1024 * 1024:
            with contextlib.suppress(TelegramAPIError):
                await m.reply(ui.VN_TOO_BIG)
            return
        info = []
        if getattr(src, "duration", None):
            info.append(ui.fmt_time(src.duration))
        if src.file_size:
            info.append(ui.fmt_bytes(src.file_size))
        st = ui.ProgressState(stage="download", title=ui.VN_TITLE, artists="  \u00b7  ".join(info),
                              total=src.file_size or 0, vn=True)
        try:
            status = await m.reply(ui.progress_text(st))
        except TelegramAPIError as e:
            log.warning("cannot reply in chat %s: %s", m.chat.id, e)
            return
        self.vn_wait.pop(key, None)
        await safe_delete(wait.prompt)
        self.active[owner] = self.active.get(owner, 0) + 1   # released in _run_vn
        self.spawn(self._run_vn(m, src.file_id, src.file_size, status, st, owner))

    async def _download_to(self, bot: Bot, file_id: str, dest: Path, st: ui.ProgressState) -> None:
        st.done, st.frac, st.speed, st.eta, st.t0 = 0, 0.0, 0.0, None, time.monotonic()
        f = _CountingFile(dest, st.on_upload_chunk)
        try:
            await bot.download(file_id, destination=f, timeout=900)
        finally:
            f.close()

    def official_bot(self) -> Bot:
        """A Bot that talks to the official Telegram server (api.telegram.org) whatever BOT_API_URL is.
        /edit uses it for every download and upload, so nothing of /edit touches the self-hosted server."""
        if not self.s.bot_api_url:
            return self.bot          # already the official server
        if self._official is None:
            self._official = Bot(self.s.token, session=AiohttpSession())
        return self._official

    async def close_official(self) -> None:
        if self._official is not None:
            with contextlib.suppress(Exception):
                await self._official.session.close()
            self._official = None

    async def _fetch_official(self, file_id: str, dest: Path, st: ui.ProgressState, too_big: str) -> None:
        """Download a file through the official server only (limit: 20 MB)."""
        try:
            await self._download_to(self.official_bot(), file_id, dest, st)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("download through the official Telegram server failed: %s", exc)
            if "too big" in str(exc).lower():
                raise vn.VideoNoteError(too_big) from exc
            raise

    async def _fetch_input(self, file_id: str, size: Optional[int], dest: Path, st: ui.ProgressState,
                           too_big: str = ui.VN_TOO_BIG, prefer_official: bool = False) -> None:
        """Download a file the user sent. With a self-hosted Bot API server that cannot hand files over
        (for example when it runs as a separate service) the official server is used too (up to 20 MB);
        `prefer_official` makes the official server the first choice."""
        limit = 20 * 1024 * 1024
        can_official = bool(self.s.bot_api_url) and not (size and size > limit)
        order = ["own"]
        if can_official:
            order = ["official", "own"] if prefer_official else ["own", "official"]
        errors: list[Exception] = []
        for which in order:
            try:
                if which == "own":
                    await self._download_to(self.bot, file_id, dest, st)
                else:
                    async with Bot(self.s.token, session=AiohttpSession()) as official:
                        await self._download_to(official, file_id, dest, st)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # Telegram errors, HTTP 404 on the file link, timeouts ...
                log.info("download through the %s Bot API server failed: %s", which, exc)
                errors.append(exc)
        if (size and size > limit and self.s.bot_api_url) or any("too big" in str(x).lower() for x in errors):
            raise vn.VideoNoteError(too_big) from errors[-1]
        raise errors[-1]

    async def _run_vn(self, m: Message, file_id: str, size: Optional[int], status: Message,
                      st: ui.ProgressState, owner: int) -> None:
        is_group = m.chat.type != ChatType.PRIVATE
        interval = self.s.progress_interval_group if is_group else self.s.progress_interval_private
        updater = asyncio.create_task(self._progress_loop(status, st, interval, ""))
        workdir = Path(tempfile.mkdtemp(prefix="vnbot_"))
        error: Optional[str] = None
        note: Optional[vn.VideoNote] = None
        ok = False
        try:
            src = workdir / "input"
            await self._fetch_input(file_id, size, src, st)
            if not src.is_file() or src.stat().st_size == 0:
                raise vn.VideoNoteError(ui.GENERIC_ERROR)

            if self.sem.locked():
                st.set_stage("queued")
            async with self.sem:
                st.set_stage("process")
                note = await vn.convert(src, workdir)
            note_size = note.path.stat().st_size
            if note_size > self.s.max_file_mb * 1024 * 1024:
                raise vn.VideoNoteError(ui.TOO_BIG)

            st.set_stage("upload")
            st.total = note_size
            reply = ReplyParameters(message_id=m.message_id, allow_sending_without_reply=True)
            thread_id = m.message_thread_id if m.is_topic_message else None

            async def send():
                st.done, st.frac, st.t0 = 0, 0.0, time.monotonic()
                return await self.bot.send_video_note(
                    chat_id=m.chat.id, message_thread_id=thread_id,
                    video_note=ProgressFile(note.path, st), duration=note.duration, length=note.side,
                    reply_parameters=reply, request_timeout=900,
                )

            await self._with_flood_retry(send)
            ok = True
        except vn.VideoNoteError as e:
            error = e.user_message
        except TelegramBadRequest as e:
            if "FORBIDDEN" in str(e).upper() and ("VOICE" in str(e).upper() or "VIDEO" in str(e).upper()):
                error = ui.VN_FORBIDDEN
            else:
                log.exception("video message failed")
                error = ui.GENERIC_ERROR
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("video message failed")
            error = ui.GENERIC_ERROR
        finally:
            updater.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await updater
            await asyncio.to_thread(shutil.rmtree, workdir, True)
            self.active[owner] = max(0, self.active.get(owner, 1) - 1)

        if ok:
            await safe_delete(status)
            if note and note.trimmed:
                with contextlib.suppress(TelegramAPIError):
                    notice = await m.reply(ui.VN_TRIMMED)
                    self.spawn(self._delete_later(notice, 15))
        else:
            await safe_edit(status, ui.vn_error_text(error or ui.GENERIC_ERROR))
            self.spawn(self._delete_later(status, 30))


    # ------------------------------------------------------------------ #
    # /edit: change title, artist and cover of an audio file
    # ------------------------------------------------------------------ #
    @staticmethod
    def _ed_audio_source(m: Message):
        if m.audio:
            return m.audio
        d = m.document
        if d:
            name = (d.file_name or "").lower()
            if (d.mime_type or "").lower().startswith("audio/") or name.endswith(
                    (".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wav", ".aac")):
                return d
        return None

    def _ed_audio_filter(self, m: Message) -> bool:
        if not m.from_user or m.sender_chat:
            return False
        s = self.ed.get((m.chat.id, m.from_user.id))
        return bool(s and s.phase == "audio" and self._ed_audio_source(m) is not None)

    def _ed_input_filter(self, m: Message) -> bool:
        """A text / photo that answers a question of the /edit card; commands and everything else go on."""
        if not m.from_user or m.sender_chat:
            return False
        s = self.ed.get((m.chat.id, m.from_user.id))
        if not s or s.phase != "card" or not s.awaiting:
            return False
        if s.awaiting in ("title", "artist"):
            return bool(m.text) and not m.text.startswith("/")
        return bool(m.photo) or bool(m.document and (m.document.mime_type or "").lower().startswith("image/"))

    @staticmethod
    def _ed_cover_state(s: EditSession) -> str:
        if s.cover_mode == "keep":
            return "keep" if (s.info and s.info.has_cover) else "none"
        return s.cover_mode

    async def _ed_refresh(self, s: EditSession) -> None:
        text = ui.ed_card_text(s.title, s.artist, self._ed_cover_state(s))
        kb = ui.ed_keyboard(s.owner, s.cover_mode, bool(s.info and s.info.has_cover))
        wait = await safe_edit(s.msg, text, kb)
        if wait:
            await asyncio.sleep(wait)
            await safe_edit(s.msg, text, kb)

    async def _ed_drop(self, key: tuple, s: EditSession) -> None:
        """Forget the conversation and delete its temporary files."""
        if self.ed.get(key) is s:
            self.ed.pop(key, None)
        if s.ask:
            await safe_delete(s.ask)
            s.ask = None
        if s.workdir:
            await asyncio.to_thread(shutil.rmtree, s.workdir, True)
            s.workdir = None

    async def on_edit(self, m: Message) -> None:
        if not m.from_user or m.sender_chat:
            return
        key = (m.chat.id, m.from_user.id)
        old = self.ed.get(key)
        if old and old.phase == "busy":
            with contextlib.suppress(TelegramAPIError):
                await m.reply(ui.ED_WORKING)
            return
        if old:
            await self._ed_drop(key, old)
            await safe_delete(old.msg)
        text = ui.ED_ASK if m.chat.type == ChatType.PRIVATE else ui.ED_ASK_GROUP
        try:
            prompt = await m.reply(text, reply_markup=ui.ed_cancel_keyboard(m.from_user.id))
        except TelegramAPIError as e:
            log.warning("cannot reply in chat %s: %s", m.chat.id, e)
            return
        self.ed[key] = EditSession(msg=prompt, owner=m.from_user.id, touched=time.time())

    async def on_ed_audio(self, m: Message) -> None:
        src = self._ed_audio_source(m)
        key = (m.chat.id, m.from_user.id) if m.from_user else None
        s = self.ed.get(key) if key else None
        if not src or not s or s.phase != "audio":
            return
        owner = s.owner
        # Problems the user can fix by sending something else: keep waiting for the next file.
        if self.active.get(owner, 0) >= self.s.max_jobs_per_user:
            with contextlib.suppress(TelegramAPIError):
                await m.reply(ui.BUSY)
            return
        if src.file_size and src.file_size > at.MAX_INPUT_MB * 1024 * 1024:
            with contextlib.suppress(TelegramAPIError):
                await m.reply(ui.ED_TOO_BIG)
            return
        s.phase = "busy"   # claimed: a second file sent right now is ignored
        st = ui.ProgressState(stage="download", title=ui.ED_TITLE, artists=ui.fmt_bytes(src.file_size or 0) if src.file_size else "",
                              total=src.file_size or 0, edit=True)
        try:
            status = await m.reply(ui.progress_text(st))
        except TelegramAPIError as e:
            s.phase = "audio"
            log.warning("cannot reply in chat %s: %s", m.chat.id, e)
            return
        await safe_delete(s.msg)
        s.msg, s.src_msg, s.touched = status, m, time.time()
        self.active[owner] = self.active.get(owner, 0) + 1   # released in _ed_load
        self.spawn(self._ed_load(key, s, src, st))

    async def _ed_load(self, key: tuple, s: EditSession, src_file, st: ui.ProgressState) -> None:
        is_group = s.msg.chat.type != ChatType.PRIVATE
        interval = self.s.progress_interval_group if is_group else self.s.progress_interval_private
        updater = asyncio.create_task(self._progress_loop(s.msg, st, interval, ""))
        error: Optional[str] = None
        ok = False
        try:
            s.workdir = Path(tempfile.mkdtemp(prefix="edbot_"))
            s.src = s.workdir / "input"
            await self._fetch_official(src_file.file_id, s.src, st, ui.ED_TOO_BIG)
            if not s.src.is_file() or s.src.stat().st_size == 0:
                raise at.AudioTagError(ui.GENERIC_ERROR)
            s.info = await at.probe(s.src)
            if s.info.has_cover:
                old = await at.extract_cover(s.src, s.workdir)
                s.old_thumb = old[1] if old else None
            tg_title = getattr(src_file, "title", None) or ""
            tg_artist = getattr(src_file, "performer", None) or ""
            file_name = getattr(src_file, "file_name", None) or ""
            s.title = s.info.title or at.clean_text(tg_title) or at.clean_text(Path(file_name).stem)
            s.artist = s.info.artist or at.clean_text(tg_artist)
            ok = True
        except (at.AudioTagError, vn.VideoNoteError) as e:
            error = e.user_message
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("edit: loading the audio failed")
            error = ui.GENERIC_ERROR
        finally:
            updater.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await updater
            self.active[s.owner] = max(0, self.active.get(s.owner, 1) - 1)
        if ok:
            s.phase, s.touched = "card", time.time()
            await self._ed_refresh(s)
        else:
            await self._ed_drop(key, s)
            await safe_edit(s.msg, ui.ed_error_text(error or ui.GENERIC_ERROR))
            self.spawn(self._delete_later(s.msg, 30))

    async def on_ed_cb(self, cb: CallbackQuery) -> None:
        try:
            _, op, uid_text = (cb.data or "").split(":")
            uid = int(uid_text)
        except ValueError:
            await cb.answer()
            return
        if cb.from_user.id != uid:
            await cb.answer(ui.NOT_YOURS, show_alert=True)
            return
        msg = cb.message if isinstance(cb.message, Message) else None
        key = (msg.chat.id, uid) if msg else None
        s = self.ed.get(key) if key else None
        valid = bool(s and msg and msg.message_id in (s.msg.message_id, s.ask.message_id if s.ask else None))
        if not s or not msg or not valid:
            await cb.answer(ui.EXPIRED, show_alert=True)
            if msg:
                with contextlib.suppress(TelegramAPIError):
                    await msg.edit_reply_markup(reply_markup=None)
            return
        if s.phase == "busy":
            await cb.answer(ui.ED_WORKING, show_alert=True)
            return
        s.touched = time.time()

        if op == "x":
            await cb.answer()
            await self._ed_drop(key, s)
            await safe_edit(s.msg, f"<b>{ui.ED_CANCELLED}</b>")
            self.spawn(self._delete_later(s.msg, 5))
            return
        if s.phase != "card":
            await cb.answer()
            return
        if op in ("t", "a", "c"):
            what = {"t": "title", "a": "artist", "c": "cover"}[op]
            await cb.answer()
            if s.ask:
                await safe_delete(s.ask)
                s.ask = None
            text = ui.ED_ASK_FIELD[what]
            if s.msg.chat.type != ChatType.PRIVATE:
                text += "\n<i>پاسخ را روی همین پیام ریپلای کنید.</i>"
            try:
                s.ask = await s.msg.reply(text, reply_markup=ui.ed_field_keyboard(uid))
            except TelegramAPIError as e:
                log.warning("cannot reply in chat %s: %s", s.msg.chat.id, e)
                return
            s.awaiting = what
        elif op == "b":
            await cb.answer()
            s.awaiting = ""
            if s.ask:
                await safe_delete(s.ask)
                s.ask = None
        elif op == "r":
            await cb.answer()
            s.cover_mode = "keep" if s.cover_mode in ("remove",) else "remove"
            await self._ed_refresh(s)
        elif op == "g":
            if not s.title:
                await cb.answer(ui.ED_NEED_TITLE, show_alert=True)
                return
            if self.active.get(s.owner, 0) >= self.s.max_jobs_per_user:
                await cb.answer(ui.BUSY, show_alert=True)
                return
            await cb.answer()
            s.phase, s.awaiting = "busy", ""
            if s.ask:
                await safe_delete(s.ask)
                s.ask = None
            self.active[s.owner] = self.active.get(s.owner, 0) + 1   # released in _ed_run
            self.spawn(self._ed_run(key, s))
        else:
            await cb.answer()

    async def on_ed_input(self, m: Message) -> None:
        key = (m.chat.id, m.from_user.id) if m.from_user else None
        s = self.ed.get(key) if key else None
        if not s or s.phase != "card" or not s.awaiting:
            return
        what = s.awaiting
        if what in ("title", "artist"):
            value = at.clean_text(m.text or "")
            if not value:
                return
            setattr(s, what, value)
        else:
            file = m.photo[-1] if m.photo else m.document
            if not file:
                return
            if (file.file_size or 0) > 15 * 1024 * 1024:
                with contextlib.suppress(TelegramAPIError):
                    notice = await m.reply(ui.ED_COVER_BIG)
                    self.spawn(self._delete_later(notice, 10))
                return
            s.phase = "busy"   # claimed while the picture is processed
            try:
                s.cover_n += 1
                raw = s.workdir / f"cover_in_{s.cover_n}"
                await self._fetch_official(file.file_id, raw, ui.ProgressState(), ui.ED_COVER_BIG)
                s.cover, s.thumb = await at.prepare_cover(raw, s.workdir, f"cover_new_{s.cover_n}")
                s.cover_mode = "new"
            except (at.AudioTagError, vn.VideoNoteError) as e:
                msg = e.user_message
                with contextlib.suppress(TelegramAPIError):
                    notice = await m.reply(msg)
                    self.spawn(self._delete_later(notice, 10))
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("edit: cover failed")
                with contextlib.suppress(TelegramAPIError):
                    notice = await m.reply(ui.GENERIC_ERROR)
                    self.spawn(self._delete_later(notice, 10))
                return
            finally:
                if s.phase == "busy":
                    s.phase = "card"
        s.awaiting, s.touched = "", time.time()
        await safe_delete(m)
        if s.ask:
            await safe_delete(s.ask)
            s.ask = None
        await self._ed_refresh(s)

    async def _ed_run(self, key: tuple, s: EditSession) -> None:
        m = s.src_msg
        is_group = s.msg.chat.type != ChatType.PRIVATE
        interval = self.s.progress_interval_group if is_group else self.s.progress_interval_private
        label = " \u2014 ".join(x for x in (s.artist, s.title) if x)
        st = ui.ProgressState(stage="process", title=ui.ED_TITLE, artists=label, edit=True)
        await safe_edit(s.msg, ui.progress_text(st))
        updater = asyncio.create_task(self._progress_loop(s.msg, st, interval, ""))
        error: Optional[str] = None
        built: Optional[at.Built] = None
        ok = False
        try:
            if self.sem.locked():
                st.set_stage("queued")
            async with self.sem:
                st.set_stage("process")
                built = await at.build(s.src, s.workdir, s.info, s.title, s.artist, s.cover_mode, s.cover)
            size = built.path.stat().st_size
            if size > 49 * 1024 * 1024:   # upload limit of the official server
                raise at.AudioTagError(ui.TOO_BIG)
            thumb: Optional[Path] = None
            if built.cover_applied:
                if s.cover_mode == "new":
                    thumb = s.thumb
                elif s.cover_mode == "keep" and s.info.has_cover:
                    thumb = s.old_thumb

            st.set_stage("upload")
            st.total = size
            reply = ReplyParameters(message_id=m.message_id, allow_sending_without_reply=True)
            thread_id = m.message_thread_id if m.is_topic_message else None

            async def send():
                st.done, st.frac, st.t0 = 0, 0.0, time.monotonic()
                return await self.official_bot().send_audio(
                    chat_id=m.chat.id, message_thread_id=thread_id,
                    audio=ProgressFile(built.path, st),
                    title=s.title[:255], performer=(s.artist[:255] or None),
                    duration=s.info.duration or None,
                    thumbnail=FSInputFile(thumb) if thumb else None,
                    reply_parameters=reply, request_timeout=900,
                )

            await self._with_flood_retry(send)
            ok = True
        except at.AudioTagError as e:
            error = e.user_message
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("edit: building or sending failed")
            error = ui.GENERIC_ERROR
        finally:
            updater.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await updater
            self.active[s.owner] = max(0, self.active.get(s.owner, 1) - 1)
            await self._ed_drop(key, s)

        if ok:
            await safe_delete(s.msg)
            if built and not built.cover_applied:
                with contextlib.suppress(TelegramAPIError):
                    notice = await m.reply(ui.ED_NO_COVER)
                    self.spawn(self._delete_later(notice, 15))
        else:
            await safe_edit(s.msg, ui.ed_error_text(error or ui.GENERIC_ERROR))
            self.spawn(self._delete_later(s.msg, 30))

    # ------------------------------------------------------------------ #
    async def janitor(self) -> None:
        """Expire confirmation messages nobody answered."""
        while True:
            await asyncio.sleep(30)
            now = time.time()
            for token, job in list(self.pending.items()):
                if now - job.created > self.s.pending_ttl:
                    self.pending.pop(token, None)
                    if job.prefetch:
                        job.prefetch.cancel()
                    await safe_edit(job.msg, ui.EXPIRED)
                    self.spawn(self._delete_later(job.msg, 10))
            for key, es in list(self.ed.items()):   # /edit left unfinished
                if es.phase != "busy" and now - es.touched > ED_TTL:
                    await self._ed_drop(key, es)
                    await safe_edit(es.msg, ui.EXPIRED)
                    self.spawn(self._delete_later(es.msg, 10))
            for key, wait in list(self.vn_wait.items()):   # /videomsg asked, but no video came
                if now - wait.created > self.s.pending_ttl:
                    self.vn_wait.pop(key, None)
                    await safe_edit(wait.prompt, ui.EXPIRED)
                    self.spawn(self._delete_later(wait.prompt, 10))


async def run(settings: Settings) -> None:
    ig.configure(settings.instagram_cookies, settings.instagram_proxy)
    if settings.bot_api_url:
        session = AiohttpSession(api=TelegramAPIServer.from_base(settings.bot_api_url, is_local=True))
    else:
        session = AiohttpSession()
    bot = Bot(
        settings.token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    app = SoundCloudBot(bot, settings)
    dp = Dispatcher()
    dp.include_router(app.router)

    me = await bot.get_me()
    log.info("started as @%s", me.username)
    with contextlib.suppress(TelegramAPIError):
        await bot.set_my_commands([
            BotCommand(command="start", description="شروع"),
            BotCommand(command="help", description="راهنما"),
            BotCommand(command="videomsg", description="تبدیل ویدیو به ویدیو مسیج"),
            BotCommand(command="edit", description="تغییر نام، آرتیست و کاور آهنگ"),
        ])

    janitor = asyncio.create_task(app.janitor())
    asyncio.create_task(ig.check_session())  # logs whether the Instagram cookie works from this server
    asyncio.create_task(sc.warmup())
    try:
        await dp.start_polling(
            bot, allowed_updates=["message", "callback_query"], drop_pending_updates=True
        )
    finally:
        janitor.cancel()
        await app.close_official()
        await bot.session.close()