"""YT Format Downloader - an interactive command-line frontend for yt-dlp.

Once installed (``pip install .`` or ``pipx install .`` from the project
root), run it from anywhere with::

    ytfmt

See ``README.md`` for installation and usage details.
"""

from __future__ import annotations

import platform
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table
from rich.text import Text

from .downloader import (
    Downloader,
    DownloadError,
    DownloadResult,
    check_ytdlp_latest_version,
    download_with_retries,
    get_ytdlp_version,
    is_update_available,
)
from .formatter import FormatInfo, FormatTableBuilder, friendly_codec_label, select_compatible_audio
from .progress import ProgressManager
from .utils import (
    add_history_entry,
    append_log,
    auto_update_ytdlp,
    check_ffmpeg_installed,
    check_internet_connection,
    check_writable,
    format_bytes,
    format_elapsed,
    load_config,
    load_history,
    open_github_issue,
    parse_index_ranges,
    sanitize_filename,
    save_config,
)


@dataclass
class Job:
    """A single download task, fully resolved and ready to hand to a worker."""

    url: str
    label: str
    format_spec: str
    playlist_items: Optional[str] = None


class App:
    """Owns application state and drives the interactive menu."""

    def __init__(self, console: Console) -> None:
        self.console = console
        self.config: Dict[str, Any] = load_config()
        self.downloader = Downloader(self.config)

    # ------------------------------------------------------------------
    # Menu / main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Run the interactive menu until the user chooses to exit."""
        self._print_banner()
        self._print_startup_diagnostics()

        if self.config.get("auto_update_ytdlp"):
            with self.console.status("[dim]Checking for yt-dlp updates...[/dim]"):
                error = auto_update_ytdlp()
            if error:
                self.console.print(f"[yellow]Could not auto-update yt-dlp: {error}[/yellow]")

        actions = {
            "1": self.handle_download_video,
            "2": self.handle_best_quality,
            "3": self.handle_audio_only,
            "4": self.handle_list_formats,
            "5": self.handle_settings,
            "6": self.handle_history,
        }

        while True:
            self._print_menu()
            try:
                choice = Prompt.ask(
                    "Select an option",
                    choices=list(actions.keys()) + ["7"],
                    default="7",
                    show_choices=False,
                )
            except EOFError:
                # stdin closed/exhausted (piped input ran out, Ctrl+D/Ctrl+Z,
                # etc.) - treat exactly like choosing Exit, not a crash.
                self.console.print("\n[cyan]Goodbye![/cyan]")
                break
            if choice == "7":
                self.console.print("[cyan]Goodbye![/cyan]")
                break
            action = actions.get(choice)
            if action is None:
                continue
            try:
                action()
            except DownloadError as exc:
                self.console.print(f"[red]Error:[/red] {exc}")
                self._offer_issue_report(str(exc))
            except KeyboardInterrupt:
                self.console.print("\n[yellow]Operation cancelled by user.[/yellow]")
                append_log("Operation cancelled by user (KeyboardInterrupt).")
            except EOFError:
                self.console.print("\n[cyan]Goodbye![/cyan]")
                append_log("Input stream ended (EOF) during an action; exiting.")
                return
            except Exception as exc:  # noqa: BLE001 - keep the menu alive no matter what
                self.console.print(f"[red]Unexpected error:[/red] {exc}")
                append_log(f"UNEXPECTED ERROR: {exc!r}")
                self._offer_issue_report(f"Unexpected error: {exc}", traceback.format_exc())

    def _print_menu(self) -> None:
        self.console.rule("[bold magenta]YT Format Downloader[/bold magenta]")
        self.console.print(
            "[bold]1.[/bold] Download Video\n"
            "[bold]2.[/bold] Download Best Quality\n"
            "[bold]3.[/bold] Download Audio Only\n"
            "[bold]4.[/bold] List Formats\n"
            "[bold]5.[/bold] Settings\n"
            "[bold]6.[/bold] Download History\n"
            "[bold]7.[/bold] Exit"
        )

    def _print_banner(self) -> None:
        """Print the app's title banner, shown once at startup."""
        banner = Text(justify="center")
        banner.append("YT Format Downloader", style="bold magenta")
        banner.append("\n")
        banner.append("Developed by Laitei", style="dim italic")
        self.console.print(Panel(banner, border_style="magenta", expand=False))

    def _print_startup_diagnostics(self) -> None:
        """Check FFmpeg, internet connectivity and the yt-dlp version once at
        startup, and print a small status table so problems are obvious
        before the user tries (and fails) an actual download."""
        ffmpeg_ok = check_ffmpeg_installed()
        installed_version = get_ytdlp_version()
        with self.console.status("[dim]Checking internet connection...[/dim]"):
            internet_ok = check_internet_connection()

        table = Table(title="System Check", show_header=False, box=None, padding=(0, 2))
        table.add_column("Check", style="bold")
        table.add_column("Status")
        table.add_row("FFmpeg", "[green]Found[/green]" if ffmpeg_ok else "[red]Not found[/red]")
        table.add_row(
            "Internet connection",
            "[green]Connected[/green]" if internet_ok else "[red]Not connected[/red]",
        )
        table.add_row("yt-dlp version", f"[cyan]{installed_version}[/cyan]")
        self.console.print(table)

        if not ffmpeg_ok:
            self.console.print(
                "[yellow]Warning:[/yellow] FFmpeg was not found on your PATH. "
                "Merging separate video/audio streams, embedding thumbnails and "
                "embedding metadata will not work until it is installed. "
                "See README.md for installation instructions."
            )
        if not internet_ok:
            self.console.print(
                "[yellow]Warning:[/yellow] No internet connection detected. "
                "Fetching formats and downloading will not work until you're back online."
            )
        elif not self.config.get("auto_update_ytdlp"):
            # Only worth a PyPI round-trip if we know we're online, and only
            # as a hint - auto_update_ytdlp already handles this proactively.
            latest_version = check_ytdlp_latest_version()
            if latest_version and is_update_available(installed_version, latest_version):
                self.console.print(
                    f"[yellow]A newer yt-dlp version is available:[/yellow] {latest_version} "
                    f"(installed: {installed_version}). Update via Settings or run "
                    "`pip install --upgrade yt-dlp`."
                )

    # ------------------------------------------------------------------
    # Menu option 1: Download Video
    # ------------------------------------------------------------------

    def handle_download_video(self) -> None:
        """Fetch formats for one URL, let the user pick one, then download it."""
        url = self._prompt_url()
        if url is None:
            return
        info = self._safe_extract(url)
        if info is None:
            return

        playlist_items = self._maybe_handle_playlist(info)
        representative = self._representative_entry(info, playlist_items)

        formats = FormatTableBuilder(representative).build_formats()
        if not formats:
            self.console.print("[red]No downloadable formats were found for this URL.[/red]")
            return
        self.console.print(FormatTableBuilder.render_table(formats))

        chosen = self._prompt_format_choice(formats)
        if chosen is None:
            return

        selection = self._resolve_format_selection(formats, chosen)
        if selection is None:
            self.console.print("Cancelled.")
            return
        format_spec, audio = selection

        output_dir = self._resolve_download_directory()
        if output_dir is None:
            self.console.print("Cancelled.")
            return
        estimate = self._estimate_for_pair(chosen, audio)
        self.console.print(f"Estimated download size: [bold]{format_bytes(estimate)}[/bold]")
        if not Confirm.ask("Proceed with download?", default=True):
            self.console.print("Cancelled.")
            return

        label = sanitize_filename(str(representative.get("title", url)))[:40]
        result = self._run_download(url, format_spec, output_dir, label, playlist_items)
        self._print_summary(result)
        self._record_history(result, format_spec)

    # ------------------------------------------------------------------
    # Menu option 2: Download Best Quality
    # ------------------------------------------------------------------

    def handle_best_quality(self) -> None:
        """Download one or more URLs using yt-dlp's bestvideo+bestaudio, in parallel."""
        self.console.print(
            "[dim]Tip: enter multiple URLs separated by commas to download them in parallel.[/dim]"
        )
        raw = Prompt.ask("Enter video URL")
        urls = [u.strip() for u in raw.replace("\n", ",").split(",") if u.strip()]
        if not urls:
            self.console.print("[red]No URL provided.[/red]")
            return

        output_dir = self._resolve_download_directory()
        if output_dir is None:
            self.console.print("Cancelled.")
            return

        jobs: List[Job] = []
        for url in urls:
            info = self._safe_extract(url)
            if info is None:
                continue
            playlist_items = self._maybe_handle_playlist(info)
            representative = self._representative_entry(info, playlist_items)
            label = sanitize_filename(str(representative.get("title", url)))[:40]
            jobs.append(
                Job(
                    url=url,
                    label=label,
                    format_spec="bestvideo+bestaudio/best",
                    playlist_items=playlist_items,
                )
            )

        if not jobs:
            self.console.print("[red]Nothing to download.[/red]")
            return

        results = self._run_jobs(jobs, output_dir)
        for result in results:
            self._print_summary(result)
            self._record_history(result, "bestvideo+bestaudio/best")

    # ------------------------------------------------------------------
    # Menu option 3: Download Audio Only
    # ------------------------------------------------------------------

    def handle_audio_only(self) -> None:
        """Show only audio-only formats and download the chosen one."""
        url = self._prompt_url()
        if url is None:
            return
        info = self._safe_extract(url)
        if info is None:
            return

        playlist_items = self._maybe_handle_playlist(info)
        representative = self._representative_entry(info, playlist_items)

        formats = FormatTableBuilder(representative).build_formats()
        audio_formats = FormatTableBuilder.filter_audio_only(formats)
        if not audio_formats:
            self.console.print("[red]No audio-only formats were found for this URL.[/red]")
            return
        self.console.print(FormatTableBuilder.render_table(audio_formats, title="Audio Formats"))

        chosen = self._prompt_format_choice(audio_formats)
        if chosen is None:
            return

        output_dir = self._resolve_download_directory()
        if output_dir is None:
            self.console.print("Cancelled.")
            return
        self.console.print(f"Estimated download size: [bold]{format_bytes(chosen.filesize)}[/bold]")
        if not Confirm.ask("Proceed with download?", default=True):
            self.console.print("Cancelled.")
            return

        # Audio-only formats are downloaded exactly as chosen - no questions.
        format_spec = chosen.format_id
        label = sanitize_filename(str(representative.get("title", url)))[:40]
        result = self._run_download(url, format_spec, output_dir, label, playlist_items)
        self._print_summary(result)
        self._record_history(result, format_spec)

    # ------------------------------------------------------------------
    # Menu option 4: List Formats
    # ------------------------------------------------------------------

    def handle_list_formats(self) -> None:
        """Display available formats without downloading anything."""
        url = self._prompt_url()
        if url is None:
            return
        info = self._safe_extract(url)
        if info is None:
            return

        representative = info
        entries = self._playlist_entries(info)
        if entries:
            representative = entries[0]
            self.console.print(
                f"[cyan]Playlist detected ({len(entries)} items) - showing formats "
                "for the first item.[/cyan]"
            )

        formats = FormatTableBuilder(representative).build_formats()
        if not formats:
            self.console.print("[red]No formats found for this URL.[/red]")
            return
        self.console.print(FormatTableBuilder.render_table(formats))
        add_history_entry(
            {
                "url": url,
                "title": representative.get("title", "Unknown"),
                "action": "list_formats",
            }
        )

    # ------------------------------------------------------------------
    # Menu option 5: Settings
    # ------------------------------------------------------------------

    def handle_settings(self) -> None:
        """Interactively view and edit persisted application settings."""
        while True:
            keys = list(self.config.keys())
            table = Table(title="Settings", header_style="bold cyan")
            table.add_column("No", justify="right")
            table.add_column("Setting")
            table.add_column("Value")
            for i, key in enumerate(keys, start=1):
                table.add_row(str(i), key, str(self.config[key]))
            self.console.print(table)

            choice = Prompt.ask("Choose a setting number to edit, or 'b' to go back", default="b")
            if choice.strip().lower() in ("b", "back", ""):
                break
            try:
                idx = int(choice.strip())
                key = keys[idx - 1]
            except (ValueError, IndexError):
                self.console.print("[red]Invalid selection.[/red]")
                continue
            self._edit_setting(key)

        save_config(self.config)
        self.downloader.config = self.config

    def _edit_setting(self, key: str) -> None:
        if key == "download_location_mode":
            self._edit_download_location_mode()
            return
        current = self.config[key]
        if isinstance(current, bool):
            self.config[key] = Confirm.ask(f"Enable '{key}'?", default=current)
        elif isinstance(current, int):
            self.config[key] = IntPrompt.ask(f"New value for '{key}'", default=current)
        elif isinstance(current, list):
            raw = Prompt.ask(
                f"New comma-separated values for '{key}'", default=",".join(str(v) for v in current)
            )
            self.config[key] = [v.strip() for v in raw.split(",") if v.strip()]
        else:
            self.config[key] = Prompt.ask(f"New value for '{key}'", default=str(current))
        save_config(self.config)
        self.console.print(f"[green]Updated '{key}'.[/green]")

    def _edit_download_location_mode(self) -> None:
        self.console.print(
            "Download Location\n\n"
            "1. Current Working Directory (Recommended)\n"
            "2. Fixed Folder\n"
            "3. Ask Every Time"
        )
        choice = Prompt.ask("Option", choices=["1", "2", "3"], default="1")
        if choice == "1":
            self.config["download_location_mode"] = "current_directory"
        elif choice == "2":
            default_folder = self.config.get("download_folder") or str(Path.home() / "Downloads")
            raw = Prompt.ask("Enter download folder", default=default_folder).strip()
            self.config["download_folder"] = str(Path(raw or default_folder).expanduser())
            self.config["download_location_mode"] = "fixed_directory"
        else:
            self.config["download_location_mode"] = "ask_every_time"
        save_config(self.config)
        self.console.print(f"[green]Download location set to '{self.config['download_location_mode']}'.[/green]")

    # ------------------------------------------------------------------
    # Menu option 6: Download History
    # ------------------------------------------------------------------

    def handle_history(self) -> None:
        """Show recent downloads/searches, optionally filtered by keyword."""
        history = load_history()
        if not history:
            self.console.print("[yellow]No history yet.[/yellow]")
            return

        keyword = Prompt.ask("Search history (title/url), or press Enter to show all", default="")
        if keyword:
            needle = keyword.lower()
            history = [
                h
                for h in history
                if needle in str(h.get("title", "")).lower() or needle in str(h.get("url", "")).lower()
            ]
            if not history:
                self.console.print("[yellow]No matching history entries.[/yellow]")
                return

        table = Table(title="Download History", header_style="bold cyan")
        table.add_column("When")
        table.add_column("Title")
        table.add_column("Resolution")
        table.add_column("Size")
        table.add_column("Status")
        table.add_column("Location")
        for entry in history[:50]:
            if entry.get("action") == "list_formats":
                status = "listed"
            else:
                status = "OK" if entry.get("success") else "FAILED"
            table.add_row(
                str(entry.get("timestamp", "")),
                str(entry.get("title", ""))[:40],
                str(entry.get("resolution", "")),
                str(entry.get("size", "")),
                status,
                str(entry.get("location", ""))[:50],
            )
        self.console.print(table)

    # ------------------------------------------------------------------
    # Shared prompts / helpers
    # ------------------------------------------------------------------

    def _prompt_url(self) -> Optional[str]:
        url = Prompt.ask("Enter video URL").strip()
        if not url:
            self.console.print("[red]No URL entered.[/red]")
            return None
        return url

    def _prompt_format_choice(
        self, formats: List[FormatInfo], prompt_text: str = "Choose format number"
    ) -> Optional[FormatInfo]:
        while True:
            raw = Prompt.ask(prompt_text)
            if raw.strip().lower() in ("q", "quit", "cancel", ""):
                return None
            try:
                idx = int(raw.strip())
            except ValueError:
                self.console.print("[red]Please enter a valid number.[/red]")
                continue
            match = next((f for f in formats if f.index == idx), None)
            if match is None:
                self.console.print(f"[red]No format numbered {idx}. Please try again.[/red]")
                continue
            return match

    def _resolve_format_selection(
        self, formats: List[FormatInfo], chosen: FormatInfo
    ) -> Optional[Tuple[str, Optional[FormatInfo]]]:
        """Decide the final yt-dlp format string for a user-picked format row.

        Formats that already contain both video and audio, or that are
        audio-only, are downloaded exactly as chosen - no extra questions.
        A video-only format triggers a sub-menu asking how to handle audio.

        Returns ``(format_spec, audio_or_none)``, or ``None`` if the user
        cancelled out of the video-only sub-menu.
        """
        if chosen.has_both or chosen.is_audio_only:
            return chosen.format_id, None

        # Video-only: the user gets to decide how audio is handled.
        self._print_selected_format(chosen)
        while True:
            self.console.print(
                "\nWhat would you like to do?\n"
                "1. Download video only (no audio)\n"
                "2. Download video + automatically select the best audio (Recommended)\n"
                "3. Manually choose an audio format\n"
                "4. Cancel"
            )
            option = Prompt.ask("Option", choices=["1", "2", "3", "4"], default="2")

            if option == "1":
                return chosen.format_id, None

            if option == "2":
                audio = select_compatible_audio(formats, chosen)
                if audio is None:
                    self.console.print("[yellow]No audio formats are available for this video.[/yellow]")
                    continue
                self._print_selected_audio(audio)
                format_spec = f"{chosen.format_id}+{audio.format_id}"
                self.console.print(f"[bold]Final format:[/bold] {format_spec}")
                return format_spec, audio

            if option == "3":
                audio_formats = FormatTableBuilder.filter_audio_only(formats)
                if not audio_formats:
                    self.console.print("[yellow]No audio formats are available for this video.[/yellow]")
                    continue
                self.console.print(FormatTableBuilder.render_audio_choice_table(audio_formats))
                audio = self._prompt_format_choice(audio_formats, prompt_text="Select audio format")
                if audio is None:
                    continue
                format_spec = f"{chosen.format_id}+{audio.format_id}"
                self.console.print(f"[bold]Final format:[/bold] {format_spec}")
                return format_spec, audio

            # option == "4"
            return None

    def _print_selected_format(self, video: FormatInfo) -> None:
        width = video.raw.get("width")
        height = video.raw.get("height")
        fps = video.raw.get("fps")

        lines = [video.format_id]
        if width and height:
            lines.append(f"{width}×{height}")
        if height:
            lines.append(f"{height}p{fps:g}" if fps else f"{height}p")
        lines.append(video.ext.upper())
        lines.append(video.notes.title())

        self.console.print(Panel("\n".join(lines), title="Selected Format", border_style="cyan"))

    def _print_selected_audio(self, audio: FormatInfo) -> None:
        lines = [audio.format_id, friendly_codec_label(audio.acodec), audio.tbr_display]
        self.console.print(
            Panel("\n".join(lines), title="Automatically selected audio", border_style="cyan")
        )

    def _resolve_download_directory(self) -> Optional[Path]:
        """Decide where a download should be saved, per ``download_location_mode``.

        Mirrors the plain yt-dlp CLI's default: save into the current
        working directory without asking, unless the user has configured a
        fixed folder or opted into being asked each time. Always prints the
        resolved location before returning it.
        """
        mode = self.config.get("download_location_mode", "current_directory")

        if mode == "fixed_directory":
            fixed = self.config.get("download_folder") or str(Path.home() / "Downloads")
            directory = self._ensure_directory(Path(fixed).expanduser())
        elif mode == "ask_every_time":
            directory = self._prompt_save_location_menu()
        else:
            directory = self._ensure_directory(Path.cwd())

        if directory is None:
            return None

        self.console.print(Panel(str(directory), title="Download Location", border_style="cyan"))
        return directory

    def _prompt_save_location_menu(self) -> Optional[Path]:
        cwd = Path.cwd()
        self.console.print(
            Panel(f"[bold]Current Directory[/bold]\n{cwd}", title="Save Location", border_style="cyan")
        )
        self.console.print("1. Save here (Recommended)\n2. Choose another folder")
        choice = Prompt.ask("Option", choices=["1", "2"], default="1")
        if choice == "1":
            return self._ensure_directory(cwd)

        raw = Prompt.ask("Enter destination folder").strip()
        folder = Path(raw).expanduser() if raw else cwd
        return self._ensure_directory(folder)

    def _ensure_directory(self, path: Path) -> Optional[Path]:
        """Make sure ``path`` exists (asking before creating) and is writable.

        Returns ``None`` if the user declines to create a missing folder, or
        if the folder isn't usable (not a directory, no write permission).
        """
        if not path.exists():
            self.console.print(f"[yellow]Folder does not exist:[/yellow] {path}")
            self.console.print("Create it?\n\n1. Yes\n2. No")
            choice = Prompt.ask("Option", choices=["1", "2"], default="1")
            if choice != "1":
                return None
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self.console.print(f"[red]Could not create folder:[/red] {exc}")
                return None

        if not path.is_dir():
            self.console.print(f"[red]Not a folder:[/red] {path}")
            return None

        if not check_writable(path):
            self.console.print(f"[red]No write permission for:[/red] {path}")
            return None

        return path

    def _safe_extract(self, url: str) -> Optional[Dict[str, Any]]:
        if not check_internet_connection():
            self.console.print(
                "[red]Error:[/red] No internet connection detected. "
                "Please check your network and try again."
            )
            append_log(f"EXTRACT SKIPPED (no internet) {url}")
            return None
        try:
            with self.console.status("[bold cyan]Fetching video information..."):
                info = self.downloader.extract_info(url)
            return info
        except DownloadError as exc:
            self.console.print(f"[red]Error:[/red] {exc}")
            append_log(f"EXTRACT FAILED {url}: {exc}")
            self._offer_issue_report(str(exc), f"URL: {url}")
            return None

    @staticmethod
    def _playlist_entries(info: Dict[str, Any]) -> List[Dict[str, Any]]:
        entries = info.get("entries")
        if not entries:
            return []
        return [e for e in entries if e]

    def _maybe_handle_playlist(self, info: Dict[str, Any]) -> Optional[str]:
        """Ask which playlist items to grab; returns a yt-dlp playlist_items string."""
        entries = self._playlist_entries(info)
        if not entries:
            return None
        count = len(entries)
        self.console.print(f"[cyan]This URL is a playlist with {count} item(s).[/cyan]")
        text = Prompt.ask("Select items to download (e.g. 1,3,5-7) or 'all'", default="all")
        try:
            indices = parse_index_ranges(text, count)
        except ValueError:
            self.console.print("[yellow]Could not parse selection, defaulting to all items.[/yellow]")
            indices = list(range(1, count + 1))
        return ",".join(str(i) for i in indices)

    def _representative_entry(
        self, info: Dict[str, Any], playlist_items: Optional[str]
    ) -> Dict[str, Any]:
        """Pick which entry's formats to show the user for a format decision."""
        entries = self._playlist_entries(info)
        if not entries:
            return info
        first_index = 1
        if playlist_items:
            first_index = int(playlist_items.split(",")[0].split("-")[0])
        first_index = min(max(first_index, 1), len(entries))
        return entries[first_index - 1]

    @staticmethod
    def _estimate_for_pair(video: FormatInfo, audio: Optional[FormatInfo]) -> Optional[int]:
        if audio is not None:
            return Downloader.estimate_size(video, audio)
        return video.filesize

    # ------------------------------------------------------------------
    # Download execution
    # ------------------------------------------------------------------

    def _run_download(
        self,
        url: str,
        format_spec: str,
        output_dir: Path,
        label: str,
        playlist_items: Optional[str] = None,
    ) -> DownloadResult:
        """Run a single download with a live Rich progress bar and retries."""
        with ProgressManager(self.console) as pm:
            task_id = pm.add_task(label or url)
            base_hook = pm.make_hook(task_id)

            def hook(d: Dict[str, Any]) -> None:
                entry_info = d.get("info_dict") or {}
                idx = entry_info.get("playlist_index")
                title = entry_info.get("title")
                if idx and title:
                    pm.set_description(task_id, f"[{idx}] {sanitize_filename(str(title))[:30]}")
                base_hook(d)

            def attempt() -> DownloadResult:
                return self.downloader.download(url, format_spec, output_dir, hook, playlist_items)

            try:
                result = download_with_retries(attempt, max_attempts=3)
            except KeyboardInterrupt:
                self.console.print("\n[yellow]Download interrupted by user.[/yellow]")
                append_log(f"INTERRUPTED {url}")
                raise
        return result

    def _run_jobs(self, jobs: List[Job], output_dir: Path) -> List[DownloadResult]:
        """Run several download jobs in parallel, sharing one progress display."""
        max_workers = max(1, int(self.config.get("max_parallel_downloads", 3)))
        results: List[DownloadResult] = []
        try:
            with ProgressManager(self.console) as pm:

                def worker(job: Job) -> DownloadResult:
                    task_id = pm.add_task(job.label)
                    hook = pm.make_hook(task_id)

                    def attempt() -> DownloadResult:
                        return self.downloader.download(
                            job.url, job.format_spec, output_dir, hook, job.playlist_items
                        )

                    return download_with_retries(attempt, max_attempts=3)

                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {executor.submit(worker, job): job for job in jobs}
                    for future in as_completed(futures):
                        results.append(future.result())
        except KeyboardInterrupt:
            self.console.print("\n[yellow]Downloads interrupted by user.[/yellow]")
            append_log("Parallel downloads interrupted by user.")
            raise
        return results

    def _print_summary(self, result: DownloadResult) -> None:
        if not result.success:
            self.console.print(
                Panel(
                    f"[red]Download failed:[/red] {result.error_message}\n[dim]{result.url}[/dim]",
                    title="Error",
                    border_style="red",
                )
            )
            self._offer_issue_report(result.error_message or "Download failed", f"URL: {result.url}")
            return
        body = (
            f"[bold]Title:[/bold] {result.title}\n"
            f"[bold]Resolution:[/bold] {result.resolution}\n"
            f"[bold]Size:[/bold] {result.filesize_display}\n"
            f"[bold]Location:[/bold] {result.filepath or 'Unknown'}\n"
            f"[bold]Elapsed time:[/bold] {format_elapsed(result.elapsed_seconds)}"
        )
        self.console.print(Panel(body, title="Download completed!", border_style="green"))
        if result.error_message:
            # Media downloaded fine, but a non-essential step (e.g. embedding
            # a thumbnail) failed - worth a warning, not a failure.
            self.console.print(f"[yellow]Note:[/yellow] {result.error_message}")

    @staticmethod
    def _record_history(result: DownloadResult, format_spec: Optional[str] = None) -> None:
        add_history_entry(
            {
                "url": result.url,
                "title": result.title,
                "resolution": result.resolution,
                "size": result.filesize_display,
                "location": str(result.filepath) if result.filepath else "",
                "success": result.success,
                "error": result.error_message,
                "format_id": format_spec,
            }
        )

    # ------------------------------------------------------------------
    # GitHub issue reporting
    # ------------------------------------------------------------------

    def _offer_issue_report(self, summary: str, detail: str = "") -> None:
        """After an error is shown, let the user optionally file a GitHub
        issue about it (with basic environment info pre-filled) instead of
        just leaving them with an error message and nothing to do about it.
        """
        try:
            wants_to_report = Confirm.ask(
                "Would you like to raise an issue on GitHub about this?", default=False
            )
        except (KeyboardInterrupt, EOFError):
            return
        if not wants_to_report:
            return

        body = self._build_issue_body(summary, detail)
        opened, url = open_github_issue(summary, body)
        if opened:
            self.console.print("[cyan]Opening your browser to file a GitHub issue...[/cyan]")
        self.console.print(f"[dim]If nothing opened, paste this link into your browser:[/dim]\n{url}")
        append_log(f"Issue report offered for: {summary}")

    @staticmethod
    def _build_issue_body(summary: str, detail: str) -> str:
        lines = [
            summary,
            "",
            "### Details",
            detail or "(no additional details)",
            "",
            "### Environment",
            f"- OS: {platform.system()} {platform.release()}",
            f"- Python: {platform.python_version()}",
            f"- yt-dlp: {get_ytdlp_version()}",
            "",
            "_Reported via YT Format Downloader's built-in error reporter._",
        ]
        return "\n".join(lines)


def main() -> None:
    """Application entry point."""
    console = Console()
    app = App(console)
    try:
        app.run()
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted. Goodbye![/yellow]")
        sys.exit(130)


if __name__ == "__main__":
    main()
