"""SoundCloud Telegram bot - entry point and settings.

Edit the settings below (or set the same names as environment variables on Railway).
"""
import asyncio
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

# =========================== SETTINGS ===========================
BOT_TOKEN = "8970727634:AAFPQZsj_GNT0w-Go9K-7Fx771o4SwnxdPc"                           # <- put your bot token here (from @BotFather)

MAX_CONCURRENT_JOBS = 3                  # downloads running at the same time (the rest wait in a queue)
MAX_JOBS_PER_USER = 2                    # active downloads allowed per user
MAX_PLAYLIST_TRACKS = 0                  # 0 = no limit (private chats)
MAX_PLAYLIST_TRACKS_GROUP = 0            # 0 = no limit (groups)
MAX_FILE_MB = 1900                         # Telegram Bot API upload limit is 50 MB
BOT_API_URL = "https://botapi-production-ebce.up.railway.app"                         # optional self-hosted Bot API server (then set MAX_FILE_MB up to ~1900)
INSTAGRAM_COOKIES_FILE = "instagram_cookies.txt"   # save your Instagram cookies file next to main.py with this name
INSTAGRAM_PROXY = ""                     # e.g. "http://user:pass@host:port" - only if Instagram rejects the server IP (or env INSTAGRAM_PROXY)
AUTO_UPDATE_YTDLP = True                 # upgrade yt-dlp to the latest version on every start
LOG_LEVEL = "INFO"
# ================================================================


def update_ytdlp() -> None:
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", "--quiet",
             "--disable-pip-version-check", "yt-dlp[default,curl-cffi]", "gallery-dl"],
            timeout=180, check=False,
        )
    except Exception as exc:  # the bot still works with the version baked into the image
        logging.getLogger("main").warning("yt-dlp update skipped: %s", exc)


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", LOG_LEVEL).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    token = (BOT_TOKEN or os.getenv("BOT_TOKEN") or "").strip()  # main.py first, env var as fallback
    if not token:
        sys.exit("Bot token is empty: write it in BOT_TOKEN at the top of main.py.")
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg was not found in PATH.")

    if AUTO_UPDATE_YTDLP:
        update_ytdlp()

    here = Path(__file__).resolve().parent
    log = logging.getLogger("main")
    import cookies

    ig_cookies = cookies.find("instagram", (INSTAGRAM_COOKIES_FILE, "www_instagram_com_cookies.txt",
                                            "www.instagram.com_cookies.txt", "cookies_instagram.txt")) or ""
    if not ig_cookies:
        log.warning("Instagram cookies NOT FOUND: stories and many posts will not work")
    elif "sessionid" not in open(ig_cookies, encoding="utf-8").read():
        log.warning("The Instagram cookie file has no 'sessionid' (you were not logged in when exporting it)")

    from bot import Settings, run  # imported after the update so the new yt-dlp is used

    asyncio.run(run(Settings(
        token=token,
        max_concurrent_jobs=MAX_CONCURRENT_JOBS,
        max_jobs_per_user=MAX_JOBS_PER_USER,
        max_file_mb=MAX_FILE_MB,
        bot_api_url=BOT_API_URL,
        max_playlist_tracks=MAX_PLAYLIST_TRACKS,
        max_playlist_tracks_group=MAX_PLAYLIST_TRACKS_GROUP,
        instagram_cookies=ig_cookies,
        instagram_proxy=os.getenv("INSTAGRAM_PROXY") or INSTAGRAM_PROXY,
    )))


if __name__ == "__main__":
    main()