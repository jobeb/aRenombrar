# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**aRenombrar** is a desktop GUI app for renaming and uploading media files (TV series, movies, anime). It queries TMDB for metadata, generates clean filenames from configurable templates, and uploads via FTP/FTPS. Written in Python with customtkinter. Originally Windows-only; macOS is now supported too (see "Platform notes" below) — Linux is untested but likely mostly works since all path/platform handling goes through `core/appdirs.py`.

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

The spec bundles `customtkinter`, `PIL`, `tkinterdnd2`, and `pystray` assets, and picks the icon by platform (`iconoPrincipal.ico` on Windows, `iconoPrincipal.icns` on macOS — both must be present in `aRenombrar/`; regenerate the `.icns` from `IconoSinFondo.png` with `Image.open(...).save('iconoPrincipal.icns')` if the source art changes, no macOS-only tool needed).

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

Polls the watch folder every `poll_interval` seconds (default 10). For each new video file it:
1. Waits 6 s for the file size to stabilize (detects incomplete downloads)
2. Calls `detect_episode()` + TMDB search
3. Renames the file
4. Optionally moves to `procesados/` subfolder or deletes
5. Uploads via FTP if configured
6. Persists the result to `auto_processed.json` so files aren't reprocessed

Launches minimized to tray when started with `--minimized` argument (used by the autostart entry — Windows Registry `Run` key or macOS LaunchAgent, see below).

## Platform notes

- **Tray icon (`pystray`)**: on macOS, `Icon.run()` must be called from the main thread (AppKit constraint, not optional) — `gui/app.py:_minimize_to_tray` branches on `core.appdirs.is_macos()` and calls `Icon.run_detached()` directly instead of spawning a background thread like Windows/Linux do. `run_detached()` on the darwin backend doesn't block; it just marks the icon ready and relies on Tk's own already-running Cocoa event loop (same shared `NSApplication` instance) to actually deliver clicks. If you touch tray code, keep that branch — don't unify it back into a single threaded `.run()` call.
- **Autostart**: `App._set_autostart` dispatches to `_set_autostart_windows` (Registry `HKCU\...\Run`) or `_set_autostart_macos` (`~/Library/LaunchAgents/com.arenombrar.app.plist`, loaded with `launchctl load -w`). Not implemented for Linux (no single standard mechanism) — the Settings switch is disabled there.
- **Desktop notifications**: `App._send_notification` tries pystray's tray notification first, then falls back to a platform-specific OS call — PowerShell balloon on Windows, `osascript -e 'display notification ...'` on macOS. No Linux fallback yet.
- **Window/dialog icon**: Tk's `iconbitmap()` only accepts `.ico` and only works on Windows; macOS/Linux use `iconphoto()` with a PNG (`IconoSinFondo.png`) instead. See `App.__init__` (loads `self._icon_path` or `self._icon_photo` depending on platform) and `App._apply_icon`.
- **"Archivo bloqueado" retry in AutoWatcher**: the substring match in `core/auto_watcher.py` (`_LOCKED_FILE_HINTS`) was originally Windows-only (`WinError 32` text). It now also matches common POSIX phrasing (`EBUSY`, "permission denied"), though this scenario is rare on macOS/Linux since POSIX generally allows renaming open files.
