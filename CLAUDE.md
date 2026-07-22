# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**aRenombrar** is a desktop GUI app for renaming and uploading media files (TV series, movies, anime, and books/comics). It queries TMDB for series/movie metadata, OpenLibrary (primary) with Google Books as automatic backup for ebooks, and ComicVine for comics/manga, generates clean filenames from configurable templates, and uploads via FTP/FTPS. Written in Python with customtkinter. Runs on Windows, macOS, and Linux (see "Platform notes" below).

## Running the app

All commands are run from the `aRenombrar/` subdirectory:

```bash
cd aRenombrar
python main.py               # launch normally (Windows: `python`, macOS/Linux: usually `python3`)
python diagnostico.py        # launch with diagnostics written to diagnostico.log
python crear_acceso_directo.py  # create a Desktop shortcut (Windows only)
```

Install dependencies with `pip install -r requirements.txt` (all installer scripts — `instalar.bat`/`instalar.ps1` on Windows, `instalar_mac.sh` on macOS — install from this same file; don't hand-list packages in those scripts, it drifts and silently drops deps like `keyring`, which is a hard, unguarded import in `config.py` — missing it means the app won't even open).

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
# Windows → dist/aRenombrar/aRenombrar.exe
# macOS   → dist/aRenombrar.app (BUNDLE block in the spec, only added when built on macOS)
```

The spec bundles `customtkinter`, `PIL`, `tkinterdnd2`, `pystray`, `py7zr`, and `rarfile` assets, and picks the icon by platform (`iconoPrincipal.ico` on Windows, `iconoPrincipal.icns` on macOS — both must be present in `aRenombrar/`; regenerate the `.icns` from `IconoSinFondo.png` with `Image.open(...).save('iconoPrincipal.icns')` if the source art changes, no macOS-only tool needed). `py7zr`/`rarfile` are pure-Python (no binary bundled) — `.rar` extraction still needs `unrar`/`unar`/`bsdtar` installed on the end user's system; if absent, extraction fails with a clear message instead of crashing.

## Architecture

```
aRenombrar/
├── main.py          # entry point — creates and starts App
├── config.py        # Config class, persists to %APPDATA%\aRenombrar\config.json
├── core/
│   ├── api_client.py     # TMDBClient, MediaInfo dataclass, detect_episode()
│   ├── openlibrary_client.py # OpenLibraryClient — PRIMARY ebook identification, no API key ever needed
│   ├── book_client.py    # GoogleBooksClient — automatic ebook fallback when OpenLibrary fails/finds nothing
│   ├── book_identify.py  # identify_book_or_comic() — shared identification logic (OpenLibrary→Google Books fallback, ComicVine + AI title translation), used by both the GUI search panel and AutoWatcher
│   ├── comicvine_client.py # ComicVineClient — comic/manga identification (cbz/cbr)
│   ├── ai_title_fallback.py # Groq-based title cleanup (video) and comic title translation (ES→EN) fallback
│   ├── learned_terms.py / learned_comic_titles.py # persisted caches for the AI fallbacks above
│   ├── archive_extract.py # extract_archive() — .zip/.7z/.rar/.tar(+.gz/.bz2/.xz variants), zip-slip safe
│   ├── renamer.py        # build_new_name(), rename_file(), is_video_file(), is_book_file(), is_archive_file()
│   ├── ftp_client.py     # FTPClient — upload with progress, speed limit, resume
│   └── auto_watcher.py   # AutoWatcher — recursive folder polling thread; identifies video AND books/comics, optionally auto-extracts archives (see "auto_extract_archives" setting)
└── gui/
    └── app.py          # entire UI in one App class (~2200 lines)
```

### Data flow

1. Files are added (drag-drop, picker, "Add folder", or AutoWatcher) — all four entry points recognize video, book/comic, and archive files, and recursively scan subfolders; any `.zip`/`.7z`/`.rar`/`.tar` found is decompressed (via `core/archive_extract.py`, nested archives resolved too) before the resulting files are added — manual add always extracts, AutoWatcher only if "auto_extract_archives" is enabled in Settings
2. `detect_episode()` in `api_client.py` parses the filename using regex patterns to extract title, season, episode, and media type (tv/movie/anime); book/comic files (`is_book_file()`/`is_comic_file()` in `renamer.py`) skip these regexes entirely and get `media_type="libro"` directly, with comics additionally getting an issue number parsed from a `#NN` pattern (or a bare trailing number, with any number of trailing `[...]`/`(...)` credit groups after it) in the filename
3. Identification is dispatched by file type: `TMDBClient.search_multi()`/`build_media_info()` for video; for books/comics, `core/book_identify.py::identify_book_or_comic()` tries `OpenLibraryClient.search_volumes()` first (no key needed), falls back to `GoogleBooksClient.search_volumes()` if OpenLibrary errors or finds nothing, or `ComicVineClient.search_volumes()` for comics (retrying with an AI-translated English title if the Spanish-derived title finds nothing) — all produce a `MediaInfo` dataclass (`media_type="libro"` for both books and comics, distinguished by `genre_ids` containing `"ebook"` or `"comic"`)
4. `build_new_name()` in `renamer.py` (via `build_name_for_media_info()`, shared by the GUI and AutoWatcher) applies the user's template string (e.g. `{serie} {temporada}x{episodio:02d} {titulo}{ext}`) — which template (tv/movie/anime/libro/comic) is chosen based on `media_type` and, for `"libro"`, the `genre_ids` ebook/comic distinction
5. `rename_file()` performs the disk rename
6. `FTPClient.upload_file()` uploads to the configured server

### Multi-select and bulk assign (Archivos tab)

The file table supports Ctrl+click / Shift+click multi-select (Explorer-style), independent of the single "anchor" entry that drives the search/detail panel (`self._selected_entry`; additional entries live in `self._multi_selected`). The "Asignar" button becomes "Asignar a la selección (N)" when more than one file is selected, applying the same chosen search result to every selected file while each keeps its own already-detected episode/issue number (`core/book_identify.py`'s per-entry `det` parameter already supported this; the bulk path just calls it once per selected file instead of once for the anchor). Mismatched types (e.g. a video mixed into a comic selection) are skipped with a warning, not force-applied.

### Template variables

Available in naming templates: `{serie}`, `{titulo}`, `{temporada}`, `{episodio}`, `{año}`, `{ext}`. Python format spec works: `{episodio:02d}`, `{episodio:03d}`, etc. For books, `{episodio}` holds the comic issue number (parsed from the filename) when applicable — ebooks and comics reuse the same variable set, no book-specific variables (e.g. no `{autor}`) were added; default comic format is `{serie} ({año}) #{episodio:02d}{ext}`, confirmed against real files on a user's server.

FTP path templates support: `{serie}`, `{temporada}`, `{temporada:02d}`, `{año}`, `{tipo}` (`"Series"`/`"Películas"`/`"Libros"`).

### Threading rules

tkinter is not thread-safe. All GUI mutations from worker threads must be scheduled via `self.after(0, lambda: ...)`. Worker threads are used for: TMDB searches (`ThreadPoolExecutor`, max 5), FTP uploads (`ThreadPoolExecutor`, max 5 parallel connections), AutoWatcher polling, poster image loading.

### Config and persistence

- Data dir via `core/appdirs.py:app_data_dir()` — the single source of truth for where app files live, used by `config.py`, `gui/app.py`, and `core/auto_watcher.py` alike (they used to each have their own copy of this logic, and only `config.py`'s distinguished macOS from Linux, so macOS installs ended up with files split across two different folders — don't reintroduce a local copy of this logic, import it):
  - Windows → `%APPDATA%\aRenombrar\`
  - macOS → `~/Library/Application Support/aRenombrar/`
  - Linux → `~/.config/aRenombrar/`
- `config.json`, `session.json`, `upload_history.json`, `auto_processed.json`, `auto_watcher.log` all live directly in that folder.
- The `Config` class merges saved values over `DEFAULTS` in `config.py` — add new settings to `DEFAULTS` first
- FTP password is stored via `keyring` (Windows Credential Locker / macOS Keychain / whatever `keyring` resolves on Linux), never in `config.json` in plaintext.

### FTP encoding quirk

`FTPClient.connect()` starts with `latin-1` encoding (not the Python 3.9+ default of UTF-8), then upgrades to UTF-8 only if the server confirms it via `FEAT`. This avoids garbled Spanish characters on common NAS servers (vsftpd, ProFTPD).

### AutoWatcher

Polls the watch folder every `poll_interval` seconds (default 10), recursively (subfolders included, excluding any `procesados/` subfolder at any depth). If "auto_extract_archives" is enabled in Settings, `.zip`/`.7z`/`.rar`/`.tar` files found are decompressed first (nested archives resolved across scan cycles); the resulting video/book/comic files are picked up on a later cycle like any other file. For each new video/book/comic file it:
1. Waits 6 s for the file size to stabilize (detects incomplete downloads)
2. Calls `detect_episode()` + TMDB search (video) or `core/book_identify.py::identify_book_or_comic()` (books/comics) — for books/comics, `min_confidence` is enforced just like video (unlike the manual search panel, where the user reviews before assigning)
3. Renames the file
4. Optionally moves to `procesados/` subfolder or deletes
5. Uploads via FTP if configured
6. Persists the result to `auto_processed.json` so files aren't reprocessed

Launches minimized to tray when started with `--minimized` argument (used by the autostart entry — Windows Registry `Run` key or macOS LaunchAgent, see below).

## Platform notes

- **Tray icon (`pystray`)**: on macOS, `Icon.run()` must be called from the main thread (AppKit constraint, not optional) — `gui/app.py:_minimize_to_tray` branches on `core.appdirs.is_macos()` and calls `Icon.run_detached()` directly instead of spawning a background thread like Windows/Linux do. `run_detached()` on the darwin backend doesn't block; it just marks the icon ready and relies on Tk's own already-running Cocoa event loop (same shared `NSApplication` instance) to actually deliver clicks. If you touch tray code, keep that branch — don't unify it back into a single threaded `.run()` call.
- **Autostart**: `App._set_autostart` dispatches to `_set_autostart_windows` (Registry `HKCU\...\Run`), `_set_autostart_macos` (`~/Library/LaunchAgents/com.arenombrar.app.plist`, loaded with `launchctl load -w`), or `_set_autostart_linux` (`~/.config/autostart/arenombrar-autostart.desktop`, the XDG autostart convention).
- **Desktop notifications**: `App._send_notification` tries pystray's tray notification first, then falls back to a platform-specific OS call — PowerShell balloon on Windows, `osascript -e 'display notification ...'` on macOS, `notify-send` on Linux.
- **Window/dialog icon**: Tk's `iconbitmap()` only accepts `.ico` and only works on Windows; macOS/Linux use `iconphoto()` with a PNG (`IconoSinFondo.png`) instead. See `App.__init__` (loads `self._icon_path` or `self._icon_photo` depending on platform) and `App._apply_icon`.
- **"Archivo bloqueado" retry in AutoWatcher**: the substring match in `core/auto_watcher.py` (`_LOCKED_FILE_HINTS`) was originally Windows-only (`WinError 32` text). It now also matches common POSIX phrasing (`EBUSY`, "permission denied"), though this scenario is rare on macOS/Linux since POSIX generally allows renaming open files.
