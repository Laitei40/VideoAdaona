"""Extraction and Rich-table rendering of yt-dlp format information.

``FormatTableBuilder`` turns the raw ``formats`` list inside a yt-dlp info
dict into a list of :class:`FormatInfo` records, and can render those
records as a polished :class:`rich.table.Table`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from rich.table import Table

from .utils import format_bytes


@dataclass
class FormatInfo:
    """A single, display-ready row describing one downloadable format."""

    index: int
    format_id: str
    ext: str
    resolution: str
    fps: str
    vcodec: str
    acodec: str
    hdr: str
    dynamic_range: str
    filesize: Optional[int]
    filesize_display: str
    tbr_display: str
    notes: str
    raw: Dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def is_video_only(self) -> bool:
        return self.raw.get("vcodec") not in (None, "none") and self.raw.get("acodec") in (None, "none")

    @property
    def is_audio_only(self) -> bool:
        return self.raw.get("acodec") not in (None, "none") and self.raw.get("vcodec") in (None, "none")

    @property
    def has_both(self) -> bool:
        return self.raw.get("vcodec") not in (None, "none") and self.raw.get("acodec") not in (None, "none")


class FormatTableBuilder:
    """Builds :class:`FormatInfo` rows and Rich tables from a yt-dlp info dict."""

    def __init__(self, info: Dict[str, Any]) -> None:
        self.info = info
        self.raw_formats: List[Dict[str, Any]] = info.get("formats") or []

    def build_formats(self) -> List[FormatInfo]:
        """Convert the raw yt-dlp format dicts into display-ready rows."""
        rows: List[FormatInfo] = []
        for i, fmt in enumerate(self.raw_formats, start=1):
            rows.append(self._to_format_info(i, fmt))
        return rows

    @staticmethod
    def _to_format_info(index: int, fmt: Dict[str, Any]) -> FormatInfo:
        vcodec = fmt.get("vcodec") or "none"
        acodec = fmt.get("acodec") or "none"

        height = fmt.get("height")
        width = fmt.get("width")
        if vcodec == "none":
            resolution = "audio"
        elif height:
            resolution = f"{height}p"
        elif fmt.get("resolution"):
            resolution = str(fmt["resolution"])
        elif width and height:
            resolution = f"{width}x{height}"
        else:
            resolution = "unknown"

        fps = fmt.get("fps")
        fps_display = f"{fps:g}" if fps else "N/A"

        dynamic_range = fmt.get("dynamic_range") or ("SDR" if vcodec != "none" else "N/A")
        hdr = "Yes" if dynamic_range not in ("SDR", "N/A", None) else "No"

        filesize = fmt.get("filesize") or fmt.get("filesize_approx")
        filesize_display = format_bytes(filesize)

        tbr = fmt.get("tbr") or fmt.get("vbr") or fmt.get("abr")
        tbr_display = f"{tbr:.0f} kbps" if tbr else "N/A"

        if vcodec != "none" and acodec != "none":
            notes = "merged"
        elif vcodec != "none":
            notes = "video only"
        elif acodec != "none":
            notes = "audio only"
        else:
            notes = "unknown"

        return FormatInfo(
            index=index,
            format_id=str(fmt.get("format_id", "?")),
            ext=fmt.get("ext", "?"),
            resolution=resolution,
            fps=fps_display,
            vcodec=vcodec,
            acodec=acodec,
            hdr=hdr,
            dynamic_range=dynamic_range,
            filesize=filesize,
            filesize_display=filesize_display,
            tbr_display=tbr_display,
            notes=notes,
            raw=fmt,
        )

    @staticmethod
    def render_table(formats: List[FormatInfo], title: str = "Available Formats") -> Table:
        """Render a full format list as a Rich table."""
        table = Table(title=title, header_style="bold cyan", show_lines=False)
        table.add_column("No", justify="right", style="bold")
        table.add_column("ID", style="magenta")
        table.add_column("Ext", style="green")
        table.add_column("Resolution")
        table.add_column("FPS", justify="right")
        table.add_column("VCodec")
        table.add_column("ACodec")
        table.add_column("HDR")
        table.add_column("Dynamic Range")
        table.add_column("Size", justify="right")
        table.add_column("Bitrate", justify="right")
        table.add_column("Notes", style="yellow")

        for f in formats:
            table.add_row(
                str(f.index),
                f.format_id,
                f.ext,
                f.resolution,
                f.fps,
                f.vcodec,
                f.acodec,
                f.hdr,
                f.dynamic_range,
                f.filesize_display,
                f.tbr_display,
                f.notes,
            )
        return table

    @staticmethod
    def render_audio_choice_table(formats: List[FormatInfo], title: str = "Available Audio") -> Table:
        """Render a compact table for picking an audio track to pair with a
        video-only format - just enough to compare codec/bitrate/size."""
        table = Table(title=title, header_style="bold cyan", show_lines=False)
        table.add_column("No", justify="right", style="bold")
        table.add_column("ID", style="magenta")
        table.add_column("Ext", style="green")
        table.add_column("Codec")
        table.add_column("Bitrate", justify="right")
        table.add_column("Size", justify="right")

        for f in formats:
            table.add_row(
                str(f.index),
                f.format_id,
                f.ext,
                friendly_codec_label(f.acodec),
                f.tbr_display,
                f.filesize_display,
            )
        return table

    @staticmethod
    def filter_audio_only(formats: List[FormatInfo]) -> List[FormatInfo]:
        """Return only the audio-only formats, re-numbered for display."""
        audio = [f for f in formats if f.is_audio_only]
        renumbered = []
        for i, f in enumerate(audio, start=1):
            renumbered.append(
                FormatInfo(
                    index=i,
                    format_id=f.format_id,
                    ext=f.ext,
                    resolution=f.resolution,
                    fps=f.fps,
                    vcodec=f.vcodec,
                    acodec=f.acodec,
                    hdr=f.hdr,
                    dynamic_range=f.dynamic_range,
                    filesize=f.filesize,
                    filesize_display=f.filesize_display,
                    tbr_display=f.tbr_display,
                    notes=f.notes,
                    raw=f.raw,
                )
            )
        return renumbered


# Container -> preferred audio extension, so "automatic" audio pairing stays
# playable in the widest range of players instead of just picking whatever
# has the highest bitrate (e.g. avoiding Opus-in-MP4 muxing quirks).
_COMPATIBLE_AUDIO_EXT = {
    "mp4": "m4a",
    "webm": "webm",
}


def friendly_codec_label(codec: Optional[str]) -> str:
    """Turn a raw codec string like ``mp4a.40.2`` into a short label like ``AAC``."""
    text = (codec or "").lower()
    if text in ("", "none"):
        return "Unknown"
    if text.startswith("mp4a") or "aac" in text:
        return "AAC"
    if "opus" in text:
        return "Opus"
    if "vorbis" in text:
        return "Vorbis"
    if "mp3" in text:
        return "MP3"
    if "flac" in text:
        return "FLAC"
    if "ac-3" in text or "ac3" in text:
        return "AC3"
    return codec.upper() if codec else "Unknown"


def select_compatible_audio(formats: List[FormatInfo], video: FormatInfo) -> Optional[FormatInfo]:
    """Pick the best audio-only format to automatically pair with ``video``.

    Prefers an audio track sharing the video's container family (MP4 -> M4A,
    WebM -> WebM/Opus) so the merged result stays broadly compatible; falls
    back to the overall best-bitrate audio track if no such match exists.
    """
    audio_formats = [f for f in formats if f.is_audio_only]
    if not audio_formats:
        return None

    preferred_ext = _COMPATIBLE_AUDIO_EXT.get(video.ext)
    pool = [f for f in audio_formats if f.ext == preferred_ext] if preferred_ext else []
    if not pool:
        pool = audio_formats

    def bitrate(f: FormatInfo) -> float:
        return f.raw.get("abr") or f.raw.get("tbr") or 0

    return max(pool, key=lambda f: (bitrate(f), f.filesize or 0))
