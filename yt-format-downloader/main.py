"""YT Format Downloader - an interactive command-line frontend for yt-dlp.

Run this file directly to launch the application::

    python main.py

See ``README.md`` for installation and usage details.
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from downloader import Downloader, DownloadError, DownloadResult, download_with_retries
from formatter import FormatInfo, FormatTableBuilder
from progress import ProgressManager
from utils import (
    add_history_entry,
    append_log,
    auto_update_ytdlp,
    check_ffmpeg_installed,
    format_bytes,
    format_elapsed,
    load_config,
    load_history,
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
        if not check_ffmpeg_installed():
            self.console.print(
                "[yellow]Warning:[/yellow] FFmpeg was not found on your PATH. "
                "Merging separate video/audio streams, embedding thumbnails and "
                "embedding metadata will not work until it is installed. "
                "See README.md for installation instructions."
            )

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
            choice = Prompt.ask(
                "Select an option",
                choices=list(actions.keys()) + ["7"],
                default="7",
                show_choices=False,
            )
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
            except KeyboardInterrupt:
                self.console.print("\n[yellow]Operation cancelled by user.[/yellow]")
                append_log("Operation cancelled by user (KeyboardInterrupt).")
            except Exception as exc:  # noqa: BLE001 - keep the menu alive no matter what
                self.console.print(f"[red]Unexpected error:[/red] {exc}")
                append_log(f"UNEXPECTED ERROR: {exc!r}")

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

        output_dir = self._prompt_output_folder()
        estimate = self._estimate_for_selection(formats, chosen)
        self.console.print(f"Estimated download size: [bold]{format_bytes(estimate)}[/bold]")
        if not Confirm.ask("Proceed with download?", default=True):
            self.console.print("Cancelled.")
            return

        format_spec = Downloader.build_format_spec(chosen)
        label = sanitize_filename(str(representative.get("title", url)))[:40]
        result = self._run_download(url, format_spec, output_dir, label, playlist_items)
        self._print_summary(result)
        self._record_history(result, chosen)

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

        output_dir = self._prompt_output_folder()

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
            self._record_history(result)

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

        output_dir = self._prompt_output_folder()
        self.console.print(f"Estimated download size: [bold]{format_bytes(chosen.filesize)}[/bold]")
        if not Confirm.ask("Proceed with download?", default=True):
            self.console.print("Cancelled.")
            return

        format_spec = Downloader.build_format_spec(chosen)
        label = sanitize_filename(str(representative.get("title", url)))[:40]
        result = self._run_download(url, format_spec, output_dir, label, playlist_items)
        self._print_summary(result)
        self._record_history(result, chosen)

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

    def _prompt_format_choice(self, formats: List[FormatInfo]) -> Optional[FormatInfo]:
        while True:
            raw = Prompt.ask("Choose format number")
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

    def _prompt_output_folder(self) -> Path:
        default_folder = self.config.get("download_folder", "Downloads")
        raw = Prompt.ask("Download folder", default=default_folder)
        folder = raw.strip() or default_folder
        return Path(folder).expanduser()

    def _safe_extract(self, url: str) -> Optional[Dict[str, Any]]:
        try:
            with self.console.status("[bold cyan]Fetching video information..."):
                info = self.downloader.extract_info(url)
            return info
        except DownloadError as exc:
            self.console.print(f"[red]Error:[/red] {exc}")
            append_log(f"EXTRACT FAILED {url}: {exc}")
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
    def _estimate_for_selection(formats: List[FormatInfo], chosen: FormatInfo) -> Optional[int]:
        if chosen.is_video_only:
            audio_candidates = [f for f in formats if f.is_audio_only]
            best_audio = max(audio_candidates, key=lambda f: f.filesize or 0, default=None)
            if best_audio is not None:
                return Downloader.estimate_size(chosen, best_audio)
        return chosen.filesize

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
    def _record_history(result: DownloadResult, chosen: Optional[FormatInfo] = None) -> None:
        add_history_entry(
            {
                "url": result.url,
                "title": result.title,
                "resolution": result.resolution,
                "size": result.filesize_display,
                "location": str(result.filepath) if result.filepath else "",
                "success": result.success,
                "error": result.error_message,
                "format_id": chosen.format_id if chosen else None,
            }
        )


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
