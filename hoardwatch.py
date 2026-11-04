#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Hoardwatch: a self-tracking bulk media archiver.

yt-dlp is the primary backend (used in-process). gallery-dl is an optional
second backend (used as an external program) for image-heavy sites and for
profiles that yt-dlp cannot list. Every archive keeps a SQLite ledger of what
the source has listed, what has been downloaded, and what has since
disappeared upstream.

Run without arguments for the GUI. Run with --help for headless options.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import html
import importlib.abc
import importlib.machinery
import io
import json
import os
import platform
import queue
import random
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import traceback
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from concurrent.futures import ThreadPoolExecutor

APP_NAME = "Hoardwatch"
APP_VERSION = "1.0.0"
REPO_URL = "https://github.com/NullAngst/Hoardwatch"
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
FROZEN = bool(getattr(sys, "frozen", False))
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WIN else 0

# Windowed builds have no console streams. Several libraries write to them.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

# PyInstaller points LD_LIBRARY_PATH at its bundled libraries. Child processes (ffmpeg, deno,
# gallery-dl, xdg-open, the file manager) inherit it and can crash against the wrong libssl or
# libstdc++. Put the user's original value back so only this process uses the bundled copies.
if FROZEN and sys.platform.startswith("linux"):
    _orig = os.environ.pop("LD_LIBRARY_PATH_ORIG", None)
    if _orig is not None:
        os.environ["LD_LIBRARY_PATH"] = _orig
    else:
        os.environ.pop("LD_LIBRARY_PATH", None)


# =============================================================== paths

def app_dir() -> str:
    return os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))


def install_dir() -> str:
    """Folder the user sees the program in. Inside an AppImage, sys.executable lives in a
    read-only mount, so use the folder holding the .AppImage file instead."""
    appimage = os.environ.get("APPIMAGE")
    if appimage and os.path.isfile(appimage):
        return os.path.dirname(os.path.abspath(appimage))
    return app_dir()


def resource_path(*parts: str) -> str:
    base = getattr(sys, "_MEIPASS", app_dir())
    return os.path.join(base, *parts)


def _data_dir() -> str:
    portable = os.path.join(install_dir(), "hoardwatch-data")
    if os.path.isdir(portable):
        return portable
    if IS_WIN:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif IS_MAC:
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, APP_NAME)


DATA_DIR = _data_dir()
TOOLS_DIR = os.path.join(DATA_DIR, "tools")
PYLIB_DIR = os.path.join(DATA_DIR, "pylib")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
os.makedirs(TOOLS_DIR, exist_ok=True)


# =============================================================== small helpers

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
URL_RE = re.compile(r"""https?://[^\s<>"'`]+""", re.I)
MEDIA_EXT = {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".m4a", ".mp3", ".ogg", ".opus",
             ".flac", ".wav", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".pdf",
             ".m3u8", ".mpd"}
STATIC_EXT = {".css", ".js", ".woff", ".woff2", ".ttf", ".ico", ".svg", ".xml", ".json"}

# Hosts where gallery-dl is usually the better lister when the source backend is "auto".
GALLERY_FIRST = ("instagram.com", "twitter.com", "x.com", "reddit.com", "pinterest.com",
                 "tumblr.com", "bsky.app", "deviantart.com", "imgur.com", "flickr.com",
                 "artstation.com", "pixiv.net", "facebook.com")


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(s: str | None):
    if not s:
        return None
    try:
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def short_ts(s: str | None) -> str:
    d = parse_iso(s)
    return d.astimezone().strftime("%Y-%m-%d %H:%M") if d else ""


def clean_msg(s) -> str:
    return ANSI_RE.sub("", str(s)).replace("ERROR: ", "").strip()


def safe_name(s: str, maxlen: int = 80) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s or "").strip(" .")
    return s[:maxlen].rstrip(" .") or "untitled"


def split_args(s: str) -> list[str]:
    if not s or not s.strip():
        return []
    if IS_WIN:
        s = s.replace("\\", "\\\\")
    return shlex.split(s)


def human_bytes(n) -> str:
    if not n:
        return ""
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def human_eta(s) -> str:
    if s is None:
        return ""
    s = int(s)
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def host_of(url: str) -> str:
    try:
        h = (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    for p in ("www.", "m.", "mobile."):
        if h.startswith(p):
            h = h[len(p):]
    return h


def host_in(url: str, domains) -> bool:
    h = host_of(url)
    return any(h == d or h.endswith("." + d) for d in domains)


def auto_label(url: str) -> str:
    p = urllib.parse.urlparse(url)
    segs = [s for s in p.path.split("/") if s][:3]
    label = host_of(url) + ((" " + "/".join(segs)) if segs else "")
    v = urllib.parse.parse_qs(p.query).get("v")
    if v:
        label += " " + v[0]
    return label.strip()[:80] or "source"


def extract_urls(text: str) -> list[str]:
    out, seen = [], set()
    for m in URL_RE.finditer(text or ""):
        u = m.group(0).rstrip(".,;:!?)]}'\"")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def first_nonempty(d: dict, keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def open_path(p: str) -> None:
    if IS_WIN:
        os.startfile(p)  # type: ignore[attr-defined]
    elif IS_MAC:
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p])


def reveal_path(p: str) -> None:
    if IS_WIN and os.path.isfile(p):
        subprocess.Popen(["explorer", "/select," + os.path.normpath(p)])
    elif IS_MAC and os.path.exists(p):
        subprocess.Popen(["open", "-R", p])
    else:
        open_path(p if os.path.isdir(p) else os.path.dirname(p))


# =============================================================== errors

class Stopped(Exception):
    """The run was stopped by the user."""


class NoProxyAvailable(Exception):
    """Every configured proxy is cooling down and direct connections are not allowed."""


class ToolError(Exception):
    pass


class EnumError(Exception):
    pass


RATE_RE = re.compile(r"(?i)(HTTP Error 429|too many requests|rate[- ]?limit|confirm you.?re not a bot|"
                     r"temporarily blocked|status code 429|please wait a few minutes)")
PERMANENT_RE = re.compile(r"(?i)(video unavailable|private video|this video is private|has been removed|"
                          r"been terminated|no longer available|removed by the uploader|removed for violating|"
                          r"copyright|members[- ]only|join this channel|post is unavailable|"
                          r"content isn.?t available|does not exist|HTTP Error 404|HTTP Error 410|"
                          r"status code 404|status code 410)")
NETWORK_RE = re.compile(r"(?i)(proxy|socks|connection (refused|reset|aborted)|timed? ?out|unable to connect|"
                        r"network is unreachable|name or service not known|getaddrinfo|"
                        r"temporary failure in name resolution|remote end closed|eof occurred|ssl:)")


def classify_error(msg: str) -> str:
    if RATE_RE.search(msg):
        return "rate"
    if PERMANENT_RE.search(msg):
        return "permanent"
    if NETWORK_RE.search(msg):
        return "network"
    return "other"


# =============================================================== yt-dlp loading
# Updates to yt-dlp are installed as plain wheels into PYLIB_DIR. A meta path
# finder placed first in sys.meta_path serves those packages ahead of the copy
# frozen into the executable, and refuses to fall back to the frozen copy for
# submodules, so an old and a new yt-dlp are never mixed.

OVERRIDE_PACKAGES = ("yt_dlp", "yt_dlp_ejs")


class _OverrideFinder(importlib.abc.MetaPathFinder):
    def __init__(self, root: str, names: tuple):
        self.root, self.names = root, names

    def find_spec(self, fullname, path=None, target=None):
        top = fullname.partition(".")[0]
        if top not in self.names:
            return None
        search = [self.root] if fullname == top else path
        spec = importlib.machinery.PathFinder.find_spec(fullname, search)
        if spec is None:
            raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
        return spec


YTDLP = None
YTDLP_SOURCE = "missing"
YTDLP_LOAD_ERROR = ""


def _override_names() -> tuple:
    return tuple(n for n in OVERRIDE_PACKAGES
                 if os.path.isfile(os.path.join(PYLIB_DIR, n, "__init__.py")))


def load_ytdlp() -> bool:
    global YTDLP, YTDLP_SOURCE, YTDLP_LOAD_ERROR
    names = _override_names()
    if names:
        sys.meta_path.insert(0, _OverrideFinder(PYLIB_DIR, names))
    try:
        import yt_dlp  # noqa: F401
        YTDLP = yt_dlp
        YTDLP_SOURCE = "updated" if "yt_dlp" in names else "bundled"
        return True
    except Exception as exc:  # broken update: fall back to the bundled copy
        YTDLP_LOAD_ERROR = f"{type(exc).__name__}: {exc}"
        if not names:
            return False
        sys.meta_path[:] = [f for f in sys.meta_path if not isinstance(f, _OverrideFinder)]
        for m in list(sys.modules):
            if m.partition(".")[0] in names:
                del sys.modules[m]
        try:
            import yt_dlp  # noqa: F401
            YTDLP = yt_dlp
            YTDLP_SOURCE = "bundled (the installed update failed to load)"
            return True
        except Exception as exc2:
            YTDLP_LOAD_ERROR = f"{type(exc2).__name__}: {exc2}"
            return False


def ytdlp_version() -> str:
    try:
        from yt_dlp.version import __version__
        return __version__
    except Exception:
        return "unknown"


class _NullLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


_DEFAULT_PARAMS = None
_MISSING = object()


def cli_to_params(args: list[str]) -> dict:
    """Turn yt-dlp command-line arguments into API params using yt-dlp's own
    option parser, keeping only what differs from yt-dlp's defaults."""
    global _DEFAULT_PARAMS
    if _DEFAULT_PARAMS is None:
        _DEFAULT_PARAMS = YTDLP.parse_options(["--ignore-config"]).ydl_opts
    try:
        new = YTDLP.parse_options(list(args)).ydl_opts
    except (SystemExit, Exception) as exc:
        detail = clean_msg(exc).splitlines()[-1] if str(exc).strip() else ""
        raise ValueError("yt-dlp rejected the arguments: " + (detail or " ".join(shlex.quote(a) for a in args)))
    diff = {k: v for k, v in new.items() if _DEFAULT_PARAMS.get(k, _MISSING) != v}
    if "postprocessors" in diff:
        base = _DEFAULT_PARAMS.get("postprocessors") or []
        diff["postprocessors"] = [pp for pp in diff["postprocessors"] if pp not in base]
    return diff


# =============================================================== config

DEFAULTS: dict = {
    "archives": [],
    # downloads
    "concurrency": 2,
    "format": "",
    "sort": "",
    "merge_format": "",
    "output_template": "%(upload_date>%Y-%m-%d|undated)s %(title).150B [%(id)s].%(ext)s",
    "write_info_json": True,
    "write_description": True,
    "write_thumbnail": True,
    "write_subs": True,
    "sub_langs": "all,-live_chat",
    "embed_metadata": True,
    "embed_thumbnail": False,
    "embed_subs": False,
    "embed_chapters": True,
    "use_ytdlp_config": False,
    "extra_ytdlp_args": "",
    # tracking
    "verify_files": True,
    "retry_unavailable": False,
    "gone_threshold": 1,
    "drop_guard_pct": 50,
    "gallerydl_metadata": True,
    "gallerydl_extra_args": "",
    # rate limits
    "item_delay_min": 2.0,
    "item_delay_max": 6.0,
    "source_delay": 5.0,
    "sleep_requests": 0.0,
    "rate_limit": "",
    "max_items_per_check": 0,
    "backoff_429_minutes": 10,
    "retries": 10,
    # proxies
    "proxies": [],
    "proxy_mode": "round_robin",
    "proxy_allow_direct": False,
    "proxy_test_url": "https://am.i.mullvad.net/json",
    # accounts
    "auth_profiles": {},
    # tools
    "ffmpeg_path": "",
    "deno_path": "",
    "gallerydl_path": "",
    # ui
    "verbose_log": False,
    "clipboard_watch": False,
    "theme": "Hoardwatch",
    "window_geometry": "",
    "sash": 350,
}


class Config:
    def __init__(self):
        self.data = json.loads(json.dumps(DEFAULTS))
        self.lock = threading.Lock()

    @classmethod
    def load(cls) -> "Config":
        c = cls()
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                c.data.update(json.load(f))
        except FileNotFoundError:
            pass
        except Exception:
            try:
                shutil.copyfile(CONFIG_PATH, CONFIG_PATH + ".corrupt")
            except OSError:
                pass
        return c

    def save(self) -> None:
        with self.lock:
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp = CONFIG_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
            if not IS_WIN:
                os.chmod(tmp, 0o600)
            os.replace(tmp, CONFIG_PATH)

    def __getitem__(self, k):
        return self.data.get(k, DEFAULTS.get(k))

    def __setitem__(self, k, v):
        self.data[k] = v

    def snapshot(self) -> dict:
        with self.lock:
            snap = json.loads(json.dumps(self.data))
        for k, v in DEFAULTS.items():
            snap.setdefault(k, v)
        return snap


def auth_args(profile: dict | None) -> list[str]:
    """Same flag names work for yt-dlp and gallery-dl."""
    if not profile:
        return []
    a = []
    if profile.get("cookies_file"):
        a += ["--cookies", profile["cookies_file"]]
    elif profile.get("cookies_browser"):
        a += ["--cookies-from-browser", profile["cookies_browser"]]
    if profile.get("username"):
        a += ["-u", profile["username"], "-p", profile.get("password", "")]
    return a


# =============================================================== networking

USER_AGENT = f"{APP_NAME}/{APP_VERSION} (+{REPO_URL})"


def http_get(url: str, proxy: str | None = None, timeout: int = 60, headers: dict | None = None,
             dest: str | None = None, progress=None):
    """GET a URL. With a proxy the request goes through yt-dlp's networking stack,
    which understands socks5:// and socks5h:// proxies."""
    hdr = {"User-Agent": USER_AGENT}
    if headers:
        hdr.update(headers)
    ydl = None
    try:
        if proxy and YTDLP is not None:
            from yt_dlp.networking import Request
            ydl = YTDLP.YoutubeDL({"proxy": proxy, "quiet": True, "no_warnings": True,
                                   "socket_timeout": timeout, "logger": _NullLogger()})
            resp = ydl.urlopen(Request(url, headers=hdr))
        else:
            resp = urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=timeout)
        total = int(resp.headers.get("Content-Length") or 0)
        out = open(dest, "wb") if dest else io.BytesIO()
        got = 0
        try:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                out.write(chunk)
                got += len(chunk)
                if progress:
                    progress(got, total)
        finally:
            resp.close()
            if dest:
                out.close()
        return dest if dest else out.getvalue()
    finally:
        if ydl is not None:
            ydl.close()


def meta_proxy(cfg: dict) -> str | None:
    """Proxy used for app traffic (updates, page scans, tests)."""
    if cfg.get("proxy_mode") == "off":
        return None
    px = [p.strip() for p in cfg.get("proxies") or [] if p.strip() and not p.strip().startswith("#")]
    return px[0] if px else None


# =============================================================== external tools

TOOL_KEYS = {"ffmpeg": "ffmpeg_path", "deno": "deno_path", "gallery-dl": "gallerydl_path"}


def exe_name(n: str) -> str:
    return n + (".exe" if IS_WIN else "")


def tool_path(cfg: dict, name: str) -> str | None:
    custom = (cfg.get(TOOL_KEYS.get(name, "")) or "").strip()
    cands = []
    if custom:
        cands.append(os.path.join(custom, exe_name(name)) if os.path.isdir(custom) else custom)
    cands += [os.path.join(TOOLS_DIR, exe_name(name)), os.path.join(app_dir(), exe_name(name))]
    for c in cands:
        if c and os.path.isfile(c) and (IS_WIN or os.access(c, os.X_OK)):
            return c
    return shutil.which(name)


def tool_version(path: str | None, name: str) -> str:
    if not path:
        return ""
    arg = "-version" if name == "ffmpeg" else "--version"
    try:
        r = subprocess.run([path, arg], capture_output=True, text=True, timeout=15,
                           creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)
        line = (r.stdout or r.stderr).strip().splitlines()
        return line[0][:80] if line else "unknown"
    except Exception as exc:
        return f"not runnable ({type(exc).__name__})"


def _arch() -> str:
    return "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"


def gh_release(repo: str, tag: str | None, proxy: str | None) -> dict:
    url = f"https://api.github.com/repos/{repo}/releases/" + (f"tags/{tag}" if tag else "latest")
    return json.loads(http_get(url, proxy=proxy, headers={"Accept": "application/vnd.github+json"}))


def _pick_asset(rel: dict, pattern: str) -> tuple[str, str]:
    rx = re.compile(pattern)
    for a in rel.get("assets", []):
        if rx.search(a["name"]):
            return a["name"], a["browser_download_url"]
    raise ToolError(f"No release asset matching {pattern} in {rel.get('tag_name')}")


def _extract_bins(archive: str, wanted: set, dest_dir: str) -> dict:
    found = {}

    def put(name, fileobj):
        target = os.path.join(dest_dir, name)
        tmp = target + ".part"
        with open(tmp, "wb") as dst:
            shutil.copyfileobj(fileobj, dst)
        os.replace(tmp, target)
        if not IS_WIN:
            os.chmod(target, 0o755)
        found[name] = target

    if archive.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                base = os.path.basename(info.filename)
                if base in wanted and not info.is_dir():
                    with z.open(info) as src:
                        put(base, src)
    else:
        with tarfile.open(archive, "r:*") as t:
            for m in t.getmembers():
                base = os.path.basename(m.name)
                if base in wanted and m.isfile():
                    src = t.extractfile(m)
                    if src:
                        put(base, src)
    missing = wanted - set(found)
    if missing:
        raise ToolError("Archive did not contain: " + ", ".join(sorted(missing)))
    return found


def install_tool(name: str, proxy: str | None, log, progress=None) -> str:
    os.makedirs(TOOLS_DIR, exist_ok=True)
    arch = _arch()
    tmpdir = tempfile.mkdtemp(prefix="hw-tool-")
    try:
        if name == "ffmpeg":
            if IS_MAC:
                raise ToolError("No static macOS build is published by yt-dlp/FFmpeg-Builds. "
                                "Install it with Homebrew (brew install ffmpeg).")
            plat = ("win" if IS_WIN else "linux") + ("arm64" if arch == "arm64" else "64")
            rel = gh_release("yt-dlp/FFmpeg-Builds", "latest", proxy)
            fname, url = _pick_asset(rel, rf"^ffmpeg-master-latest-{plat}-gpl\.(zip|tar\.xz)$")
            wanted = {exe_name("ffmpeg"), exe_name("ffprobe")}
        elif name == "deno":
            triple = {
                ("win", "x64"): "x86_64-pc-windows-msvc", ("win", "arm64"): "aarch64-pc-windows-msvc",
                ("mac", "x64"): "x86_64-apple-darwin", ("mac", "arm64"): "aarch64-apple-darwin",
                ("linux", "x64"): "x86_64-unknown-linux-gnu", ("linux", "arm64"): "aarch64-unknown-linux-gnu",
            }[("win" if IS_WIN else "mac" if IS_MAC else "linux", arch)]
            rel = gh_release("denoland/deno", None, proxy)
            fname, url = _pick_asset(rel, rf"^deno-{re.escape(triple)}\.zip$")
            wanted = {exe_name("deno")}
        elif name == "gallery-dl":
            if IS_WIN and arch == "x64":
                pat = r"^gallery-dl\.exe$"
            elif not IS_WIN and not IS_MAC and arch == "x64":
                pat = r"^gallery-dl\.bin$"
            else:
                raise ToolError("No standalone gallery-dl build for this platform. "
                                "Install it with pip (pip install -U gallery-dl) and it will be found on PATH.")
            rel = gh_release("mikf/gallery-dl", None, proxy)
            fname, url = _pick_asset(rel, pat)
            wanted = None
        else:
            raise ToolError(f"Unknown tool {name}")

        log(f"Downloading {fname} ({rel.get('tag_name')})")
        dl = os.path.join(tmpdir, fname)
        http_get(url, proxy=proxy, timeout=120, dest=dl, progress=progress)
        if wanted is None:
            target = os.path.join(TOOLS_DIR, exe_name("gallery-dl"))
            shutil.move(dl, target + ".part")
            os.replace(target + ".part", target)
            if not IS_WIN:
                os.chmod(target, 0o755)
        else:
            _extract_bins(dl, wanted, TOOLS_DIR)
        log(f"Installed {name} {rel.get('tag_name')} into {TOOLS_DIR}")
        return rel.get("tag_name", "")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def update_python_packages(proxy: str | None, log) -> str:
    """Install the newest yt-dlp and yt-dlp-ejs wheels into PYLIB_DIR. Takes effect after restart."""
    staging = PYLIB_DIR + ".new"
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)
    versions = []
    for pkg, mod, required in (("yt-dlp", "yt_dlp", True), ("yt-dlp-ejs", "yt_dlp_ejs", False)):
        try:
            meta = json.loads(http_get(f"https://pypi.org/pypi/{pkg}/json", proxy=proxy))
            ver = meta["info"]["version"]
            wheel = next((f for f in meta["urls"] if f["packagetype"] == "bdist_wheel"
                          and f["filename"].endswith("-none-any.whl")), None)
            if not wheel:
                raise ToolError(f"No pure-Python wheel published for {pkg} {ver}")
            log(f"Downloading {wheel['filename']}")
            data = http_get(wheel["url"], proxy=proxy, timeout=120)
            if hashlib.sha256(data).hexdigest() != wheel["digests"]["sha256"]:
                raise ToolError(f"Checksum mismatch for {wheel['filename']}")
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for n in z.namelist():
                    if n.startswith(mod + "/"):
                        z.extract(n, staging)
            versions.append(f"{pkg} {ver}")
        except Exception as exc:
            if required:
                shutil.rmtree(staging, ignore_errors=True)
                raise
            log(f"Skipped {pkg}: {exc}")
    old = PYLIB_DIR + ".old"
    shutil.rmtree(old, ignore_errors=True)
    if os.path.isdir(PYLIB_DIR):
        os.replace(PYLIB_DIR, old)
    os.replace(staging, PYLIB_DIR)
    shutil.rmtree(old, ignore_errors=True)
    msg = "Installed " + ", ".join(versions) + ". Restart Hoardwatch to use it."
    log(msg)
    return msg


def reset_python_packages() -> None:
    shutil.rmtree(PYLIB_DIR, ignore_errors=True)


# =============================================================== archive ledger

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sources(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url TEXT NOT NULL UNIQUE,
  label TEXT, subdir TEXT,
  backend TEXT DEFAULT 'auto', resolved_backend TEXT,
  auth TEXT DEFAULT '', enabled INTEGER DEFAULT 1,
  added TEXT, last_check TEXT, last_status TEXT, last_count INTEGER);
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  item_key TEXT NOT NULL, url TEXT, title TEXT, uploader TEXT, upload_date TEXT, backend TEXT,
  status TEXT DEFAULT 'pending', upstream TEXT DEFAULT 'present', missing_count INTEGER DEFAULT 0,
  first_seen TEXT, last_seen TEXT, gone_since TEXT, downloaded_at TEXT,
  filepath TEXT, filecount INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0, error TEXT,
  UNIQUE(source_id, item_key));
CREATE INDEX IF NOT EXISTS idx_items_status ON items(source_id, status);
CREATE INDEX IF NOT EXISTS idx_items_key ON items(item_key);
CREATE TABLE IF NOT EXISTS checks(id INTEGER PRIMARY KEY AUTOINCREMENT, started TEXT, finished TEXT, summary TEXT);
"""

ITEM_FIELDS = {"url", "title", "uploader", "upload_date", "backend", "status", "upstream", "gone_since",
               "downloaded_at", "filepath", "filecount", "attempts", "error"}
SOURCE_FIELDS = {"url", "label", "subdir", "backend", "resolved_backend", "auth", "enabled",
                 "last_check", "last_status", "last_count"}


class ArchiveDB:
    """One archive = one folder. The ledger lives inside it, so the folder is portable."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.state = os.path.join(self.root, ".hoardwatch")
        os.makedirs(os.path.join(self.state, "logs"), exist_ok=True)
        self.path = os.path.join(self.state, "archive.db")
        self.lock = threading.RLock()
        self.con = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA foreign_keys=ON")
        with self.con:
            self.con.executescript(SCHEMA)
            self.con.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('schema','1')")

    @staticmethod
    def is_archive(folder: str) -> bool:
        return os.path.isfile(os.path.join(folder, ".hoardwatch", "archive.db"))

    def close(self):
        with self.lock:
            self.con.close()

    # ---- meta / settings
    def get_meta(self, key, default=None):
        with self.lock:
            r = self.con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def set_meta(self, key, value):
        with self.lock, self.con:
            self.con.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (key, value))

    def settings(self) -> dict:
        try:
            s = json.loads(self.get_meta("settings", "{}"))
        except ValueError:
            s = {}
        s.setdefault("name", os.path.basename(self.root.rstrip("/\\")) or self.root)
        return s

    def save_settings(self, s: dict):
        self.set_meta("settings", json.dumps(s))

    @property
    def name(self) -> str:
        return self.settings()["name"]

    # ---- sources
    def add_source(self, url, label="", subdir="", backend="auto", auth="") -> int:
        label = label or auto_label(url)
        subdir = subdir or safe_name(label)
        with self.lock, self.con:
            cur = self.con.execute(
                "INSERT INTO sources(url,label,subdir,backend,auth,added) VALUES(?,?,?,?,?,?)",
                (url.strip(), label, subdir, backend, auth, now_iso()))
            return cur.lastrowid

    def update_source(self, sid, **fields):
        fields = {k: v for k, v in fields.items() if k in SOURCE_FIELDS}
        if not fields:
            return
        with self.lock, self.con:
            self.con.execute(f"UPDATE sources SET {', '.join(k + '=?' for k in fields)} WHERE id=?",
                             (*fields.values(), sid))

    def remove_source(self, sid):
        with self.lock, self.con:
            self.con.execute("DELETE FROM sources WHERE id=?", (sid,))

    def sources(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.con.execute("SELECT * FROM sources ORDER BY label COLLATE NOCASE")]

    def source(self, sid) -> dict | None:
        with self.lock:
            r = self.con.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
        return dict(r) if r else None

    def source_stats(self) -> dict:
        q = """SELECT source_id, COUNT(*) n, SUM(status='done') done,
                      SUM(status IN ('pending','failed')) pending, SUM(status='failed') failed,
                      SUM(status='unavailable') unavail, SUM(upstream='gone') gone
               FROM items GROUP BY source_id"""
        with self.lock:
            return {r["source_id"]: dict(r) for r in self.con.execute(q)}

    def totals(self) -> dict:
        q = """SELECT COUNT(*) n, SUM(status='done') done, SUM(status IN ('pending','failed')) pending,
                      SUM(upstream='gone') gone FROM items"""
        with self.lock:
            r = dict(self.con.execute(q).fetchone())
            r["sources"] = self.con.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        return {k: (v or 0) for k, v in r.items()}

    # ---- items
    def record_enumeration(self, sid: int, entries: list[dict], complete: bool,
                           threshold: int, drop_pct: float, backend: str = "ytdlp") -> tuple[int, int, str]:
        """Upsert what the source lists right now. Items that stop appearing in a
        complete listing are counted as missing and, past the threshold, marked
        as deleted upstream. Returns (new, newly_gone, note)."""
        ts = now_iso()
        with self.lock, self.con:
            # Only items from the same backend are comparable: keys differ between backends.
            prev = self.con.execute("SELECT COUNT(*) FROM items WHERE source_id=? AND backend=? AND upstream='present'",
                                    (sid, backend)).fetchone()[0]
            for e in entries:
                self.con.execute(
                    """INSERT INTO items(source_id,item_key,url,title,uploader,upload_date,backend,
                                         status,upstream,first_seen,last_seen)
                       VALUES(?,?,?,?,?,?,?,'pending','present',?,?)
                       ON CONFLICT(source_id,item_key) DO UPDATE SET
                         last_seen=excluded.last_seen, upstream='present', gone_since=NULL, missing_count=0,
                         url=COALESCE(excluded.url, items.url),
                         title=COALESCE(NULLIF(excluded.title,''), items.title),
                         uploader=COALESCE(NULLIF(excluded.uploader,''), items.uploader),
                         upload_date=COALESCE(NULLIF(excluded.upload_date,''), items.upload_date)""",
                    (sid, e["key"], e.get("url"), e.get("title") or "", e.get("uploader") or "",
                     e.get("upload_date") or "", e.get("backend"), ts, ts))
            new = self.con.execute("SELECT COUNT(*) FROM items WHERE source_id=? AND first_seen=?",
                                   (sid, ts)).fetchone()[0]
            listed = len({e["key"] for e in entries})
            gone, note = 0, ""
            if not complete:
                note = "listing incomplete, deletion tracking skipped this time"
            elif prev >= 10 and listed < prev * (1 - drop_pct / 100.0):
                note = (f"listing returned {listed} of {prev} known items, which looks like a partial "
                        f"listing; nothing was marked deleted")
            else:
                self.con.execute("""UPDATE items SET missing_count=missing_count+1
                                    WHERE source_id=? AND backend=? AND upstream='present' AND last_seen<?""",
                                 (sid, backend, ts))
                gone = self.con.execute("""UPDATE items SET upstream='gone', gone_since=?
                                           WHERE source_id=? AND backend=? AND upstream='present'
                                           AND missing_count>=?""",
                                        (ts, sid, backend, max(1, threshold))).rowcount
        return new, gone, note

    def mark_single_gone(self, sid) -> bool:
        with self.lock, self.con:
            n = self.con.execute("SELECT COUNT(*) FROM items WHERE source_id=?", (sid,)).fetchone()[0]
            if n != 1:
                return False
            return self.con.execute("""UPDATE items SET upstream='gone', gone_since=?
                                       WHERE source_id=? AND upstream='present'""",
                                    (now_iso(), sid)).rowcount > 0

    def pending_items(self, sid, backend, include_unavailable=False) -> list[dict]:
        states = ("pending", "failed", "unavailable") if include_unavailable else ("pending", "failed")
        q = f"""SELECT * FROM items WHERE source_id=? AND backend=? AND upstream='present'
                AND status IN ({','.join('?' * len(states))}) ORDER BY upload_date, first_seen"""
        with self.lock:
            return [dict(r) for r in self.con.execute(q, (sid, backend, *states))]

    def count_pending(self, sid, backend) -> int:
        with self.lock:
            return self.con.execute("""SELECT COUNT(*) FROM items WHERE source_id=? AND backend=?
                                       AND status IN ('pending','failed') AND upstream='present'""",
                                    (sid, backend)).fetchone()[0]

    def find_done_by_key(self, key, exclude_id) -> dict | None:
        with self.lock:
            r = self.con.execute("""SELECT * FROM items WHERE item_key=? AND id!=? AND status='done'
                                    AND filepath IS NOT NULL LIMIT 1""", (key, exclude_id)).fetchone()
        return dict(r) if r and os.path.exists(r["filepath"]) else None

    def update_item(self, iid, **fields):
        fields = {k: v for k, v in fields.items() if k in ITEM_FIELDS}
        if not fields:
            return
        with self.lock, self.con:
            self.con.execute(f"UPDATE items SET {', '.join(k + '=?' for k in fields)} WHERE id=?",
                             (*fields.values(), iid))

    def bump_attempts(self, iid):
        with self.lock, self.con:
            self.con.execute("UPDATE items SET attempts=attempts+1 WHERE id=?", (iid,))

    def mark_done_by_key(self, sid, key, path) -> bool:
        with self.lock, self.con:
            r = self.con.execute("SELECT id, status, filepath FROM items WHERE source_id=? AND item_key=?",
                                 (sid, key)).fetchone()
            if not r:
                return False
            keep_path = r["filepath"] if r["status"] == "done" and r["filepath"] and os.path.exists(r["filepath"]) else path
            self.con.execute("""UPDATE items SET status='done', error=NULL, filepath=?,
                                filecount=CASE WHEN status='done' THEN filecount+1 ELSE 1 END,
                                downloaded_at=COALESCE(downloaded_at, ?) WHERE id=?""",
                             (keep_path, now_iso(), r["id"]))
        return True

    def verify_files(self) -> int:
        with self.lock:
            rows = self.con.execute("SELECT id, filepath FROM items WHERE status='done'").fetchall()
            missing = [r["id"] for r in rows if not r["filepath"] or not os.path.exists(r["filepath"])]
            if missing:
                with self.con:
                    self.con.executemany("""UPDATE items SET status='pending', error='file missing locally',
                                            downloaded_at=NULL WHERE id=?""", [(i,) for i in missing])
        return len(missing)

    def set_status(self, ids, status):
        with self.lock, self.con:
            self.con.executemany("UPDATE items SET status=?, error=NULL WHERE id=?", [(status, i) for i in ids])

    def query_items(self, source_id=None, status=None, upstream=None, text=None, limit=5000) -> list[dict]:
        sql = "SELECT i.*, s.label AS source_label FROM items i JOIN sources s ON s.id=i.source_id WHERE 1=1"
        args: list = []
        if source_id:
            sql += " AND i.source_id=?"
            args.append(source_id)
        if status:
            sql += " AND i.status=?"
            args.append(status)
        if upstream:
            sql += " AND i.upstream=?"
            args.append(upstream)
        if text:
            sql += " AND (i.title LIKE ? OR i.url LIKE ? OR i.item_key LIKE ? OR i.uploader LIKE ?)"
            args += [f"%{text}%"] * 4
        sql += " ORDER BY COALESCE(NULLIF(i.upload_date,''), i.first_seen) DESC LIMIT ?"
        args.append(limit)
        with self.lock:
            return [dict(r) for r in self.con.execute(sql, args)]

    def export_csv(self, path: str) -> int:
        cols = ["source_label", "item_key", "url", "title", "uploader", "upload_date", "status", "upstream",
                "first_seen", "last_seen", "gone_since", "downloaded_at", "filepath", "error"]
        rows = self.query_items(limit=10_000_000)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for r in rows:
                w.writerow([r.get(c) or "" for c in cols])
        return len(rows)

    def start_check(self) -> int:
        with self.lock, self.con:
            return self.con.execute("INSERT INTO checks(started) VALUES(?)", (now_iso(),)).lastrowid

    def finish_check(self, cid, summary: dict):
        with self.lock, self.con:
            self.con.execute("UPDATE checks SET finished=?, summary=? WHERE id=?",
                             (now_iso(), json.dumps(summary), cid))
        self.set_meta("last_check", now_iso())


# =============================================================== engine parts

class ProxyPool:
    def __init__(self, proxies, mode: str, allow_direct: bool):
        self.proxies = [p.strip() for p in proxies or [] if p.strip() and not p.strip().startswith("#")]
        self.mode = mode if self.proxies else "off"
        self.allow_direct = allow_direct or not self.proxies or mode == "off"
        self.bad: dict[str, float] = {}
        self.sticky: dict = {}
        self.idx = 0
        self.lock = threading.Lock()

    def pick(self, sticky_key=None) -> str:
        """Return a proxy URL, or '' for a direct connection."""
        with self.lock:
            if self.mode == "off":
                return ""
            now = time.time()
            live = [p for p in self.proxies if self.bad.get(p, 0) <= now]
            if not live:
                if self.allow_direct:
                    return ""
                raise NoProxyAvailable("All proxies failed recently and direct connections are disabled.")
            if self.mode == "sticky" and sticky_key is not None:
                p = self.sticky.get(sticky_key)
                if p not in live:
                    p = random.choice(live)
                    self.sticky[sticky_key] = p
                return p
            if self.mode == "random":
                return random.choice(live)
            p = live[self.idx % len(live)]
            self.idx += 1
            return p

    def fail(self, proxy: str, cooldown: float = 300):
        if not proxy:
            return
        with self.lock:
            self.bad[proxy] = time.time() + cooldown
            for k, v in list(self.sticky.items()):
                if v == proxy:
                    del self.sticky[k]


class Pacer:
    """Spaces out item starts across all worker threads, with random jitter."""

    def __init__(self, lo: float, hi: float):
        self.lo = max(0.0, float(lo or 0))
        self.hi = max(self.lo, float(hi or 0))
        self.next = 0.0
        self.lock = threading.Lock()

    def wait(self, stop_evt: threading.Event):
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next)
            self.next = start + random.uniform(self.lo, self.hi)
        if start > now and stop_evt.wait(start - now):
            raise Stopped()


class YLogger:
    def __init__(self, engine: "Engine", prefix: str = ""):
        self.e, self.prefix = engine, prefix

    def debug(self, msg):
        if not msg.startswith("[debug] "):
            self.info(msg)

    def info(self, msg):
        if msg.startswith("[download]") and "%" in msg:
            return
        self.e.log("debug", self.prefix + clean_msg(msg))

    def warning(self, msg):
        self.e.log("warn", self.prefix + clean_msg(msg))

    def error(self, msg):
        self.e.log("debug", self.prefix + clean_msg(msg))


def collect_files(info) -> list[str]:
    out = []
    if not isinstance(info, dict):
        return out
    for rd in info.get("requested_downloads") or []:
        fp = rd.get("filepath") or rd.get("_filename")
        if fp:
            out.append(fp)
    for e in info.get("entries") or []:
        out += collect_files(e)
    return out


CONTAINER_IE = re.compile(r"(?i)(tab|playlist|channel|user|profile|album|collection|feed|series|season|"
                          r"show|tag|search|list|podcast)")


def gdl_uploader(kw: dict) -> str:
    for k in ("username", "uploader", "author", "user", "owner", "artist"):
        v = kw.get(k)
        if isinstance(v, dict):
            v = v.get("name") or v.get("username") or v.get("nick")
        if v:
            return str(v)
    return ""


def gdl_entry(kw: dict, src_url: str) -> dict:
    cat = kw.get("category") or "gdl"
    post = first_nonempty(kw, ("tweet_id", "post_shortcode", "post_id", "shortcode", "id", "gallery_id", "media_id"))
    part = first_nonempty(kw, ("num", "media_id", "filename"))
    if post is None:
        key = f"gdl {cat} {kw.get('filename')}.{kw.get('extension')}"
    else:
        key = f"gdl {cat} {post}#{part if part is not None else 1}"
    url = kw.get("post_url")
    if not url and kw.get("tweet_id"):
        url = f"https://x.com/{gdl_uploader(kw) or 'i'}/status/{kw['tweet_id']}"
    title = first_nonempty(kw, ("description", "content", "title", "caption")) or kw.get("filename") or ""
    title = re.sub(r"\s+", " ", str(title))[:200]
    date = str(kw.get("date") or "")[:10]
    return {"key": key, "url": url or src_url, "title": title, "uploader": gdl_uploader(kw),
            "upload_date": date.replace("-", ""), "backend": "gallerydl"}


# =============================================================== engine

class Engine:
    """Runs one check at a time. emit(event, **data) is called from worker threads."""

    def __init__(self, cfg: dict, emit):
        self.cfg = cfg
        self.emit = emit
        self.stop_evt = threading.Event()
        self.proxies = ProxyPool(cfg["proxies"], cfg["proxy_mode"], cfg["proxy_allow_direct"])
        self.pacer = Pacer(cfg["item_delay_min"], cfg["item_delay_max"])
        self.backoff_until = 0.0
        self.lock = threading.Lock()
        self.procs: set = set()
        self.ffmpeg = tool_path(cfg, "ffmpeg")
        self.deno = tool_path(cfg, "deno")
        self.gdl = tool_path(cfg, "gallery-dl")
        self._task = 0
        self._logf = None
        self.downloaded = 0

    # ---- plumbing
    def log(self, level: str, msg: str):
        self.emit("log", level=level, msg=msg)
        f = self._logf
        if f:
            try:
                f.write(f"{dt.datetime.now().strftime('%H:%M:%S')} {level.upper():5} {msg}\n")
                f.flush()
            except (ValueError, OSError):
                pass

    def stop(self):
        self.stop_evt.set()
        for p in list(self.procs):
            try:
                p.kill()
            except OSError:
                pass

    def check_stop(self):
        if self.stop_evt.is_set():
            raise Stopped()

    def new_task(self, title: str) -> int:
        with self.lock:
            self._task += 1
            tid = self._task
        self.emit("task", id=tid, title=title, state="starting")
        return tid

    def set_backoff(self, reason: str):
        mins = float(self.cfg["backoff_429_minutes"] or 0)
        if mins <= 0:
            return
        with self.lock:
            self.backoff_until = max(self.backoff_until, time.time() + mins * 60)
        self.log("warn", f"Rate limited ({reason[:120]}). Pausing new requests for {mins:g} min.")

    def wait_backoff(self):
        while True:
            rem = self.backoff_until - time.time()
            if rem <= 0:
                return
            self.emit("status", msg=f"Rate-limit pause, {human_eta(rem)} left")
            if self.stop_evt.wait(min(rem, 5)):
                raise Stopped()

    def sleep(self, seconds: float):
        if seconds > 0 and self.stop_evt.wait(seconds):
            raise Stopped()

    def resolve_auth(self, src: dict, archive_settings: dict) -> dict | None:
        name = src.get("auth") or ""
        if name == "-":
            return None
        if not name:
            name = archive_settings.get("default_auth") or ""
        return (self.cfg.get("auth_profiles") or {}).get(name) if name else None

    def pick_backend(self, src: dict) -> str:
        b = src.get("backend") or "auto"
        if b in ("ytdlp", "gallerydl"):
            return b
        if src.get("resolved_backend") in ("ytdlp", "gallerydl"):
            return src["resolved_backend"]
        if self.gdl and host_in(src["url"], GALLERY_FIRST):
            return "gallerydl"
        return "ytdlp"

    # ---- yt-dlp
    def ytdlp_args(self, archive_settings: dict, dest: str, auth: dict | None) -> list[str]:
        c = self.cfg
        a = [] if c["use_ytdlp_config"] else ["--ignore-config"]
        a += ["--no-playlist", "-P", dest, "-o", archive_settings.get("output_template") or c["output_template"]]
        if c["format"]:
            a += ["-f", c["format"]]
        if c["sort"]:
            a += ["-S", c["sort"]]
        if c["merge_format"]:
            a += ["--merge-output-format", c["merge_format"]]
        for key, flag in (("write_info_json", "--write-info-json"), ("write_description", "--write-description"),
                          ("write_thumbnail", "--write-thumbnail"), ("embed_metadata", "--embed-metadata"),
                          ("embed_thumbnail", "--embed-thumbnail"), ("embed_subs", "--embed-subs"),
                          ("embed_chapters", "--embed-chapters")):
            if c[key]:
                a.append(flag)
        if c["write_subs"]:
            a += ["--write-subs", "--sub-langs", c["sub_langs"] or "all,-live_chat"]
        if c["rate_limit"]:
            a += ["--limit-rate", str(c["rate_limit"])]
        if float(c["sleep_requests"] or 0) > 0:
            a += ["--sleep-requests", str(c["sleep_requests"])]
        a += ["--retries", str(c["retries"]), "--fragment-retries", str(c["retries"])]
        if self.ffmpeg:
            a += ["--ffmpeg-location", self.ffmpeg]
        if self.deno and self.deno != shutil.which("deno"):
            a += ["--js-runtimes", f"deno:{self.deno}"]
        a += auth_args(auth)
        a += split_args(c["extra_ytdlp_args"])
        a += split_args(archive_settings.get("extra_ytdlp_args", ""))
        return a

    def ytdlp_params(self, args: list[str]) -> dict:
        p = cli_to_params(args)
        p.update(ignoreerrors=False, noprogress=True, no_color=True, consoletitle=False, quiet=True)
        return p

    def enumerate_ytdlp(self, src: dict, params: dict) -> tuple[list[dict], bool]:
        from yt_dlp.utils import DownloadError
        last = None
        for attempt in range(3):
            self.check_stop()
            self.wait_backoff()
            proxy = self.proxies.pick(("src", src["id"]))
            p = dict(params)
            for k in ("writeinfojson", "writedescription", "writethumbnail", "write_all_thumbnails",
                      "writesubtitles", "writeautomaticsub", "writelink", "writeurllink", "getcomments",
                      "allow_playlist_files"):
                p[k] = False
            p.update(extract_flat="in_playlist", skip_download=True, simulate=True, noplaylist=False,
                     postprocessors=[], proxy=proxy, logger=YLogger(self, f"[{src['label']}] "))
            state = {"complete": True}
            entries, seen_keys, seen_urls = [], set(), set()
            try:
                with YTDLP.YoutubeDL(p) as ydl:
                    info = ydl.extract_info(src["url"], download=False)
                    self._walk(ydl, info, entries, seen_keys, seen_urls, 0, state, src)
                return entries, state["complete"]
            except DownloadError as exc:
                msg = clean_msg(exc)
                kind = classify_error(msg)
                last = msg
                if kind == "network" and proxy:
                    self.proxies.fail(proxy)
                    self.log("warn", f"Proxy failed ({proxy}), retrying listing: {msg[:160]}")
                    continue
                if kind == "rate":
                    self.set_backoff(msg)
                    self.proxies.fail(proxy)
                    if attempt < 2:
                        continue
                raise EnumError(msg)
        raise EnumError(last or "listing failed")

    def _walk(self, ydl, info, out, keys, urls, depth, state, src):
        from yt_dlp.utils import DownloadError
        self.check_stop()
        if not info:
            return
        if info.get("_type") in ("playlist", "multi_video") or "entries" in info:
            for e in info.get("entries") or []:
                self.check_stop()
                if e is None:
                    state["complete"] = False
                    continue
                et = e.get("_type", "video")
                if et in ("playlist", "multi_video"):
                    self._walk(ydl, e, out, keys, urls, depth + 1, state, src)
                elif et in ("url", "url_transparent") and depth < 3 and CONTAINER_IE.search(e.get("ie_key") or ""):
                    try:
                        sub = ydl.extract_info(e["url"], download=False, ie_key=e.get("ie_key"))
                    except DownloadError as exc:
                        state["complete"] = False
                        self.log("warn", f"[{src['label']}] could not list {e.get('url')}: {clean_msg(exc)[:160]}")
                        continue
                    self._walk(ydl, sub, out, keys, urls, depth + 1, state, src)
                else:
                    self._leaf(e, info, out, keys, urls, src)
        else:
            self._leaf(info, None, out, keys, urls, src)

    def _leaf(self, e, parent, out, keys, urls, src):
        if e.get("_type") in ("url", "url_transparent"):
            url = e.get("url")
        else:
            url = e.get("webpage_url") or e.get("original_url") or (parent or {}).get("webpage_url") or src["url"]
        ie = e.get("ie_key") or e.get("extractor_key") or (parent or {}).get("extractor_key")
        vid = e.get("id")
        key = f"{ie.lower()} {vid}" if ie and vid else f"url {url}"
        if not url or key in keys or url in urls:
            return
        keys.add(key)
        urls.add(url)
        date = e.get("upload_date") or ""
        if not date and e.get("timestamp"):
            try:
                date = dt.datetime.fromtimestamp(e["timestamp"], dt.timezone.utc).strftime("%Y%m%d")
            except (OverflowError, OSError, ValueError, TypeError):
                date = ""
        out.append({"key": key, "url": url, "title": e.get("title") or "",
                    "uploader": e.get("uploader") or e.get("channel") or (parent or {}).get("uploader") or "",
                    "upload_date": date, "backend": "ytdlp"})

    def download_ytdlp_item(self, db: ArchiveDB, src: dict, item: dict, params: dict, auth) -> str:
        from yt_dlp.utils import DownloadCancelled, DownloadError
        if self.stop_evt.is_set():
            return "stopped"
        cap = int(self.cfg["max_items_per_check"] or 0)
        with self.lock:
            if cap and self.downloaded >= cap:
                return "deferred"
            self.downloaded += 1

        dup = db.find_done_by_key(item["item_key"], item["id"])
        if dup:
            db.update_item(item["id"], status="done", filepath=dup["filepath"], filecount=dup["filecount"],
                           downloaded_at=now_iso(), error=None)
            self.emit("db_changed", path=db.root)
            return "done"

        self.wait_backoff()
        self.pacer.wait(self.stop_evt)
        title = item.get("title") or item["url"]
        tid = self.new_task(title)
        last = {"t": 0.0}

        def hook(d):
            if self.stop_evt.is_set():
                raise DownloadCancelled("Stopped")
            t = time.monotonic()
            if d.get("status") == "downloading" and t - last["t"] < 0.4:
                return
            last["t"] = t
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            got = d.get("downloaded_bytes") or 0
            self.emit("task", id=tid, pct=(got * 100.0 / total) if total else None,
                      speed=d.get("speed"), eta=d.get("eta"),
                      state="downloading" if d.get("status") == "downloading" else "processing")

        err = "unknown error"
        attempts = max(1, min(3, len(self.proxies.proxies) or 1))
        for attempt in range(attempts):
            proxy = self.proxies.pick(("src", src["id"]) if self.cfg["proxy_mode"] == "sticky" else None)
            p = dict(params)
            p.update(proxy=proxy, progress_hooks=[hook], logger=YLogger(self, f"[{src['label']}] "))
            cookie_tmp = None
            if p.get("cookiefile") and os.path.isfile(p["cookiefile"]):
                fd, cookie_tmp = tempfile.mkstemp(prefix="hw-cookies-", suffix=".txt")
                os.close(fd)
                shutil.copyfile(p["cookiefile"], cookie_tmp)
                p["cookiefile"] = cookie_tmp
            db.bump_attempts(item["id"])
            try:
                with YTDLP.YoutubeDL(p) as ydl:
                    info = ydl.extract_info(item["url"], download=True)
                files = [f for f in collect_files(info) if f]
                info = info or {}
                db.update_item(item["id"], status="done" if files else "skipped",
                               filepath=files[0] if files else None, filecount=len(files),
                               downloaded_at=now_iso(), error=None if files else "nothing downloaded (filtered?)",
                               title=info.get("title") or item.get("title"),
                               uploader=info.get("uploader") or info.get("channel") or item.get("uploader"),
                               upload_date=info.get("upload_date") or item.get("upload_date"))
                self.emit("task", id=tid, pct=100.0, state="done" if files else "skipped")
                self.emit("db_changed", path=db.root)
                if files:
                    self.log("ok", f"Saved {os.path.basename(files[0])}")
                return "done" if files else "skipped"
            except DownloadCancelled:
                self.emit("task", id=tid, state="stopped")
                raise Stopped()
            except DownloadError as exc:
                err = clean_msg(exc)
                kind = classify_error(err)
                if kind == "network" and proxy and attempt < attempts - 1:
                    self.proxies.fail(proxy)
                    self.log("warn", f"Proxy {proxy} failed, trying another: {err[:140]}")
                    continue
                if kind == "rate":
                    self.set_backoff(err)
                    self.proxies.fail(proxy)
                status = "unavailable" if kind == "permanent" else "failed"
                db.update_item(item["id"], status=status, error=err[:1000])
                self.emit("task", id=tid, state=status)
                self.emit("db_changed", path=db.root)
                self.log("warn" if status == "unavailable" else "error", f"{title[:80]}: {err[:200]}")
                return status
            finally:
                if cookie_tmp:
                    try:
                        os.remove(cookie_tmp)
                    except OSError:
                        pass
        db.update_item(item["id"], status="failed", error=err[:1000])
        self.emit("task", id=tid, state="failed")
        self.emit("db_changed", path=db.root)
        return "failed"

    def download_ytdlp_items(self, db, src, params, auth, summary):
        items = db.pending_items(src["id"], "ytdlp", self.cfg["retry_unavailable"])
        if not items:
            return
        self.log("info", f"[{src['label']}] downloading {len(items)} item(s)")
        workers = max(1, int(self.cfg["concurrency"] or 1))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(self.download_ytdlp_item, db, src, it, params, auth) for it in items]
            stopped = False
            for f in futs:
                try:
                    r = f.result()
                except Stopped:
                    stopped = True
                    self.stop_evt.set()
                    continue
                except NoProxyAvailable as exc:
                    stopped = True
                    self.log("error", str(exc))
                    self.stop_evt.set()
                    continue
                except Exception as exc:
                    summary["failed"] += 1
                    self.log("error", f"Unexpected error: {exc}\n{traceback.format_exc()}")
                    continue
                if r == "done":
                    summary["downloaded"] += 1
                elif r in ("failed", "unavailable"):
                    summary["failed"] += 1
            if stopped:
                raise Stopped()

    # ---- gallery-dl
    def gdl_args(self, proxy: str, auth) -> list[str]:
        a = []
        if proxy:
            a += ["--proxy", proxy]
        a += auth_args(auth)
        if float(self.cfg["sleep_requests"] or 0) > 0:
            a += ["--sleep-request", str(self.cfg["sleep_requests"])]
        if self.cfg["rate_limit"]:
            a += ["--limit-rate", str(self.cfg["rate_limit"])]
        a += split_args(self.cfg["gallerydl_extra_args"])
        return a

    def _popen(self, cmd):
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                             env=env, creationflags=NO_WINDOW, text=True, encoding="utf-8", errors="replace")
        self.procs.add(p)
        return p

    def _gdl_json(self, url: str, args: list[str], resolve: bool):
        cmd = [self.gdl, "--resolve-json" if resolve else "--dump-json", *args, url]
        p = self._popen(cmd)
        try:
            out, err = p.communicate()
        finally:
            self.procs.discard(p)
        self.check_stop()
        if resolve and re.search(r"unrecognized arguments|no such option", err or "", re.I):
            return self._gdl_json(url, args, resolve=False)
        try:
            data = json.loads(out) if out.strip() else []
        except ValueError:
            raise EnumError((err or "gallery-dl returned no usable output").strip().splitlines()[-1][:300])
        return data, err or "", p.returncode, resolve

    def enumerate_gdl(self, src: dict, auth) -> tuple[list[dict], bool]:
        if not self.gdl:
            raise EnumError("gallery-dl is not installed. Install it from Tools.")
        self.wait_backoff()
        proxy = self.proxies.pick(("src", src["id"]))
        args = self.gdl_args(proxy, auth)
        entries, keys, state = [], set(), {"complete": True}
        self._gdl_walk(src["url"], args, entries, keys, state, 0, src)
        return entries, state["complete"]

    def _gdl_walk(self, url, args, entries, keys, state, depth, src):
        res = self._gdl_json(url, args, resolve=True)
        data, err, rc, resolved = res[0], res[1], res[2], res[3]
        if rc not in (0, None):
            state["complete"] = False
            tail = [ln for ln in err.splitlines() if "error" in ln.lower()]
            if tail:
                self.log("warn", f"[{src['label']}] gallery-dl: {tail[-1][:200]}")
                if RATE_RE.search(tail[-1]):
                    self.set_backoff(tail[-1])
        if not data and rc not in (0, None):
            raise EnumError((err.strip().splitlines() or ["gallery-dl failed"])[-1][:300])
        for msg in data if isinstance(data, list) else []:
            if not isinstance(msg, list) or not msg:
                continue
            t = msg[0]
            if t == 3 and len(msg) >= 3 and isinstance(msg[2], dict):
                e = gdl_entry(msg[2], src["url"])
                if e["key"] not in keys:
                    keys.add(e["key"])
                    entries.append(e)
            elif t == 6 and len(msg) >= 2 and not resolved and depth < 2:
                self.sleep(float(self.cfg["item_delay_min"] or 0))
                self._gdl_walk(msg[1], args, entries, keys, state, depth + 1, src)
            elif isinstance(t, int) and t < 0:
                state["complete"] = False
                info = msg[1] if len(msg) > 1 and isinstance(msg[1], dict) else {}
                self.log("warn", f"[{src['label']}] gallery-dl: {info.get('message') or info}")

    def download_gdl_source(self, db, src, dest, auth, summary):
        if not self.gdl or not db.count_pending(src["id"], "gallerydl"):
            return
        self.wait_backoff()
        proxy = self.proxies.pick(("src", src["id"]))
        lo, hi = float(self.cfg["item_delay_min"] or 0), float(self.cfg["item_delay_max"] or 0)
        cmd = [self.gdl, "-d", dest, "-o", "output.mode=pipe"]
        if hi > 0:
            cmd += ["--sleep", f"{lo:g}-{max(lo, hi):g}"]
        if self.cfg["gallerydl_metadata"]:
            cmd.append("--write-metadata")
        cmd += self.gdl_args(proxy, auth) + [src["url"]]
        tid = self.new_task(f"gallery-dl: {src['label']}")
        self.log("info", f"[{src['label']}] gallery-dl fetching missing files")
        p = self._popen(cmd)

        def drain():
            for line in p.stderr:
                line = clean_msg(line.rstrip())
                if not line:
                    continue
                low = line.lower()
                if "[error]" in low:
                    self.log("error", f"[{src['label']}] {line}")
                    if RATE_RE.search(line):
                        self.set_backoff(line)
                elif "[warning]" in low:
                    self.log("warn", f"[{src['label']}] {line}")
                else:
                    self.log("debug", f"[{src['label']}] {line}")

        th = threading.Thread(target=drain, daemon=True)
        th.start()
        got = matched = 0
        try:
            for line in p.stdout:
                line = line.rstrip("\r\n")
                if not line:
                    continue
                skipped = line.startswith("# ")
                path = line[2:] if skipped else line
                if not os.path.isfile(path):
                    continue
                got += 1
                meta = None
                for cand in (path + ".json", os.path.splitext(path)[0] + ".json"):
                    if os.path.isfile(cand):
                        try:
                            with open(cand, encoding="utf-8") as f:
                                meta = json.load(f)
                            break
                        except (OSError, ValueError):
                            pass
                if meta and db.mark_done_by_key(src["id"], gdl_entry(meta, src["url"])["key"], path):
                    matched += 1
                    if not skipped:
                        summary["downloaded"] += 1
                self.emit("task", id=tid, state="downloading", pct=None, speed=None, eta=None,
                          title=f"gallery-dl: {src['label']} ({got} files)")
                if got % 10 == 0:
                    self.emit("db_changed", path=db.root)
            p.wait()
        finally:
            self.procs.discard(p)
            th.join(timeout=5)
        self.emit("db_changed", path=db.root)
        self.check_stop()
        state = "done" if p.returncode == 0 else "failed"
        self.emit("task", id=tid, pct=100.0 if state == "done" else None, state=state)
        if got and matched < got:
            self.log("warn", f"[{src['label']}] {got - matched} of {got} files could not be matched to ledger "
                             f"entries (turn on gallery-dl metadata in Settings for reliable matching)")
        self.log("info", f"[{src['label']}] gallery-dl finished with exit code {p.returncode}, {got} file(s) seen")

    # ---- orchestration
    def check_archive(self, db: ArchiveDB, source_ids=None, download: bool = True) -> dict:
        summary = {"sources": 0, "listed": 0, "new": 0, "gone": 0, "downloaded": 0, "failed": 0, "errors": 0}
        settings = db.settings()
        cid = db.start_check()
        logname = os.path.join(db.state, "logs", dt.datetime.now().strftime("check-%Y%m%d-%H%M%S.log"))
        try:
            self._logf = open(logname, "a", encoding="utf-8")
        except OSError:
            self._logf = None
        self.emit("check_started", path=db.root, name=db.name)
        self.log("info", f"Check started: {db.name} ({'scan and download' if download else 'scan only'})")
        try:
            if self.cfg["verify_files"]:
                n = db.verify_files()
                if n:
                    self.log("warn", f"{n} previously downloaded file(s) are missing on disk and were queued again")
            srcs = [s for s in db.sources() if (s["id"] in source_ids if source_ids else s["enabled"])]
            for i, src in enumerate(srcs):
                self.check_stop()
                if i:
                    self.sleep(float(self.cfg["source_delay"] or 0))
                summary["sources"] += 1
                try:
                    self.check_source(db, src, settings, download, summary)
                except (Stopped, NoProxyAvailable):
                    raise
                except Exception as exc:
                    summary["errors"] += 1
                    self.log("error", f"[{src['label']}] {exc}")
                    self.log("debug", traceback.format_exc())
        except Stopped:
            self.log("warn", "Stopped by user")
        except NoProxyAvailable as exc:
            self.log("error", str(exc))
        finally:
            db.finish_check(cid, summary)
            self.log("info", "Check finished: " + ", ".join(f"{k} {v}" for k, v in summary.items()))
            if self._logf:
                self._logf.close()
                self._logf = None
            self.emit("check_finished", path=db.root, summary=summary)
            self.emit("db_changed", path=db.root)
        return summary

    def check_source(self, db: ArchiveDB, src: dict, settings: dict, download: bool, summary: dict):
        auth = self.resolve_auth(src, settings)
        dest = os.path.join(db.root, src.get("subdir") or safe_name(src["label"]))
        backend = self.pick_backend(src)
        self.emit("status", msg=f"Listing {src['label']}")
        self.log("info", f"[{src['label']}] listing with {'yt-dlp' if backend == 'ytdlp' else 'gallery-dl'}")
        params = None
        entries = complete = None
        err = ""
        if backend == "ytdlp":
            params = self.ytdlp_params(self.ytdlp_args(settings, dest, auth))
            try:
                entries, complete = self.enumerate_ytdlp(src, params)
            except EnumError as exc:
                err = str(exc)
                # Fall back only when yt-dlp cannot handle the URL at all. A 404, a removal notice, a rate
                # limit or a network failure means something about the content, so switching backends
                # would hide it.
                if ((src.get("backend") or "auto") == "auto" and self.gdl
                        and classify_error(err) == "other"
                        and not host_in(src["url"], ("youtube.com", "youtu.be"))):
                    self.log("warn", f"[{src['label']}] yt-dlp could not list it ({err[:140]}), trying gallery-dl")
                    backend = "gallerydl"
        if backend == "gallerydl":
            try:
                entries, complete = self.enumerate_gdl(src, auth)
                err = ""
            except EnumError as exc:
                err = str(exc)
                entries = None
        if entries is None:
            summary["errors"] += 1
            status = "error: " + err[:200]
            if classify_error(err) == "permanent" and db.mark_single_gone(src["id"]):
                summary["gone"] += 1
                status = "deleted upstream: " + err[:180]
                self.log("warn", f"[{src['label']}] single item appears deleted upstream: {err[:160]}")
            else:
                self.log("error", f"[{src['label']}] listing failed: {err[:300]}")
            db.update_source(src["id"], last_check=now_iso(), last_status=status)
            self.emit("db_changed", path=db.root)
            return
        if (src.get("backend") or "auto") == "auto" and src.get("resolved_backend") != backend:
            db.update_source(src["id"], resolved_backend=backend)
        new, gone, note = db.record_enumeration(src["id"], entries, bool(complete),
                                                int(self.cfg["gone_threshold"] or 1),
                                                float(self.cfg["drop_guard_pct"] or 50), backend)
        summary["listed"] += len(entries)
        summary["new"] += new
        summary["gone"] += gone
        msg = f"[{src['label']}] {len(entries)} listed, {new} new, {gone} newly missing upstream"
        self.log("warn" if note else "info", msg + (f" ({note})" if note else ""))
        db.update_source(src["id"], last_check=now_iso(), last_count=len(entries),
                         last_status=("ok" if not note else "partial") + f", {len(entries)} listed")
        self.emit("db_changed", path=db.root)
        if not download:
            return
        if db.count_pending(src["id"], "ytdlp") or (self.cfg["retry_unavailable"] and
                                                     db.pending_items(src["id"], "ytdlp", True)):
            if params is None:
                params = self.ytdlp_params(self.ytdlp_args(settings, dest, auth))
            self.download_ytdlp_items(db, src, params, auth, summary)
        if db.count_pending(src["id"], "gallerydl"):
            if not self.gdl:
                self.log("warn", f"[{src['label']}] has gallery-dl items but gallery-dl is not installed")
            else:
                self.download_gdl_source(db, src, dest, auth, summary)


# =============================================================== link discovery

ATTR_URL_RE = re.compile(r"""(?:href|src|data-src|data-url|content)\s*=\s*["']([^"'<>\s]+)["']""", re.I)
_IE_CACHE = None


def ytdlp_extractor_for(url: str) -> str | None:
    global _IE_CACHE
    if YTDLP is None:
        return None
    if _IE_CACHE is None:
        from yt_dlp.extractor import gen_extractor_classes
        _IE_CACHE = [ie for ie in gen_extractor_classes() if ie.ie_key() != "Generic"]
    for ie in _IE_CACHE:
        try:
            if ie.suitable(url):
                return ie.ie_key()
        except Exception:
            continue
    return None


def classify_url(url: str) -> str:
    ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower()
    if ext in MEDIA_EXT:
        return "direct file"
    if host_in(url, GALLERY_FIRST):
        return "gallery-dl"
    ie = ytdlp_extractor_for(url)
    return f"yt-dlp: {ie}" if ie else ""


def scan_page(url: str, proxy: str | None) -> list[tuple[str, str]]:
    page = http_get(url, proxy=proxy, timeout=30).decode("utf-8", "replace")
    raw = [html.unescape(m.group(1)) for m in ATTR_URL_RE.finditer(page)]
    raw += URL_RE.findall(page.replace("\\/", "/"))
    out, seen = [], {url}
    for r in raw:
        u = urllib.parse.urljoin(url, r).split("#")[0].rstrip(".,;:!?)]}'\"")
        if not u.startswith(("http://", "https://")) or u in seen:
            continue
        seen.add(u)
        if os.path.splitext(urllib.parse.urlparse(u).path)[1].lower() in STATIC_EXT:
            continue
        kind = classify_url(u)
        if kind:
            out.append((u, kind))
    return out


# =============================================================== GUI

try:
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import filedialog, messagebox, ttk
    _Toplevel = tk.Toplevel
    _Frame = ttk.Frame
except Exception:  # headless systems without Tk can still use --check
    tk = None
    _Toplevel = _Frame = object

# Every theme defines the same keys. "on_accent" is text drawn on the accent color, "stripe" is
# the background of every other table row.
THEMES = {
    "Hoardwatch": {"bg": "#1b2330", "panel": "#222c3a", "field": "#2a3647", "fg": "#dfe6ee",
                   "muted": "#8d9bb0", "accent": "#c99a3f", "accent_hi": "#ddb25a", "on_accent": "#1b2330",
                   "sel": "#35506e", "border": "#33415a", "stripe": "#263142", "ok": "#8cbf8f",
                   "warn": "#d8b35e", "err": "#e0806f", "gone": "#c99a3f"},
    "Light": {"bg": "#f3f3f3", "panel": "#ffffff", "field": "#ffffff", "fg": "#1f1f1f",
              "muted": "#6b6b6b", "accent": "#2f6fbd", "accent_hi": "#3d82d6", "on_accent": "#ffffff",
              "sel": "#cfe2f8", "border": "#c8c8c8", "stripe": "#f6f8fa", "ok": "#2e7d32",
              "warn": "#9a6700", "err": "#c62828", "gone": "#b25e00"},
    "Dark": {"bg": "#1e1e1e", "panel": "#252526", "field": "#2d2d30", "fg": "#e6e6e6",
             "muted": "#9a9a9a", "accent": "#4a90d9", "accent_hi": "#63a4e8", "on_accent": "#ffffff",
             "sel": "#264f78", "border": "#3c3c3c", "stripe": "#2a2a2b", "ok": "#7fbf7f",
             "warn": "#d7ba7d", "err": "#f07a6a", "gone": "#d7a05a"},
    "Verdigris": {"bg": "#16282a", "panel": "#1c3235", "field": "#243f3f", "fg": "#d9ece6",
                  "muted": "#86aaa2", "accent": "#43b3a0", "accent_hi": "#5fc9b6", "on_accent": "#10201f",
                  "sel": "#2e5b57", "border": "#2f4f4d", "stripe": "#20393b", "ok": "#8fd19e",
                  "warn": "#d9b25f", "err": "#e07b67", "gone": "#c98a4b"},
    "Kawaii Light": {"bg": "#fff4f8", "panel": "#ffffff", "field": "#ffeaf2", "fg": "#4a2c3d",
                     "muted": "#a07890", "accent": "#ff7eb6", "accent_hi": "#ff99c6", "on_accent": "#ffffff",
                     "sel": "#ffd0e4", "border": "#f5c2d7", "stripe": "#fff7fa", "ok": "#3f9c6d",
                     "warn": "#b8860b", "err": "#d6455d", "gone": "#a35bc4",
                     "fonts": ["M PLUS Rounded 1c", "Comfortaa", "Nunito", "Quicksand", "Varela Round"]},
    "Kawaii Dark": {"bg": "#24182b", "panel": "#2d1f36", "field": "#3a2846", "fg": "#f6e6f2",
                    "muted": "#b496b5", "accent": "#ff8fc8", "accent_hi": "#ffaad6", "on_accent": "#2a1530",
                    "sel": "#5a3a6b", "border": "#4a3357", "stripe": "#33233d", "ok": "#9be3b5",
                    "warn": "#f3cf7a", "err": "#ff7d8c", "gone": "#c9a0ff",
                    "fonts": ["M PLUS Rounded 1c", "Comfortaa", "Nunito", "Quicksand", "Varela Round"]},
}
PAL: dict = dict(THEMES["Hoardwatch"])
BROWSERS = ["", "firefox", "chrome", "chromium", "brave", "edge", "opera", "vivaldi", "safari", "whale"]
TREE_TAGS = (("done", "ok"), ("failed", "err"), ("unavailable", "muted"), ("skipped", "muted"),
             ("gone", "gone"), ("warn", "warn"), ("error", "err"), ("off", "muted"), ("ok", "ok"))
FONTS: dict = {}
_THEMED: list = []
STATUS_WORDS = {"pending": "pending", "done": "saved", "failed": "failed", "unavailable": "unavailable",
                "skipped": "skipped", "missed": "missed"}


def ago(s: str | None) -> str:
    d = parse_iso(s)
    if not d:
        return "never"
    secs = (dt.datetime.now(dt.timezone.utc) - d).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400:
        return f"{int(secs // 3600)} h ago"
    return short_ts(s)


def _pick_family(wanted, fallback):
    have = {f.lower(): f for f in tkfont.families()}
    for w in wanted:
        if w.lower() in have:
            return have[w.lower()]
    return fallback


def init_fonts(root):
    default = tkfont.nametofont("TkDefaultFont")
    if IS_WIN:
        fam, size, mono = "Segoe UI", 10, ["Consolas", "Cascadia Mono"]
    elif IS_MAC:
        fam, size, mono = default.actual("family"), 12, ["Menlo", "Monaco"]
    else:
        fam = _pick_family(["Noto Sans", "DejaVu Sans", "Liberation Sans", "Cantarell"], default.actual("family"))
        size, mono = 10, ["Noto Sans Mono", "DejaVu Sans Mono", "Liberation Mono"]
    fixed = tkfont.nametofont("TkFixedFont")
    FONTS["family"] = fam
    FONTS["base"] = tkfont.Font(root, family=fam, size=size)
    FONTS["bold"] = tkfont.Font(root, family=fam, size=size, weight="bold")
    FONTS["title"] = tkfont.Font(root, family=fam, size=size + 2, weight="bold")
    FONTS["big"] = tkfont.Font(root, family=fam, size=size + 6, weight="bold")
    FONTS["mono"] = tkfont.Font(root, family=_pick_family(mono, fixed.actual("family")), size=size - 1 if size > 9 else size)
    FONTS["size"] = size
    # Classic Tk widgets (menus, text boxes, entries, combobox lists) read these named fonts. Setting
    # "*Font" in the option database instead would override every ttk style font.
    for n in NAMED_FONTS:
        tkfont.nametofont(n).configure(family=fam, size=size)


NAMED_FONTS = ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkTooltipFont", "TkCaptionFont")


def _set_family():
    fam = _pick_family(PAL["fonts"], FONTS["family"]) if PAL.get("fonts") else FONTS["family"]
    for k in ("base", "bold", "title", "big"):
        FONTS[k].configure(family=fam)
    for n in NAMED_FONTS:
        tkfont.nametofont(n).configure(family=fam)


def apply_theme(root):
    st = ttk.Style(root)
    st.theme_use("clam")
    P = PAL
    _set_family()
    base = FONTS["base"]
    st.configure(".", background=P["bg"], foreground=P["fg"], fieldbackground=P["field"],
                 bordercolor=P["border"], lightcolor=P["panel"], darkcolor=P["panel"],
                 troughcolor=P["panel"], focuscolor=P["accent"], selectbackground=P["sel"],
                 selectforeground=P["fg"], insertcolor=P["fg"], font=base)
    st.configure("TFrame", background=P["bg"])
    st.configure("Panel.TFrame", background=P["panel"])
    st.configure("Bar.TFrame", background=P["panel"])
    st.configure("TLabel", background=P["bg"], foreground=P["fg"])
    st.configure("Muted.TLabel", foreground=P["muted"])
    st.configure("Bar.TLabel", background=P["panel"], foreground=P["muted"])
    st.configure("Panel.TLabel", background=P["panel"])
    st.configure("Title.TLabel", font=FONTS["title"])
    st.configure("Big.TLabel", font=FONTS["big"])
    st.configure("Link.TLabel", foreground=P["muted"])
    st.configure("Ok.TLabel", foreground=P["ok"])
    st.configure("Warn.TLabel", foreground=P["warn"])
    st.configure("Err.TLabel", foreground=P["err"])
    st.configure("Gone.TLabel", foreground=P["gone"])
    st.configure("PanelBig.TLabel", background=P["panel"], font=FONTS["big"])
    st.configure("PanelWarn.TLabel", background=P["panel"], foreground=P["warn"])
    st.configure("Empty.TLabel", background=P["panel"], foreground=P["muted"], font=base)
    st.configure("TButton", background=P["field"], foreground=P["fg"], padding=(10, 4), borderwidth=1, width=-4,
                 bordercolor=P["border"], lightcolor=P["field"], darkcolor=P["field"])
    st.map("TButton", background=[("disabled", P["panel"]), ("active", P["sel"])],
           foreground=[("disabled", P["muted"])], bordercolor=[("focus", P["accent"])])
    st.configure("Accent.TButton", background=P["accent"], foreground=P["on_accent"], font=FONTS["bold"],
                 lightcolor=P["accent"], darkcolor=P["accent"], bordercolor=P["accent"])
    st.map("Accent.TButton", background=[("disabled", P["panel"]), ("active", P["accent_hi"])],
           foreground=[("disabled", P["muted"])])
    st.configure("Stop.TButton", background=P["field"], foreground=P["err"])
    st.map("Stop.TButton", background=[("disabled", P["panel"]), ("active", P["sel"])],
           foreground=[("disabled", P["muted"])])
    st.configure("Treeview", background=P["panel"], fieldbackground=P["panel"], foreground=P["fg"],
                 rowheight=base.metrics("linespace") + 10, borderwidth=0, font=base)
    st.map("Treeview", background=[("selected", P["sel"])], foreground=[("selected", P["fg"])])
    st.configure("Treeview.Heading", background=P["field"], foreground=P["muted"], relief="flat",
                 padding=(6, 5), font=FONTS["bold"])
    st.map("Treeview.Heading", background=[("active", P["sel"])])
    st.configure("TNotebook", background=P["bg"], borderwidth=0, tabmargins=(0, 4, 0, 0))
    st.configure("TNotebook.Tab", background=P["bg"], foreground=P["muted"], padding=(16, 7), borderwidth=1,
                 bordercolor=P["border"], lightcolor=P["bg"], darkcolor=P["bg"])
    st.map("TNotebook.Tab", background=[("selected", P["panel"]), ("active", P["field"])],
           foreground=[("selected", P["fg"])], lightcolor=[("selected", P["panel"])],
           bordercolor=[("selected", P["border"])], padding=[("selected", (16, 7))],
           expand=[("selected", (0, 0, 0, 0))])
    for w in ("TEntry", "TSpinbox", "TCombobox"):
        st.configure(w, fieldbackground=P["field"], foreground=P["fg"], arrowcolor=P["fg"],
                     background=P["field"], insertcolor=P["fg"], bordercolor=P["border"],
                     lightcolor=P["field"], darkcolor=P["field"])
        st.map(w, bordercolor=[("focus", P["accent"])], lightcolor=[("focus", P["accent"])])
    st.map("TCombobox", fieldbackground=[("readonly", P["field"])], foreground=[("readonly", P["fg"])],
           selectbackground=[("readonly", P["field"])], selectforeground=[("readonly", P["fg"])],
           bordercolor=[("focus", P["accent"])])
    st.configure("TCheckbutton", background=P["bg"], foreground=P["fg"], indicatorbackground=P["field"],
                 indicatorforeground=P["on_accent"], indicatorcolor=P["field"])
    st.map("TCheckbutton", background=[("active", P["bg"])],
           indicatorcolor=[("selected", P["accent"])], indicatorbackground=[("selected", P["accent"])])
    st.configure("Bar.TCheckbutton", background=P["panel"])
    st.map("Bar.TCheckbutton", background=[("active", P["panel"])])
    st.configure("TLabelframe", background=P["bg"], bordercolor=P["border"])
    st.configure("TLabelframe.Label", background=P["bg"], foreground=P["muted"])
    st.configure("TPanedwindow", background=P["bg"])
    st.configure("Sash", background=P["border"], sashthickness=6, gripcount=0)
    st.configure("TSeparator", background=P["border"], lightcolor=P["border"], darkcolor=P["border"],
                 bordercolor=P["border"])
    st.configure("Horizontal.TProgressbar", background=P["accent"], troughcolor=P["field"],
                 bordercolor=P["border"], lightcolor=P["accent"], darkcolor=P["accent"], thickness=8)
    for sb in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
        st.configure(sb, background=P["field"], troughcolor=P["panel"], arrowcolor=P["muted"],
                     bordercolor=P["panel"], lightcolor=P["field"], darkcolor=P["field"], gripcount=0)
        st.map(sb, background=[("active", P["sel"])])
    root.option_add("*TCombobox*Listbox.background", P["field"])
    root.option_add("*TCombobox*Listbox.foreground", P["fg"])
    root.option_add("*TCombobox*Listbox.selectBackground", P["sel"])
    root.option_add("*TCombobox*Listbox.selectForeground", P["fg"])
    root.configure(bg=P["bg"])


def _style_widget(w, kind):
    P = PAL
    if kind == "text":
        w.configure(bg=P["field"], fg=P["fg"], insertbackground=P["fg"], highlightbackground=P["border"],
                    highlightcolor=P["accent"], selectbackground=P["sel"], selectforeground=P["fg"])
        for tag, key in getattr(w, "_hw_tags", {}).items():
            w.tag_configure(tag, foreground=P[key])
    elif kind == "listbox":
        w.configure(bg=P["field"], fg=P["fg"], selectbackground=P["sel"], selectforeground=P["fg"],
                    highlightbackground=P["border"], highlightcolor=P["accent"])
    elif kind == "menu":
        w.configure(bg=P["field"], fg=P["fg"], activebackground=P["sel"], activeforeground=P["fg"],
                    selectcolor=P["accent"], disabledforeground=P["muted"], relief="flat", borderwidth=1)
    elif kind == "tree":
        for tag, key in TREE_TAGS:
            w.tag_configure(tag, foreground=P[key])
        w.tag_configure("stripe", background=P["stripe"])
    elif kind in ("toplevel", "canvas"):
        w.configure(bg=P["bg"])


def themed(w, kind):
    _THEMED.append((w, kind))
    _style_widget(w, kind)
    return w


def _walk(w):
    yield w
    for c in w.winfo_children():
        yield from _walk(c)


def retheme(root, name):
    PAL.clear()
    PAL.update(THEMES.get(name) or THEMES["Hoardwatch"])
    apply_theme(root)
    alive = []
    for w, kind in _THEMED:
        try:
            if w.winfo_exists():
                _style_widget(w, kind)
                alive.append((w, kind))
        except tk.TclError:
            pass
    _THEMED[:] = alive
    # Combobox drop-down lists that already exist do not pick up option_add changes.
    for w in _walk(root):
        if w.winfo_class() == "TCombobox":
            try:
                pd = w.tk.call("ttk::combobox::PopdownWindow", w)
                w.tk.call(f"{pd}.f.l", "configure", "-background", PAL["field"], "-foreground", PAL["fg"],
                          "-selectbackground", PAL["sel"], "-selectforeground", PAL["fg"])
            except tk.TclError:
                pass


def make_text(parent, height=6, mono=False, tags=None, **kw):
    kw.setdefault("wrap", "word")
    t = tk.Text(parent, height=height, relief="flat", highlightthickness=1, padx=6, pady=4,
                font=FONTS["mono"] if mono else FONTS["base"], undo=True, **kw)
    t._hw_tags = tags or {}
    return themed(t, "text")


def make_menu(parent):
    return themed(tk.Menu(parent, tearoff=0), "menu")


def _sort_key(v):
    s = str(v).strip().rstrip("%")
    try:
        return (0, float(s), "")
    except ValueError:
        return (1, 0.0, str(v).lower())


def restripe(tree):
    for n, i in enumerate(tree.get_children("")):
        tags = tuple(t for t in tree.item(i, "tags") if t != "stripe")
        tree.item(i, tags=tags + (("stripe",) if n % 2 else ()))


def sort_tree(tree, col=None):
    """Click a heading to sort by it, click again to reverse. Re-applied after every refresh."""
    st = getattr(tree, "_hw_sort", None)
    if col is not None:
        st = (col, not st[1]) if st and st[0] == col else (col, False)
        tree._hw_sort = st
    if st:
        c, rev = st
        rows = sorted(((tree.set(i, c), i) for i in tree.get_children("")),
                      key=lambda r: _sort_key(r[0]), reverse=rev)
        for n, (_v, i) in enumerate(rows):
            tree.move(i, "", n)
        for cid in tree["columns"]:
            title = tree._hw_titles[cid]
            tree.heading(cid, text=title + ((" \u25bc" if rev else " \u25b2") if cid == c else ""))
    restripe(tree)


def set_empty(tree, text):
    """Show a centered hint over a table that has no rows."""
    lab = getattr(tree, "_hw_empty", None)
    if text and not tree.get_children(""):
        if lab is None:
            lab = ttk.Label(tree, style="Empty.TLabel", justify="center", anchor="center")
            tree._hw_empty = lab
        lab.configure(text=text, wraplength=max(260, tree.winfo_width() - 80))
        lab.place(relx=0.5, rely=0.42, anchor="center")
    elif lab is not None:
        lab.place_forget()


def make_tree(parent, cols: list[tuple[str, str, int]], selectmode="extended", height=10, stretch=None):
    frame = ttk.Frame(parent)
    tree = ttk.Treeview(frame, columns=[c[0] for c in cols], show="headings", selectmode=selectmode, height=height)
    tree._hw_titles = {}
    stretch = stretch or ("title", "url", "label", "name")
    for cid, title, width in cols:
        tree._hw_titles[cid] = title
        tree.heading(cid, text=title, anchor="w", command=lambda c=cid: sort_tree(tree, c))
        tree.column(cid, width=width, minwidth=40, stretch=cid in stretch, anchor="w")
    ys = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=ys.set)
    tree.grid(row=0, column=0, sticky="nsew")
    ys.grid(row=0, column=1, sticky="ns")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    themed(tree, "tree")
    return frame, tree


class Tooltip:
    def __init__(self, widget, text, delay=600):
        self.w, self.text, self.delay = widget, text, delay
        self.tip = self.job = None
        widget.bind("<Enter>", self._sched, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _sched(self, _e=None):
        self._hide()
        self.job = self.w.after(self.delay, self._show)

    def _show(self):
        self.job = None
        if self.tip or not self.w.winfo_viewable():
            return
        x = self.w.winfo_pointerx() + 14
        y = self.w.winfo_rooty() + self.w.winfo_height() + 6
        self.tip = tk.Toplevel(self.w)
        self.tip.wm_overrideredirect(True)
        self.tip.configure(bg=PAL["border"])
        tk.Label(self.tip, text=self.text, justify="left", wraplength=360, bg=PAL["field"], fg=PAL["fg"],
                 font=FONTS["base"], padx=8, pady=5).pack(padx=1, pady=1)
        self.tip.wm_geometry(f"+{x}+{y}")

    def _hide(self, _e=None):
        if self.job:
            self.w.after_cancel(self.job)
            self.job = None
        if self.tip:
            self.tip.destroy()
            self.tip = None


def tip(widget, text):
    Tooltip(widget, text)
    return widget


class ScrollFrame(_Frame):
    """A frame that scrolls vertically once its content is taller than max_height."""

    def __init__(self, parent, max_height=None, padding=12):
        super().__init__(parent)
        self.max_height = max_height or int(self.winfo_screenheight() * 0.6)
        self.canvas = themed(tk.Canvas(self, highlightthickness=0, bd=0), "canvas")
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas, padding=padding)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner.bind("<Configure>", self._fit)
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        top = self.winfo_toplevel()
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            top.bind(seq, self._wheel, add="+")

    def _fit(self, _e=None):
        h = self.inner.winfo_reqheight()
        self.canvas.configure(scrollregion=(0, 0, self.inner.winfo_reqwidth(), h),
                              width=self.inner.winfo_reqwidth(), height=min(h, self.max_height))
        if h > self.max_height:
            self.vsb.pack(side="right", fill="y")
        else:
            self.vsb.pack_forget()

    def _wheel(self, e):
        w = self.winfo_containing(e.x_root, e.y_root)
        me = str(self)
        if w is None or not (str(w) == me or str(w).startswith(me + ".")) or not self.winfo_ismapped():
            return
        if self.inner.winfo_reqheight() <= self.canvas.winfo_height():
            return
        if getattr(e, "num", 0) == 4:
            step = -3
        elif getattr(e, "num", 0) == 5:
            step = 3
        else:
            step = -int(e.delta / (1 if IS_MAC else 40)) or (-1 if e.delta > 0 else 1)
        self.canvas.yview_scroll(step, "units")



class Dialog(_Toplevel):
    def __init__(self, parent, title, ok_text="Save"):
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        themed(self, "toplevel")
        self.result = None
        self.body = ttk.Frame(self, padding=16)
        self.body.pack(fill="both", expand=True)
        ttk.Separator(self).pack(fill="x")
        bar = ttk.Frame(self, padding=(16, 10, 16, 12))
        bar.pack(fill="x")
        self.bar = bar
        ttk.Button(bar, text="Cancel" if ok_text else "Close", command=self.destroy).pack(side="right")
        if ok_text:
            ttk.Button(bar, text=ok_text, style="Accent.TButton", command=self._ok).pack(side="right", padx=(0, 8))
            self.bind("<Return>", self._enter)
        self.bind("<Escape>", lambda e: self.destroy())

    def _enter(self, _e):
        # Return inside a multi-line box adds a line; it should not also submit the dialog.
        if isinstance(self.focus_get(), tk.Text):
            return
        self._ok()

    def row(self, r, label, widget, hint=None, parent=None):
        parent = parent or self.body
        ttk.Label(parent, text=label).grid(row=r * 2, column=0, sticky="nw", padx=(0, 14), pady=(8, 0))
        widget.grid(row=r * 2, column=1, sticky="ew", pady=(8, 0))
        if hint:
            ttk.Label(parent, text=hint, style="Muted.TLabel", wraplength=480, justify="left").grid(
                row=r * 2 + 1, column=1, sticky="w", pady=(2, 0))
        parent.columnconfigure(1, weight=1)

    def show(self):
        self.update_idletasks()
        p = self.master
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(self.winfo_reqwidth(), sw - 40), min(self.winfo_reqheight(), sh - 80)
        x = p.winfo_rootx() + max(0, (p.winfo_width() - w) // 2)
        y = p.winfo_rooty() + max(0, (p.winfo_height() - h) // 3)
        self.geometry(f"+{max(0, min(x, sw - w))}+{max(0, min(y, sh - h))}")
        self.grab_set()
        self.focus_set()
        self.wait_window()
        return self.result

    def _ok(self):
        r = self.validate()
        if r is not None:
            self.result = r
            self.destroy()

    def validate(self):
        return True


def path_entry(parent, var, directory=False, save=False):
    f = ttk.Frame(parent)
    ttk.Entry(f, textvariable=var).pack(side="left", fill="x", expand=True)

    def browse():
        if directory:
            p = filedialog.askdirectory(parent=parent, initialdir=var.get() or os.path.expanduser("~"))
        else:
            p = filedialog.askopenfilename(parent=parent)
        if p:
            var.set(p)
    ttk.Button(f, text="Browse", command=browse).pack(side="left", padx=(6, 0))
    return f


class ArchiveDialog(Dialog):
    def __init__(self, parent, app, db: ArchiveDB | None = None):
        super().__init__(parent, "Edit archive" if db else "New archive", ok_text="Save" if db else "Create")
        self.app, self.db = app, db
        s = db.settings() if db else {}
        self.v_name = tk.StringVar(value=s.get("name", ""))
        self.v_path = tk.StringVar(value=db.root if db else "")
        self.v_auto = tk.StringVar(value=str(s.get("auto_check_minutes", 0)))
        self.v_auth = tk.StringVar(value=s.get("default_auth", ""))
        self.v_tmpl = tk.StringVar(value=s.get("output_template", ""))
        self.v_extra = tk.StringVar(value=s.get("extra_ytdlp_args", ""))
        e_name = ttk.Entry(self.body, textvariable=self.v_name, width=50)
        self.row(0, "Name", e_name)
        pe = path_entry(self.body, self.v_path, directory=True)
        if db:
            for c in pe.winfo_children():
                c.configure(state="disabled")
        self.row(1, "Folder", pe, "Downloads and the ledger (.hoardwatch) live here. Move the folder and "
                                  "reopen it to keep the full history." +
                 ("" if db else " Leave blank to use a new folder named after the archive in ~/Hoardwatch."))
        self.row(2, "Auto-check every", ttk.Spinbox(self.body, from_=0, to=100000, increment=15,
                                                    textvariable=self.v_auto, width=8),
                 "Minutes. 0 turns it off. Runs while the app is open; use --check with cron or a "
                 "systemd timer for unattended runs.")
        self.row(3, "Default account", ttk.Combobox(self.body, textvariable=self.v_auth, state="readonly",
                                                   values=[""] + sorted(app.cfg["auth_profiles"])),
                 "Used by sources that do not pick their own.")
        self.row(4, "Output template", ttk.Entry(self.body, textvariable=self.v_tmpl),
                 "Blank uses the global template from Settings.")
        self.row(5, "Extra yt-dlp arguments", ttk.Entry(self.body, textvariable=self.v_extra),
                 "Added after the global extra arguments, only for this archive.")
        e_name.focus_set()

    def validate(self):
        name = self.v_name.get().strip()
        path = self.v_path.get().strip()
        if not path and not self.db:
            if not name:
                messagebox.showerror(APP_NAME, "Give the archive a name or pick a folder.", parent=self)
                return None
            path = os.path.join(os.path.expanduser("~"), APP_NAME, safe_name(name))
        path = os.path.expanduser(path)
        try:
            mins = int(float(self.v_auto.get() or 0))
        except ValueError:
            messagebox.showerror(APP_NAME, "Auto-check must be a number of minutes.", parent=self)
            return None
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Cannot use that folder: {exc}", parent=self)
            return None
        return {"path": path, "name": name or os.path.basename(path.rstrip("/\\")),
                "auto_check_minutes": max(0, mins), "default_auth": self.v_auth.get(),
                "output_template": self.v_tmpl.get().strip(), "extra_ytdlp_args": self.v_extra.get().strip()}


class SourceDialog(Dialog):
    BACKENDS = [("Automatic", "auto"), ("yt-dlp", "ytdlp"), ("gallery-dl", "gallerydl")]

    def __init__(self, parent, app, src: dict | None = None, urls: str = ""):
        super().__init__(parent, "Edit source" if src else "Add sources", ok_text="Save" if src else "Add")
        self.src = src
        src = src or {}
        self.t_urls = make_text(self.body, height=5 if not self.src else 2, width=64)
        self.t_urls.insert("1.0", src.get("url", "") or urls)
        self.row(0, "URL" if self.src else "URLs", self.t_urls,
                 None if self.src else "One per line, or paste any text and every link in it is used: "
                                       "channels, profiles, playlists or single posts.")
        self.v_label = tk.StringVar(value=src.get("label", ""))
        self.v_sub = tk.StringVar(value=src.get("subdir", ""))
        self.v_backend = tk.StringVar(value={v: k for k, v in self.BACKENDS}.get(src.get("backend", "auto")))
        auth = src.get("auth", "")
        self.v_auth = tk.StringVar(value="Archive default" if auth == "" else "None" if auth == "-" else auth)
        self.v_enabled = tk.BooleanVar(value=bool(src.get("enabled", 1)))
        self.row(1, "Label", ttk.Entry(self.body, textvariable=self.v_label),
                 "Blank builds one from the URL. Ignored when adding more than one URL." if not self.src else None)
        self.row(2, "Subfolder", ttk.Entry(self.body, textvariable=self.v_sub),
                 "Relative to the archive folder. Blank uses the label. Sources may share a subfolder.")
        self.row(3, "Backend", ttk.Combobox(self.body, textvariable=self.v_backend, state="readonly",
                                           values=[k for k, _ in self.BACKENDS]),
                 "Automatic uses gallery-dl for image-first sites when it is installed and falls back "
                 "to it when yt-dlp cannot list a URL.")
        self.row(4, "Account", ttk.Combobox(self.body, textvariable=self.v_auth, state="readonly",
                                           values=["Archive default", "None"] + sorted(app.cfg["auth_profiles"])))
        self.row(5, "", ttk.Checkbutton(self.body, text="Include in archive checks", variable=self.v_enabled))
        self.t_urls.focus_set()

    def validate(self):
        urls = extract_urls(self.t_urls.get("1.0", "end"))
        if not urls:
            messagebox.showerror(APP_NAME, "Enter at least one http(s) URL.", parent=self)
            return None
        a = self.v_auth.get()
        return {"urls": urls[:1] if self.src else list(dict.fromkeys(urls)), "label": self.v_label.get().strip(),
                "subdir": self.v_sub.get().strip(),
                "backend": dict(self.BACKENDS)[self.v_backend.get()],
                "auth": "" if a == "Archive default" else "-" if a == "None" else a,
                "enabled": 1 if self.v_enabled.get() else 0}


SETTINGS_LAYOUT = {
    "Downloads": [
        ("concurrency", "int", "Parallel downloads", "Items downloaded at once. Keep this low for sites that rate limit."),
        ("format", "str", "Format (-f)", "Blank uses yt-dlp's default, which is best video plus best audio."),
        ("sort", "str", "Format sort (-S)", "Example: res:1080,codec:h264"),
        ("merge_format", "str", "Merge container", "Example: mkv or mp4. Blank lets yt-dlp choose."),
        ("output_template", "str", "Output template", "yt-dlp template, relative to each source's subfolder."),
        ("write_info_json", "bool", "Save metadata as .info.json", None),
        ("write_description", "bool", "Save description", None),
        ("write_thumbnail", "bool", "Save thumbnail", None),
        ("write_subs", "bool", "Save subtitles", None),
        ("sub_langs", "str", "Subtitle languages", "yt-dlp --sub-langs syntax."),
        ("embed_metadata", "bool", "Embed metadata", None),
        ("embed_thumbnail", "bool", "Embed thumbnail", None),
        ("embed_subs", "bool", "Embed subtitles", None),
        ("embed_chapters", "bool", "Embed chapters", None),
        ("use_ytdlp_config", "bool", "Read my yt-dlp config files", "Off means --ignore-config."),
        ("extra_ytdlp_args", "text", "Extra yt-dlp arguments",
         "Any yt-dlp command-line options, applied last so they override the settings above. "
         "Example: --match-filters \"!is_live\" --no-mtime"),
    ],
    "Tracking": [
        ("verify_files", "bool", "Re-download files missing from disk", "Checked at the start of every check."),
        ("retry_unavailable", "bool", "Retry items that were private or removed", None),
        ("gone_threshold", "int", "Mark deleted after", "Number of complete listings an item must be absent from."),
        ("drop_guard_pct", "int", "Distrust listings that shrink by", "Percent. A listing that suddenly drops "
         "this much is treated as partial and marks nothing deleted."),
        ("gallerydl_metadata", "bool", "Write gallery-dl metadata (.json)", "Needed to match gallery-dl files "
         "to ledger entries."),
        ("gallerydl_extra_args", "text", "Extra gallery-dl arguments", None),
    ],
    "Rate limits": [
        ("item_delay_min", "float", "Delay between items, min", "Seconds, shared across all parallel downloads."),
        ("item_delay_max", "float", "Delay between items, max", "A random delay between min and max is used."),
        ("source_delay", "float", "Delay between sources", "Seconds."),
        ("sleep_requests", "float", "Delay between requests", "Seconds between extraction requests "
         "(--sleep-requests / --sleep-request). Helps with Instagram and TikTok."),
        ("rate_limit", "str", "Bandwidth limit per download", "Example: 2M or 500K. Blank is unlimited."),
        ("max_items_per_check", "int", "Max downloads per check", "0 is unlimited. The rest wait for the next check."),
        ("backoff_429_minutes", "float", "Pause after rate limiting", "Minutes to wait after HTTP 429 or a bot check."),
        ("retries", "int", "Retries", "Network and fragment retries inside yt-dlp."),
    ],
}
SETTING_LABELS = {k: lab for fields in SETTINGS_LAYOUT.values() for k, _kind, lab, _h in fields}


class SettingsDialog(Dialog):
    def __init__(self, parent, app):
        super().__init__(parent, "Settings")
        self.app = app
        self.cfg = app.cfg
        self.vars: dict = {}
        nb = ttk.Notebook(self.body)
        nb.pack(fill="both", expand=True)
        for tab, fields in SETTINGS_LAYOUT.items():
            sf = ScrollFrame(nb)
            nb.add(sf, text=tab)
            outer = sf.inner
            for i, (key, kind, label, hint) in enumerate(fields):
                val = self.cfg[key]
                if kind == "bool":
                    v = tk.BooleanVar(value=bool(val))
                    w = ttk.Checkbutton(outer, text=label, variable=v)
                    w.grid(row=i * 2, column=0, columnspan=2, sticky="w", pady=(8, 0))
                    if hint:
                        ttk.Label(outer, text=hint, style="Muted.TLabel").grid(row=i * 2 + 1, column=0, columnspan=2,
                                                                              sticky="w", padx=(26, 0))
                    self.vars[key] = (kind, v)
                    continue
                if kind == "text":
                    w = make_text(outer, height=3, width=60, mono=True)
                    w.insert("1.0", val or "")
                    self.vars[key] = (kind, w)
                else:
                    v = tk.StringVar(value=str(val if val is not None else ""))
                    self.vars[key] = (kind, v)
                    if kind in ("int", "float"):
                        w = ttk.Frame(outer)
                        ttk.Spinbox(w, textvariable=v, width=10, from_=0, to=1_000_000,
                                    increment=1 if kind == "int" else 0.5).pack(side="left")
                    else:
                        w = ttk.Entry(outer, textvariable=v, width=50)
                self.row(i, label, w, hint, parent=outer)
        self._proxy_tab(nb)
        self._accounts_tab(nb)
        if 0 <= app.settings_tab < len(nb.tabs()):
            nb.select(app.settings_tab)
        nb.bind("<<NotebookTabChanged>>", lambda e: setattr(app, "settings_tab", nb.index("current")))

    def _proxy_tab(self, nb):
        f = ttk.Frame(nb, padding=12)
        nb.add(f, text="Proxies")
        self.t_proxies = make_text(f, height=6, width=60, mono=True, wrap="none")
        self.t_proxies.insert("1.0", "\n".join(self.cfg["proxies"]))
        self.row(0, "Proxy list", self.t_proxies,
                 "One per line. socks5h://host:1080 resolves DNS through the proxy; use it to avoid DNS "
                 "leaks. Mullvad: connect the VPN, then use socks5h://10.64.0.1:1080 or relay proxies such as "
                 "socks5h://se-got-wg-socks5-001.relays.mullvad.net:1080. Lines starting with # are ignored.",
                 parent=f)
        self.v_pmode = tk.StringVar(value=self.cfg["proxy_mode"])
        self.row(1, "Rotation", ttk.Combobox(f, textvariable=self.v_pmode, state="readonly",
                                            values=["round_robin", "random", "sticky", "off"]),
                 "round_robin rotates per item. sticky keeps one proxy per source. off ignores the list.", parent=f)
        self.v_pdirect = tk.BooleanVar(value=self.cfg["proxy_allow_direct"])
        self.row(2, "", ttk.Checkbutton(f, text="Fall back to a direct connection when every proxy fails",
                                        variable=self.v_pdirect),
                 "Leave off if your real IP must never be used. Note that ffmpeg-driven downloads "
                 "(some live streams) do not go through SOCKS.", parent=f)
        self.v_ptest = tk.StringVar(value=self.cfg["proxy_test_url"])
        bar = ttk.Frame(f)
        ttk.Entry(bar, textvariable=self.v_ptest).pack(side="left", fill="x", expand=True)
        ttk.Button(bar, text="Test proxies", command=self._test).pack(side="left", padx=(6, 0))
        self.row(3, "Test URL", bar, parent=f)
        self.t_ptest = make_text(f, height=5, width=60, mono=True, wrap="none",
                                 tags={"ok": "ok", "error": "err"})
        self.row(4, "Results", self.t_ptest, parent=f)

    def _test(self):
        proxies = [p.strip() for p in self.t_proxies.get("1.0", "end").splitlines()
                   if p.strip() and not p.strip().startswith("#")]
        url = self.v_ptest.get().strip() or DEFAULTS["proxy_test_url"]
        self.t_ptest.delete("1.0", "end")
        if not proxies:
            self.t_ptest.insert("end", "No proxies listed.\n")
            return
        self.t_ptest.insert("end", f"Testing {len(proxies)} proxies against {url}\n")

        def work():
            for p in proxies:
                t = time.time()
                try:
                    data = http_get(url, proxy=p, timeout=20).decode("utf-8", "replace").strip()
                    try:
                        j = json.loads(data)
                        info = ", ".join(f"{k}={j[k]}" for k in ("ip", "country", "city", "mullvad_exit_ip") if k in j)
                    except ValueError:
                        info = data[:80]
                    line, tag = f"OK    {p}  {int((time.time() - t) * 1000)} ms  {info}", "ok"
                except Exception as exc:
                    line, tag = f"FAIL  {p}  {clean_msg(exc)[:160]}", "error"
                self.app.emit("proxy_test", widget=self.t_ptest, line=line, tag=tag)
        threading.Thread(target=work, daemon=True).start()

    def _accounts_tab(self, nb):
        f = ttk.Frame(nb, padding=12)
        nb.add(f, text="Accounts")
        self.profiles = json.loads(json.dumps(self.cfg["auth_profiles"]))
        left = ttk.Frame(f)
        left.grid(row=0, column=0, sticky="ns", padx=(0, 14))
        self.lb = themed(tk.Listbox(left, width=22, height=12, relief="flat", highlightthickness=1,
                                    exportselection=False, activestyle="none"), "listbox")
        self.lb.pack(fill="y", expand=True)
        self.lb.bind("<<ListboxSelect>>", lambda e: self._load_profile())
        b = ttk.Frame(left)
        b.pack(fill="x", pady=(6, 0))
        ttk.Button(b, text="New", command=self._new_profile).pack(side="left")
        ttk.Button(b, text="Delete", command=self._del_profile).pack(side="left", padx=(6, 0))
        form = ttk.Frame(f)
        form.grid(row=0, column=1, sticky="nsew")
        f.columnconfigure(1, weight=1)
        self.a_name, self.a_cookies = tk.StringVar(), tk.StringVar()
        self.a_browser, self.a_user, self.a_pass = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.row(0, "Name", ttk.Entry(form, textvariable=self.a_name), parent=form)
        self.row(1, "Cookies file", path_entry(form, self.a_cookies),
                 "Netscape cookies.txt exported from a logged-in browser. Takes priority over browser cookies.",
                 parent=form)
        self.row(2, "Browser cookies", ttk.Combobox(form, textvariable=self.a_browser, values=BROWSERS),
                 "Reads cookies straight from a browser profile. Syntax: firefox, chrome:Profile 1, "
                 "firefox:/path/to/profile. Chromium browsers on Windows may need to be closed first.",
                 parent=form)
        self.row(3, "Username", ttk.Entry(form, textvariable=self.a_user), parent=form)
        self.row(4, "Password", ttk.Entry(form, textvariable=self.a_pass, show="*"),
                 "Stored in plain text in config.json (mode 600 on Linux and macOS). Cookies are the "
                 "safer and more widely supported option.", parent=form)
        bar = ttk.Frame(form)
        bar.grid(row=10, column=1, sticky="w", pady=10)
        ttk.Button(bar, text="Save account", command=self._save_profile).pack(side="left")
        self.l_acct = ttk.Label(bar, text="", style="Muted.TLabel")
        self.l_acct.pack(side="left", padx=10)
        ttk.Label(form, text="Unsaved account edits are kept when you press Save at the bottom.",
                  style="Muted.TLabel").grid(row=11, column=1, sticky="w")
        self._fill_lb()

    def _fill_lb(self, select=None):
        self.lb.delete(0, "end")
        for i, n in enumerate(sorted(self.profiles)):
            self.lb.insert("end", n)
            if n == select:
                self.lb.selection_set(i)

    def _load_profile(self):
        sel = self.lb.curselection()
        if not sel:
            return
        n = self.lb.get(sel[0])
        p = self.profiles.get(n, {})
        self.a_name.set(n)
        self.a_cookies.set(p.get("cookies_file", ""))
        self.a_browser.set(p.get("cookies_browser", ""))
        self.a_user.set(p.get("username", ""))
        self.a_pass.set(p.get("password", ""))
        self.l_acct.configure(text="")

    def _new_profile(self):
        for v in (self.a_name, self.a_cookies, self.a_browser, self.a_user, self.a_pass):
            v.set("")
        self.lb.selection_clear(0, "end")
        self.l_acct.configure(text="")

    def _form_profile(self) -> dict:
        return {"cookies_file": self.a_cookies.get().strip(), "cookies_browser": self.a_browser.get().strip(),
                "username": self.a_user.get().strip(), "password": self.a_pass.get()}

    def _save_profile(self, quiet=False) -> bool:
        n = self.a_name.get().strip()
        if not n or n == "-":
            if not quiet:
                messagebox.showerror(APP_NAME, "Give the account a name.", parent=self)
            return False
        sel = self.lb.curselection()
        if sel and self.lb.get(sel[0]) != n:
            self.profiles.pop(self.lb.get(sel[0]), None)
        self.profiles[n] = self._form_profile()
        self._fill_lb(select=n)
        self.l_acct.configure(text=f"Saved {n}")
        return True

    def _del_profile(self):
        sel = self.lb.curselection()
        if sel:
            self.profiles.pop(self.lb.get(sel[0]), None)
            self._fill_lb()
            self._new_profile()

    def validate(self):
        # Pressing Save with a filled-in account form should not silently drop the account.
        n = self.a_name.get().strip()
        if n and n != "-" and self.profiles.get(n) != self._form_profile():
            self._save_profile(quiet=True)
        out = {}
        for key, (kind, v) in self.vars.items():
            try:
                if kind == "bool":
                    out[key] = bool(v.get())
                elif kind == "text":
                    out[key] = v.get("1.0", "end").strip()
                elif kind == "int":
                    out[key] = max(0, int(float(v.get() or 0)))
                elif kind == "float":
                    out[key] = max(0.0, float(v.get() or 0))
                else:
                    out[key] = v.get().strip()
            except ValueError:
                messagebox.showerror(APP_NAME, f"{SETTING_LABELS.get(key, key)} must be a number.", parent=self)
                return None
        out["concurrency"] = max(1, out["concurrency"])
        out["item_delay_max"] = max(out["item_delay_min"], out["item_delay_max"])
        extra = split_args(out["extra_ytdlp_args"])
        if extra and YTDLP is not None:
            try:
                cli_to_params(["--ignore-config"] + extra)
            except ValueError as exc:
                messagebox.showerror(APP_NAME, str(exc), parent=self)
                return None
        out["proxies"] = [p.strip() for p in self.t_proxies.get("1.0", "end").splitlines() if p.strip()]
        out["proxy_mode"] = self.v_pmode.get()
        out["proxy_allow_direct"] = bool(self.v_pdirect.get())
        out["proxy_test_url"] = self.v_ptest.get().strip()
        out["auth_profiles"] = self.profiles
        return out


class ToolsDialog(Dialog):
    TOOLS = [("ffmpeg", "Merges video and audio, embeds metadata. Needed for best quality."),
             ("deno", "JavaScript runtime yt-dlp needs for full YouTube support."),
             ("gallery-dl", "Second backend for Instagram, X, Reddit and other image-first sites.")]

    def __init__(self, parent, app):
        super().__init__(parent, "Tools", ok_text=None)
        self.app = app
        self.labels = {}
        self.paths = {}
        self.busy = 0
        ttk.Label(self.body, text="yt-dlp", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        self.l_yt = ttk.Label(self.body, text="")
        self.l_yt.grid(row=0, column=1, sticky="w", padx=10)
        yb = ttk.Frame(self.body)
        yb.grid(row=0, column=2, sticky="e")
        tip(ttk.Button(yb, text="Update", command=self._upd_ytdlp),
            "Install the newest yt-dlp from PyPI into the data folder. Try this first when a site stops working.").pack(side="left")
        tip(ttk.Button(yb, text="Use bundled", command=self._reset_ytdlp),
            "Remove the installed update and go back to the yt-dlp built into this release.").pack(side="left", padx=(6, 0))
        ttk.Label(self.body, text="Site extractors, updated separately from Hoardwatch. Restart after updating.",
                  style="Muted.TLabel").grid(row=1, column=1, columnspan=2, sticky="w", padx=10)
        for i, (name, desc) in enumerate(self.TOOLS, start=1):
            ttk.Separator(self.body).grid(row=i * 4 - 1, column=0, columnspan=3, sticky="ew", pady=(12, 0))
            ttk.Label(self.body, text=name, style="Title.TLabel").grid(row=i * 4, column=0, sticky="w", pady=(10, 0))
            lab = ttk.Label(self.body, text="checking...", style="Muted.TLabel")
            lab.grid(row=i * 4, column=1, sticky="w", padx=10, pady=(10, 0))
            self.labels[name] = lab
            ttk.Button(self.body, text="Install or update",
                       command=lambda n=name: self._install(n)).grid(row=i * 4, column=2, sticky="e", pady=(10, 0))
            ttk.Label(self.body, text=desc, style="Muted.TLabel").grid(row=i * 4 + 1, column=1, columnspan=2,
                                                                       sticky="w", padx=10)
            v = tk.StringVar(value=app.cfg[TOOL_KEYS[name]])
            self.paths[name] = v
            pf = path_entry(self.body, v)
            pf.grid(row=i * 4 + 2, column=1, columnspan=2, sticky="ew", padx=10, pady=(4, 0))
            ttk.Label(self.body, text="Custom path", style="Muted.TLabel").grid(row=i * 4 + 2, column=0, sticky="w")
        self.body.columnconfigure(1, weight=1)
        bar = ttk.Frame(self.body)
        bar.grid(row=20, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        tip(ttk.Button(bar, text="Save paths", command=self._save_paths),
            "Use the paths typed above. Leave a path blank to use the installed copy or one found on PATH.").pack(side="left")
        ttk.Button(bar, text="Open tools folder", command=lambda: open_path(TOOLS_DIR)).pack(side="left", padx=6)
        self.pb = ttk.Progressbar(bar, mode="indeterminate", length=140)
        self.t_log = make_text(self.body, height=7, width=84, mono=True, wrap="none")
        self.t_log.grid(row=21, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.refresh()

    def refresh(self):
        self.l_yt.configure(text=f"{ytdlp_version()} ({YTDLP_SOURCE})")
        cfg = self.app.cfg.snapshot()

        def work():
            res = {}
            for n, _ in self.TOOLS:
                p = tool_path(cfg, n)
                res[n] = (f"{tool_version(p, n)}  [{p}]", True) if p else ("not found", False)
            self.app.emit("tools_status", dialog=self, res=res)
        threading.Thread(target=work, daemon=True).start()

    def set_status(self, res):
        for n, (text, ok) in res.items():
            if n in self.labels and self.labels[n].winfo_exists():
                self.labels[n].configure(text=text[:110], style="TLabel" if ok else "Warn.TLabel")

    def log(self, msg):
        self.app.emit("tools_log", dialog=self, msg=msg)

    def set_busy(self, delta):
        self.busy = max(0, self.busy + delta)
        if self.busy:
            self.pb.pack(side="right")
            self.pb.start(12)
        else:
            self.pb.stop()
            self.pb.pack_forget()

    def _run(self, fn):
        self.set_busy(1)

        def work():
            try:
                fn()
            except Exception as exc:
                self.log(f"Failed: {exc}")
            self.app.emit("tools_refresh", dialog=self)
        threading.Thread(target=work, daemon=True).start()

    def _install(self, name):
        proxy = meta_proxy(self.app.cfg.snapshot())
        last = {"t": 0}

        def prog(got, total):
            if time.time() - last["t"] > 1:
                last["t"] = time.time()
                self.log(f"  {human_bytes(got)}" + (f" of {human_bytes(total)}" if total else ""))
        self._run(lambda: install_tool(name, proxy, self.log, prog))

    def _upd_ytdlp(self):
        proxy = meta_proxy(self.app.cfg.snapshot())
        self._run(lambda: update_python_packages(proxy, self.log))

    def _reset_ytdlp(self):
        reset_python_packages()
        self.log("Removed the installed update. Restart to use the bundled yt-dlp.")

    def _save_paths(self):
        for n, v in self.paths.items():
            self.app.cfg[TOOL_KEYS[n]] = v.get().strip()
        self.app.cfg.save()
        self.log("Paths saved.")
        self.refresh()


class JobRunner:
    """Runs archive checks one after another on a background thread."""

    def __init__(self, app):
        self.app = app
        self.jobs: list = []
        self.cv = threading.Condition()
        self.engine: Engine | None = None
        self.current = None
        threading.Thread(target=self._loop, daemon=True).start()

    def submit(self, path, source_ids=None, download=True) -> bool:
        key = (path, tuple(source_ids or ()), download)
        with self.cv:
            if any(j["key"] == key for j in self.jobs) or (self.current and self.current["key"] == key):
                return False
            self.jobs.append({"key": key, "path": path, "ids": source_ids, "download": download})
            self.cv.notify()
        self.app.emit("runner_state")
        return True

    def stop_all(self):
        with self.cv:
            self.jobs.clear()
        if self.engine:
            self.engine.stop()

    def busy(self) -> bool:
        return self.current is not None or bool(self.jobs)

    def queued(self) -> int:
        return len(self.jobs)

    def _loop(self):
        while True:
            with self.cv:
                while not self.jobs:
                    self.cv.wait()
                job = self.jobs.pop(0)
                self.current = job
            self.app.emit("runner_state")
            try:
                db = self.app.get_db(job["path"])
                if db is None:
                    raise RuntimeError(f"Archive folder not found: {job['path']}")
                self.engine = Engine(self.app.cfg.snapshot(), self.app.emit)
                self.engine.check_archive(db, job["ids"], job["download"])
            except Exception as exc:
                self.app.emit("log", level="error", msg=f"{exc}\n{traceback.format_exc()}")
            finally:
                self.engine = None
                self.current = None
                self.app.emit("runner_state")


TAB_SOURCES, TAB_ITEMS, TAB_GRABBER, TAB_ACTIVITY = range(4)
FINISHED_STATES = ("done", "failed", "unavailable", "skipped", "stopped")


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = Config.load()
        self.q: queue.Queue = queue.Queue()
        self.dbs: dict[str, ArchiveDB] = {}
        self.db_lock = threading.Lock()
        self.sel_path: str | None = None
        self.tasks: dict[int, str] = {}
        self.active: set = set()
        self.finished: set = set()
        self.settings_tab = 0
        self.dirty: set = set()
        self._last_items_refresh = 0.0
        self._last_clip = ""
        self._search_job = None
        self.source_ids: dict[str, int] = {}

        root.title(f"{APP_NAME} {APP_VERSION}")
        geo = re.match(r"^(\d{3,5})x(\d{3,5})", self.cfg["window_geometry"] or "")
        root.geometry(f"{geo.group(1)}x{geo.group(2)}" if geo else "1280x800")
        root.minsize(980, 620)
        PAL.clear()
        PAL.update(THEMES.get(self.cfg["theme"]) or THEMES["Hoardwatch"])
        init_fonts(root)
        apply_theme(root)
        self._set_icon()
        self._build()
        self.runner = JobRunner(self)
        self.refresh_archives()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self.pump)
        self.root.after(1000, self.tick)
        self.root.after(30000, self.scheduler_tick)
        self.root.after(1500, self.clip_tick)
        self.log("info", f"yt-dlp {ytdlp_version()} ({YTDLP_SOURCE}). Data folder: {DATA_DIR}")
        if YTDLP_LOAD_ERROR:
            self.log("warn", f"yt-dlp update could not be loaded: {YTDLP_LOAD_ERROR}")
        self._startup_tool_check()
        self._set_busy(False)
        self.root.after(300, lambda: (set_empty(self.t_tasks, "No downloads running."), self._links_changed()))

    # ---- infrastructure
    def emit(self, event, **data):
        self.q.put((event, data))

    def get_db(self, path) -> ArchiveDB | None:
        with self.db_lock:
            if path in self.dbs:
                return self.dbs[path]
            if not os.path.isdir(path):
                return None
            db = ArchiveDB(path)
            self.dbs[path] = db
            return db

    def _set_icon(self):
        try:
            png = resource_path("assets", "icon.png")
            if os.path.isfile(png):
                self._icon = tk.PhotoImage(file=png)
                self.root.iconphoto(True, self._icon)
            ico = resource_path("assets", "icon.ico")
            if IS_WIN and os.path.isfile(ico):
                self.root.iconbitmap(default=ico)
        except Exception:
            pass

    def _startup_tool_check(self):
        cfg = self.cfg.snapshot()

        def work():
            missing = [n for n in ("ffmpeg", "deno") if not tool_path(cfg, n)]
            if missing:
                self.emit("log", level="warn", msg="Not found: " + ", ".join(missing) +
                          ". Open Tools to install. Without ffmpeg, yt-dlp falls back to lower quality "
                          "single-file formats; without deno, many YouTube formats are unavailable.")
                self.emit("tools_missing", names=missing)
            if not tool_path(cfg, "gallery-dl"):
                self.emit("log", level="info", msg="gallery-dl not found. It is optional; Instagram and X "
                          "profiles usually need it.")
        threading.Thread(target=work, daemon=True).start()

    # ---- layout
    def _build(self):
        self._build_menu()
        top = ttk.Frame(self.root, padding=(10, 8), style="Bar.TFrame")
        top.pack(fill="x")
        self.b_check = tip(ttk.Button(top, text="Check archive", style="Accent.TButton",
                                      command=self.check_selected_archive),
                           "List every source in the selected archive and download whatever is missing. Ctrl+R")
        self.b_check.pack(side="left")
        tip(ttk.Button(top, text="Scan only", command=self.scan_archive),
            "List every source and update the ledger without downloading. Cheap way to record "
            "deletions often. Ctrl+Shift+S").pack(side="left", padx=(6, 0))
        tip(ttk.Button(top, text="Check all", command=self.check_all),
            "Queue a check for every archive in the list. Ctrl+Shift+R").pack(side="left", padx=(6, 0))
        self.b_stop = tip(ttk.Button(top, text="Stop", style="Stop.TButton", command=self.stop),
                          "Cancel the running check and anything queued. Partial files resume next time.")
        self.b_stop.pack(side="left", padx=(6, 0))
        self.pb = ttk.Progressbar(top, mode="indeterminate", length=110)
        self.l_state = ttk.Label(top, text="Idle", style="Bar.TLabel")
        self.l_state.pack(side="left", padx=14)
        self.b_tools = tip(ttk.Button(top, text="Tools", command=self.open_tools),
                           "Install or update ffmpeg, deno and gallery-dl, and update yt-dlp.")
        self.b_tools.pack(side="right")
        tip(ttk.Button(top, text="Settings", command=self.open_settings),
            "Downloads, tracking, rate limits, proxies and accounts. Ctrl+,").pack(side="right", padx=(0, 6))

        sb = ttk.Frame(self.root, padding=(10, 2, 10, 6))
        sb.pack(side="bottom", fill="x")
        self.status = ttk.Label(sb, text="", style="Muted.TLabel")
        self.status.pack(side="left")
        self.l_data = ttk.Label(sb, text=f"Data: {DATA_DIR}", style="Link.TLabel", cursor="hand2")
        self.l_data.pack(side="right")
        self.l_data.bind("<Button-1>", lambda e: open_path(DATA_DIR))
        tip(self.l_data, "Settings, accounts, installed tools and yt-dlp updates. Click to open.")
        self.pw = ttk.PanedWindow(self.root, orient="horizontal")
        self.pw.pack(fill="both", expand=True, padx=10, pady=(10, 4))
        sash = int(self.cfg["sash"] or 350)
        self.root.after(80, lambda: self.pw.sashpos(0, max(220, min(sash, 520))))

        left = ttk.Frame(self.pw)
        self.pw.add(left, weight=1)
        hdr = ttk.Frame(left)
        hdr.pack(fill="x", pady=(0, 6))
        ttk.Label(hdr, text="Archives", style="Title.TLabel").pack(side="left")
        f, self.t_arch = make_tree(left, [("name", "Name", 170), ("pending", "Missing", 78),
                                          ("gone", "Deleted", 78)],
                                   selectmode="browse")
        f.pack(fill="both", expand=True)
        self.t_arch.bind("<<TreeviewSelect>>", lambda e: self.on_archive_select())
        self.t_arch.bind("<Double-1>", lambda e: self.edit_archive() if self.t_arch.identify_row(e.y) else None)
        self.t_arch.bind("<Button-3>", self._arch_menu)
        self.t_arch.bind("<Delete>", lambda e: self.remove_archive())
        self.m_arch = make_menu(self.root)
        self.m_arch.add_command(label="Check archive", command=self.check_selected_archive)
        self.m_arch.add_command(label="Scan only", command=self.scan_archive)
        self.m_arch.add_separator()
        self.m_arch.add_command(label="Edit...", command=self.edit_archive)
        self.m_arch.add_command(label="Show folder", command=self.open_archive_folder)
        self.m_arch.add_command(label="Open check logs", command=self.open_logs)
        self.m_arch.add_separator()
        self.m_arch.add_command(label="Remove from list...", command=self.remove_archive)
        bar = ttk.Frame(left)
        bar.pack(fill="x", pady=(6, 0))
        for i, (text, cmd, hint) in enumerate((
                ("New", self.new_archive, "Create an archive in a new or empty folder. Ctrl+N"),
                ("Open", self.open_archive, "Add an existing archive folder to the list. Ctrl+O"),
                ("Edit", self.edit_archive, "Name, auto-check interval, default account and per-archive "
                                            "yt-dlp options. Right-click an archive for more."))):
            tip(ttk.Button(bar, text=text, command=cmd), hint).grid(row=0, column=i, sticky="ew",
                                                                     padx=(0 if i == 0 else 6, 0))
            bar.columnconfigure(i, weight=1, uniform="archbtn")

        right = ttk.Frame(self.pw)
        self.pw.add(right, weight=4)
        self.right = right
        head = ttk.Frame(right, padding=(2, 0, 2, 8))
        head.pack(fill="x")
        self.l_title = ttk.Label(head, text="", style="Big.TLabel")
        self.l_title.grid(row=0, column=0, sticky="w")
        self.l_path = ttk.Label(head, text="", style="Link.TLabel", cursor="hand2")
        self.l_path.grid(row=1, column=0, sticky="w")
        self.l_path.bind("<Button-1>", lambda e: self.open_archive_folder())
        tip(self.l_path, "Click to open the archive folder in your file manager")
        stats = ttk.Frame(head)
        stats.grid(row=0, column=1, rowspan=2, sticky="e")
        head.columnconfigure(0, weight=1)
        self.stat_labels = {}
        for key, style in (("n", "TLabel"), ("done", "Ok.TLabel"), ("pending", "Warn.TLabel"),
                           ("gone", "Gone.TLabel")):
            col = ttk.Frame(stats)
            col.pack(side="left", padx=(18, 0))
            v = ttk.Label(col, text="0", style=style, font=FONTS["title"])
            v.pack(anchor="e")
            ttk.Label(col, text={"n": "items", "done": "saved", "pending": "missing",
                                 "gone": "deleted upstream"}[key], style="Muted.TLabel").pack(anchor="e")
            self.stat_labels[key] = v
        self.l_meta = ttk.Label(head, text="", style="Muted.TLabel")
        self.l_meta.grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

        self.nb = ttk.Notebook(right)
        self.nb.pack(fill="both", expand=True)
        self._build_sources()
        self._build_items()
        self._build_grabber()
        self._build_activity()
        self.nb.bind("<<NotebookTabChanged>>",
                     lambda e: self.refresh_items(force=True) if self.nb.index("current") == TAB_ITEMS else None)
        self._build_welcome(right)

        self._bind_keys()

    def _build_menu(self):
        mb = make_menu(self.root)
        m = make_menu(mb)
        m.add_command(label="New archive...", accelerator="Ctrl+N", command=self.new_archive)
        m.add_command(label="Open archive folder...", accelerator="Ctrl+O", command=self.open_archive)
        m.add_separator()
        m.add_command(label="Export ledger to CSV...", command=self.export_csv)
        m.add_separator()
        m.add_command(label="Quit", accelerator="Ctrl+Q", command=self.on_close)
        mb.add_cascade(label="File", menu=m)
        m = make_menu(mb)
        m.add_command(label="Check archive", accelerator="Ctrl+R", command=self.check_selected_archive)
        m.add_command(label="Scan only (no downloads)", accelerator="Ctrl+Shift+S", command=self.scan_archive)
        m.add_command(label="Check all archives", accelerator="Ctrl+Shift+R", command=self.check_all)
        m.add_command(label="Stop", command=self.stop)
        m.add_separator()
        m.add_command(label="Add sources...", accelerator="Ctrl+Shift+A", command=self.add_source)
        m.add_command(label="Edit archive...", command=self.edit_archive)
        m.add_command(label="Show folder", command=self.open_archive_folder)
        m.add_command(label="Open check logs", command=self.open_logs)
        m.add_separator()
        m.add_command(label="Remove from list...", command=self.remove_archive)
        mb.add_cascade(label="Archive", menu=m)
        m = make_menu(mb)
        for i, name in enumerate(("Sources", "Items", "Link grabber", "Activity")):
            m.add_command(label=name, accelerator=f"Ctrl+{i + 1}", command=lambda i=i: self.nb.select(i))
        m.add_separator()
        tm = make_menu(m)
        self.v_theme = tk.StringVar(value=self.cfg["theme"] if self.cfg["theme"] in THEMES else "Hoardwatch")
        for name in THEMES:
            tm.add_radiobutton(label=name, value=name, variable=self.v_theme, command=self.set_theme)
        m.add_cascade(label="Theme", menu=tm)
        self.v_verbose = tk.BooleanVar(value=self.cfg["verbose_log"])
        m.add_checkbutton(label="Show yt-dlp detail in the log", variable=self.v_verbose,
                          command=lambda: self._save_flag("verbose_log", self.v_verbose.get()))
        mb.add_cascade(label="View", menu=m)
        m = make_menu(mb)
        m.add_command(label="Tools and updates...", command=self.open_tools)
        m.add_command(label="Settings...", accelerator="Ctrl+,", command=self.open_settings)
        self.v_clip = tk.BooleanVar(value=self.cfg["clipboard_watch"])
        m.add_checkbutton(label="Watch clipboard for links", variable=self.v_clip, command=self._clip_toggle)
        mb.add_cascade(label="Tools", menu=m)
        m = make_menu(mb)
        m.add_command(label="Project page", command=lambda: webbrowser.open(REPO_URL))
        m.add_command(label="Open data folder", command=lambda: open_path(DATA_DIR))
        m.add_separator()
        m.add_command(label="About", command=self.about)
        mb.add_cascade(label="Help", menu=m)
        self.root.configure(menu=mb)

    def _bind_keys(self):
        r = self.root
        for seq, fn in (("<Control-n>", self.new_archive), ("<Control-o>", self.open_archive),
                        ("<Control-r>", self.check_selected_archive), ("<Control-R>", self.check_all),
                        ("<Control-S>", self.scan_archive), ("<Control-A>", self.add_source),
                        ("<Control-q>", self.on_close), ("<Control-comma>", self.open_settings),
                        ("<F5>", lambda: self.refresh_all())):
            r.bind(seq, lambda e, fn=fn: (fn(), "break")[1])
        for i in range(4):
            r.bind(f"<Control-Key-{i + 1}>", lambda e, i=i: self.nb.select(i))
        r.bind("<Control-f>", lambda e: self.focus_search())

    def _build_welcome(self, parent):
        w = ttk.Frame(parent, style="Panel.TFrame")
        box = ttk.Frame(w, style="Panel.TFrame", padding=30)
        box.place(relx=0.5, rely=0.42, anchor="center")
        ttk.Label(box, text="No archives yet", style="PanelBig.TLabel").pack()
        self._welcome_labels = [ttk.Label(box, style="Panel.TLabel", wraplength=520, justify="center", text=(
            "An archive is a folder Hoardwatch downloads into, with a ledger that remembers every item "
            "each source has listed. Create one, add channel, profile or playlist URLs as sources, "
            "then press Check archive."))]
        self._welcome_labels[0].pack(pady=(10, 18))
        row = ttk.Frame(box, style="Panel.TFrame")
        row.pack()
        ttk.Button(row, text="New archive", style="Accent.TButton", command=self.new_archive).pack(side="left")
        ttk.Button(row, text="Open existing folder", command=self.open_archive).pack(side="left", padx=(8, 0))
        self.l_welcome_tools = ttk.Label(box, text="", style="PanelWarn.TLabel", wraplength=520,
                                         justify="center")
        self.l_welcome_tools.pack(pady=(18, 0))
        self.welcome = w

    def _show_welcome(self, on):
        if on:
            self.welcome.place(in_=self.right, relx=0, rely=0, relwidth=1, relheight=1)
            self.welcome.lift()
        else:
            self.welcome.place_forget()

    def _build_sources(self):
        f = ttk.Frame(self.nb, padding=8)
        self.nb.add(f, text="Sources")
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(0, 6))
        tip(ttk.Button(bar, text="Add sources", style="Accent.TButton", command=self.add_source),
            "Paste one or more channel, profile, playlist or post URLs. Ctrl+Shift+A").pack(side="left")
        ttk.Button(bar, text="Edit", command=self.edit_source).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Remove", command=self.remove_source).pack(side="left", padx=(6, 0))
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=12)
        tip(ttk.Button(bar, text="Check selected", command=lambda: self.check_sources(True)),
            "List the selected sources and download what is missing.").pack(side="left")
        tip(ttk.Button(bar, text="Scan selected", command=lambda: self.check_sources(False)),
            "List the selected sources and update the ledger without downloading.").pack(side="left", padx=(6, 0))
        tf, self.t_src = make_tree(f, [("label", "Label", 170), ("url", "URL", 250), ("backend", "Backend", 95),
                                       ("items", "Items", 60), ("done", "Saved", 65), ("pending", "Missing", 75),
                                       ("gone", "Deleted", 75), ("last", "Last check", 130),
                                       ("status", "Status", 200)])
        tf.pack(fill="both", expand=True)
        self.t_src.bind("<Double-1>", lambda e: self.edit_source() if self.t_src.identify_row(e.y) else None)
        self.t_src.bind("<Return>", lambda e: self.edit_source())
        self.t_src.bind("<Delete>", lambda e: self.remove_source())
        self.t_src.bind("<Button-3>", self._src_menu)
        self.t_src.bind("<Control-a>", lambda e: (self.t_src.selection_set(self.t_src.get_children()), "break")[1])
        self.m_src = make_menu(self.root)
        self.m_src.add_command(label="Check selected", command=lambda: self.check_sources(True))
        self.m_src.add_command(label="Scan selected", command=lambda: self.check_sources(False))
        self.m_src.add_separator()
        self.m_src.add_command(label="Show items", command=self.source_items)
        self.m_src.add_command(label="Open URL", command=self.source_url)
        self.m_src.add_command(label="Copy URL", command=self.source_copy)
        self.m_src.add_command(label="Show folder", command=self.source_folder)
        self.m_src.add_separator()
        self.m_src.add_command(label="Edit...", command=self.edit_source)
        self.m_src.add_command(label="Include in checks", command=lambda: self.source_enable(1))
        self.m_src.add_command(label="Exclude from checks", command=lambda: self.source_enable(0))
        self.m_src.add_separator()
        self.m_src.add_command(label="Remove...", command=self.remove_source)
        ttk.Label(f, text="Check lists each source and downloads what is missing. Scan only updates the "
                          "ledger, which is how deletions get recorded without spending bandwidth. "
                          "Right-click a source for more.",
                  style="Muted.TLabel", wraplength=860, justify="left").pack(anchor="w", pady=(6, 0))

    def _build_items(self):
        f = ttk.Frame(self.nb, padding=8)
        self.nb.add(f, text="Items")
        flt = ttk.Frame(f)
        flt.pack(fill="x", pady=(0, 6))
        self.v_fsrc = tk.StringVar(value="All sources")
        self.v_fstat = tk.StringVar(value="Any status")
        self.v_fup = tk.StringVar(value="Online or deleted")
        self.v_ftext = tk.StringVar()
        self.c_fsrc = ttk.Combobox(flt, textvariable=self.v_fsrc, state="readonly", width=26)
        self.c_fsrc.pack(side="left")
        ttk.Combobox(flt, textvariable=self.v_fstat, state="readonly", width=13,
                     values=["Any status", "pending", "done", "failed", "unavailable", "skipped", "missed"]
                     ).pack(side="left", padx=6)
        ttk.Combobox(flt, textvariable=self.v_fup, state="readonly", width=17,
                     values=["Online or deleted", "Still online", "Deleted upstream"]).pack(side="left")
        tip(ttk.Button(flt, text="Reset", command=self.reset_filters), "Clear every filter").pack(side="left", padx=6)
        ttk.Button(flt, text="Export CSV", command=self.export_csv).pack(side="right")
        flt2 = ttk.Frame(f)
        flt2.pack(fill="x", pady=(0, 6))
        ttk.Label(flt2, text="Search").pack(side="left")
        self.e_search = ttk.Entry(flt2, textvariable=self.v_ftext, width=40)
        self.e_search.pack(side="left", padx=6)
        tip(self.e_search, "Title, URL, uploader or item key. Filters as you type. Ctrl+F")
        self.e_search.bind("<Escape>", lambda ev: self.v_ftext.set(""))
        self.e_search.bind("<Return>", lambda ev: self.refresh_items(force=True))
        for v in (self.v_fsrc, self.v_fstat, self.v_fup):
            v.trace_add("write", lambda *a: self.refresh_items(force=True))
        self.v_ftext.trace_add("write", lambda *a: self._search_soon())
        self.l_items = ttk.Label(flt2, text="", style="Muted.TLabel")
        self.l_items.pack(side="right")
        tf, self.t_items = make_tree(f, [("title", "Title", 320), ("source", "Source", 140), ("status", "Status", 90),
                                         ("upstream", "Online", 80), ("date", "Posted", 90),
                                         ("seen", "First seen", 135), ("file", "File", 220)])
        tf.pack(fill="both", expand=True)
        self.t_items.bind("<Double-1>", lambda e: self.item_open() if self.t_items.identify_row(e.y) else None)
        self.t_items.bind("<Return>", lambda e: self.item_open())
        self.t_items.bind("<Control-c>", lambda e: self.item_copy())
        self.t_items.bind("<Control-a>", lambda e: (self.t_items.selection_set(self.t_items.get_children()), "break")[1])
        self.m_items = make_menu(self.root)
        self.m_items.add_command(label="Open file", command=self.item_open)
        self.m_items.add_command(label="Show in folder", command=self.item_reveal)
        self.m_items.add_command(label="Open original URL", command=self.item_url)
        self.m_items.add_command(label="Copy URL", accelerator="Ctrl+C", command=self.item_copy)
        self.m_items.add_command(label="Copy title", command=self.item_copy_title)
        self.m_items.add_separator()
        self.m_items.add_command(label="Queue again (mark pending)", command=lambda: self.item_status("pending"))
        self.m_items.add_command(label="Mark as saved", command=lambda: self.item_status("done"))
        self.m_items.add_command(label="Show details and error", command=self.item_error)
        self.t_items.bind("<Button-3>", self._items_menu)
        if IS_MAC:
            self.t_items.bind("<Button-2>", self._items_menu)
        self.item_rows: dict[str, dict] = {}

    def _build_grabber(self):
        f = ttk.Frame(self.nb, padding=8)
        self.nb.add(f, text="Link grabber")
        ttk.Label(f, text="Paste text, a list of links or web pages. Find links pulls out every link and shows "
                          "which backend can handle it. Scan pages reads each pasted page and finds embedded "
                          "or linked media.", style="Muted.TLabel", wraplength=900, justify="left").pack(anchor="w")
        self.t_paste = make_text(f, height=5)
        self.t_paste.pack(fill="x", pady=6)
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        ttk.Button(bar, text="Find links", command=self.grab_links).pack(side="left")
        ttk.Button(bar, text="Scan pages", command=self.grab_deep).pack(side="left", padx=6)
        ttk.Button(bar, text="Clear", command=self.grab_clear).pack(side="left")
        tip(ttk.Checkbutton(bar, text="Watch clipboard", variable=self.v_clip, command=self._clip_toggle),
            "Any link you copy anywhere shows up in the list below while this is on.").pack(side="left", padx=18)
        self.l_links = ttk.Label(bar, text="", style="Muted.TLabel")
        self.l_links.pack(side="right")
        tf, self.t_links = make_tree(f, [("url", "URL", 600), ("handler", "Handled by", 220)])
        tf.pack(fill="both", expand=True, pady=6)
        self.t_links.bind("<Delete>", lambda e: self.grab_remove())
        self.t_links.bind("<Control-a>", lambda e: (self.t_links.selection_set(self.t_links.get_children()), "break")[1])
        add = ttk.Frame(f)
        add.pack(fill="x")
        ttk.Label(add, text="Add selected to").pack(side="left")
        self.v_gtarget = tk.StringVar()
        self.c_gtarget = ttk.Combobox(add, textvariable=self.v_gtarget, state="readonly", width=28)
        self.c_gtarget.pack(side="left", padx=6)
        ttk.Label(add, text="Subfolder").pack(side="left", padx=(12, 0))
        self.v_gsub = tk.StringVar()
        tip(ttk.Entry(add, textvariable=self.v_gsub, width=18),
            "Blank gives each source its own folder. A name puts them all into one shared folder.").pack(side="left", padx=6)
        ttk.Button(add, text="Remove", command=self.grab_remove).pack(side="left", padx=(0, 6))
        ttk.Button(add, text="Add as sources", command=lambda: self.grab_add(False)).pack(side="left")
        ttk.Button(add, text="Add and check now", style="Accent.TButton",
                   command=lambda: self.grab_add(True)).pack(side="left", padx=6)
        ttk.Label(f, text="With nothing selected, every link in the list is added.",
                  style="Muted.TLabel").pack(anchor="w", pady=(6, 0))

    def _build_activity(self):
        f = ttk.Frame(self.nb, padding=8)
        self.nb.add(f, text="Activity")
        pw = ttk.PanedWindow(f, orient="vertical")
        pw.pack(fill="both", expand=True)
        tf, self.t_tasks = make_tree(pw, [("title", "Item", 520), ("progress", "Progress", 90),
                                          ("speed", "Speed", 100), ("eta", "ETA", 70), ("state", "State", 100)],
                                     height=7)
        pw.add(tf, weight=1)
        lf = ttk.Frame(pw)
        pw.add(lf, weight=2)
        hdr = ttk.Frame(lf)
        hdr.pack(fill="x", pady=(8, 4))
        ttk.Label(hdr, text="Log", style="Title.TLabel").pack(side="left")
        ttk.Checkbutton(hdr, text="Show yt-dlp detail", variable=self.v_verbose,
                        command=lambda: self._save_flag("verbose_log", self.v_verbose.get())).pack(side="left", padx=12)
        self.v_wrap = tk.BooleanVar(value=False)
        ttk.Checkbutton(hdr, text="Wrap lines", variable=self.v_wrap,
                        command=lambda: self.t_log.configure(wrap="word" if self.v_wrap.get() else "none")
                        ).pack(side="left")
        ttk.Button(hdr, text="Clear", command=lambda: self.t_log.delete("1.0", "end")).pack(side="right")
        ttk.Button(hdr, text="Copy all", command=self.copy_log).pack(side="right", padx=6)
        ttk.Button(hdr, text="Open log folder", command=self.open_logs).pack(side="right")
        lw = ttk.Frame(lf)
        lw.pack(fill="both", expand=True)
        self.t_log = make_text(lw, height=10, wrap="none", mono=True,
                               tags={"warn": "warn", "error": "err", "ok": "ok", "debug": "muted", "time": "muted"})
        ys = ttk.Scrollbar(lw, orient="vertical", command=self.t_log.yview)
        xs = ttk.Scrollbar(lw, orient="horizontal", command=self.t_log.xview)
        self.t_log.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.t_log.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        lw.rowconfigure(0, weight=1)
        lw.columnconfigure(0, weight=1)

    # ---- event pump
    def pump(self):
        try:
            for _ in range(500):
                ev, d = self.q.get_nowait()
                self.handle(ev, d)
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

    def handle(self, ev, d):
        if ev == "log":
            self.log(d["level"], d["msg"])
        elif ev == "task":
            self.update_task(d)
        elif ev == "db_changed":
            self.dirty.add(d["path"])
        elif ev == "status":
            self.l_state.configure(text=d["msg"])
        elif ev == "check_started":
            self.l_state.configure(text=f"Checking {d['name']}")
        elif ev == "check_finished":
            s = d["summary"]
            db = self.get_db(d["path"])
            name = db.name if db else os.path.basename(d["path"])
            self.l_state.configure(text=f"{name}: {s['downloaded']} saved, {s['new']} new, "
                                        f"{s['gone']} deleted upstream, {s['failed']} failed"
                                        + (f", {s['errors']} source error(s)" if s.get("errors") else ""))
            self.dirty.add(d["path"])
            if self.root.focus_displayof() is None:
                self.root.bell()
        elif ev == "runner_state":
            self._set_busy(self.runner.busy())
        elif ev == "links":
            self.add_link_rows(d["rows"])
            if d.get("msg"):
                self.log("info", d["msg"])
        elif ev == "proxy_test":
            w = d["widget"]
            if w.winfo_exists():
                w.insert("end", d["line"] + "\n", d.get("tag") or ())
        elif ev == "tools_status":
            dlg = d["dialog"]
            if dlg.winfo_exists():
                dlg.set_status(d["res"])
        elif ev == "tools_log":
            dlg = d["dialog"]
            if dlg.winfo_exists():
                dlg.t_log.insert("end", d["msg"] + "\n")
                dlg.t_log.see("end")
            self.log("info", d["msg"])
        elif ev == "tools_refresh":
            dlg = d["dialog"]
            if dlg.winfo_exists():
                dlg.set_busy(-1)
                dlg.refresh()
        elif ev == "tools_missing":
            self.l_welcome_tools.configure(text="Not installed yet: " + ", ".join(d["names"]) +
                                           ". Open Tools to install them before the first check.")
            self.b_tools.configure(text="Tools (" + str(len(d["names"])) + " missing)")

    def _set_busy(self, busy):
        q = self.runner.queued() if hasattr(self, "runner") else 0
        self.b_stop.state(["!disabled"] if busy else ["disabled"])
        if busy:
            if not self.pb.winfo_ismapped():
                self.pb.pack(side="left", padx=(14, 0), before=self.l_state)
                self.pb.start(12)
        else:
            self.pb.stop()
            self.pb.pack_forget()
            if not self.l_state.cget("text").startswith(tuple(n + ":" for n in self._archive_names())):
                self.l_state.configure(text="Idle")
        self.status.configure(text=f"{APP_NAME} {APP_VERSION}   yt-dlp {ytdlp_version()} ({YTDLP_SOURCE})"
                                   + (f"   {q} check(s) queued" if q else ""))

    def _archive_names(self):
        return [db.name for db in (self.get_db(p) for p in self.cfg["archives"]) if db]

    def tick(self):
        if self.dirty:
            paths, self.dirty = self.dirty, set()
            self.refresh_archives(keep=True)
            if self.sel_path in paths:
                self.refresh_sources()
                if self.nb.index("current") == TAB_ITEMS:
                    self.refresh_items()
        self.root.after(1000, self.tick)

    def refresh_all(self):
        self.refresh_archives(keep=True)
        self.refresh_sources()
        self.refresh_items(force=True)

    def log(self, level, msg):
        if level == "debug" and not self.v_verbose.get():
            return
        t = self.t_log
        at_end = t.yview()[1] >= 0.999
        t.insert("end", dt.datetime.now().strftime("%H:%M:%S "), "time")
        t.insert("end", msg + "\n", level if level in ("warn", "error", "ok", "debug") else ())
        lines = int(t.index("end-1c").split(".")[0])
        if lines > 6000:
            t.delete("1.0", f"{lines - 5000}.0")
        if at_end:
            t.see("end")

    def copy_log(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.t_log.get("1.0", "end-1c"))
        self._last_clip = self.t_log.get("1.0", "end-1c")

    def update_task(self, d):
        tid = d["id"]
        iid = self.tasks.get(tid)
        if iid is None or not self.t_tasks.exists(iid):
            iid = self.t_tasks.insert("", 0, values=(d.get("title", ""), "", "", "", d.get("state", "")))
            self.tasks[tid] = iid
        vals = list(self.t_tasks.item(iid, "values"))
        if d.get("title"):
            vals[0] = d["title"]
        if "pct" in d:
            vals[1] = f"{d['pct']:.1f}%" if d["pct"] is not None else vals[1]
        if "speed" in d:
            vals[2] = (human_bytes(d["speed"]) + "/s") if d["speed"] else ""
        if "eta" in d:
            vals[3] = human_eta(d["eta"])
        if d.get("state"):
            vals[4] = d["state"]
        tag = {"done": "done", "failed": "failed", "unavailable": "unavailable", "skipped": "skipped"}.get(vals[4], ())
        self.t_tasks.item(iid, values=vals, tags=tag)
        if vals[4] in FINISHED_STATES:
            self.active.discard(tid)
            if tid not in self.finished:
                self.finished.add(tid)
                self.root.after(15000, lambda i=iid: (self.t_tasks.exists(i) and self.t_tasks.delete(i),
                                                      set_empty(self.t_tasks, "No downloads running.")))
        else:
            self.active.add(tid)
        set_empty(self.t_tasks, "No downloads running.")
        self.nb.tab(TAB_ACTIVITY, text=f"Activity ({len(self.active)})" if self.active else "Activity")

    # ---- archives
    def refresh_archives(self, keep=False):
        sel = self.sel_path
        self.t_arch.delete(*self.t_arch.get_children())
        names = []
        for path in self.cfg["archives"]:
            db = self.get_db(path)
            if db is None:
                self.t_arch.insert("", "end", iid=path, values=(f"{os.path.basename(path)} (folder missing)", "", ""),
                                   tags=("error",))
                continue
            t = db.totals()
            tags = ("gone",) if t["gone"] else ()
            self.t_arch.insert("", "end", iid=path, tags=tags, values=(db.name, t["pending"], t["gone"]))
            names.append(db.name)
        sort_tree(self.t_arch)
        set_empty(self.t_arch, "Press New to create\nyour first archive.")
        self.c_gtarget.configure(values=names)
        self._show_welcome(not self.cfg["archives"])
        if sel and self.t_arch.exists(sel):
            self.t_arch.selection_set(sel)
        elif not keep and self.cfg["archives"]:
            first = self.cfg["archives"][0]
            if self.t_arch.exists(first):
                self.t_arch.selection_set(first)
        if not self.v_gtarget.get() and names:
            self.v_gtarget.set(names[0])
        self.refresh_header()

    def refresh_header(self):
        db = self.cur_db(warn=False)
        if not db:
            self.l_title.configure(text="No archive selected" if self.cfg["archives"] else "")
            self.l_path.configure(text="")
            self.l_meta.configure(text="")
            for v in self.stat_labels.values():
                v.configure(text="-")
            return
        t = db.totals()
        s = db.settings()
        self.l_title.configure(text=db.name)
        p = db.root
        self.l_path.configure(text=p if len(p) <= 64 else p[:22] + "..." + p[-39:])
        for k, v in self.stat_labels.items():
            v.configure(text=f"{t.get(k, 0):,}")
        mins = int(s.get("auto_check_minutes") or 0)
        bits = [f"{t['sources']} source{'s' if t['sources'] != 1 else ''}",
                f"last check {ago(db.get_meta('last_check'))}",
                f"auto-check every {mins} min" if mins else "auto-check off"]
        if s.get("default_auth"):
            bits.append(f"account {s['default_auth']}")
        self.l_meta.configure(text="   |   ".join(bits))

    def on_archive_select(self):
        sel = self.t_arch.selection()
        path = sel[0] if sel else None
        if path == self.sel_path:
            return
        self.sel_path = path
        db = self.get_db(path) if path else None
        if db:
            self.v_gtarget.set(db.name)
        self.refresh_header()
        self.refresh_sources()
        self.v_fsrc.set("All sources")
        self.refresh_items(force=True)

    def _arch_menu(self, ev):
        iid = self.t_arch.identify_row(ev.y)
        if iid:
            self.t_arch.selection_set(iid)
            self.on_archive_select()
            self.m_arch.tk_popup(ev.x_root, ev.y_root)

    def cur_db(self, warn=True) -> ArchiveDB | None:
        db = self.get_db(self.sel_path) if self.sel_path else None
        if db is None and warn:
            messagebox.showinfo(APP_NAME, "Select or create an archive first.", parent=self.root)
        return db

    def _register_archive(self, path, settings):
        db = self.get_db(path)
        s = db.settings()
        s.update({k: v for k, v in settings.items() if k != "path"})
        db.save_settings(s)
        if path not in self.cfg["archives"]:
            self.cfg["archives"] = self.cfg["archives"] + [path]
            self.cfg.save()
        self.sel_path = None
        self.refresh_archives()
        self.t_arch.selection_set(path)
        self.t_arch.see(path)

    def new_archive(self):
        r = ArchiveDialog(self.root, self).show()
        if r:
            path = os.path.abspath(r["path"])
            self._register_archive(path, r)
            self.nb.select(TAB_SOURCES)
            if not self.get_db(path).sources():
                self.add_source()

    def open_archive(self):
        path = filedialog.askdirectory(parent=self.root, title="Open an archive folder")
        if not path:
            return
        path = os.path.abspath(path)
        if path in self.cfg["archives"]:
            self.t_arch.selection_set(path)
            return
        if not ArchiveDB.is_archive(path) and not messagebox.askyesno(
                APP_NAME, "That folder is not an archive yet. Create one there?", parent=self.root):
            return
        self._register_archive(path, {})

    def edit_archive(self):
        db = self.cur_db()
        if not db:
            return
        r = ArchiveDialog(self.root, self, db).show()
        if r:
            s = db.settings()
            s.update({k: v for k, v in r.items() if k != "path"})
            db.save_settings(s)
            self.refresh_archives(keep=True)

    def remove_archive(self):
        if not self.sel_path:
            return
        db = self.get_db(self.sel_path)
        name = db.name if db else os.path.basename(self.sel_path)
        if not messagebox.askyesno(APP_NAME, f"Remove \"{name}\" from the list?\n\nFiles and the ledger stay on "
                                             "disk and can be opened again later.", parent=self.root):
            return
        self.cfg["archives"] = [p for p in self.cfg["archives"] if p != self.sel_path]
        self.cfg.save()
        with self.db_lock:
            db = self.dbs.pop(self.sel_path, None)
        if db and not self.runner.busy():
            db.close()
        self.sel_path = None
        self.refresh_archives()
        self.refresh_sources()
        self.refresh_items(force=True)

    def open_archive_folder(self):
        db = self.cur_db()
        if db:
            open_path(db.root)

    def open_logs(self):
        db = self.cur_db(warn=False)
        open_path(os.path.join(db.state, "logs") if db else DATA_DIR)

    # ---- sources
    def refresh_sources(self):
        sel = set(self.t_src.selection())
        self.t_src.delete(*self.t_src.get_children())
        db = self.cur_db(warn=False)
        self.source_ids = {}
        if not db:
            self.c_fsrc.configure(values=["All sources"])
            set_empty(self.t_src, "Select an archive on the left." if self.cfg["archives"] else "")
            return
        stats = db.source_stats()
        names = ["All sources"]
        bnames = {"auto": "auto", "ytdlp": "yt-dlp", "gallerydl": "gallery-dl"}
        for s in db.sources():
            st = stats.get(s["id"], {})
            backend = bnames.get(s["backend"], s["backend"])
            if s["backend"] == "auto" and s.get("resolved_backend"):
                backend = f"auto: {bnames.get(s['resolved_backend'])}"
            tags = ()
            status = s.get("last_status") or ""
            if not s["enabled"]:
                tags = ("off",)
                status = "excluded from checks" + (f"; {status}" if status else "")
            elif status.startswith(("error", "deleted")):
                tags = ("error",)
            elif (st.get("gone") or 0) > 0:
                tags = ("gone",)
            iid = str(s["id"])
            self.t_src.insert("", "end", iid=iid, tags=tags, values=(
                s["label"], s["url"], backend, st.get("n") or 0, st.get("done") or 0, st.get("pending") or 0,
                st.get("gone") or 0, short_ts(s.get("last_check")) or "never", status))
            label = f"{s['label']}  #{s['id']}"
            names.append(label)
            self.source_ids[label] = s["id"]
        for i in sel:
            if self.t_src.exists(i):
                self.t_src.selection_add(i)
        sort_tree(self.t_src)
        set_empty(self.t_src, "No sources in this archive yet.\nPress Add sources and paste channel, "
                              "profile, playlist or post URLs.")
        self.c_fsrc.configure(values=names)

    def selected_source_ids(self) -> list[int]:
        return [int(i) for i in self.t_src.selection()]

    def _src_menu(self, ev):
        iid = self.t_src.identify_row(ev.y)
        if iid:
            if iid not in self.t_src.selection():
                self.t_src.selection_set(iid)
            self.m_src.tk_popup(ev.x_root, ev.y_root)

    def _sel_sources(self) -> list[dict]:
        db = self.cur_db(warn=False)
        return [s for s in (db.source(i) for i in self.selected_source_ids())] if db else []

    def source_items(self):
        ids = self.selected_source_ids()
        if ids:
            label = next((k for k, v in self.source_ids.items() if v == ids[0]), "All sources")
            self.v_fsrc.set(label)
            self.nb.select(TAB_ITEMS)

    def source_url(self):
        for s in self._sel_sources()[:5]:
            webbrowser.open(s["url"])

    def source_copy(self):
        urls = "\n".join(s["url"] for s in self._sel_sources())
        if urls:
            self.root.clipboard_clear()
            self.root.clipboard_append(urls)
            self._last_clip = urls

    def source_folder(self):
        db = self.cur_db()
        for s in self._sel_sources()[:1]:
            p = os.path.join(db.root, s.get("subdir") or safe_name(s["label"]))
            open_path(p if os.path.isdir(p) else db.root)

    def source_enable(self, on):
        db = self.cur_db()
        ids = self.selected_source_ids()
        if db and ids:
            for i in ids:
                db.update_source(i, enabled=on)
            self.refresh_sources()

    def add_source(self, urls=""):
        db = self.cur_db()
        if not db:
            return
        r = SourceDialog(self.root, self, urls=urls).show()
        if not r:
            return
        added, dupes = self._add_sources(db, r["urls"], r["label"] if len(r["urls"]) == 1 else "",
                                         r["subdir"], r["backend"], r["auth"])
        self.refresh_sources()
        self.refresh_archives(keep=True)
        self.nb.select(TAB_SOURCES)
        for i in added:
            if self.t_src.exists(str(i)):
                self.t_src.selection_add(str(i))
        msg = f"Added {len(added)} source(s) to {db.name}." + (f" {dupes} were already in it." if dupes else "")
        self.log("ok", msg)
        if added and messagebox.askyesno(APP_NAME, msg + "\n\nCheck the new sources now?", parent=self.root):
            self.runner.submit(db.root, added, True)

    def _add_sources(self, db, urls, label, subdir, backend="auto", auth=""):
        added, dupes = [], 0
        for u in urls:
            try:
                added.append(db.add_source(u, label, subdir, backend, auth))
            except sqlite3.IntegrityError:
                dupes += 1
        return added, dupes

    def edit_source(self):
        db = self.cur_db()
        ids = self.selected_source_ids()
        if not db or not ids:
            return
        src = db.source(ids[0])
        r = SourceDialog(self.root, self, src).show()
        if not r:
            return
        try:
            db.update_source(src["id"], url=r["urls"][0], label=r["label"] or auto_label(r["urls"][0]),
                             subdir=r["subdir"] or safe_name(r["label"] or auto_label(r["urls"][0])),
                             backend=r["backend"], auth=r["auth"], enabled=r["enabled"],
                             resolved_backend=None if r["backend"] != src["backend"] else src.get("resolved_backend"))
        except sqlite3.IntegrityError:
            messagebox.showerror(APP_NAME, "Another source already uses that URL.", parent=self.root)
        self.refresh_sources()

    def remove_source(self):
        db = self.cur_db()
        ids = self.selected_source_ids()
        if not db or not ids:
            return
        if self.runner.busy():
            messagebox.showinfo(APP_NAME, "Stop the running check before removing sources.", parent=self.root)
            return
        what = f"\"{db.source(ids[0])['label']}\"" if len(ids) == 1 else f"{len(ids)} sources"
        if not messagebox.askyesno(APP_NAME, f"Remove {what} and their ledger entries?\n\n"
                                             "Downloaded files are not deleted.", parent=self.root):
            return
        for i in ids:
            db.remove_source(i)
        self.refresh_sources()
        self.refresh_archives(keep=True)

    # ---- running
    def check_selected_archive(self):
        db = self.cur_db()
        if db and not db.sources():
            if messagebox.askyesno(APP_NAME, "This archive has no sources yet. Add some now?", parent=self.root):
                self.add_source()
            return
        if db and not self.runner.submit(db.root):
            self.log("info", "That check is already queued or running.")

    def scan_archive(self):
        db = self.cur_db()
        if db and not self.runner.submit(db.root, None, False):
            self.log("info", "That scan is already queued or running.")

    def check_sources(self, download):
        db = self.cur_db()
        ids = self.selected_source_ids()
        if not db:
            return
        if not ids:
            messagebox.showinfo(APP_NAME, "Select one or more sources first.", parent=self.root)
            return
        self.runner.submit(db.root, ids, download)

    def check_all(self):
        n = sum(1 for p in self.cfg["archives"] if self.get_db(p) and self.runner.submit(p))
        self.log("info", f"Queued {n} archive check(s)")

    def stop(self):
        if not self.runner.busy():
            return
        self.runner.stop_all()
        self.l_state.configure(text="Stopping...")
        self.log("warn", "Stopping. Running downloads are cancelled; partial files resume next time.")

    def scheduler_tick(self):
        try:
            now = dt.datetime.now(dt.timezone.utc)
            for path in self.cfg["archives"]:
                db = self.get_db(path)
                if not db:
                    continue
                mins = int(db.settings().get("auto_check_minutes") or 0)
                if mins <= 0:
                    continue
                last = parse_iso(db.get_meta("last_check"))
                if last is None or (now - last).total_seconds() >= mins * 60:
                    if self.runner.submit(path):
                        self.log("info", f"Scheduled check queued: {db.name}")
            self.refresh_header()
        except Exception as exc:
            self.log("error", f"Scheduler: {exc}")
        self.root.after(30000, self.scheduler_tick)

    # ---- items
    def _search_soon(self):
        if self._search_job:
            self.root.after_cancel(self._search_job)
        self._search_job = self.root.after(300, lambda: self.refresh_items(force=True))

    def focus_search(self):
        self.nb.select(TAB_ITEMS)
        self.e_search.focus_set()
        self.e_search.select_range(0, "end")
        return "break"

    def reset_filters(self):
        self.v_fsrc.set("All sources")
        self.v_fstat.set("Any status")
        self.v_fup.set("Online or deleted")
        self.v_ftext.set("")

    def refresh_items(self, force=False):
        self._search_job = None
        if not force and time.time() - self._last_items_refresh < 3:
            return
        self._last_items_refresh = time.time()
        self.t_items.delete(*self.t_items.get_children())
        self.item_rows = {}
        db = self.cur_db(warn=False)
        if not db:
            self.l_items.configure(text="")
            set_empty(self.t_items, "")
            return
        sid = self.source_ids.get(self.v_fsrc.get())
        fstat = self.v_fstat.get()
        status = fstat if fstat not in ("Any status", "missed") else None
        up = {"Still online": "present", "Deleted upstream": "gone"}.get(self.v_fup.get())
        if fstat == "missed":
            if up == "present":
                rows = []
            else:
                rows = [r for r in db.query_items(sid, None, "gone", self.v_ftext.get().strip() or None, limit=5000)
                        if r["status"] != "done"]
        else:
            rows = db.query_items(sid, status, up, self.v_ftext.get().strip() or None, limit=5000)
        counts: dict = {}
        for r in rows:
            d = r.get("upload_date") or ""
            if len(d) == 8 and d.isdigit():
                d = f"{d[:4]}-{d[4:6]}-{d[6:]}"
            tags = ("gone",) if r["upstream"] == "gone" else (r["status"],) if r["status"] != "pending" else ()
            shown = "missed" if r["upstream"] == "gone" and r["status"] != "done" else r["status"]
            counts[shown] = counts.get(shown, 0) + 1
            iid = self.t_items.insert("", "end", tags=tags, values=(
                r.get("title") or r.get("url"), r["source_label"], shown,
                "deleted" if r["upstream"] == "gone" else "yes", d, short_ts(r["first_seen"]),
                os.path.basename(r.get("filepath") or "")))
            self.item_rows[iid] = r
        sort_tree(self.t_items)
        parts = [f"{counts[k]} {STATUS_WORDS[k]}" for k in ("done", "pending", "failed", "missed",
                                                           "unavailable", "skipped") if counts.get(k)]
        txt = f"{len(rows):,} shown" + (": " + ", ".join(parts) if parts and len(counts) > 1 else "")
        if len(rows) >= 5000:
            txt += "  (limit reached, filter to narrow)"
        self.l_items.configure(text=txt)
        filtered = (sid or fstat != "Any status" or up or self.v_ftext.get().strip())
        set_empty(self.t_items, "No items match these filters." if filtered else
                  "Nothing recorded yet.\nPress Check archive or Scan only to list this archive's sources.")

    def _items_menu(self, ev):
        iid = self.t_items.identify_row(ev.y)
        if iid:
            if iid not in self.t_items.selection():
                self.t_items.selection_set(iid)
            self.m_items.tk_popup(ev.x_root, ev.y_root)

    def _sel_items(self) -> list[dict]:
        return [self.item_rows[i] for i in self.t_items.selection() if i in self.item_rows]

    def item_open(self):
        for r in self._sel_items()[:5]:
            fp = r.get("filepath")
            if fp and os.path.exists(fp):
                open_path(fp)
            elif r.get("url"):
                webbrowser.open(r["url"])

    def item_reveal(self):
        for r in self._sel_items()[:1]:
            if r.get("filepath"):
                reveal_path(r["filepath"])

    def item_url(self):
        for r in self._sel_items()[:5]:
            if r.get("url"):
                webbrowser.open(r["url"])

    def _clip(self, text):
        if text:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self._last_clip = text

    def item_copy(self):
        self._clip("\n".join(r["url"] for r in self._sel_items() if r.get("url")))

    def item_copy_title(self):
        self._clip("\n".join(r.get("title") or r.get("url") or "" for r in self._sel_items()))

    def item_status(self, status):
        db = self.cur_db()
        rows = self._sel_items()
        if db and rows:
            db.set_status([r["id"] for r in rows], status)
            self.refresh_items(force=True)
            self.refresh_sources()
            self.refresh_archives(keep=True)

    def item_error(self):
        rows = self._sel_items()
        if rows:
            r = rows[0]
            gone = f"\nDeleted upstream since: {short_ts(r.get('gone_since'))}" if r.get("gone_since") else ""
            messagebox.showinfo(APP_NAME, f"{r.get('title') or r.get('url')}\n\nKey: {r['item_key']}\n"
                                          f"URL: {r.get('url') or ''}\nFile: {r.get('filepath') or 'none'}\n"
                                          f"First seen: {short_ts(r.get('first_seen'))}{gone}\n"
                                          f"Attempts: {r.get('attempts')}\n\n{r.get('error') or 'No error recorded.'}",
                                parent=self.root)

    def export_csv(self):
        db = self.cur_db()
        if not db:
            return
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".csv",
                                            initialfile=safe_name(db.name) + "-ledger.csv",
                                            filetypes=[("CSV", "*.csv")])
        if path:
            n = db.export_csv(path)
            self.log("ok", f"Exported {n} ledger rows to {path}")
            self.l_state.configure(text=f"Exported {n} rows to {os.path.basename(path)}")

    # ---- link grabber
    def _links_changed(self):
        n = len(self.t_links.get_children())
        set_empty(self.t_links, "Links you find, scan or copy (with Watch clipboard on) show up here.")
        self.l_links.configure(text=f"{n} link(s)" if n else "")

    def grab_clear(self):
        self.t_paste.delete("1.0", "end")
        self.t_links.delete(*self.t_links.get_children())
        self._links_changed()

    def grab_remove(self):
        for i in self.t_links.selection():
            self.t_links.delete(i)
        self._links_changed()

    def grab_links(self):
        urls = extract_urls(self.t_paste.get("1.0", "end"))
        if not urls:
            self.log("info", "No links found in the pasted text.")
            self.l_links.configure(text="No links found in the pasted text")
            return
        self._classify_async(urls)

    def _classify_async(self, urls, msg=None):
        def work():
            rows = [(u, classify_url(u) or "yt-dlp generic (unverified)") for u in urls]
            self.emit("links", rows=rows, msg=msg)
        threading.Thread(target=work, daemon=True).start()

    def grab_deep(self):
        urls = extract_urls(self.t_paste.get("1.0", "end"))
        if not urls:
            self.log("info", "Paste one or more page URLs first.")
            self.l_links.configure(text="Paste one or more page URLs first")
            return
        proxy = meta_proxy(self.cfg.snapshot())

        def work():
            for u in urls:
                try:
                    rows = scan_page(u, proxy)
                    self.emit("links", rows=rows, msg=f"Scanned {u}: {len(rows)} usable link(s)")
                except Exception as exc:
                    self.emit("log", level="error", msg=f"Scan failed for {u}: {clean_msg(exc)[:200]}")
        self.log("info", f"Scanning {len(urls)} page(s)")
        self.l_links.configure(text=f"Scanning {len(urls)} page(s)...")
        threading.Thread(target=work, daemon=True).start()

    def add_link_rows(self, rows):
        have = {self.t_links.item(i, "values")[0] for i in self.t_links.get_children()}
        for u, h in rows:
            if u not in have:
                self.t_links.insert("", "end", values=(u, h))
                have.add(u)
        sort_tree(self.t_links)
        self._links_changed()

    def grab_add(self, check_now):
        name = self.v_gtarget.get()
        path = next((p for p in self.cfg["archives"] if self.get_db(p) and self.get_db(p).name == name), None)
        if not path:
            messagebox.showinfo(APP_NAME, "Pick a target archive.", parent=self.root)
            return
        sel = self.t_links.selection() or self.t_links.get_children()
        urls = [self.t_links.item(i, "values")[0] for i in sel]
        if not urls:
            return
        db = self.get_db(path)
        added, dupes = self._add_sources(db, urls, "", self.v_gsub.get().strip())
        self.log("ok", f"Added {len(added)} source(s) to {db.name}" + (f", {dupes} already present" if dupes else ""))
        for i in sel:
            self.t_links.delete(i)
        self._links_changed()
        self.l_links.configure(text=f"Added {len(added)} to {db.name}" + (f", {dupes} already present" if dupes else ""))
        if check_now and added:
            self.runner.submit(path, added, True)
        self.dirty.add(path)

    def _clip_toggle(self):
        self._save_flag("clipboard_watch", self.v_clip.get())
        if self.v_clip.get():
            try:
                self._last_clip = self.root.clipboard_get()
            except tk.TclError:
                self._last_clip = ""

    def clip_tick(self):
        if self.v_clip.get():
            try:
                txt = self.root.clipboard_get()
            except tk.TclError:
                txt = ""
            if txt and txt != self._last_clip:
                self._last_clip = txt
                urls = extract_urls(txt)
                if urls:
                    self._classify_async(urls, msg=f"Clipboard: {len(urls)} link(s) added to the grabber")
        self.root.after(1500, self.clip_tick)

    # ---- misc
    def _save_flag(self, key, value):
        self.cfg[key] = bool(value)
        self.cfg.save()

    def set_theme(self):
        name = self.v_theme.get()
        retheme(self.root, name)
        self.cfg["theme"] = name
        self.cfg.save()

    def open_tools(self):
        ToolsDialog(self.root, self).show()
        cfg = self.cfg.snapshot()
        missing = [n for n in ("ffmpeg", "deno") if not tool_path(cfg, n)]
        self.b_tools.configure(text=f"Tools ({len(missing)} missing)" if missing else "Tools")
        self.l_welcome_tools.configure(text="Not installed yet: " + ", ".join(missing) +
                                       ". Open Tools to install them before the first check." if missing else "")

    def open_settings(self):
        r = SettingsDialog(self.root, self).show()
        if r:
            for k, v in r.items():
                self.cfg[k] = v
            self.cfg.save()
            self.log("info", "Settings saved. They apply to the next check.")

    def about(self):
        messagebox.showinfo(APP_NAME, f"{APP_NAME} {APP_VERSION}\n\nA bulk media archiver that remembers what "
                                      f"it has seen.\n\nyt-dlp {ytdlp_version()} ({YTDLP_SOURCE})\n"
                                      f"Data folder: {DATA_DIR}\n\n{REPO_URL}\nGPL-3.0-or-later", parent=self.root)

    def on_close(self):
        if self.runner.busy() and not messagebox.askyesno(
                APP_NAME, "A check is running. Stop it and quit?", parent=self.root):
            return
        try:
            self.cfg["window_geometry"] = self.root.geometry()
            pos = int(self.pw.sashpos(0))
            if pos >= 150:
                self.cfg["sash"] = pos
            self.cfg.save()
        except Exception:
            pass
        self.runner.stop_all()
        deadline = time.time() + 5
        while self.runner.current and time.time() < deadline:
            self.root.update()
            time.sleep(0.1)
        self.root.destroy()


# =============================================================== entry points

def run_headless(args) -> int:
    cfg = Config.load()
    if args.list:
        for p in cfg["archives"]:
            ok = ArchiveDB.is_archive(p)
            name = ArchiveDB(p).name if ok else "(missing)"
            print(f"{name}\t{p}")
        return 0
    targets = []
    want = args.check
    for p in cfg["archives"]:
        if not ArchiveDB.is_archive(p):
            continue
        if want == "all" or os.path.abspath(want) == os.path.abspath(p) or ArchiveDB(p).name == want:
            targets.append(p)
    if not targets and os.path.isdir(want):
        targets = [os.path.abspath(want)]
    if not targets:
        print(f"No archive matches {want!r}. Use --list to see names.", file=sys.stderr)
        return 2

    def emit(ev, **d):
        if ev == "log" and (d["level"] != "debug" or args.verbose):
            print(f"{dt.datetime.now().strftime('%H:%M:%S')} {d['level'].upper():5} {d['msg']}", flush=True)

    print(f"{APP_NAME} {APP_VERSION}, yt-dlp {ytdlp_version()} ({YTDLP_SOURCE})", flush=True)
    rc = 0
    for p in targets:
        eng = Engine(cfg.snapshot(), emit)
        try:
            s = eng.check_archive(ArchiveDB(p), None, not args.scan_only)
        except KeyboardInterrupt:
            eng.stop()
            return 130
        if s["errors"] or s["failed"]:
            rc = 1
    return rc


def _attach_windows_console():
    if IS_WIN and FROZEN:
        try:
            import ctypes
            if ctypes.windll.kernel32.AttachConsole(-1):
                sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
                sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace")
        except Exception:
            pass


def main():
    if len(sys.argv) > 1:
        _attach_windows_console()
    ap = argparse.ArgumentParser(prog="hoardwatch", description=f"{APP_NAME}: self-tracking media archiver")
    ap.add_argument("--check", metavar="ARCHIVE", help='run a check without the GUI; ARCHIVE is a name, a folder, or "all"')
    ap.add_argument("--scan-only", action="store_true", help="update the ledger without downloading")
    ap.add_argument("--list", action="store_true", help="list known archives")
    ap.add_argument("--verbose", action="store_true", help="include yt-dlp detail in headless output")
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")
    args, _unknown = ap.parse_known_args()

    if not load_ytdlp():
        msg = f"yt-dlp could not be loaded: {YTDLP_LOAD_ERROR}\nInstall it with: pip install -U \"yt-dlp[default]\""
        if (args.check or args.list) or tk is None:
            print(msg, file=sys.stderr)
            sys.exit(3)
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror(APP_NAME, msg)
        sys.exit(3)

    if args.check or args.list:
        sys.exit(run_headless(args))

    if tk is None:
        print("Tkinter is not available. Install your distribution's python3-tk package, "
              "or use --check for headless runs.", file=sys.stderr)
        sys.exit(3)
    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("NullAngst.Hoardwatch")
        except Exception:
            pass
    root = tk.Tk(className=APP_NAME)
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
