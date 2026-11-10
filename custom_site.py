"""One extra site (any site yt-dlp supports): send a video link, get the video back.

Delivery is plain: just the video, no caption and no buttons under it.

HOW TO USE: fill SITE_DOMAINS below. While the list is empty this module is switched off.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

import cookies
import social


def _impersonation() -> dict:
    """Look like a real Chrome (TLS fingerprint) when curl-cffi is installed."""
    try:
        import curl_cffi  # noqa: F401
        from yt_dlp.networking.impersonate import ImpersonateTarget

        return {"impersonate": ImpersonateTarget.from_str("chrome")}
    except Exception:
        return {}


_HERE = Path(__file__).resolve().parent


def _find(*names: str) -> Optional[str]:
    for n in names:
        if (_HERE / n).is_file():
            return str(_HERE / n)
    return None


# ============================== FILL THIS IN ==============================
# Every subdomain (www., nl., de., fr., ...) of these domains is matched automatically.
SITE_DOMAINS: list[str] = ["pornhub.com", "pornhub.org", "pornhub.net"]
SITE_NAME = "Pornhub"          # name shown in the progress message
ASK_CONFIRM = True             # False = start downloading right away, without the confirm buttons

# Cookie file of this site: save it next to main.py with ONE of these names.
SITE_COOKIE_FILE = cookies.find("pornhub", ("pornhub_cookies.txt", "www_pornhub_com_cookies.txt",
                                            "www.pornhub.com_cookies.txt", "cookies.txt"))

# If the site refuses your server's IP/region, put a proxy here (or set the SITE_PROXY variable),
# for example "http://user:pass@host:port"
SITE_PROXY = os.getenv("SITE_PROXY", "")

YTDLP_OPTIONS: dict = {
    **_impersonation(),
    **({"cookiefile": SITE_COOKIE_FILE} if SITE_COOKIE_FILE else {}),
    **({"proxy": SITE_PROXY} if SITE_PROXY else {}),
}
# ==========================================================================


def _normalize(domain: str) -> str:
    d = re.sub(r"^https?://", "", (domain or "").strip().lower()).split("/")[0]
    return d[4:] if d.startswith("www.") else d


_raw = [SITE_DOMAINS] if isinstance(SITE_DOMAINS, str) else list(SITE_DOMAINS)  # a plain string works too
_domains = [d for d in (_normalize(x) for x in _raw) if d]
_RX = (
    re.compile(
        r"https?://(?:[\w-]+\.)*(?:" + "|".join(re.escape(d) for d in _domains) + r")(?::\d+)?/[^\s<>\"']+",
        re.I,
    )
    if _domains else None
)

URL_REWRITES: list[tuple[str, str]] = [   # (regex, replacement) applied to every link before downloading
    # Country subdomains (nl., de., fr., ...) and other endings (.org, .net) make the site redirect
    # servers elsewhere, so every variant is turned into the main address www.pornhub.com.
    (r"^https?://(?:[\w-]+\.)*pornhub\.(?:com|org|net)(?=[/?#]|$)", "https://www.pornhub.com"),
]


def rewrite(url: str) -> str:
    for pattern, repl in URL_REWRITES:
        url = re.sub(pattern, repl, url, flags=re.I)
    return url


social.BRANDS["site"] = SITE_NAME
social.REWRITERS["site"] = rewrite
social.EXTRA_OPTS["site"] = YTDLP_OPTIONS


def enabled() -> bool:
    return _RX is not None


def detect(text: Optional[str]) -> Optional[tuple[str, str]]:
    if _RX is None:
        return None
    m = _RX.search(text or "")
    return ("site", m.group(0).rstrip(".,;:!?)]}»")) if m else None