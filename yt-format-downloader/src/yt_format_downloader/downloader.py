"""Thin, friendly wrapper around yt-dlp.

Everything that actually talks to yt-dlp lives here so the rest of the
application never has to deal with its exceptions or option dictionaries
directly. All failure modes are translated into a single
:class:`DownloadError` carrying a human-readable message.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yt_dlp

from .formatter import FormatInfo
from .utils import ARCHIVE_PATH, append_log, format_bytes, sanitize_filename

ProgressHook = Callable[[Dict[str, Any]], None]


class DownloadError(Exception):
    """Raised for any yt-dlp failure, with a message safe to show a user."""


class _SilentLogger:
    """Swallows yt-dlp's own console output.

    yt-dlp writes warnings/errors straight to stderr even when ``quiet`` is
    set, which clutters the Rich UI. Real failures still reach us via
    exceptions (or a ``None`` result under ``ignoreerrors``), so it is safe
    to just log these messages to the file-based progress log instead.
    """

    def debug(self, msg: str) -> None:  # noqa: D102
        pass

    def info(self, msg: str) -> None:  # noqa: D102
        pass

    def warning(self, msg: str) -> None:  # noqa: D102
        append_log(f"yt-dlp warning: {msg}")

    def error(self, msg: str) -> None:  # noqa: D102
        append_log(f"yt-dlp error: {msg}")


@dataclass
class DownloadResult:
    """Outcome of a single download, used to render the final summary."""

    success: bool
    title: str = "Unknown"
    resolution: str = "Unknown"
    filesize_display: str = "Unknown"
    filepath: Optional[Path] = None
    elapsed_seconds: float = 0.0
    error_message: Optional[str] = None
    url: str = ""


def get_ytdlp_version() -> str:
    """Return the installed yt-dlp version string (e.g. ``2024.12.13``)."""
    return getattr(yt_dlp.version, "__version__", "unknown")


def check_ytdlp_latest_version(timeout: float = 3.0) -> Optional[str]:
    """Best-effort lookup of the latest yt-dlp release on PyPI.

    Returns ``None`` on any failure (offline, PyPI unreachable, etc.) so
    callers can treat this purely as an optional, non-blocking hint.
    """
    import json
    import urllib.request

    try:
        with urllib.request.urlopen("https://pypi.org/pypi/yt-dlp/json", timeout=timeout) as resp:
            data = json.load(resp)
        return data.get("info", {}).get("version")
    except Exception:  # noqa: BLE001 - purely informational, never fatal
        return None


def _normalize_version(version: str) -> tuple:
    """Turn a version string into a tuple of ints for numeric comparison.

    PyPI normalizes version identifiers (e.g. strips leading zeros), so
    yt-dlp's own ``2026.07.04`` shows up there as ``2026.7.4``. Comparing
    the raw strings would treat those as different versions even though
    they're identical; comparing numeric tuples does not.
    """
    parts = []
    for segment in version.split("."):
        digits = "".join(ch for ch in segment if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def is_update_available(installed: str, latest: str) -> bool:
    """Return True if ``latest`` is a strictly newer version than ``installed``."""
    installed_parts = _normalize_version(installed)
    latest_parts = _normalize_version(latest)
    length = max(len(installed_parts), len(latest_parts))
    installed_parts += (0,) * (length - len(installed_parts))
    latest_parts += (0,) * (length - len(latest_parts))
    return latest_parts > installed_parts


def friendly_error_message(exc: Exception) -> str:
    """Translate a yt-dlp/network exception into a short, friendly message."""
    text = str(exc).lower()

    if "private video" in text:
        return "This video is private and cannot be downloaded."
    if "sign in to confirm your age" in text or "age-restricted" in text or "age restricted" in text:
        return "This video is age-restricted. Try providing a cookies file in Settings."
    if "unavailable" in text and "video" in text:
        return "This video is unavailable (it may have been removed)."
    if "not available in your country" in text or "geo" in text and "restrict" in text:
        return "This video is blocked in your region (geo-restricted)."
    if "unsupported url" in text or "no video formats found" in text:
        return "This URL is not supported or contains no downloadable media."
    if "ffmpeg" in text and ("not found" in text or "not installed" in text):
        return "FFmpeg is required for this action but was not found on your PATH."
    if "http error 429" in text:
        return "Too many requests (rate limited). Please wait and try again."
    if any(term in text for term in ("timed out", "temporary failure", "connection", "network", "urlopen")):
        return "A network error occurred. Check your internet connection and try again."
    if "unable to download webpage" in text:
        return "Could not reach the site. Check the URL and your connection."
    # Fall back to yt-dlp's own message, trimmed to a reasonable length.
    message = str(exc).strip()
    return message[:300] if message else "An unknown error occurred."


class Downloader:
    """Wraps yt-dlp for metadata extraction and downloading."""

    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config

    # ------------------------------------------------------------------
    # Metadata / format discovery
    # ------------------------------------------------------------------

    def extract_info(self, url: str, flat_playlist: bool = False) -> Dict[str, Any]:
        """Fetch metadata for ``url`` without downloading anything.

        Raises :class:`DownloadError` with a friendly message on failure.
        """
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": False,
            "extract_flat": "in_playlist" if flat_playlist else False,
            "skip_download": True,
            "logger": _SilentLogger(),
        }
        cookies_file = self.config.get("cookies_file")
        if cookies_file:
            opts["cookiefile"] = cookies_file

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as exc:
            raise DownloadError(friendly_error_message(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - never let yt-dlp crash the app
            raise DownloadError(friendly_error_message(exc)) from exc

        if info is None:
            raise DownloadError("No information could be retrieved for this URL.")
        return info

    # ------------------------------------------------------------------
    # Format helpers
    # ------------------------------------------------------------------

    @staticmethod
    def estimate_size(*formats: FormatInfo) -> Optional[int]:
        """Sum known filesizes across one or more formats (e.g. video+audio)."""
        total = 0
        known = False
        for f in formats:
            if f.filesize:
                total += f.filesize
                known = True
        return total if known else None

    # ------------------------------------------------------------------
    # Option building
    # ------------------------------------------------------------------

    def _build_ydl_opts(
        self,
        output_dir: Path,
        format_spec: str,
        progress_hook: Optional[ProgressHook],
        playlist_items: Optional[str] = None,
    ) -> Dict[str, Any]:
        template = self.config.get("filename_template") or "%(title)s.%(ext)s"
        outtmpl = str(output_dir / template)

        postprocessors: List[Dict[str, Any]] = []
        if self.config.get("embed_metadata", True):
            postprocessors.append({"key": "FFmpegMetadata", "add_metadata": True})
        if self.config.get("embed_thumbnail", True):
            postprocessors.append({"key": "EmbedThumbnail"})

        opts: Dict[str, Any] = {
            "format": format_spec,
            "outtmpl": outtmpl,
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "retries": 10,
            "fragment_retries": 10,
            "continuedl": True,
            "postprocessors": postprocessors,
            "writethumbnail": self.config.get("embed_thumbnail", True),
            "restrictfilenames": False,
            "logger": _SilentLogger(),
        }

        if progress_hook is not None:
            opts["progress_hooks"] = [progress_hook]

        if self.config.get("use_download_archive", True):
            opts["download_archive"] = str(ARCHIVE_PATH)

        if self.config.get("download_subtitles"):
            opts["writesubtitles"] = True
            opts["subtitleslangs"] = self.config.get("subtitle_languages", ["en"])
            opts["subtitlesformat"] = "best"

        cookies_file = self.config.get("cookies_file")
        if cookies_file:
            opts["cookiefile"] = cookies_file

        if playlist_items:
            opts["playlist_items"] = playlist_items
        else:
            opts["noplaylist"] = True

        return opts

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def download(
        self,
        url: str,
        format_spec: str,
        output_dir: Path,
        progress_hook: Optional[ProgressHook] = None,
        playlist_items: Optional[str] = None,
    ) -> DownloadResult:
        """Download ``url`` using ``format_spec``, returning a result summary."""
        output_dir.mkdir(parents=True, exist_ok=True)

        # Postprocessors (thumbnail/metadata embedding) run *after* the media
        # file itself is fully downloaded. If one of them fails (e.g. a
        # container yt-dlp can't embed a thumbnail into), the media is still
        # safely on disk and the download should not be reported as a
        # failure. We track the last "finished" media file here so that
        # fallback path can find it.
        captured: Dict[str, Any] = {}

        def internal_hook(d: Dict[str, Any]) -> None:
            if d.get("status") == "finished":
                captured["filename"] = d.get("filename")
                captured["info_dict"] = d.get("info_dict") or {}
            if progress_hook is not None:
                progress_hook(d)

        opts = self._build_ydl_opts(output_dir, format_spec, internal_hook, playlist_items)

        start = time.monotonic()
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except (yt_dlp.utils.DownloadError, Exception) as exc:  # noqa: BLE001
            elapsed = time.monotonic() - start
            fallback = self._fallback_result_from_capture(exc, url, captured, elapsed)
            if fallback is not None:
                return fallback
            message = friendly_error_message(exc)
            append_log(f"FAILED {url}: {message}")
            return DownloadResult(success=False, error_message=message, url=url)

        elapsed = time.monotonic() - start

        if info is None:
            # yt-dlp returns None here (rather than raising) when the item
            # was skipped because it's already recorded in the download
            # archive - not a failure, just nothing new to report.
            append_log(f"SKIPPED {url}: already in the download archive")
            return DownloadResult(
                success=True,
                title="(already downloaded previously)",
                resolution="unknown",
                filesize_display="Unknown",
                filepath=None,
                elapsed_seconds=elapsed,
                error_message=(
                    "Skipped: this was already downloaded before and found in "
                    "the download archive. Turn off 'use_download_archive' in "
                    "Settings to force a re-download."
                ),
                url=url,
            )

        # Playlists return a dict with 'entries'; use the first entry actually
        # downloaded for the headline summary, but report success overall.
        target = info
        if info.get("entries"):
            entries = [e for e in info["entries"] if e]
            target = entries[0] if entries else info

        title = sanitize_filename(target.get("title", "Unknown"))
        height = target.get("height")
        resolution = f"{height}p" if height else target.get("resolution", "audio/unknown")
        filepath = self._resolve_output_path(target, output_dir)
        filesize = target.get("filesize") or target.get("filesize_approx")
        filesize_display = self._display_filesize(filesize, filepath)

        append_log(f"SUCCESS {url} -> {filepath}")

        return DownloadResult(
            success=True,
            title=title,
            resolution=str(resolution),
            filesize_display=filesize_display,
            filepath=filepath,
            elapsed_seconds=elapsed,
            url=url,
        )

    @staticmethod
    def _fallback_result_from_capture(
        exc: Exception, url: str, captured: Dict[str, Any], elapsed: float
    ) -> Optional[DownloadResult]:
        """Build a degraded-but-successful result if postprocessing failed
        after the media file itself was already saved to disk."""
        if "postprocess" not in str(exc).lower():
            return None
        filename = captured.get("filename")
        if not filename or not Path(filename).exists():
            return None

        filepath = Path(filename)
        info_dict = captured.get("info_dict") or {}
        title = sanitize_filename(str(info_dict.get("title", filepath.stem)))
        height = info_dict.get("height")
        resolution = f"{height}p" if height else info_dict.get("resolution", "audio/unknown")
        message = friendly_error_message(exc)

        append_log(f"PARTIAL SUCCESS {url}: media saved, post-processing failed: {message}")
        return DownloadResult(
            success=True,
            title=title,
            resolution=str(resolution),
            filesize_display=format_bytes(filepath.stat().st_size),
            filepath=filepath,
            elapsed_seconds=elapsed,
            error_message=f"Saved, but could not embed thumbnail/metadata ({message})",
            url=url,
        )

    @staticmethod
    def _resolve_output_path(info: Dict[str, Any], output_dir: Path) -> Optional[Path]:
        requested = info.get("requested_downloads")
        if requested:
            filepath = requested[0].get("filepath")
            if filepath:
                return Path(filepath)
        filename = info.get("_filename") or info.get("filename")
        if filename:
            return Path(filename)
        return None

    @staticmethod
    def _display_filesize(filesize: Optional[int], filepath: Optional[Path]) -> str:
        if filesize:
            return format_bytes(filesize)
        if filepath and filepath.exists():
            return format_bytes(filepath.stat().st_size)
        return "Unknown"


def download_with_retries(
    download_fn: Callable[[], DownloadResult],
    max_attempts: int = 3,
    backoff_seconds: float = 3.0,
) -> DownloadResult:
    """Retry a download a few times if it fails for a transient reason.

    Permanent failures (private/age-restricted/geo-blocked/unsupported) are
    not worth retrying, so they short-circuit immediately.
    """
    permanent_markers = (
        "private",
        "age-restricted",
        "unavailable",
        "geo-restricted",
        "not supported",
        "ffmpeg is required",
    )
    result = DownloadResult(success=False, error_message="Not attempted")
    for attempt in range(1, max_attempts + 1):
        result = download_fn()
        if result.success:
            return result
        message = (result.error_message or "").lower()
        if any(marker in message for marker in permanent_markers):
            return result
        if attempt < max_attempts:
            append_log(f"Retrying ({attempt}/{max_attempts}) after error: {result.error_message}")
            time.sleep(backoff_seconds)
    return result
