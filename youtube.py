"""YouTube videos and Shorts (single videos, no playlists) via yt-dlp.

Optional files / settings:
  * a cookie file next to main.py named  youtube_cookies.txt  (needed for age-restricted videos and when
    YouTube asks the server to "sign in to confirm you are not a bot")
  * environment variable YOUTUBE_PROXY (for example http://user:pass@host:port) if YouTube blocks the server IP
"""
from __future__ import annotations

import os

import cookies
import social

YT_COOKIE_FILE = cookies.find("youtube", ("youtube_cookies.txt", "www_youtube_com_cookies.txt",
                                          "www.youtube.com_cookies.txt"), warn=False)
YT_PROXY = os.getenv("YOUTUBE_PROXY", "")

YTDLP_OPTIONS: dict = {
    **({"cookiefile": YT_COOKIE_FILE} if YT_COOKIE_FILE else {}),
    **({"proxy": YT_PROXY} if YT_PROXY else {}),
}

social.EXTRA_OPTS["youtube"] = YTDLP_OPTIONS
