"""General-purpose helpers shared across the application.

This module has no dependency on yt-dlp or rich beyond simple types, so it
can be imported anywhere without risk of circular imports. It covers:

* filename sanitisation
* human readable formatting of bytes / speed / durations
* persistence for ``config.json``, ``history.json`` and the plain-text
  progress log
* small environment checks (e.g. is ffmpeg installed)
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

APP_DIR: Path = Path(__file__).resolve().parent
CONFIG_PATH: Path = APP_DIR / "config.json"
HISTORY_PATH: Path = APP_DIR / "history.json"
LOG_PATH: Path = APP_DIR / "download_log.txt"
ARCHIVE_PATH: Path = APP_DIR / "download_archive.txt"

MAX_HISTORY_ENTRIES = 200

# Default application settings. Persisted to ``config.json`` on first run and
# merged with whatever the user already has saved so new keys introduced in
# later versions of the app are picked up automatically.
DEFAULT_CONFIG: Dict[str, Any] = {
    "download_folder": "Downloads",
    "filename_template": "%(title)s.%(ext)s",
    "embed_thumbnail": True,
    "embed_metadata": True,
    "download_subtitles": False,
    "subtitle_languages": ["en"],
    "use_download_archive": True,
    "cookies_file": "",
    "max_parallel_downloads": 3,
    "auto_update_ytdlp": False,
}

# Characters that are invalid in filenames on Windows (also safe to strip on
# Linux/macOS, where the rules are more permissive).
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_TRAILING_DOTS_SPACES = re.compile(r"[ .]+$")


# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------

def load_config() -> Dict[str, Any]:
    """Load ``config.json``, creating it with defaults if missing."""
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as fh:
                saved = json.load(fh)
            if isinstance(saved, dict):
                config.update(saved)
        except (json.JSONDecodeError, OSError):
            # Corrupt or unreadable config: fall back to defaults rather
            # than crashing the whole application.
            pass
    else:
        save_config(config)
    return config


def save_config(config: Dict[str, Any]) -> None:
    """Persist ``config`` to ``config.json``."""
    try:
        with CONFIG_PATH.open("w", encoding="utf-8") as fh:
            json.dump(config, fh, indent=2, ensure_ascii=False)
    except OSError:
        # Non-fatal: the app can keep running with in-memory settings only.
        pass


# ---------------------------------------------------------------------------
# History persistence
# ---------------------------------------------------------------------------

def load_history() -> List[Dict[str, Any]]:
    """Return the list of past download/search entries, newest first."""
    if not HISTORY_PATH.exists():
        return []
    try:
        with HISTORY_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return []


def add_history_entry(entry: Dict[str, Any]) -> None:
    """Prepend ``entry`` to the history file, trimming old entries."""
    entry = dict(entry)
    entry.setdefault("timestamp", datetime.now().isoformat(timespec="seconds"))
    history = load_history()
    history.insert(0, entry)
    history = history[:MAX_HISTORY_ENTRIES]
    try:
        with HISTORY_PATH.open("w", encoding="utf-8") as fh:
            json.dump(history, fh, indent=2, ensure_ascii=False)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Progress log
# ---------------------------------------------------------------------------

def append_log(message: str) -> None:
    """Append a timestamped line to the plain-text progress log file."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"[{timestamp}] {message}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Filename handling
# ---------------------------------------------------------------------------

def sanitize_filename(name: str, max_length: int = 150) -> str:
    """Strip characters that are illegal in filenames on any platform.

    Also trims trailing dots/spaces (invalid on Windows) and caps the
    length so the sanitised name plus extension stays under common
    filesystem limits.
    """
    if not name:
        return "untitled"
    cleaned = _INVALID_FILENAME_CHARS.sub("_", name)
    cleaned = _TRAILING_DOTS_SPACES.sub("", cleaned)
    cleaned = cleaned.strip()
    if not cleaned:
        return "untitled"
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip()
    return cleaned


# ---------------------------------------------------------------------------
# Human-readable formatting
# ---------------------------------------------------------------------------

def format_bytes(num_bytes: Optional[float]) -> str:
    """Format a byte count as e.g. ``12.3 MB``. Returns ``Unknown`` if None."""
    if num_bytes is None or num_bytes < 0:
        return "Unknown"
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return "Unknown"


def format_speed(bytes_per_sec: Optional[float]) -> str:
    """Format a transfer rate as e.g. ``1.2 MB/s``."""
    if bytes_per_sec is None or bytes_per_sec <= 0:
        return "-- B/s"
    return f"{format_bytes(bytes_per_sec)}/s"


def format_eta(seconds: Optional[float]) -> str:
    """Format a countdown in seconds as ``MM:SS`` or ``HH:MM:SS``."""
    if seconds is None or seconds < 0:
        return "--:--"
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_elapsed(seconds: float) -> str:
    """Format an elapsed duration for the final summary."""
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


# ---------------------------------------------------------------------------
# Environment checks
# ---------------------------------------------------------------------------

def check_ffmpeg_installed() -> bool:
    """Return True if an ``ffmpeg`` executable is reachable on PATH."""
    return shutil.which("ffmpeg") is not None


def check_internet_connection(timeout: float = 2.5) -> bool:
    """Return True if the internet appears reachable.

    Tries a plain TCP handshake against a couple of well-known, highly
    available hosts (DNS resolvers) rather than any single site, so a
    single outage doesn't produce a false negative. No HTTP request is
    made - this is only meant to catch "no network" fast, before handing
    a URL to yt-dlp and waiting on a long timeout.
    """
    import socket

    probes = (("1.1.1.1", 443), ("8.8.8.8", 443))
    for host, port in probes:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def parse_index_ranges(text: str, maximum: int) -> List[int]:
    """Parse a human-friendly selection string like ``1,3,5-7`` into indices.

    Indices are 1-based on input (matching what is shown to the user) and
    are returned as a sorted, de-duplicated list of 1-based ints clamped to
    ``[1, maximum]``. Raises ``ValueError`` on malformed input.
    """
    text = text.strip().lower()
    if text in ("all", "*", ""):
        return list(range(1, maximum + 1))

    indices: set = set()
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            start_str, _, end_str = chunk.partition("-")
            start, end = int(start_str), int(end_str)
            if start > end:
                start, end = end, start
            for i in range(start, end + 1):
                if 1 <= i <= maximum:
                    indices.add(i)
        else:
            i = int(chunk)
            if 1 <= i <= maximum:
                indices.add(i)
    if not indices:
        raise ValueError(f"No valid indices found in '{text}'")
    return sorted(indices)


def get_python_executable() -> str:
    """Return the path to the current Python interpreter."""
    return sys.executable


def auto_update_ytdlp() -> Optional[str]:
    """Attempt to upgrade yt-dlp via pip.

    Returns an error message on failure, or ``None`` on success/skip. Kept
    best-effort: a failure here should never stop the app from starting.
    """
    import subprocess

    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if completed.returncode != 0:
            return completed.stderr.strip()[:300] or "pip returned a non-zero exit code."
        return None
    except Exception as exc:  # noqa: BLE001
        return str(exc)[:300]


# ---------------------------------------------------------------------------
# GitHub issue reporting
# ---------------------------------------------------------------------------

GITHUB_ISSUES_URL = "https://github.com/Laitei40/VideoAdaona/issues"

# GitHub (and browsers/proxies in front of it) can reject very long URLs, so
# the prefilled body is capped well under common limits.
_MAX_ISSUE_BODY_LENGTH = 1800


def build_github_issue_url(title: str, body: str) -> str:
    """Build a prefilled "new issue" URL for the project's GitHub repo."""
    from urllib.parse import urlencode

    if len(body) > _MAX_ISSUE_BODY_LENGTH:
        body = body[:_MAX_ISSUE_BODY_LENGTH].rstrip() + "\n\n... (truncated)"
    query = urlencode({"title": title[:120], "body": body})
    return f"{GITHUB_ISSUES_URL}/new?{query}"


def open_github_issue(title: str, body: str) -> tuple:
    """Try to open the user's browser to a prefilled GitHub issue.

    Returns ``(opened, url)`` - ``opened`` is a best-effort guess (some
    platforms/browsers report success even when nothing visibly happened),
    so callers should always show ``url`` as a fallback the user can copy.
    """
    import webbrowser

    url = build_github_issue_url(title, body)
    try:
        opened = webbrowser.open(url)
    except Exception:  # noqa: BLE001 - never let issue reporting itself crash
        opened = False
    return opened, url
