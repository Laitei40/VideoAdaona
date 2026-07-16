"""Rich-powered progress reporting for yt-dlp downloads.

``ProgressManager`` wraps a single :class:`rich.progress.Progress` instance
so that one or many concurrent downloads (e.g. a playlist or several URLs
downloaded in parallel) can each get their own progress bar, while sharing
one live display.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, Optional

from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    Task,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from .utils import append_log


def build_progress(console: Optional[Console] = None) -> Progress:
    """Construct a :class:`Progress` instance with the columns we need."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.fields[label]}", justify="left"),
        BarColumn(bar_width=30),
        "[progress.percentage]{task.percentage:>5.1f}%",
        DownloadColumn(),
        TransferSpeedColumn(),
        TextColumn("ETA"),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    )


class ProgressManager:
    """Owns a Rich :class:`Progress` instance and hands out yt-dlp hooks."""

    def __init__(self, console: Optional[Console] = None) -> None:
        self.progress = build_progress(console)
        self._lock = threading.Lock()

    def __enter__(self) -> "ProgressManager":
        self.progress.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.progress.stop()

    def add_task(self, label: str) -> TaskID:
        """Register a new progress bar and return its task id."""
        with self._lock:
            return self.progress.add_task("download", label=label, total=None)

    def make_hook(self, task_id: TaskID) -> Callable[[Dict[str, Any]], None]:
        """Return a yt-dlp ``progress_hooks`` compatible callback."""

        def hook(d: Dict[str, Any]) -> None:
            status = d.get("status")
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                downloaded = d.get("downloaded_bytes", 0)
                with self._lock:
                    self.progress.update(
                        task_id,
                        total=total,
                        completed=downloaded,
                    )
            elif status == "finished":
                with self._lock:
                    task = self._get_task(task_id)
                    total = task.total if task and task.total else d.get("total_bytes")
                    self.progress.update(task_id, completed=total or task.completed if task else None)
                append_log(f"Finished stream: {d.get('filename', '?')}")
            elif status == "error":
                append_log(f"Error while downloading: {d.get('filename', '?')}")

        return hook

    def _get_task(self, task_id: TaskID) -> Optional[Task]:
        for task in self.progress.tasks:
            if task.id == task_id:
                return task
        return None

    def set_description(self, task_id: TaskID, label: str) -> None:
        with self._lock:
            self.progress.update(task_id, label=label)

    def finish_task(self, task_id: TaskID) -> None:
        with self._lock:
            task = self._get_task(task_id)
            if task is not None and task.total is not None:
                self.progress.update(task_id, completed=task.total)
