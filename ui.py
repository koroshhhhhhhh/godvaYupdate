"""All user-facing text, keyboards and the progress-bar rendering live here."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from html import escape
from typing import Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# --------------------------------------------------------------------------- #
# Static texts
# --------------------------------------------------------------------------- #
START_TEXT = (
    "<b>خوش آمدید</b>\n\n"
    "لینک را بفرستید و فایل را دریافت کنید.\n"
    "برای دیدن امکانات و روش استفاده، دکمه راهنما را بزنید.\n\n"
    "در گروه‌ها هم قابل استفاده است."
)
HELP_MAIN = (
    "<b>راهنمای ربات</b>\n\n"
    "لینک را بفرستید و فایل را دریافت کنید.\n\n"
    "\u25b8  آهنگ با کاور و اطلاعات کامل\n"
    "\u25b8  ویدیوی پین‌ها\n"
    "\u25b8  ویدیو، ریلز، استوری و عکس اینستاگرام\n"
    "\u25b8  ویدیو و شورت یوتیوب با انتخاب کیفیت\n"
    "\u25b8  تبدیل ویدیو به ویدیو مسیج با دستور /videomsg\n"
    "\u25b8  تغییر نام، آرتیست و کاور آهنگ با دستور /edit\n\n"
    "بعد از فرستادن لینک، یک پیام تایید نمایش داده می‌شود. دکمه سبز را بزنید تا دانلود شروع شود.\n"
    "در گروه هم قابل استفاده است؛ لینک را همان‌جا بفرستید.\n\n"
    "<i>برای توضیح هر بخش، یکی از دکمه‌های زیر را بزنید.</i>"
)
HELP_SOUNDCLOUD = (
    "<b>SoundCloud</b>\n\n"
    "دریافت آهنگ با کاور، نام همه خواننده‌ها و اطلاعات کامل.\n\n"
    "<b>چطور استفاده کنید</b>\n"
    "۱. لینک آهنگ را از ساندکلاد کپی کنید و بفرستید.\n"
    "۲. روی پیام تایید، دکمه ✓ را بزنید.\n"
    "۳. نوار پیشرفت نمایش داده می‌شود و آهنگ بعد از چند لحظه ارسال می‌شود.\n\n"
    "<b>پلی‌لیست</b>\n"
    "لینک پلی‌لیست هم پشتیبانی می‌شود؛ آهنگ‌ها یکی‌یکی ارسال می‌شوند. با دکمه «توقف» هر وقت خواستید قطعش کنید."
)
HELP_PINTEREST = (
    "<b>Pinterest</b>\n\n"
    "دریافت ویدیوی پین همراه با توضیحات.\n\n"
    "<b>چطور استفاده کنید</b>\n"
    "۱. لینک پین ویدیویی را بفرستید. لینک کوتاه pin.it هم کار می‌کند.\n"
    "۲. روی پیام تایید، دکمه ✓ را بزنید.\n\n"
    "فقط پین‌های ویدیویی دانلود می‌شوند؛ عکس پشتیبانی نمی‌شود."
)
HELP_INSTAGRAM = (
    "<b>Instagram</b>\n\n"
    "دریافت ویدیو، ریلز، استوری و عکس از اینستاگرام.\n\n"
    "<b>چطور استفاده کنید</b>\n"
    "۱. لینک پست، ریلز یا استوری را بفرستید.\n"
    "۲. روی پیام تایید، دکمه ✓ را بزنید.\n\n"
    "پست‌های چندتایی (اسلایدی) به‌صورت آلبوم ارسال می‌شوند.\n"
    "پیج‌های خصوصی ممکن است قابل دانلود نباشند."
)
HELP_YOUTUBE = (
    "<b>YouTube</b>\n\n"
    "دریافت ویدیو و شورت یوتیوب با کیفیتی که خودتان انتخاب می‌کنید.\n\n"
    "<b>چطور استفاده کنید</b>\n"
    "۱. لینک ویدیو یا شورت را بفرستید.\n"
    "۲. از دکمه‌های آبی، کیفیت مورد نظر را بزنید؛ دانلود همان لحظه شروع می‌شود.\n\n"
    "فقط کیفیت‌هایی نشان داده می‌شوند که خود ویدیو دارد.\n"
    "پلی‌لیست و پخش زنده پشتیبانی نمی‌شود."
)
SEND_LINK_HINT = "لینک یک آهنگ ساندکلاد را بفرستید."
EXPIRED = "این درخواست منقضی شده است. لینک را دوباره بفرستید."
NOT_YOURS = "فقط فرستنده لینک می‌تواند این دکمه را بزند."
BUSY = "شما چند دانلود فعال دارید. کمی صبر کنید."
CANCELLED = "دانلود لغو شد."
TOO_BIG = "حجم فایل از حد مجاز ارسال در تلگرام بیشتر است."
GENERIC_ERROR = "مشکلی پیش آمد. دوباره امتحان کنید."
STOPPING = "در حال توقف..."
PL_BUSY = "شما یک پلی‌لیست فعال دارید. تا پایان آن صبر کنید یا آن را متوقف کنید."

# ---- video message (round video) ----
VN_TITLE = "تبدیل به ویدیو مسیج"
VN_ASK = (
    f"<b>{VN_TITLE}</b>\n\n"
    "ویدیوی خود را بفرستید.\n\n"
    "\u25b8  تصویر به‌صورت مربع و از وسط برش می‌خورد\n"
    "\u25b8  حداکثر ۶۰ ثانیه؛ اگر بلندتر باشد، ۶۰ ثانیه اول گرفته می‌شود\n"
    "\u25b8  صدای ویدیو حفظ می‌شود"
)
VN_ASK_GROUP = VN_ASK + "\n\n<i>ویدیو را روی همین پیام ریپلای کنید.</i>"
VN_STEPS = ("download", "process", "upload")
VN_LABELS = {"download": "دریافت ویدیو", "process": "تبدیل به ویدیو مسیج", "upload": "ارسال"}
VN_CANCELLED = "تبدیل لغو شد."
VN_TOO_BIG = "حجم ویدیو برای دریافت زیاد است. ویدیوی کوچک‌تری بفرستید."
VN_TRIMMED = "ویدیو بیشتر از ۶۰ ثانیه بود؛ ۶۰ ثانیه اول آن تبدیل شد."
VN_FORBIDDEN = (
    "تلگرام اجازه ارسال ویدیو مسیج به این چت را نداد. "
    "اگر در تنظیمات حریم خصوصی، گزینه «پیام‌های صوتی» را محدود کرده‌اید، آن را روی «همه» بگذارید و دوباره امتحان کنید."
)


# ---- audio tag editor (/edit) ----
ED_TITLE = "ویرایش آهنگ"
ED_ASK = (
    f"<b>{ED_TITLE}</b>\n\n"
    "آهنگ خود را بفرستید تا نام، آرتیست و کاور آن را عوض کنید.\n\n"
    "\u25b8  کیفیت صدا تغییر نمی‌کند\n"
    "\u25b8  فایل‌های mp3، m4a و flac همان فرمت می‌مانند؛ بقیه به mp3 تبدیل می‌شوند\n"
    "\u25b8  حداکثر حجم آهنگ ۲۰ مگابایت"
)
ED_ASK_GROUP = ED_ASK + "\n\n<i>آهنگ را روی همین پیام ریپلای کنید.</i>"
ED_STEPS = ("download", "process", "upload")
ED_LABELS = {"download": "دریافت آهنگ", "process": "اعمال تغییرات", "upload": "ارسال"}
ED_CANCELLED = "ویرایش لغو شد."
ED_WORKING = "در حال انجام است. کمی صبر کنید."
ED_TOO_BIG = "حجم آهنگ بیشتر از ۲۰ مگابایت است. فایل کوچک‌تری بفرستید."
ED_COVER_BIG = "حجم این تصویر زیاد است. عکس کوچک‌تری بفرستید."
ED_NEED_TITLE = "نام آهنگ نباید خالی باشد."
ED_NO_COVER = "کاور روی این فایل اعمال نشد؛ آهنگ بدون تغییر کاور ارسال شد."
ED_ASK_FIELD = {
    "title": "نام جدید آهنگ را بفرستید.",
    "artist": "نام آرتیست جدید را بفرستید.",
    "cover": "کاور جدید را به‌صورت عکس بفرستید.",
}
ED_COVER_STATE = {
    "keep": "فعلی (بدون تغییر)",
    "none": "ندارد",
    "new": "جدید",
    "remove": "حذف می‌شود",
}


def ed_card_text(title: str, artist: str, cover_state: str) -> str:
    dash = "\u2014"
    return (
        f"<b>{ED_TITLE}</b>\n\n"
        f"<b>نام آهنگ:</b> {escape(title) if title else dash}\n"
        f"<b>آرتیست:</b> {escape(artist) if artist else dash}\n"
        f"<b>کاور:</b> {ED_COVER_STATE[cover_state]}\n\n"
        "<i>برای تغییر هر بخش دکمه‌اش را بزنید و در پایان «ساخت و ارسال» را بزنید.</i>"
    )


def ed_error_text(msg: str) -> str:
    return f"<b>ویرایش انجام نشد</b>\n\n{escape(msg)}"


def _steps_progress_text(st: ProgressState, steps: tuple, labels: dict, default_title: str) -> str:
    """Same look as the other progress messages: title, step list, bar, speed, size."""
    head = f"<b>{escape(st.title or default_title)}</b>"
    if st.artists:
        head += f"\n<i>{escape(st.artists)}</i>"
    if st.stage == "queued":
        return f"{head}\n\nدر صف انتظار..."
    idx = steps.index(st.stage) if st.stage in steps else 0
    lines = []
    for i, s in enumerate(steps):
        mark = "\u2713" if i < idx else ("\u25b8" if i == idx else "\u25cb")
        lines.append(f"<b>{mark}  {labels[s]}</b>" if i == idx else f"{mark}  {labels[s]}")
    text = f"{head}\n\n" + "\n".join(lines)
    if st.stage in ("download", "upload"):
        text += f"\n\n<code>{bar(st.frac)}</code>  <b>{int(st.frac * 100)}%</b>"
        info = []
        if st.speed:
            info.append(f"سرعت: {fmt_bytes(st.speed)}/s")
        if st.eta is not None and st.speed:
            info.append(f"باقی‌مانده: {fmt_time(st.eta)}")
        if info:
            text += "\n" + "  \u00b7  ".join(info)
        if st.total:
            text += f"\nحجم: {fmt_bytes(st.done)} / {fmt_bytes(st.total)}"
        elif st.done:
            text += f"\nحجم: {fmt_bytes(st.done)}"
    return text


def vn_progress_text(st: ProgressState) -> str:
    return _steps_progress_text(st, VN_STEPS, VN_LABELS, VN_TITLE)


def ed_progress_text(st: ProgressState) -> str:
    return _steps_progress_text(st, ED_STEPS, ED_LABELS, ED_TITLE)


def vn_error_text(msg: str) -> str:
    return f"<b>تبدیل انجام نشد</b>\n\n{escape(msg)}"


def error_text(msg: str) -> str:
    return f"<b>دانلود انجام نشد</b>\n\n{escape(msg)}"


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def fmt_bytes(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def fmt_time(sec: Optional[float]) -> str:
    if sec is None:
        return "--:--"
    sec = max(0, int(sec))
    return f"{sec // 60:02d}:{sec % 60:02d}"


def fmt_num(n: Optional[int]) -> str:
    if n is None:
        return ""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def bar(frac: float, width: int = 14) -> str:
    frac = max(0.0, min(1.0, frac))
    filled = round(frac * width)
    return "\u25b0" * filled + "\u25b1" * (width - filled)


# --------------------------------------------------------------------------- #
# Keyboards
# --------------------------------------------------------------------------- #
def _button(text: str, data: str, style: Optional[str] = None) -> InlineKeyboardButton:
    # `style` (primary = blue, success = green, danger = red) exists in Bot API 9.4+.
    # If the installed aiogram does not know the field, fall back to a plain button.
    if style:
        try:
            return InlineKeyboardButton(text=text, callback_data=data, style=style)
        except Exception:
            pass
    return InlineKeyboardButton(text=text, callback_data=data)


def confirm_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            _button("\u2713  تایید", f"dl:y:{token}", "success"),
            _button("\u2715  لغو", f"dl:n:{token}", "danger"),
        ]]
    )


def cancel_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_button("\u2715  لغو", f"dl:n:{token}", "danger")]])


def quality_keyboard(token: str, qualities: list) -> InlineKeyboardMarkup:
    """Blue quality buttons (480p, 720p ...) and a red cancel button."""
    row = [_button("بهترین کیفیت" if q == 0 else f"{q}p", f"dl:q{q}:{token}", "primary") for q in qualities]
    return InlineKeyboardMarkup(inline_keyboard=[row, [_button("\u2715  لغو", f"dl:n:{token}", "danger")]])


def vn_cancel_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_button("\u2715  لغو", f"vn:c:{user_id}", "danger")]])


def ed_cancel_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_button("\u2715  لغو", f"ed:x:{user_id}", "danger")]])


def ed_field_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_button("\u21a9  بازگشت", f"ed:b:{user_id}")]])


def ed_keyboard(user_id: int, cover_mode: str, has_cover: bool) -> InlineKeyboardMarkup:
    cover_row = [_button("\u25a3  کاور", f"ed:c:{user_id}")]
    if (cover_mode == "keep" and has_cover) or cover_mode == "new":
        cover_row.append(_button("حذف کاور", f"ed:r:{user_id}"))
    elif cover_mode == "remove" and has_cover:
        cover_row.append(_button("\u21ba  کاور قبلی", f"ed:r:{user_id}"))
    return InlineKeyboardMarkup(inline_keyboard=[
        [_button("\u270e  نام آهنگ", f"ed:t:{user_id}"), _button("\u270e  آرتیست", f"ed:a:{user_id}")],
        cover_row,
        [_button("\u2713  ساخت و ارسال", f"ed:g:{user_id}", "success"),
         _button("\u2715  لغو", f"ed:x:{user_id}", "danger")],
    ])


def link_keyboard(url: str, label: str = "SoundCloud") -> InlineKeyboardMarkup:
    try:  # blue (primary) link button
        btn = InlineKeyboardButton(text=label, url=url, style="primary")
    except Exception:
        btn = InlineKeyboardButton(text=label, url=url)
    return InlineKeyboardMarkup(inline_keyboard=[[btn]])


def start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_button("راهنمای ربات", "hp:m", "primary")]])


def help_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_button("اپلیکیشن‌هایی که ربات پشتیبانی می‌کند", "hp:hdr")],
        # Telegram only allows blue / green / red button colors, so SoundCloud (orange)
        # gets an orange marker in the text instead of a background color.
        [_button("\U0001F7E0  SoundCloud", "hp:sc"), _button("Pinterest", "hp:pin", "danger")],
        [_button("Instagram", "hp:ig", "primary"), _button("YouTube", "hp:yt", "danger")],
    ])


def help_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_button("\u25c2  بازگشت به راهنما", "hp:m", "primary")]])


def stop_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_button("\u2715  توقف", f"dl:s:{token}", "danger")]])


# --------------------------------------------------------------------------- #
# Confirmation message
# --------------------------------------------------------------------------- #
def confirm_text(meta=None, platform: str = "soundcloud") -> str:
    head = "<b>آیا از دانلود اطمینان دارید؟</b>"
    if meta is None:
        what = "آهنگ" if platform == "soundcloud" else "لینک"
        return f"{head}\n\n<i>در حال خواندن اطلاعات {what}...</i>"
    lines = [f"<b>{escape(meta.title)}</b>", f"<i>{escape(meta.artists_text)}</i>"]
    extra = [fmt_time(meta.duration)] if meta.duration else []
    if meta.genre:
        extra.append(escape(meta.genre))
    if extra:
        lines.append("  \u00b7  ".join(extra))
    if meta.preview_only:
        lines.append("\nتوجه: فقط پیش‌نمایش کوتاه این آهنگ در دسترس است.")
    return head + "\n\n" + "\n".join(lines)


def playlist_confirm_text(pl, cap: int) -> str:
    head = "<b>آیا از دانلود پلی‌لیست اطمینان دارید؟</b>"
    lines = [f"<b>{escape(pl.title)}</b>"]
    if pl.uploader:
        lines.append(f"<i>{escape(pl.uploader)}</i>")
    info = [f"{pl.total} آهنگ"]
    if pl.duration:
        info.append(fmt_time(pl.duration) if pl.duration < 3600
                    else f"{int(pl.duration // 3600)}:{int(pl.duration % 3600 // 60):02d}:{int(pl.duration % 60):02d}")
    lines.append("  \u00b7  ".join(info))
    if pl.total > cap:
        lines.append(f"\nحداکثر {cap} آهنگ اول دانلود می‌شود.")
    lines.append("\nآهنگ‌ها یکی‌یکی ارسال می‌شوند.")
    return head + "\n\n" + "\n".join(lines)


def playlist_summary(title: str, ok: int, failed: list, stopped: bool, skipped: int) -> str:
    head = "<b>پلی‌لیست متوقف شد</b>" if stopped else "<b>پلی‌لیست تمام شد</b>"
    lines = [head, escape(title), "", f"موفق: {ok}"]
    if failed:
        lines.append(f"ناموفق: {len(failed)}")
        lines += [f"\u25cb  {escape(t[:60])}" for t in failed[:10]]
        if len(failed) > 10:
            lines.append(f"و {len(failed) - 10} مورد دیگر")
    if skipped > 0 and not stopped:
        lines.append(f"\nبه خاطر سقف مجاز، {skipped} آهنگ آخر دانلود نشد.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Progress
# --------------------------------------------------------------------------- #
STEPS = ("info", "download", "process", "upload")
LABELS = {
    "info": "دریافت اطلاعات",
    "download": "دانلود",
    "process": "افزودن کاور و تگ‌ها",
    "upload": "ارسال",
}


MEDIA_LABELS = {
    "info": "دریافت اطلاعات",
    "download": "دانلود",
    "process": "پردازش",
    "upload": "ارسال",
}


@dataclass
class ProgressState:
    stage: str = "info"
    title: str = ""
    artists: str = ""
    frac: float = 0.0
    done: int = 0
    total: int = 0
    speed: float = 0.0
    eta: Optional[float] = None
    pl_title: str = ""
    pl_index: int = 0
    pl_total: int = 0
    brand: str = "ساندکلاد"
    media: bool = False
    vn: bool = False          # video-message conversion (own list of steps)
    edit: bool = False        # audio tag editing (own list of steps)
    t0: float = field(default_factory=time.monotonic)

    def set_stage(self, stage: str) -> None:
        self.stage = stage
        self.frac, self.done, self.total, self.speed, self.eta = 0.0, 0, 0, 0.0, None
        self.t0 = time.monotonic()

    # called from the yt-dlp worker thread
    def on_download(self, done: int, total: int, speed: float, eta, frac: float) -> None:
        self.done, self.total, self.speed, self.eta, self.frac = done, total, speed, eta, frac

    # called by the upload stream for every chunk really sent
    def on_upload_chunk(self, n: int) -> None:
        self.done += n
        elapsed = max(time.monotonic() - self.t0, 0.001)
        self.speed = self.done / elapsed
        if self.total:
            self.frac = min(self.done / self.total, 1.0)
            self.eta = (self.total - self.done) / self.speed if self.speed else None


def progress_text(st: ProgressState) -> str:
    if st.vn:
        return vn_progress_text(st)
    if st.edit:
        return ed_progress_text(st)
    head = f"<b>{escape(st.brand)}</b>"
    if st.title:
        head = f"<b>{escape(st.title)}</b>"
        if st.artists:
            head += f"\n<i>{escape(st.artists)}</i>"
    if st.pl_total:
        head = f"<b>{escape(st.pl_title)}</b>\nآهنگ {st.pl_index} از {st.pl_total}\n\n" + (
            head if st.title else "")
        head = head.rstrip()

    if st.stage == "queued":
        return f"{head}\n\nدر صف انتظار..."

    idx = STEPS.index(st.stage) if st.stage in STEPS else 0
    steps = []
    for i, s in enumerate(STEPS):
        mark = "\u2713" if i < idx else ("\u25b8" if i == idx else "\u25cb")
        label = (MEDIA_LABELS if st.media else LABELS)[s]
        steps.append(f"<b>{mark}  {label}</b>" if i == idx else f"{mark}  {label}")
    text = f"{head}\n\n" + "\n".join(steps)

    if st.pl_total:
        overall = ((st.pl_index - 1) + (idx / len(STEPS))) / st.pl_total
        text += f"\n\nکل: <code>{bar(overall, 10)}</code>  {int(overall * 100)}%"

    if st.stage in ("download", "upload"):
        text += f"\n\n<code>{bar(st.frac)}</code>  <b>{int(st.frac * 100)}%</b>"
        info = []
        if st.speed:
            info.append(f"سرعت: {fmt_bytes(st.speed)}/s")
        if st.eta is not None and st.speed:
            info.append(f"باقی‌مانده: {fmt_time(st.eta)}")
        if info:
            text += "\n" + "  \u00b7  ".join(info)
        if st.total:
            text += f"\nحجم: {fmt_bytes(st.done)} / {fmt_bytes(st.total)}"
        elif st.done:
            text += f"\nحجم: {fmt_bytes(st.done)}"
    return text


# --------------------------------------------------------------------------- #
# Caption under the audio file
# --------------------------------------------------------------------------- #
def _hashtag(t: str) -> str:
    t = re.sub(r"\W+", "_", t.strip()).strip("_")
    return f"#{t}" if t else ""


def caption(meta, limit: int = 1024) -> tuple[str, bool]:
    """HTML caption (<= limit) and whether the description had to be cut."""
    lines = [f"<b>{escape(meta.title)}</b>", escape(meta.artists_text), ""]
    if meta.album and meta.album != meta.title:
        lines.append(f"آلبوم: {escape(meta.album)}")
    if meta.genre:
        lines.append(f"ژانر: {escape(meta.genre)}")
    if meta.date:
        lines.append(f"تاریخ انتشار: {meta.date}")
    if meta.label:
        lines.append(f"لیبل: {escape(meta.label)}")
    if meta.composer:
        lines.append(f"آهنگساز: {escape(meta.composer)}")

    stats = []
    dur = meta.full_duration or meta.duration
    if dur:
        stats.append(f"مدت {fmt_time(dur)}")
    for label, val in (("پخش", meta.plays), ("لایک", meta.likes), ("ریپست", meta.reposts)):
        if val:
            stats.append(f"{label} {fmt_num(val)}")
    if stats:
        lines.append("  \u00b7  ".join(stats))
    if meta.preview_only:
        lines.append("توجه: این فایل فقط پیش‌نمایش کوتاه آهنگ است.")

    tags = " ".join(filter(None, (_hashtag(t) for t in meta.tags[:6])))
    if tags:
        lines += ["", escape(tags)]

    base = "\n".join(lines).rstrip()
    truncated = False
    desc = meta.description
    if desc:
        room = limit - len(base) - 60
        if room > 80:
            cut = desc[:room]
            while len(escape(cut)) > room and cut:
                cut = cut[:-20]
            if len(cut) < len(desc):
                cut = cut.rstrip() + "..."
                truncated = True
            base += f"\n\n<blockquote expandable>{escape(cut)}</blockquote>"
        else:
            truncated = True
    return base, truncated


# --------------------------------------------------------------------------- #
# Pinterest
# --------------------------------------------------------------------------- #
def media_title(info) -> str:
    if info.title:
        return info.title
    at = "@" if getattr(info, "platform", "") == "instagram" else ""
    return f"{at}{info.author}" if info.author else ""


def media_confirm_text(info, choose_quality: bool = False) -> str:
    head = "<b>کیفیت مورد نظر را انتخاب کنید</b>" if choose_quality else "<b>آیا از دانلود اطمینان دارید؟</b>"
    lines = [f"<b>{escape(info.brand)}</b>"]
    if info.author:
        at = "@" if getattr(info, "platform", "") == "instagram" else ""
        lines.append(f"<i>{escape(at + info.author)}</i>")
    kind = getattr(info, "kind_text", None) or "ویدیو"
    dur = getattr(info, "duration", None)
    lines.append(kind + (f"  \u00b7  {fmt_time(dur)}" if dur else ""))
    if info.title:
        lines.append(escape(info.title[:80]))
    return head + "\n\n" + "\n".join(lines)


def media_caption(info, limit: int = 1024) -> tuple[str, bool]:
    """HTML caption (<= limit) and whether the description had to be cut."""
    lines = []
    if info.title:
        lines.append(f"<b>{escape(info.title)}</b>")
    if info.author:
        ig = getattr(info, "platform", "") == "instagram"
        who = ("@" if ig else "") + info.author
        if info.author_name and info.author_name != info.author:
            who += f"  \u00b7  {info.author_name}"
        lines.append(f"<b>{escape(who)}</b>" if ig else escape(who))
    if getattr(info, "story", False):
        lines.append("استوری")
    meta = []
    if info.date:
        meta.append(info.date)
    for label, val in (("لایک", info.likes), ("بازدید", info.views), ("کامنت", info.comments)):
        if val:
            meta.append(f"{label} {fmt_num(val)}")
    if meta:
        lines.append("  \u00b7  ".join(meta))

    base = "\n".join(lines)
    link = f'<a href="{escape(info.url, quote=True)}">{escape(info.brand)}</a>'
    truncated, body = False, ""
    desc = (info.description or "").strip()
    if desc:
        room = limit - len(base) - len(link) - 60
        if room > 80:
            cut = desc[:room]
            while len(escape(cut)) > room and cut:
                cut = cut[:-20]
            if len(cut) < len(desc):
                cut = cut.rstrip() + "..."
                truncated = True
            body = f"<blockquote expandable>{escape(cut)}</blockquote>"
        else:
            truncated = True
    return "\n\n".join(p for p in (base, body, link) if p), truncated