# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**aRenombrar** is a Windows desktop GUI app for renaming and uploading media files (TV series, movies, anime). It queries TMDB for metadata, generates clean filenames from configurable templates, and uploads via FTP/FTPS. Written in Python with customtkinter.

## Running the app

All commands are run from the `aRenombrar/` subdirectory:

```bash
cd aRenombrar
python main.py               # launch normally
python diagnostico.py        # launch with diagnostics written to diagnostico.log
python crear_acceso_directo.py  # create a Desktop shortcut (Windows)
```

Unit tests cover the pure `core/` logic (no tkinter) in `aRenombrar/tests/`:

```bash
cd aRenombrar
pip install -r requirements-dev.txt
pytest tests/ -v
```

## Building the executable

From inside `aRenombrar/`:

```bash
pip install -r requirements.txt
pyinstaller aRenombrar.spec
# Output: dist/aRenombrar/aRenombrar.exe
```

The spec bundles `customtkinter`, `PIL`, `tkinterdnd2`, and `pystray` assets. The icon `iconoPrincipal.ico` must be present in `aRenombrar/`.

## Architecture

```
aRenombrar/
├── main.py          # entry point — creates and starts App
├── config.py        # Config class, persists to %APPDATA%\aRenombrar\config.json
├── core/
│   ├── api_client.py   # TMDBClient, MediaInfo dataclass, detect_episode()
│   ├── renamer.py      # build_new_name(), rename_file(), is_video_file()
│   ├── ftp_client.py   # FTPClient — upload with progress, speed limit, resume
│   └── auto_watcher.py # AutoWatcher — background folder polling thread
└── gui/
    └── app.py          # entire UI in one App class (~2200 lines)
```

### Data flow

1. Files are added (drag-drop, picker, or AutoWatcher)
2. `detect_episode()` in `api_client.py` parses the filename using regex patterns to extract title, season, episode, and media type (tv/movie/anime)
3. `TMDBClient.search_multi()` queries TMDB; `build_media_info()` constructs a `MediaInfo` dataclass
4. `build_new_name()` in `renamer.py` applies the user's template string (e.g. `{serie} {temporada}x{episodio:02d} {titulo}{ext}`)
5. `rename_file()` performs the disk rename
6. `FTPClient.upload_file()` uploads to the configured server

### Template variables

Available in naming templates: `{serie}`, `{titulo}`, `{temporada}`, `{episodio}`, `{año}`, `{ext}`. Python format spec works: `{episodio:02d}`, `{episodio:03d}`, etc.

FTP path templates support: `{serie}`, `{temporada}`, `{temporada:02d}`, `{año}`, `{tipo}`.

### Threading rules

tkinter is not thread-safe. All GUI mutations from worker threads must be scheduled via `self.after(0, lambda: ...)`. Worker threads are used for: TMDB searches (`ThreadPoolExecutor`, max 5), FTP uploads (`ThreadPoolExecutor`, max 5 parallel connections), AutoWatcher polling, poster image loading.

### Config and persistence

- Config: `%APPDATA%\aRenombrar\config.json` (Windows) / `~/.config/aRenombrar/config.json` (Linux)
- AutoWatcher processed-files DB: `%APPDATA%\aRenombrar\auto_processed.json`
- The `Config` class merges saved values over `DEFAULTS` in `config.py` — add new settings to `DEFAULTS` first

### FTP encoding quirk

`FTPClient.connect()` starts with `latin-1` encoding (not the Python 3.9+ default of UTF-8), then upgrades to UTF-8 only if the server confirms it via `FEAT`. This avoids garbled Spanish characters on common NAS servers (vsftpd, ProFTPD).

### AutoWatcher

Polls the watch folder every `poll_interval` seconds (default 10). For each new video file it:
1. Waits 6 s for the file size to stabilize (detects incomplete downloads)
2. Calls `detect_episode()` + TMDB search
3. Renames the file
4. Optionally moves to `procesados/` subfolder or deletes
5. Uploads via FTP if configured
6. Persists the result to `auto_processed.json` so files aren't reprocessed

Launches minimized to tray when started with `--minimized` argument (used for Windows startup entry).
