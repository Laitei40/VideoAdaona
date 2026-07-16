# YT Format Downloader

An interactive, terminal-based frontend for [yt-dlp](https://github.com/yt-dlp/yt-dlp),
styled with [rich](https://github.com/Textualize/rich). Browse every format a
video offers in a clean table, pick exactly what you want, and watch a live
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

## Installation

1. Install Python 3.9+.
2. Install the dependencies:

   ```bash
   pip install -r requirements.txt
   ```

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

## Usage

```bash
python main.py
```

You'll see a menu:

```
==============================
YT Format Downloader
==============================

1. Download Video
2. Download Best Quality
3. Download Audio Only
4. List Formats
5. Settings
6. Download History
7. Exit
```

1. **Download Video** - paste a URL, browse its formats, pick one by number,
   choose a download folder (blank = `Downloads`), confirm the estimated
   size, and watch it download.
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
    main.py         # interactive menu and orchestration
    downloader.py   # yt-dlp wrapper, error handling, retries
    formatter.py    # format extraction + Rich table rendering
    progress.py     # Rich progress bars wired to yt-dlp progress hooks
    utils.py        # config/history persistence, sanitisation, formatting
    requirements.txt
    README.md
```

Generated at runtime (safe to delete, they'll be recreated):

- `config.json` - your saved settings
- `history.json` - recent downloads/searches
- `download_archive.txt` - IDs of already-downloaded videos
- `download_log.txt` - a plain-text log of every download attempt

## Notes on resuming interrupted downloads

Downloads use yt-dlp's native `continuedl` support and `.part` files, so
re-running a download for the same file will resume where it left off
rather than starting over. Pressing `Ctrl+C` at any point cancels the
current operation cleanly and returns you to the menu.

## Extending

Each module has a single responsibility, so it's easy to extend:

- Add a new menu action in `main.py`.
- Add a new yt-dlp option in `downloader.py`'s `_build_ydl_opts`.
- Add a new table column in `formatter.py`.
- Add a new setting to `DEFAULT_CONFIG` in `utils.py`.

## License

MIT - see [LICENSE](LICENSE).
