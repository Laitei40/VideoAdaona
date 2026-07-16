# YT Format Downloader

**Developed by Laitei** — an interactive, terminal-based frontend for
[yt-dlp](https://github.com/yt-dlp/yt-dlp), styled with
[rich](https://github.com/Textualize/rich). Browse every format a video
offers in a clean table, pick exactly what you want, and watch a live
progress bar while it downloads and merges automatically.

Works on Windows, Linux and macOS.

## Features

- Browse all available formats (video, audio, combined) in a Rich table:
  ID, extension, resolution, FPS, codecs, HDR/dynamic range, file size,
  bitrate and notes.
- Download a specific format, the best available quality, or audio only.
- Automatic video+audio merging via FFmpeg when a video-only format is chosen.
- Playlist support, including selecting specific items (`1,3,5-7` or `all`).
- Multiple URLs at once with parallel downloads (Download Best Quality).
- Thumbnail and metadata embedding, subtitle downloads.
- Download archive to skip files you already have.
- Cookie file support for age-restricted/private content you have access to.
- Search-able download history and a persisted `config.json`.
- Friendly, non-crashing error handling for private/age-restricted/geo-blocked
  videos, missing FFmpeg, and network failures, with automatic retries.
- A plain-text progress log (`download_log.txt`) of everything that happened.
- Startup system check: FFmpeg availability, internet connectivity, and the
  installed yt-dlp version (with a hint if a newer release is on PyPI).
  Internet connectivity is also checked before every URL fetch, so an
  offline machine fails fast with a clear message instead of hanging.
- Whenever an error occurs, the app never just stops - it asks if you'd
  like to open a pre-filled GitHub issue about it
  ([Laitei40/VideoAdaona/issues](https://github.com/Laitei40/VideoAdaona/issues))
  with the error and basic environment info included, then returns you to
  the menu either way.

## Installation

1. Install Python 3.9+.

2. Install this project as a command-line tool, from the project root (the
   folder containing `pyproject.toml`):

   ```bash
   pipx install .
   ```

   [pipx](https://pipx.pypa.io/) installs it in its own isolated environment
   and puts the `ytfmt` command on your `PATH`, so it's available from any
   directory, in any terminal. Don't have pipx? `pip install --user .` works
   the same way (just without the isolation). If you're actively developing
   the code, use `pipx install --editable .` (or `pip install -e .`) instead,
   so your edits take effect immediately without reinstalling.

3. Install FFmpeg (required to merge separate video/audio streams, embed
   thumbnails, and embed metadata):

   - **Windows**: `winget install ffmpeg` (or download a build from
     [ffmpeg.org](https://ffmpeg.org/download.html) and add its `bin` folder
     to your `PATH`).
   - **macOS**: `brew install ffmpeg`
   - **Linux (Debian/Ubuntu)**: `sudo apt install ffmpeg`
   - **Linux (Fedora)**: `sudo dnf install ffmpeg`
   - **Linux (Arch)**: `sudo pacman -S ffmpeg`

   Verify it's on your `PATH` with `ffmpeg -version`. The app will warn you
   on startup if it can't find FFmpeg, and merging/embedding steps will be
   skipped until it's installed.

A plain `pip install -r requirements.txt` (installing just `yt-dlp` and
`rich`, without the `ytfmt` command) still works too, if you'd rather run it
straight from a checkout with `python -m yt_format_downloader.main`.

## Usage

From any directory, in any terminal:

```bash
ytfmt
```

You'll see a startup banner and system check, then the menu:

```
┌──────────────────────┐
│ YT Format Downloader │
│ Developed by Laitei  │
└──────────────────────┘
System Check
  FFmpeg                 Found
  Internet connection    Connected
  yt-dlp version         2026.07.04

1. Download Video
2. Download Best Quality
3. Download Audio Only
4. List Formats
5. Settings
6. Download History
7. Exit
```

1. **Download Video** - paste a URL, browse its formats, pick one by number,
   choose a download folder (blank = your `Downloads` folder), confirm the
   estimated size, and watch it download.
2. **Download Best Quality** - paste one or more comma-separated URLs and
   they'll download in parallel using `bestvideo+bestaudio`.
3. **Download Audio Only** - same as option 1, but only audio formats are
   shown.
4. **List Formats** - just prints the format table, no download.
5. **Settings** - edit `config.json` interactively (download folder,
   filename template, thumbnail/metadata embedding, subtitles, cookies file,
   parallel download limit, and more).
6. **Download History** - browse or search past downloads.

Playlists are detected automatically; you'll be asked which items to grab.

## Project structure

```
yt-format-downloader/
    pyproject.toml               # packaging + the `ytfmt` console-script entry point
    src/
        yt_format_downloader/
            main.py               # interactive menu and orchestration
            downloader.py         # yt-dlp wrapper, error handling, retries
            formatter.py          # format extraction + Rich table rendering
            progress.py           # Rich progress bars wired to yt-dlp progress hooks
            utils.py              # config/history persistence, sanitisation, formatting
    requirements.txt
    LICENSE
    README.md
```

Because the package is installed (e.g. into `site-packages`), its data files
don't live next to the source - they live in your per-user data directory,
so they persist across reinstalls/upgrades and any account on the machine
gets its own:

- **Windows**: `%APPDATA%\yt-format-downloader\`
- **macOS**: `~/Library/Application Support/yt-format-downloader/`
- **Linux**: `${XDG_CONFIG_HOME:-~/.config}/yt-format-downloader/`

Inside that folder (safe to delete, they'll be recreated):

- `config.json` - your saved settings
- `history.json` - recent downloads/searches
- `download_archive.txt` - IDs of already-downloaded videos
- `download_log.txt` - a plain-text log of every download attempt

## Notes on resuming interrupted downloads

Downloads use yt-dlp's native `continuedl` support and `.part` files, so
re-running a download for the same file will resume where it left off
rather than starting over. Pressing `Ctrl+C` at any point cancels the
current operation cleanly and returns you to the menu, and closing the
input stream entirely (`Ctrl+D`/`Ctrl+Z`, or piped input running out)
exits the app cleanly instead of crashing.

## Extending

Each module has a single responsibility, so it's easy to extend:

- Add a new menu action in `main.py`.
- Add a new yt-dlp option in `downloader.py`'s `_build_ydl_opts`.
- Add a new table column in `formatter.py`.
- Add a new setting to `DEFAULT_CONFIG` in `utils.py`.

## License

MIT - see [LICENSE](LICENSE). Copyright (c) 2026 Laitei.
