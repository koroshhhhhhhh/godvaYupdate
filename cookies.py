"""Find a site's cookie file no matter how it was named, and make it usable.

Looked up (in this order) next to main.py, in a "cookies" folder there, and in the current folder:
  1. the exact names the code expects, then
  2. ANY file whose name contains the site word and "cookie" - for example
     "www.instagram.com_cookies (1).txt" or "instagram_cookies.txt.txt" (Windows hides extensions).

The file is cleaned (Windows line endings, BOM) and a JSON export is converted to the
Netscape format, then saved as a private copy; that copy's path is returned.
"""
from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Optional

log = logging.getLogger("cookies")
HERE = Path(__file__).resolve().parent


def _json_to_netscape(text: str) -> Optional[str]:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if isinstance(data, dict):
        data = data.get("cookies", [])
    if not isinstance(data, list):
        return None
    lines = ["# Netscape HTTP Cookie File"]
    for c in data:
        if not isinstance(c, dict) or "name" not in c or "domain" not in c:
            continue
        domain = str(c["domain"])
        exp = c.get("expirationDate", c.get("expires", 0)) or 0
        try:
            exp = int(float(exp))
        except (TypeError, ValueError):
            exp = 0
        lines.append("\t".join([
            domain, "TRUE" if domain.startswith(".") else "FALSE", str(c.get("path") or "/"),
            "TRUE" if c.get("secure") else "FALSE", str(max(exp, 0)), str(c["name"]), str(c.get("value", "")),
        ]))
    return "\n".join(lines) + "\n" if len(lines) > 1 else None


def _usable(path: Path, keyword: str) -> Optional[str]:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        log.warning("cannot read %s: %s", path, exc)
        return None
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text.lstrip().startswith(("[", "{")):
        converted = _json_to_netscape(text)
        if not converted:
            log.warning("%s looks like JSON but has no usable cookies", path.name)
            return None
        log.info("%s was a JSON export: converted to the Netscape format", path.name)
        text = converted
    elif "\t" not in text:
        log.warning("%s does not look like a Netscape cookie file (no tab-separated lines)", path.name)
        return None
    out = Path(tempfile.gettempdir()) / f"bot_cookies_{keyword}.txt"
    out.write_text(text, encoding="utf-8")
    return str(out)


def find(keyword: str, exact: tuple = (), dirs: Optional[list] = None, warn: bool = True) -> Optional[str]:
    dirs = dirs or [HERE, HERE / "cookies", Path.cwd()]
    dirs = [d for d in dirs if d.is_dir()]
    kw = keyword.lower()
    for d in dirs:
        for name in exact:
            p = d / name
            if name and p.is_file():
                res = _usable(p, kw)
                if res:
                    log.info("%s cookies: %s", keyword, p)
                    return res
    for d in dirs:
        for p in sorted(d.iterdir()):
            n = p.name.lower()
            if p.is_file() and kw in n and "cookie" in n:
                res = _usable(p, kw)
                if res:
                    log.info("%s cookies: %s", keyword, p)
                    return res
    if not warn:
        return None
    shown = sorted({f.name for d in dirs for f in d.iterdir()
                    if f.is_file() and f.suffix.lower() in (".txt", ".json")})
    log.warning("no %s cookie file found. Folders searched: %s. Text/json files seen there: %s",
                keyword, ", ".join(str(d) for d in dirs), shown or "none")
    return None