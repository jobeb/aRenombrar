"""
Ventana principal de aRenombrar.
Tabs: Archivos | FTP | Configuracion
Sin ventanas emergentes: todo integrado en la ventana principal.
Progreso FTP integrado como columnas en la lista de archivos.
"""

import difflib
import json
import os
import subprocess
import sys
import threading
import time as _time

try:
    import pystray
    from PIL import Image as _PILImage
    _PYSTRAY_AVAILABLE = True
except ImportError:
    _PYSTRAY_AVAILABLE = False
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Optional

import customtkinter as ctk
from PIL import Image, ImageTk
import requests
from io import BytesIO

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    _DND_AVAILABLE = True
except ImportError:
    _DND_AVAILABLE = False

from config import Config
from core.api_client import TMDBClient, detect_episode, MediaInfo
from core.renamer import build_new_name, rename_file, is_video_file, get_extension
from core.ftp_client import FTPClient, _ftp_safe
from core.auto_watcher import AutoWatcher
from core.series_match import best_match
from core.ftp_categories import choose_category, new_category_id
from core.upload_slots import UploadSlotManager


ACCENT        = "#1DB954"
ACCENT_HOVER  = "#17a349"
ERROR_COLOR   = "#e74c3c"
WARNING_COLOR = "#f39c12"
SUCCESS_COLOR = "#2ecc71"
PENDING_COLOR = "#95a5a6"
QUEUED_COLOR  = "#3498db"
SELECTED_ROW_COLOR = ("#c8e6d0", "#204a34")   # (modo claro, modo oscuro) — fila seleccionada
STATUS_LABELS = {"en_cola": "En cola",
                  "esperando_confirmacion": "Esperando confirmación"}   # textos de estado que no quedan bien con .capitalize()


def _status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status.capitalize())


def _truncate(text, max_len):
    return text if len(text) <= max_len else text[:max_len - 1] + "..."

def _fit_text(text: str, px_width: int, font) -> str:
    """Trunca texto para que quepa en px_width píxeles, usando el ancho real de *font*.
    CTkLabel usa width como mínimo, no máximo: si el texto es más ancho el widget se expande
    y desplaza las columnas. Este helper evita ese desbordamiento sin desperdiciar espacio."""
    if not text or px_width <= 0:
        return ""
    if font.measure(text) <= px_width:
        return text
    ellipsis = "…"
    ell_w = font.measure(ellipsis)
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.measure(text[:mid]) + ell_w <= px_width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + ellipsis if lo > 0 else ellipsis


def _fmt_speed(bps):
    if bps >= 1048576:
        return f"{bps / 1048576:.1f} MB/s"
    return f"{bps / 1024:.0f} KB/s"


def _fmt_size(nbytes):
    for unit, divisor in (("TB", 1024**4), ("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if nbytes >= divisor:
            return f"{nbytes / divisor:.1f} {unit}"
    return f"{nbytes} B"


class _OverwriteDialog(ctk.CTkToplevel):
    """Diálogo modal con tres opciones: Sobreescribir / Omitir / Sobreescribir todos."""
    def __init__(self, parent, filename: str,
                 title="Archivo ya existe",
                 message="El archivo ya existe en el servidor:"):
        super().__init__(parent)
        parent._apply_icon(self)
        self.result = None          # "overwrite" | "skip" | "all"
        self.title(title)
        self.resizable(False, False)
        self.grab_set()
        self.lift()
        self.attributes("-topmost", True)
        # Centrar en la ventana padre
        self.update_idletasks()
        pw = parent.winfo_rootx() + parent.winfo_width() // 2
        ph = parent.winfo_rooty() + parent.winfo_height() // 2
        dw, dh = 430, 170
        self.geometry(f"{dw}x{dh}+{pw - dw//2}+{ph - dh//2}")

        ctk.CTkLabel(self, text=message,
                     font=ctk.CTkFont(size=13)).pack(padx=24, pady=(20, 4))
        ctk.CTkLabel(self, text=filename, font=ctk.CTkFont(size=11),
                     text_color="#95a5a6", wraplength=380).pack(padx=24, pady=(0, 16))

        bf = ctk.CTkFrame(self, fg_color="transparent")
        bf.pack(padx=24, pady=(0, 20))
        ctk.CTkButton(bf, text="Sobreescribir",
                      fg_color="#e67e22", hover_color="#ca6f1e", width=130,
                      command=lambda: self._close("overwrite")).pack(side="left", padx=4)
        ctk.CTkButton(bf, text="Omitir",
                      fg_color="transparent", border_width=1, width=100,
                      command=lambda: self._close("skip")).pack(side="left", padx=4)
        ctk.CTkButton(bf, text="Sobreescribir todos",
                      fg_color="#c0392b", hover_color="#96281b", width=150,
                      command=lambda: self._close("all")).pack(side="left", padx=4)

        self.protocol("WM_DELETE_WINDOW", lambda: self._close("skip"))
        self.wait_window()

    def _close(self, result: str):
        self.result = result
        self.destroy()


class _CloseDialog(ctk.CTkToplevel):
    """Diálogo modal al intentar cerrar con subida en progreso."""
    def __init__(self, parent):
        super().__init__(parent)
        parent._apply_icon(self)
        self.result = None   # "cancel" | "close"
        self.title("Subida en progreso")
        self.resizable(False, False)
        self.grab_set()
        self.lift()
        self.attributes("-topmost", True)
        self.update_idletasks()
        pw = parent.winfo_rootx() + parent.winfo_width() // 2
        ph = parent.winfo_rooty() + parent.winfo_height() // 2
        dw, dh = 380, 150
        self.geometry(f"{dw}x{dh}+{pw - dw//2}+{ph - dh//2}")

        ctk.CTkLabel(self, text="Hay una subida FTP en progreso.",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(padx=24, pady=(22, 4))
        ctk.CTkLabel(self, text="¿Qué quieres hacer?",
                     font=ctk.CTkFont(size=11), text_color="#95a5a6").pack(padx=24, pady=(0, 18))

        bf = ctk.CTkFrame(self, fg_color="transparent")
        bf.pack(padx=24, pady=(0, 20))
        ctk.CTkButton(bf, text="Seguir subiendo", width=140,
                      fg_color="transparent", border_width=1,
                      command=lambda: self._close("cancel")).pack(side="left", padx=6)
        ctk.CTkButton(bf, text="Cerrar aplicación", width=140,
                      fg_color="#c0392b", hover_color="#96281b",
                      command=lambda: self._close("close")).pack(side="left", padx=6)

        self.protocol("WM_DELETE_WINDOW", lambda: self._close("cancel"))
        self.wait_window()

    def _close(self, result: str):
        self.result = result
        self.destroy()


class _ClearDialog(ctk.CTkToplevel):
    """Diálogo modal al limpiar la lista con una subida FTP en progreso."""
    def __init__(self, parent, pendientes: int):
        super().__init__(parent)
        parent._apply_icon(self)
        self.result = None   # "solo_subidos" | "todo" | None (cancelado)
        self.title("Subida en progreso")
        self.resizable(False, False)
        self.grab_set()
        self.lift()
        self.attributes("-topmost", True)
        self.update_idletasks()
        pw = parent.winfo_rootx() + parent.winfo_width() // 2
        ph = parent.winfo_rooty() + parent.winfo_height() // 2
        dw, dh = 420, 160
        self.geometry(f"{dw}x{dh}+{pw - dw//2}+{ph - dh//2}")

        plural = "s" if pendientes != 1 else ""
        ctk.CTkLabel(self, text="Hay una subida FTP en progreso.",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(padx=24, pady=(22, 4))
        ctk.CTkLabel(self, text=f"Aún queda{'n' if pendientes != 1 else ''} {pendientes} archivo{plural} por subir. ¿Qué quieres hacer?",
                     font=ctk.CTkFont(size=11), text_color="#95a5a6", wraplength=370).pack(padx=24, pady=(0, 18))

        bf = ctk.CTkFrame(self, fg_color="transparent")
        bf.pack(padx=24, pady=(0, 20))
        ctk.CTkButton(bf, text="Limpiar solo subidos", width=160,
                      fg_color="transparent", border_width=1,
                      command=lambda: self._close("solo_subidos")).pack(side="left", padx=6)
        ctk.CTkButton(bf, text="Limpiar todo y cancelar", width=160,
                      fg_color="#c0392b", hover_color="#96281b",
                      command=lambda: self._close("todo")).pack(side="left", padx=6)

        self.protocol("WM_DELETE_WINDOW", lambda: self._close(None))
        self.wait_window()

    def _close(self, result):
        self.result = result
        self.destroy()


class _SeriesMatchDialog(ctk.CTkToplevel):
    """Diálogo modal: ¿la serie a subir es la misma que una carpeta parecida
    que ya existe en el FTP? (idioma, artículo, nombre corto vs largo...)"""
    def __init__(self, parent, desired: str, existing: str):
        super().__init__(parent)
        parent._apply_icon(self)
        self.result = None   # "yes" | "no"
        self.title("Carpeta de serie existente")
        self.resizable(False, False)
        self.grab_set()
        self.lift()
        self.attributes("-topmost", True)
        self.update_idletasks()
        pw = parent.winfo_rootx() + parent.winfo_width() // 2
        ph = parent.winfo_rooty() + parent.winfo_height() // 2
        dw, dh = 440, 200
        self.geometry(f"{dw}x{dh}+{pw - dw//2}+{ph - dh//2}")

        ctk.CTkLabel(self, text="Ya existe una carpeta con un nombre parecido en el FTP.",
                     font=ctk.CTkFont(size=13, weight="bold"), wraplength=390).pack(padx=24, pady=(20, 8))
        ctk.CTkLabel(self, text=f"Serie a subir:\n{desired}\n\nCarpeta existente:\n{existing}",
                     font=ctk.CTkFont(size=11), justify="left",
                     text_color="#95a5a6", wraplength=390).pack(padx=24, pady=(0, 10))
        ctk.CTkLabel(self, text="¿Es la misma serie? Si respondes que sí, se subirá dentro de esa carpeta.",
                     font=ctk.CTkFont(size=11), text_color="#95a5a6", wraplength=390).pack(padx=24, pady=(0, 14))

        bf = ctk.CTkFrame(self, fg_color="transparent")
        bf.pack(padx=24, pady=(0, 20))
        ctk.CTkButton(bf, text="Sí, usar esa carpeta", width=170,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER,
                      command=lambda: self._close("yes")).pack(side="left", padx=6)
        ctk.CTkButton(bf, text="No, crear carpeta nueva", width=170,
                      fg_color="transparent", border_width=1,
                      command=lambda: self._close("no")).pack(side="left", padx=6)

        self.protocol("WM_DELETE_WINDOW", lambda: self._close("no"))
        self.wait_window()

    def _close(self, result: str):
        self.result = result
        self.destroy()


class _UnsavedSettingsDialog(ctk.CTkToplevel):
    """Diálogo modal al salir de Ajustes con cambios sin guardar."""
    def __init__(self, parent):
        super().__init__(parent)
        parent._apply_icon(self)
        self.result = None   # "save" | "discard" | None (cancelado, seguir en Ajustes)
        self.title("Cambios sin guardar")
        self.resizable(False, False)
        self.grab_set()
        self.lift()
        self.attributes("-topmost", True)
        self.update_idletasks()
        pw = parent.winfo_rootx() + parent.winfo_width() // 2
        ph = parent.winfo_rooty() + parent.winfo_height() // 2
        dw, dh = 400, 160
        self.geometry(f"{dw}x{dh}+{pw - dw//2}+{ph - dh//2}")

        ctk.CTkLabel(self, text="Tienes cambios sin guardar en Ajustes.",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(padx=24, pady=(22, 4))
        ctk.CTkLabel(self, text="¿Qué quieres hacer?",
                     font=ctk.CTkFont(size=11), text_color="#95a5a6").pack(padx=24, pady=(0, 18))

        bf = ctk.CTkFrame(self, fg_color="transparent")
        bf.pack(padx=24, pady=(0, 20))
        ctk.CTkButton(bf, text="Guardar y salir", width=140,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER,
                      command=lambda: self._close("save")).pack(side="left", padx=6)
        ctk.CTkButton(bf, text="Salir sin guardar", width=150,
                      fg_color="transparent", border_width=1,
                      command=lambda: self._close("discard")).pack(side="left", padx=6)

        self.protocol("WM_DELETE_WINDOW", lambda: self._close(None))
        self.wait_window()

    def _close(self, result):
        self.result = result
        self.destroy()


class FileEntry:
    def __init__(self, path):
        # Normalizar separadores (str(Path(...)) usa "\" en Windows) — sin
        # esto, la MISMA ruta con "/" y con "\" se trata como dos archivos
        # distintos en cualquier comparación por igualdad de texto (duplica
        # filas al detectar el mismo archivo dos veces, y hace fallar la
        # búsqueda en auto_processed.json, cuyas claves siempre usan "\").
        self.path         = str(Path(path))
        self.name         = Path(path).name
        self.ext          = get_extension(path)
        self.detected     = detect_episode(self.name)
        self.media_info   = None
        self.new_name     = ""
        self.status       = "pendiente"
        self.error_msg    = ""
        self.ftp_progress = 0.0
        self.ftp_speed    = 0.0
        self.ftp_status   = ""
        self.confidence   = 0   # 0-100 porcentaje de confianza en la detección TMDB

    def to_dict(self) -> dict:
        status = self.status
        if status in ("buscando", "subiendo", "auto", "en_cola"):
            status = "pendiente"
        return {
            "path":       self.path,
            "new_name":   self.new_name,
            "status":     status,
            "confidence": self.confidence,
            "media_info": _mediainfo_to_dict(self.media_info) if self.media_info else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FileEntry":
        entry = cls(d["path"])
        entry.new_name   = d.get("new_name", "")
        entry.status     = d.get("status", "pendiente")
        entry.confidence = d.get("confidence", 0)
        mi = d.get("media_info")
        if mi:
            from core.api_client import MediaInfo
            entry.media_info = MediaInfo(
                tmdb_id        = mi.get("tmdb_id", 0),
                media_type     = mi.get("media_type", "tv"),
                title          = mi.get("title", ""),
                original_title = mi.get("original_title", ""),
                year           = mi.get("year", ""),
                poster_url     = mi.get("poster_url"),
                season         = mi.get("season"),
                episode        = mi.get("episode"),
                episode_title  = mi.get("episode_title"),
                overview       = mi.get("overview", ""),
                genre_ids      = mi.get("genre_ids", []),
            )
        return entry


_STATUS_RANK = {
    "subido": 5, "renombrado": 4, "listo": 3, "en_cola": 3, "subiendo": 3,
    "auto": 2, "omitido": 1, "error": 1, "pendiente": 0, "buscando": 0,
}


def _dedupe_entries(entries: list) -> list:
    """Colapsa entradas con el mismo entry.path (normalizado) en una sola —
    puede haber duplicados en session.json de antes de normalizar
    separadores de ruta (la misma ruta con "/" y con "\\" se guardaba como
    dos archivos distintos). Se queda con la de estado más avanzado; en
    empate, con la que tenga media_info."""
    best = {}
    order = []
    for e in entries:
        if e.path not in best:
            best[e.path] = e
            order.append(e.path)
            continue
        cur = best[e.path]
        rank_new = _STATUS_RANK.get(e.status, 0)
        rank_cur = _STATUS_RANK.get(cur.status, 0)
        if rank_new > rank_cur or (rank_new == rank_cur and e.media_info and not cur.media_info):
            best[e.path] = e
    return [best[k] for k in order]


def _mediainfo_to_dict(info) -> dict:
    return {
        "tmdb_id":        info.tmdb_id,
        "media_type":     info.media_type,
        "title":          info.title,
        "original_title": info.original_title,
        "year":           info.year,
        "poster_url":     info.poster_url,
        "season":         info.season,
        "episode":        info.episode,
        "episode_title":  info.episode_title,
        "overview":       info.overview,
        "genre_ids":      info.genre_ids,
    }


def _appdata_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path.home() / ".config"
    p = base / "aRenombrar"
    p.mkdir(parents=True, exist_ok=True)
    return p


if _DND_AVAILABLE:
    _AppBase = type("_AppBase", (ctk.CTk, TkinterDnD.DnDWrapper), {})
else:
    _AppBase = ctk.CTk


class App(_AppBase):
    def __init__(self):
        super().__init__()
        # Activar drag & drop si tkinterdnd2 esta disponible
        if _DND_AVAILABLE:
            self.TkdndVersion = TkinterDnD._require(self)
        self.config_data = Config()
        self.tmdb = TMDBClient(self.config_data["tmdb_api_key"])
        self.ftp  = FTPClient()
        self.files = []
        self._selected_entry = None

        self._upload_queue          = []
        self._upload_cancel         = threading.Event()
        self._upload_skip           = threading.Event()
        self._upload_running        = False
        self._upload_current_idx    = -1
        self._upload_current_remote = ""
        self._upload_overwrite_all  = False
        self._rename_overwrite_all  = False
        self._upload_slot_of        = {}
        self._upload_skip_events    = []

        # Reutilización de carpetas de serie ya existentes en el FTP (evita
        # crear una carpeta duplicada por idioma/nombre corto distinto)
        self._series_folder_cache = {}
        self._ftp_dir_cache       = {}
        self._series_folder_lock  = threading.Lock()

        self._watcher: AutoWatcher = None
        self._tray        = None
        self._tray_running = False
        self._upload_history: list = []   # historial de subidas en memoria
        self._history_lock = threading.Lock()   # evita carreras al escribir upload_history.json
        # Cupo de subidas simultáneas COMPARTIDO entre la cola manual y el modo
        # automático — "Subidas simultáneas" en Ajustes limita el total real,
        # no cada origen por separado (ver core/upload_slots.py).
        self._upload_slots = UploadSlotManager(self.config_data)

        ctk.set_appearance_mode(self.config_data.get("appearance", "dark"))
        ctk.set_default_color_theme(self.config_data.get("color_theme", "blue"))

        self.title("aRenombrar")
        self.geometry("1200x820")
        self.minsize(1000, 680)

        self._icon_path = None
        try:
            if getattr(sys, "frozen", False):
                base = sys._MEIPASS
            else:
                base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            ico = os.path.join(base, "iconoPrincipal.ico")
            if os.path.exists(ico):
                self._icon_path = ico
        except Exception:
            pass
        self._apply_icon(self)
        if self._icon_path:
            # CTk puede resetear el icono; re-aplicar tras el primer frame
            self.after(50, lambda: self._apply_icon(self))

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._setup_tray()
        self._load_session()   # restaurar lista de archivos de la sesión anterior
        self._cleanup_stale_uploading_marks()   # ver docstring: limpia marcas "subiendo" de una sesión anterior cerrada a medias
        if "--minimized" in sys.argv:
            self.after(200, self._minimize_to_tray)
            # Arrancar modo automático si hay carpeta configurada
            self.after(400, self._auto_start_if_configured)

    def _apply_icon(self, window):
        """Aplica el icono de la app a *window* (ventana principal o cualquier
        popup/diálogo Toplevel) — iconbitmap(default=...) no se propaga de
        forma fiable a los Toplevel hijos, así que se aplica explícitamente."""
        if self._icon_path:
            try:
                window.iconbitmap(self._icon_path)
            except Exception:
                pass

    # ------------------------------------------------------------------ UI --

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(2, weight=0)

        self._build_header()

        self._main_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._main_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 4))
        self._main_frame.grid_columnconfigure(0, weight=1)
        self._main_frame.grid_rowconfigure(0, weight=1)

        # Vista de archivos (siempre presente, se oculta al abrir config)
        self._files_frame = ctk.CTkFrame(self._main_frame, fg_color="transparent")
        self._files_frame.grid(row=0, column=0, sticky="nsew")
        self._files_frame.grid_columnconfigure(0, weight=1)
        self._build_files_tab(self._files_frame)

        # Panel de configuracion (oculto por defecto)
        self._config_panel_frame = ctk.CTkFrame(self._main_frame, fg_color="transparent")
        self._config_panel_frame.grid_columnconfigure(0, weight=1)
        self._config_panel_frame.grid_rowconfigure(0, weight=1)
        self._config_visible = False
        self._build_config_panel(self._config_panel_frame)

        self._build_status_bar()

        # Barra de aviso de cierre (oculta normalmente)


    def _build_header(self):
        header = ctk.CTkFrame(self, height=56, corner_radius=0)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(header, text="  aRenombrar",
                     font=ctk.CTkFont(size=20, weight="bold")).grid(
            row=0, column=0, padx=20, pady=10)
        self._status_lbl = ctk.CTkLabel(header, text="", text_color=ACCENT,
                                         font=ctk.CTkFont(size=13))
        self._status_lbl.grid(row=0, column=1, padx=10)
        self._config_btn = ctk.CTkButton(
            header, text="⚙  Configuración", width=130, height=30,
            fg_color="transparent", border_width=1,
            command=self._toggle_config)
        self._config_btn.grid(row=0, column=2, padx=(0, 4))
        self._save_settings_btn = ctk.CTkButton(
            header, text="💾 Guardar configuración", width=170, height=30,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            command=self._save_all_settings)
        self._save_settings_btn.grid(row=0, column=3, padx=(0, 4))
        self._save_settings_btn.grid_remove()   # solo visible dentro de Ajustes
        ctk.CTkButton(
            header, text="📋 Historial", width=100, height=30,
            fg_color="transparent", border_width=1,
            command=self._show_history).grid(row=0, column=4, padx=(0, 8))
        self._auto_btn = ctk.CTkButton(
            header, text="⚡ Auto", width=90, height=30,
            fg_color="transparent", border_width=1,
            command=self._toggle_auto)
        self._auto_btn.grid(row=0, column=5, padx=(0, 8))
        self._tray_btn = ctk.CTkButton(
            header, text="⊟", width=36, height=30,
            fg_color="transparent", border_width=1,
            command=self._minimize_to_tray)
        self._tray_btn.grid(row=0, column=6, padx=(0, 16))

    def _build_status_bar(self):
        bar = ctk.CTkFrame(self, height=28, corner_radius=0)
        bar.grid(row=2, column=0, sticky="ew")
        bar.grid_columnconfigure(1, weight=1)
        self._status_bar_left = ctk.CTkLabel(
            bar, text="", font=ctk.CTkFont(size=11), text_color=PENDING_COLOR, anchor="w")
        self._status_bar_left.grid(row=0, column=0, sticky="w", padx=12, pady=4)
        self._status_bar_right = ctk.CTkLabel(
            bar, text="", font=ctk.CTkFont(size=11), text_color=PENDING_COLOR, anchor="e")
        self._status_bar_right.grid(row=0, column=2, sticky="e", padx=12, pady=4)
        self._update_status_bar()

    def _update_status_bar(self):
        n = len(self.files)
        if n == 0:
            self._status_bar_left.configure(text="Sin archivos en la lista")
        else:
            total_size = 0
            counts = {}
            for e in self.files:
                counts[e.status] = counts.get(e.status, 0) + 1
                try:
                    total_size += Path(e.path).stat().st_size
                except OSError:
                    pass
            labels = {
                "pendiente": "pendientes", "buscando": "buscando", "listo": "listos",
                "renombrado": "renombrados", "en_cola": "en cola para subir",
                "subiendo": "subiendo", "subido": "subidos",
                "error": "con error", "auto": "en automático", "omitido": "omitidos",
            }
            breakdown = " · ".join(f"{cnt} {labels.get(st, st)}" for st, cnt in counts.items() if cnt)
            parts = [f"{n} archivo{'s' if n != 1 else ''}", _fmt_size(total_size)]
            if breakdown:
                parts.append(breakdown)
            self._status_bar_left.configure(text="   ·   ".join(parts))

        if self._selected_entry is not None:
            e = self._selected_entry
            try:
                size_str = _fmt_size(Path(e.path).stat().st_size)
            except OSError:
                size_str = "?"
            text = f"Seleccionado: {e.name}  ({size_str})"
            remote = self._preview_remote_path(e)
            if remote:
                text += f"   →   {remote}"
            self._status_bar_right.configure(text=text)
        else:
            self._status_bar_right.configure(text="Ningún archivo seleccionado")

    def _preview_remote_path(self, entry) -> str:
        """Ruta FTP de destino para *entry* según la categoría que le
        correspondería, sin conectarse al servidor (solo para mostrar en la
        barra de estado — no reutiliza carpetas existentes ni pregunta nada,
        eso solo se resuelve de verdad al subir)."""
        info = entry.media_info
        if not info:
            return ""
        category = self._category_for(info)
        if not category:
            return ""
        root = category.get("root", "")
        if not root:
            return ""
        full_tpl = root.rstrip("/") + "/" + category.get("template", "{serie}/")
        remote_dir = self.ftp.build_remote_path(
            full_tpl, info.title, info.season, info.year, info.media_type)
        filename = entry.new_name or Path(entry.path).name
        return f"{remote_dir.rstrip('/')}/{filename}"

    # ---------------------------------------------------------------- Files tab

    def _auto_start_if_configured(self):
        """Arranca el modo automático al iniciar con Windows si hay carpeta configurada."""
        folder = self.config_data.get("watch_folder", "").strip()
        if folder and not (self._watcher and self._watcher.running):
            self._toggle_auto()

    def _toggle_auto(self):
        if self._watcher and self._watcher.running:
            self._watcher.stop()
            self._watcher = None
            self._auto_btn.configure(
                text="⚡ Auto", width=90, fg_color="transparent", border_width=1)
            self._set_status("Modo automático detenido", PENDING_COLOR)
        else:
            folder = self.config_data.get("watch_folder", "").strip()
            if not folder:
                self._set_status(
                    "Configura la carpeta vigilada en ⚙ Configuración", WARNING_COLOR)
                return
            self._watcher = AutoWatcher(
                folder, self.config_data, self.tmdb, self.ftp,
                self._on_auto_event, self._on_auto_file_event,
                upload_slots=self._upload_slots)
            self._watcher.start()
            self._auto_btn.configure(
                text="⏹ Detener", width=90, fg_color="#c0392b", hover_color="#96281b",
                border_width=0)
            self._set_status(f"Vigilando: {folder}", SUCCESS_COLOR)

    def _on_auto_event(self, tipo, msg):
        colors = {"info": ACCENT, "ok": SUCCESS_COLOR,
                  "skip": WARNING_COLOR, "error": ERROR_COLOR}
        self.after(0, lambda: self._set_status(msg, colors.get(tipo, ACCENT)))

    def _on_auto_file_event(self, path, tipo, new_name=None, progress=None, speed=None,
                             media_info=None, confidence=None, reason=None, renamed_on_disk=True):
        """Recibe eventos de archivo del AutoWatcher y actualiza la tabla."""
        path = str(Path(path))   # normalizar separadores antes de comparar/asignar
        def _update():
            # Buscar entrada existente por path
            entry = next((e for e in self.files if e.path == path), None)
            if entry is None:
                # El archivo ya fue renombrado en un ciclo anterior (p.ej. la
                # subida FTP falló y AutoWatcher lo reprocesa): el path en disco
                # cambió pero sigue siendo el mismo archivo — buscarlo por el
                # nombre con el que quedó tras el renombrado para no duplicar la fila.
                fname = Path(path).name
                entry = next((e for e in self.files
                              if e.new_name == fname and e.status != "subido"), None)
                if entry is not None:
                    entry.path = path

            if tipo == "start":
                if entry is None:
                    entry = FileEntry(path)
                    entry.status = "auto"
                    self.files.append(entry)
                    self._refresh_table()
                else:
                    entry.status    = "auto"
                    entry.error_msg = ""   # nuevo intento -- descartar el motivo anterior
                    self._update_row(entry)
                return

            if entry is None:
                return  # archivo no en lista, ignorar

            if tipo == "renamed":
                entry.new_name = new_name or ""
                entry.status   = "renombrado"
                if media_info is not None:
                    entry.media_info = media_info
                if confidence is not None:
                    entry.confidence = confidence
                # El renombrado es en la misma carpeta: el archivo real ahora
                # vive en <misma carpeta>/<new_name>, no en la ruta original
                # con la que se detectó. Sin esto, cualquier acción manual
                # posterior sobre esta entrada (subir, ver detalles) usaba una
                # ruta que ya no existe en disco ("Archivo no encontrado").
                # Si "Renombrar en origen" está desactivado, el archivo real
                # NO se tocó — entry.path debe seguir apuntando al original.
                if new_name and renamed_on_disk:
                    entry.path = str(Path(path).parent / new_name)
                self._update_row(entry)
                if self._selected_entry is entry:
                    self._update_detail(entry)

            elif tipo == "uploading":
                entry.status       = "subiendo"
                entry.ftp_progress = progress or 0.0
                entry.ftp_speed    = speed or 0.0
                entry.new_name     = new_name or entry.new_name
                self._update_row(entry)
                self._ftp_row_live(entry, entry.ftp_progress, entry.ftp_speed)

            elif tipo == "uploaded":
                entry.status       = "subido"
                entry.ftp_progress = 1.0
                entry.ftp_speed    = 0.0
                entry.new_name     = new_name or entry.new_name
                self._update_row(entry)
                self._ftp_row_live(entry, 1.0, 0.0)
                # Notificación de escritorio
                fname = entry.new_name or entry.name
                self._send_notification("aRenombrar — Subida completada", fname)
                # Guardar en historial (modo automático)
                self._save_history_entry(fname, path, "ok", 0)

            elif tipo == "skip":
                entry.status    = "omitido"
                entry.new_name  = new_name or entry.new_name
                entry.error_msg = reason or ""
                self._update_row(entry)
                if self._selected_entry is entry:
                    self._update_detail(entry)

            elif tipo == "error":
                entry.status    = "error"
                entry.error_msg = reason or ""
                entry.new_name  = new_name or entry.new_name
                if media_info is not None:
                    entry.media_info = media_info
                if confidence is not None:
                    entry.confidence = confidence
                self._update_row(entry)
                if self._selected_entry is entry:
                    self._update_detail(entry)

        self.after(0, _update)

    def _toggle_config(self):
        if self._config_visible:
            if self._settings_dirty():
                dlg = _UnsavedSettingsDialog(self)
                if dlg.result == "save":
                    self._save_all_settings()
                elif dlg.result != "discard":
                    return   # cancelado -> seguir en Ajustes
            self._config_panel_frame.grid_remove()
            self._files_frame.grid(row=0, column=0, sticky="nsew")
            self._config_visible = False
            self._config_btn.configure(text="⚙  Configuración",
                                        fg_color="transparent", border_width=1)
            self._save_settings_btn.grid_remove()
        else:
            self._files_frame.grid_remove()
            self._config_panel_frame.grid(row=0, column=0, sticky="nsew")
            self._config_visible = True
            self._config_btn.configure(text="\u2190 Volver",
                                        fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                        border_width=0)
            self._save_settings_btn.grid()

    def _build_files_tab(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)

        toolbar = ctk.CTkFrame(parent, fg_color="transparent")
        toolbar.grid(row=0, column=0, sticky="ew", pady=(8, 8))

        # Izquierda: añadir archivos
        ctk.CTkButton(toolbar, text="+ Archivos", command=self._add_files,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER, width=110).pack(side="left", padx=(0, 4))
        ctk.CTkButton(toolbar, text="+ Carpeta", command=self._add_folder,
                      width=100).pack(side="left", padx=(0, 4))

        # Derecha: espacio libre FTP, Subir todo, Renombrar, Limpiar
        # (pack right en orden inverso al visual)
        self._ftp_space_lbl = ctk.CTkLabel(
            toolbar, text="", font=ctk.CTkFont(size=11),
            text_color=PENDING_COLOR)
        self._ftp_space_lbl.pack(side="right", padx=(0, 8))

        ctk.CTkButton(toolbar, text="Subir todo", command=self._upload_all_ftp,
                      width=100).pack(side="right", padx=(0, 4))
        ctk.CTkButton(toolbar, text="Renombrar", command=self._rename_all,
                      width=100).pack(side="right", padx=(0, 4))
        ctk.CTkButton(toolbar, text="Limpiar", command=self._clear_files,
                      fg_color="transparent", border_width=1, width=80).pack(side="right", padx=(0, 16))

        body = ctk.CTkFrame(parent, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=0)
        body.grid_rowconfigure(0, weight=1)

        self._file_list_frame = ctk.CTkScrollableFrame(body, label_text="Archivos")
        self._file_list_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self._file_list_frame.grid_columnconfigure(0, weight=1)
        # Fuentes compartidas por las columnas truncadas — se reutilizan tanto para
        # dibujar el texto como para medirlo en _fit_text, así ambos ancho coinciden.
        self._font_name = ctk.CTkFont(size=12)
        self._font_det  = ctk.CTkFont(size=11)
        self._font_nn   = ctk.CTkFont(size=11)
        self._col_widths = self._compute_cw()
        self._sash_state = None   # estado de arrastre de separadores
        self._hdr_labels = []
        # Enlazar redimensionado adaptativo al canvas interno del CTkScrollableFrame
        # add="+" preserva el binding nativo _fit_frame_dimensions_to_canvas de CTkScrollableFrame
        # que ajusta el ancho del frame interno al canvas; sin él las filas no llenan el ancho completo
        self._file_list_frame._parent_canvas.bind(
            "<Configure>", self._on_table_resize, add="+")
        self._build_table_header(self._file_list_frame)
        self._file_rows = []

        self._drop_zone = ctk.CTkLabel(self._file_list_frame,
                                        text="Arrastra archivos aquí\no usa + Archivos",
                                        font=ctk.CTkFont(size=15), text_color=PENDING_COLOR, height=200)
        self._drop_zone.pack(pady=40)

        self._detail_panel = self._build_detail_panel(body)
        self._detail_panel.grid(row=0, column=1, sticky="nsew")

        # Drag & drop — registrar la ventana entera como destino
        if _DND_AVAILABLE:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_drop)

    def _compute_cw(self, avail_w=None):
        """Calcula anchos de columna adaptativos. avail_w = px disponibles en la tabla.
        FIXED incluye los 5 sashes de 4px + padx de botones (4+2+2+2) = 26px constantes."""
        FIXED = 74 + 110 + 80 + 28*3 + 26  # widgets fijos + todo el spacing constante = 374
        PAD   = 16                           # padding interno CTkScrollableFrame
        if avail_w is None:
            avail_w = 900
        flex = max(0, avail_w - FIXED - PAD)
        name = max(80, int(flex * 0.26))
        det  = max(55, int(flex * 0.12))
        nn   = max(0,  flex - name - det)
        return dict(name=name, det=det, nn=nn, stat=74, bar=110, spd=80, btn=28)

    def _on_table_resize(self, event):
        """Reescala columnas al cambiar el ancho del canvas."""
        new_cw = self._compute_cw(event.width)
        cw = self._col_widths
        if new_cw["name"] == cw["name"] and new_cw["det"] == cw["det"]:
            return
        self._col_widths.update(new_cw)
        self._apply_col_widths()

    def _apply_col_widths(self):
        """Actualiza anchos y texto de cabeceras y filas. nn no recibe ancho fijo (expand=True)."""
        cw = self._col_widths
        for lbl, key in self._hdr_labels:
            if key in ("name", "det", "stat", "bar", "spd"):
                lbl.configure(width=cw[key])
            elif key == "btns":
                lbl.configure(width=cw["btn"] * 3 + 6)
        for row in self._file_rows:
            row["name"].configure(
                width=cw["name"],
                text=_fit_text(row.get("_raw_name", ""), cw["name"], self._font_name))
            row["detected"].configure(
                width=cw["det"],
                text=_fit_text(row.get("_raw_det", ""), cw["det"], self._font_det))
            row["new_name"].configure(
                text=_fit_text(row["entry"].new_name, cw["nn"], self._font_nn))
            row["status"].configure(width=cw["stat"])
            row["ftp_bar"].configure(width=cw["bar"])
            row["ftp_speed"].configure(width=cw["spd"])

    def _sash_press(self, event, left_col, right_col):
        self._sash_state = {
            "left":   left_col,
            "right":  right_col,
            "x0":     event.x_root,
            "left_w": self._col_widths[left_col],
            "right_w": self._col_widths[right_col],
        }

    def _sash_motion(self, event):
        if not self._sash_state:
            return
        delta = event.x_root - self._sash_state["x0"]
        MIN = 60
        new_l = max(MIN, self._sash_state["left_w"]  + delta)
        new_r = max(MIN, self._sash_state["right_w"] - delta)
        self._col_widths[self._sash_state["left"]]  = new_l
        self._col_widths[self._sash_state["right"]] = new_r
        self._apply_col_widths()

    def _sash_release(self, event):
        self._sash_state = None

    def _build_table_header(self, parent):
        import tkinter as tk
        cw = self._col_widths
        hdr = ctk.CTkFrame(parent, corner_radius=0)
        hdr.pack(fill="x", pady=(0, 2))
        _BF = ctk.CTkFont(weight="bold")

        def _sash(left_col, right_col=None):
            """Separador de 4px; arrastrable si se dan los nombres de columnas."""
            draggable = right_col is not None
            s = tk.Frame(hdr, width=4, bg="#505060",
                         cursor="sb_h_double_arrow" if draggable else "")
            s.pack(side="left", fill="y", pady=6)
            if draggable:
                s.bind("<ButtonPress-1>",   lambda e: self._sash_press(e, left_col, right_col))
                s.bind("<B1-Motion>",       self._sash_motion)
                s.bind("<ButtonRelease-1>", self._sash_release)

        # Nombre original
        lbl = ctk.CTkLabel(hdr, text="Nombre original", font=_BF, width=cw["name"], anchor="w")
        lbl.pack(side="left", padx=0, pady=4)
        self._hdr_labels.append((lbl, "name"))

        _sash("name", "det")   # arrastrable

        # Detectado
        lbl = ctk.CTkLabel(hdr, text="Detectado", font=_BF, width=cw["det"], anchor="w")
        lbl.pack(side="left", padx=0, pady=4)
        self._hdr_labels.append((lbl, "det"))

        _sash("det", "nn")     # arrastrable

        # Nuevo nombre — expande para llenar el espacio sobrante
        lbl = ctk.CTkLabel(hdr, text="Nuevo nombre", font=_BF, anchor="w")
        lbl.pack(side="left", padx=0, pady=4, fill="x", expand=True)
        self._hdr_labels.append((lbl, "nn"))

        _sash("nn", "stat")    # arrastrable

        # Estado
        lbl = ctk.CTkLabel(hdr, text="Estado", font=_BF, width=cw["stat"], anchor="w")
        lbl.pack(side="left", padx=0, pady=4)
        self._hdr_labels.append((lbl, "stat"))

        _sash("stat", "bar")   # arrastrable

        # Subida FTP
        lbl = ctk.CTkLabel(hdr, text="Subida FTP", font=_BF, width=cw["bar"], anchor="w")
        lbl.pack(side="left", padx=0, pady=4)
        self._hdr_labels.append((lbl, "bar"))

        _sash("bar", "spd")    # arrastrable

        # Vel.
        lbl = ctk.CTkLabel(hdr, text="Vel.", font=_BF, width=cw["spd"], anchor="w")
        lbl.pack(side="left", padx=0, pady=4)
        self._hdr_labels.append((lbl, "spd"))

        # Cabecera vacía para los 3 botones (btn*3 + inter-btn padx: 2+2+2=6)
        lbl = ctk.CTkLabel(hdr, text="", font=_BF, width=cw["btn"] * 3 + 6, anchor="w")
        lbl.pack(side="left", padx=0, pady=4)
        self._hdr_labels.append((lbl, "btns"))

    def _build_detail_panel(self, parent):
        panel = ctk.CTkFrame(parent, width=255)
        panel.grid_propagate(False)
        panel.grid_rowconfigure(1, weight=1)
        panel.grid_columnconfigure(0, weight=1)

        # -- Zona superior: busqueda (siempre visible) --
        search_top = ctk.CTkFrame(panel, fg_color="transparent")
        search_top.grid(row=0, column=0, sticky="ew", padx=8, pady=(10, 4))
        search_top.columnconfigure(0, weight=1)

        ctk.CTkLabel(search_top, text="Buscar manualmente:",
                     font=ctk.CTkFont(size=11), anchor="w").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 2))
        mf = ctk.CTkFrame(search_top, fg_color="transparent")
        mf.grid(row=1, column=0, columnspan=2, sticky="ew")
        mf.columnconfigure(0, weight=1)
        self._manual_entry = ctk.CTkEntry(mf, placeholder_text="Nombre...")
        self._manual_entry.grid(row=0, column=0, sticky="ew")
        self._manual_entry.bind("<Return>", lambda _: self._manual_search())
        ctk.CTkButton(mf, text="Buscar", width=60,
                      command=self._manual_search).grid(row=0, column=1, padx=(4, 0))

        ctk.CTkLabel(search_top, text="Resultado TMDB:",
                     font=ctk.CTkFont(size=11), anchor="w").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(6, 2))
        # Elegir un resultado del desplegable solo lo PREVISUALIZA (póster,
        # sinopsis...) — no se aplica a la entrada hasta pulsar "Asignar".
        # Antes se asignaba automáticamente el primer resultado nada más
        # buscar, sin dar ocasión a revisar si era el correcto.
        self._result_combo = ctk.CTkComboBox(search_top, values=[],
                                              command=self._on_result_preview)
        self._result_combo.set("")
        self._result_combo.grid(row=3, column=0, sticky="ew")
        ctk.CTkButton(search_top, text="Asignar", width=70,
                      command=self._assign_selected_result).grid(
            row=3, column=1, padx=(4, 0))
        self._tmdb_results = []

        # -- Zona inferior: detalles con scroll --
        scroll = ctk.CTkScrollableFrame(panel, fg_color="transparent", label_text="")
        scroll.grid(row=1, column=0, sticky="nsew", padx=4, pady=(4, 6))
        scroll.columnconfigure(0, weight=1)

        self._poster_label = ctk.CTkLabel(scroll, text="", width=180, height=220)
        self._poster_label.pack(pady=(4, 2))
        # CTkTextbox (no CTkLabel) para el título, la sinopsis y el error: de
        # solo lectura (state="disabled") pero seleccionable y copiable con
        # ratón/teclado, a diferencia de un CTkLabel normal.
        self._detail_title = ctk.CTkTextbox(
            scroll, width=215, height=32, wrap="word",
            font=ctk.CTkFont(size=13, weight="bold"), fg_color="transparent",
            activate_scrollbars=False)
        self._detail_title.configure(state="disabled")
        self._detail_title.pack(pady=(4, 0), fill="x")
        self._detail_episode = ctk.CTkLabel(scroll, text="", wraplength=215,
                                             font=ctk.CTkFont(size=12))
        self._detail_episode.pack()
        self._detail_year = ctk.CTkLabel(scroll, text="", text_color=PENDING_COLOR)
        self._detail_year.pack()
        self._detail_confidence = ctk.CTkLabel(scroll, text="", font=ctk.CTkFont(size=11))
        self._detail_confidence.pack()
        self._detail_overview = ctk.CTkTextbox(
            scroll, width=215, height=110, wrap="word",
            font=ctk.CTkFont(size=11), fg_color="transparent",
            activate_scrollbars=True)
        self._detail_overview.configure(state="disabled")
        self._detail_overview.pack(pady=4, fill="x")
        self._detail_error = ctk.CTkTextbox(
            scroll, width=215, height=60, wrap="word",
            font=ctk.CTkFont(size=11), fg_color="transparent",
            text_color=ERROR_COLOR, activate_scrollbars=True)
        self._detail_error.configure(state="disabled")
        self._detail_error.pack(pady=(0, 4), fill="x")

        return panel

    # ------------------------------------------- Close warning bar



    # ------------------------------------------------- Queue / upload

    def _refresh_ftp_space(self):
        """Consulta espacio libre en el servidor FTP y actualiza la etiqueta del toolbar."""
        def worker():
            free = self.ftp.get_free_space()
            if free is None:
                self.after(0, lambda: self._ftp_space_lbl.configure(text=""))
                return
            # Formatear en la unidad más legible
            for unit, divisor in (("TB", 1024**4), ("GB", 1024**3), ("MB", 1024**2)):
                if free >= divisor:
                    text = f"💾 {free / divisor:.1f} {unit} libres"
                    break
            else:
                text = f"💾 {free} B libres"
            self.after(0, lambda t=text: self._ftp_space_lbl.configure(
                text=t, text_color=SUCCESS_COLOR if free > 1024**3 else WARNING_COLOR))
        threading.Thread(target=worker, daemon=True).start()

    def _start_ftp_upload(self, entries):
        if self._upload_running:
            self._set_status("Ya hay una subida en progreso", WARNING_COLOR)
            return
        if not self._ensure_ftp():
            return
        self._refresh_ftp_space()
        self._upload_queue = list(entries)
        self._upload_cancel.clear()
        self._upload_skip.clear()
        self._upload_current_idx    = -1
        self._upload_current_remote = ""
        self._upload_overwrite_all  = False
        # Refrescar en cada tanda por si han cambiado carpetas en el servidor
        self._series_folder_cache.clear()
        self._ftp_dir_cache.clear()
        # Resetear columnas FTP de los archivos en cola
        for entry in entries:
            entry.ftp_progress = 0.0
            entry.ftp_speed    = 0.0
            entry.ftp_status   = "En espera"
            entry.status       = "en_cola"
            self._update_row(entry)
        self._refresh_ftp_columns()
        self._upload_running = True
        threading.Thread(target=self._queue_worker, daemon=True).start()

    def _refresh_ftp_columns(self):
        """Actualiza barra / velocidad / estado FTP para todos los file_rows."""
        for row in self._file_rows:
            entry = row["entry"]
            row["ftp_bar"].set(entry.ftp_progress)
            spd = _fmt_speed(entry.ftp_speed) if entry.ftp_speed > 0 else ""
            row["ftp_speed"].configure(text=spd)

    def _queue_stop_all(self):
        self._upload_cancel.set()
        self._set_status("Deteniendo subida...", WARNING_COLOR)

    def _queue_skip_entry(self, entry):
        """Salta la subida del archivo dado señalando su skip_event."""
        if not self._upload_running:
            return
        slot_of     = getattr(self, "_upload_slot_of",     {})
        skip_events = getattr(self, "_upload_skip_events", [])
        slot = slot_of.get(id(entry))
        if slot is not None and slot < len(skip_events):
            skip_events[slot].set()

    # ------------------------------------------------- Upload worker

    def _category_for(self, info):
        """Elige la categoría FTP (nombre/géneros/rutas/plantilla) que le
        corresponde a *info* según sus géneros de TMDB. None si no hay
        ninguna categoría configurada aplicable."""
        cats = self.config_data.get("ftp_categories", {"tv": [], "movie": []})
        return choose_category(info.genre_ids, cats.get(info.media_type, []))

    def _resolve_series_folder(self, ftp_conn, category: dict, info, entry=None) -> str:
        """Si ya existe en la raíz de *category* una carpeta con nombre
        parecido (idioma, artículo, nombre corto vs largo...) a la serie a
        subir, pregunta una sola vez por serie si hay que reutilizarla en vez
        de crear una nueva a mayores. La respuesta se cachea para el resto de
        episodios de la misma serie dentro de esta tanda de subida."""
        desired = info.title
        with self._series_folder_lock:
            if info.tmdb_id in self._series_folder_cache:
                return self._series_folder_cache[info.tmdb_id]

            chosen = desired
            root = category.get("root", "")
            if root:
                if root not in self._ftp_dir_cache:
                    self._ftp_dir_cache[root] = ftp_conn.list_dirs(root)
                existing = self._ftp_dir_cache[root]

                sanitized_desired = _ftp_safe(desired)
                if sanitized_desired in existing:
                    chosen = sanitized_desired
                else:
                    candidate, ratio = best_match(desired, existing, min_ratio=0.55)
                    if candidate:
                        if ratio >= 1.0:
                            chosen = candidate
                        else:
                            if entry is not None:
                                entry.status = "esperando_confirmacion"
                                self.after(0, lambda e=entry: self._update_row(e))
                            answer = [None]
                            ev = threading.Event()
                            def _ask(d=desired, c=candidate, ans=answer, e=ev):
                                dlg = _SeriesMatchDialog(self, d, c)
                                ans[0] = dlg.result
                                e.set()
                            self.after(0, _ask)
                            ev.wait()
                            if answer[0] == "yes":
                                chosen = candidate

            self._series_folder_cache[info.tmdb_id] = chosen
            return chosen

    def _upload_entry_with(self, entry, ftp_conn, speed_kbs, skip_ev):
        """Sube un único archivo usando la conexión ftp_conn dada. Devuelve (ok, msg)."""
        if not Path(entry.path).exists():
            # El renombrado de AutoWatcher no espera cupo de subida (solo la
            # transferencia en sí — ver core/upload_slots.py), así que puede
            # renombrar y subir el archivo por su cuenta mientras esta
            # entrada seguía esperando turno en la cola manual con el nombre
            # viejo. Si AutoWatcher ya lo dio por subido, sincronizar el
            # estado aquí en vez de fallar con "archivo no encontrado" en
            # bucle en cada reintento.
            auto_status = self._auto_processed_status(entry.path)
            if auto_status == "subido":
                entry.status = "subido"
                self._ftp_row_set(entry, "Subido (automático)", 1, 0)
                return True, "ya_subido_por_auto"
            entry.status    = "error"
            entry.error_msg = "Archivo local no encontrado (¿se movió, se renombró o lo procesó el modo automático?)"
            self._ftp_row_set(entry, "No encontrado", 0, 0)
            return False, "archivo_no_encontrado"

        info = entry.media_info
        if not info:
            self._ftp_row_set(entry, "Sin info TMDB", 0, 0)
            return True, "sin_info"

        category = self._category_for(info)
        if not category:
            entry.status    = "error"
            entry.error_msg = "Sin categoría FTP configurada"
            self._ftp_row_set(entry, "Sin categoría", 0, 0)
            return False, "sin_categoria"
        root = category.get("root", "")
        if not root:
            entry.status    = "error"
            entry.error_msg = f"Categoría '{category.get('name')}' sin ruta configurada"
            self._ftp_row_set(entry, "Sin ruta", 0, 0)
            return False, "sin_ruta"

        if info.media_type == "tv":
            serie_name = self._resolve_series_folder(ftp_conn, category, info, entry)
        else:
            serie_name = info.title
        full_tpl    = root.rstrip("/") + "/" + category.get("template", "{serie}/")
        remote_dir  = ftp_conn.build_remote_path(full_tpl, serie_name, info.season, info.year, info.media_type)
        # "Renombrar archivos en destino" (Ajustes) — si está desactivado, se
        # sube con el nombre ORIGINAL (el que tenía al añadirlo, entry.name,
        # que no cambia aunque luego se renombre en local), aunque la carpeta
        # se organice igualmente por serie/temporada según TMDB.
        rename_remote   = self.config_data.get("rename_remote", True)
        remote_filename = (entry.new_name if rename_remote else entry.name) or Path(entry.path).name
        remote_file = f"{remote_dir.rstrip('/')}/{remote_filename}"

        try:
            local_size = Path(entry.path).stat().st_size
        except OSError:
            local_size = 0

        # Solo preguntar si el archivo remoto ya está completo (o más grande):
        # eso es un archivo ya subido de verdad. Si el remoto existe pero es
        # más pequeño, es una subida anterior interrumpida a medias — se
        # reanuda sola más abajo (try_resume) sin interrumpir con un diálogo.
        remote_size = ftp_conn.get_remote_size(remote_file)
        # Comparado con lo que había ANTES del intento anterior (si lo hubo):
        # si el archivo remoto sigue exactamente igual de grande tras un
        # intento fallido, lo más probable es que el servidor no pudiera
        # escribir más (p.ej. disco lleno) y solo cerrara la conexión, sin
        # devolver un error FTP claro — eso se ve como un WinError de
        # conexión genérico, muy confuso para diagnosticar sin este aviso.
        prev_remote_size = getattr(entry, "_last_upload_remote_size", None)
        stalled = (remote_size is not None and prev_remote_size is not None
                   and remote_size <= prev_remote_size)
        entry._last_upload_remote_size = remote_size
        force_overwrite = False
        if remote_size is not None and remote_size >= local_size and not self._upload_overwrite_all:
            # Estado visible en la columna "Estado": si no se distingue de
            # "En cola" es fácil no darse cuenta de que hay un diálogo
            # esperando respuesta (p.ej. cuando el modo automático ya subió
            # este mismo archivo antes) y la fila parece quedarse "colgada"
            # sin explicación.
            entry.status = "esperando_confirmacion"
            self.after(0, lambda e=entry: self._update_row(e))
            answer = [None]
            ev = threading.Event()
            def _ask(rf=remote_file, ans=answer, e=ev):
                dlg = _OverwriteDialog(self, rf)
                ans[0] = dlg.result
                e.set()
            self.after(0, _ask)
            ev.wait()
            if answer[0] == "all":
                self._upload_overwrite_all = True
                force_overwrite = True
            elif answer[0] == "skip":
                entry.status = "omitido"
                self._ftp_row_set(entry, "Omitido", 0, 0)
                return True, "omitido"
            else:   # "overwrite"
                force_overwrite = True
        elif remote_size is not None and remote_size >= local_size and self._upload_overwrite_all:
            # "Sobrescribir todos" ya activo de un archivo anterior de esta tanda
            force_overwrite = True

        free = ftp_conn.get_free_space()
        if free is not None and free < local_size:
            self._ftp_row_set(entry, "Sin espacio", 0, 0)
            self.after(0, lambda gb=free/(1024**3): self._set_status(
                f"Disco lleno — libre: {gb:.1f} GB", ERROR_COLOR))
            self._upload_cancel.set()
            return False, "disco_lleno"

        # "Subidas simultáneas" es un cupo GLOBAL compartido con el modo
        # automático (ver core/upload_slots.py) — si está a 1, esta subida
        # espera aquí (la fila se queda "En cola") a que termine cualquier
        # otra, manual o automática, antes de empezar a transferir de verdad.
        if not self._upload_slots.acquire(cancel_event=self._upload_cancel):
            entry.status = "listo"
            self._ftp_row_set(entry, "Cancelado", entry.ftp_progress, 0)
            self.after(0, lambda e=entry: self._update_row(e))
            return False, "cancelado"

        entry.status     = "subiendo"
        entry.ftp_status = "Subiendo..."
        # Protege el archivo de AutoWatcher mientras dura la transferencia: si
        # el modo automático se activa (o ya lo está) y escanea la carpeta
        # justo ahora, no debe meterse a identificar/renombrar/subir este
        # mismo archivo por su cuenta mientras ya lo tenemos abierto subiéndolo.
        self._mark_auto_processed(entry.path, "subiendo", entry.new_name)
        self.after(0, lambda e=entry: self._update_row(e))
        self.after(0, lambda e=entry: self._ftp_row_uploading(e))

        def progress(sent, total_b, spd, e=entry):
            e.ftp_progress = sent / total_b if total_b > 0 else 0
            e.ftp_speed    = spd
            self.after(0, lambda p=e.ftp_progress, s=spd, en=e: self._ftp_row_live(en, p, s))

        try:
            ok, msg = ftp_conn.upload_file(
                entry.path, remote_dir, progress,
                cancel_event=self._upload_cancel,
                skip_event=skip_ev,
                speed_limit_kbs=speed_kbs,
                try_resume=not force_overwrite,
                remote_filename=remote_filename,
            )
        finally:
            self._upload_slots.release()

        try:
            size = Path(entry.path).stat().st_size
        except OSError:
            size = 0

        if ok:
            entry.status = "subido"
            if hasattr(entry, "_last_upload_remote_size"):
                del entry._last_upload_remote_size
            self._ftp_row_set(entry, "Subido", 1, 0)
            self._mark_auto_processed(entry.path, "subido", entry.new_name)
            self._save_history_entry(
                entry.new_name or Path(entry.path).name,
                remote_file, "ok", size)
        elif msg == "cancelado":
            entry.status = "listo"
            self._ftp_row_set(entry, "Cancelado", entry.ftp_progress, 0)
        elif msg == "saltado":
            entry.status = "listo"
            self._ftp_row_set(entry, "Saltado", 0, 0)
        elif msg == "disco_lleno":
            entry.status    = "error"
            entry.error_msg = "Disco lleno en el servidor"
            self._ftp_row_set(entry, "Disco lleno", 0, 0)
            self.after(0, lambda: self._set_status("Disco lleno en servidor", ERROR_COLOR))
            self._upload_cancel.set()
            self._save_history_entry(
                entry.new_name or Path(entry.path).name,
                remote_file, "error", size, error_msg=entry.error_msg)
        else:
            entry.status = "error"
            if stalled:
                entry.error_msg = (
                    f"{msg} — el archivo lleva más de un intento sin avanzar en el "
                    f"servidor (sigue en {_fmt_size(remote_size)} de {_fmt_size(local_size)}). "
                    f"Puede que el disco del servidor esté lleno.")
            else:
                entry.error_msg = msg
            self._ftp_row_set(entry, "Error", 0, 0)
            self._save_history_entry(
                entry.new_name or Path(entry.path).name,
                remote_file, "error", size, error_msg=entry.error_msg)

        if not ok:
            # La subida no llegó a completarse: quitar la marca "subiendo"
            # para no dejar el archivo bloqueado para AutoWatcher para
            # siempre — si de verdad se quedó a medias, que pueda reintentarlo
            # él también (o el usuario, a mano, otra vez).
            self._unmark_auto_processed(entry.path)

        self.after(0, lambda e=entry: self._update_row(e))
        return ok or msg in ("omitido", "saltado", "sin_info"), msg

    def _queue_worker(self):
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from core.ftp_client import FTPClient as _FTPClient

        parallel   = max(1, min(5, int(self.config_data.get("ftp_parallel", 1))))

        # Callable — lee config_data en cada chunk para cambio instantáneo
        # Config almacena MB/s; devolvemos KB/s (ftp_client lo multiplica × 1024 → bytes/s)
        def get_speed_kbs():
            try:
                mbs = float(self.config_data.get("ftp_speed_limit", 0) or 0)
                per = mbs / parallel if mbs > 0 else 0.0
                return int(per * 1024)  # MB/s → KB/s
            except Exception:
                return 0

        # Crear pool de conexiones FTP
        host     = self.config_data.get("ftp_host", "")
        port     = int(self.config_data.get("ftp_port", 21))
        user     = self.config_data.get("ftp_user", "")
        password = self.config_data.get("ftp_password", "")
        use_tls  = bool(self.config_data.get("ftp_use_tls", False))

        pool = []
        for _ in range(parallel):
            c = _FTPClient()
            ok, msg = c.connect(host, port, user, password, use_tls)
            if ok:
                pool.append(c)
            else:
                self.after(0, lambda m=msg: self._set_status(m, ERROR_COLOR))
                break

        if not pool:
            self._upload_running = False
            return

        import queue as _queue
        conn_q = _queue.Queue()
        for c in pool:
            conn_q.put(c)

        # Un skip_event por slot de conexión — guardados en self para que
        # _queue_skip_entry pueda acceder desde el hilo principal
        self._upload_skip_events = [threading.Event() for _ in pool]
        self._upload_slot_of = {}  # id(entry) → slot index
        skip_events = self._upload_skip_events
        slot_of     = self._upload_slot_of

        _slot_lock = threading.Lock()
        _slot_counter = [0]
        _nslots = len(pool)

        def _next_slot():
            with _slot_lock:
                s = _slot_counter[0] % _nslots
                _slot_counter[0] += 1
                return s

        max_retries = max(0, int(self.config_data.get("ftp_retries", 3)))

        def process(entry):
            if self._upload_cancel.is_set():
                return
            slot = _next_slot()
            slot_of[id(entry)] = slot
            ftp_conn = conn_q.get()
            try:
                for attempt in range(max_retries + 1):
                    if self._upload_cancel.is_set():
                        break
                    # Reconectar si la conexión se cayó
                    if not ftp_conn.is_connected():
                        self.after(0, lambda e=entry: self._ftp_row_set(e, "Reconectando…", e.ftp_progress, 0))
                        ok_rc, _ = ftp_conn.connect(host, port, user, password, use_tls)
                        if not ok_rc:
                            if attempt < max_retries:
                                _time.sleep(2 ** attempt)
                                continue
                            break
                    ok, msg = self._upload_entry_with(
                        entry, ftp_conn, get_speed_kbs, skip_events[slot])
                    skip_events[slot].clear()
                    # No reintentar si cancelado/saltado/omitido, éxito o error de configuración
                    if ok or msg in ("cancelado", "saltado", "omitido", "sin_info",
                                      "sin_categoria", "sin_ruta", "archivo_no_encontrado"):
                        break
                    # Error recuperable — reintentar
                    if attempt < max_retries:
                        wait = 2 ** attempt
                        self.after(0, lambda e=entry, a=attempt+1, m=max_retries:
                            self._ftp_row_set(e, f"Reintento {a}/{m}…", e.ftp_progress, 0))
                        _time.sleep(wait)
            finally:
                conn_q.put(ftp_conn)

        from concurrent.futures import wait as _fut_wait, FIRST_COMPLETED as _FIRST
        with ThreadPoolExecutor(max_workers=len(pool)) as executor:
            idx     = 0
            pending = {}   # future → entry
            while not self._upload_cancel.is_set():
                # Enviar trabajos disponibles hasta llenar el pool
                while idx < len(self._upload_queue) and len(pending) < len(pool):
                    e = self._upload_queue[idx]
                    idx += 1
                    f = executor.submit(process, e)
                    pending[f] = e
                if pending:
                    done, _ = _fut_wait(list(pending), timeout=0.3, return_when=_FIRST)
                    for f in done:
                        e = pending.pop(f)
                        try:
                            f.result()
                        except Exception as exc:
                            e.status    = "error"
                            e.error_msg = str(exc)
                elif idx >= len(self._upload_queue):
                    # Cola vacía — esperar brevemente por nuevos ítems encolados en caliente
                    _time.sleep(0.2)
                    if idx >= len(self._upload_queue):
                        break

        for c in pool:
            try:
                c.disconnect()
            except Exception:
                pass

        self._upload_running        = False
        self._upload_current_idx    = -1
        self._upload_current_remote = ""
        cancelled = self._upload_cancel.is_set()
        self.after(0, lambda: self._set_status(
            "Subida cancelada" if cancelled else "Subida completada",
            WARNING_COLOR if cancelled else SUCCESS_COLOR))
        self.after(0, self._restore_ftp_row_buttons)

    def _ftp_row_uploading(self, entry):
        """Cambia el botón ↑ a ⏹ rojo mientras se sube este archivo."""
        for row in self._file_rows:
            if row["entry"] is entry:
                row["ftp_up"].configure(
                    text="⏹", fg_color="#c0392b", hover_color="#96281b",
                    font=ctk.CTkFont(size=12),
                    command=lambda e=entry: self._queue_skip_entry(e))
                break

    def _ftp_row_done(self, entry):
        """Restaura el botón ⏹ a ↑ verde cuando termina la subida de este archivo."""
        for row in self._file_rows:
            if row["entry"] is entry:
                row["ftp_up"].configure(
                    text="▲", fg_color=ACCENT, hover_color=ACCENT_HOVER,
                    font=ctk.CTkFont(size=12),
                    command=lambda e=entry: self._upload_one(e))
                break

    def _ftp_row_live(self, entry, pct, speed_bps):
        """Actualiza barra y velocidad durante la subida (llamado desde after)."""
        for row in self._file_rows:
            if row["entry"] is entry:
                row["ftp_bar"].set(pct)
                row["ftp_speed"].configure(text=_fmt_speed(speed_bps) if speed_bps > 0 else "")
                break

    def _ftp_row_set(self, entry, status_text, pct, speed_bps):
        """Fija el estado final de una fila FTP (llamado desde worker, usa after)."""
        entry.ftp_progress = pct
        entry.ftp_speed    = speed_bps
        entry.ftp_status   = status_text
        color_map = {
            "Subido":        SUCCESS_COLOR,
            "Cancelado":     WARNING_COLOR,
            "Saltado":       WARNING_COLOR,
            "Omitido":       WARNING_COLOR,
            "Error":         ERROR_COLOR,
            "Disco lleno":   ERROR_COLOR,
            "Sin espacio":   ERROR_COLOR,
            "Sin info TMDB": PENDING_COLOR,
        }
        clr = color_map.get(status_text, PENDING_COLOR)
        def _do():
            for row in self._file_rows:
                if row["entry"] is entry:
                    row["ftp_bar"].set(pct)
                    row["ftp_speed"].configure(text="", text_color=PENDING_COLOR)
                    self._ftp_row_done(entry)
                    break
        self.after(0, _do)

    def _restore_ftp_row_buttons(self):
        """Restaura todos los botones ↑ al terminar la cola completa."""
        for row in self._file_rows:
            self._ftp_row_done(row["entry"])

    def _play_file(self, entry):
        """Abre el archivo con la aplicación predeterminada de Windows."""
        try:
            os.startfile(entry.path)
        except Exception as e:
            self._set_status(f"No se pudo abrir: {e}", ERROR_COLOR)

    # ------------------------------------------------------------- FTP tab

    def _build_config_panel(self, panel):
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_columnconfigure(1, weight=1)
        panel.grid_rowconfigure(0, weight=1)

        scroll = ctk.CTkScrollableFrame(panel, fg_color="transparent")
        scroll.grid(row=0, column=0, columnspan=2, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)
        scroll.grid_columnconfigure(1, weight=1)

        # ── Modo Automático ──
        auto_fr = ctk.CTkFrame(scroll)
        auto_fr.grid(row=0, column=0, columnspan=2, sticky="nsew", padx=0, pady=(0, 8))
        auto_fr.grid_columnconfigure(1, weight=1)
        auto_fr.grid_columnconfigure(3, weight=1)
        ctk.CTkLabel(auto_fr, text="Modo Automático",
                     font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, columnspan=4, pady=(12, 6))
        ctk.CTkLabel(auto_fr, text="La carpeta vigilada se monitoriza en segundo plano: detecta, renombra y sube archivos de vídeo automáticamente.",
                     font=ctk.CTkFont(size=11), text_color=PENDING_COLOR, wraplength=600).grid(
            row=1, column=0, columnspan=4, pady=(0, 10))

        # Carpeta vigilada
        ctk.CTkLabel(auto_fr, text="Carpeta vigilada:").grid(
            row=2, column=0, sticky="e", padx=(16, 8), pady=8)
        self._watch_folder_entry = ctk.CTkEntry(auto_fr, width=400,
                                                 placeholder_text="Selecciona la carpeta que quieres vigilar...")
        self._watch_folder_entry.insert(0, self.config_data.get("watch_folder", ""))
        self._watch_folder_entry.grid(row=2, column=1, padx=(0, 4), pady=8, sticky="ew")
        ctk.CTkButton(auto_fr, text="📂 Examinar", width=100,
                      command=self._browse_watch_folder).grid(row=2, column=2, padx=(0, 16), pady=8)

        # Intervalo + acción
        ctk.CTkLabel(auto_fr, text="Intervalo de escaneo (seg):").grid(
            row=3, column=0, sticky="e", padx=(16, 8), pady=8)
        self._poll_interval_entry = ctk.CTkEntry(auto_fr, width=80)
        self._poll_interval_entry.insert(0, str(self.config_data.get("poll_interval", 10)))
        self._poll_interval_entry.grid(row=3, column=1, sticky="w", padx=(0, 8), pady=8)

        ctk.CTkLabel(auto_fr, text="Acción tras procesar:").grid(
            row=4, column=0, sticky="e", padx=(16, 8), pady=8)
        self._auto_action_combo = ctk.CTkComboBox(auto_fr, width=280, values=[
            "Mantener original",
            "Mover a subcarpeta 'procesados'",
            "Eliminar original",
        ])
        self._auto_action_combo.set(self.config_data.get(
            "auto_action", "Mantener original"))
        self._auto_action_combo.grid(row=4, column=1, sticky="w", padx=(0, 8), pady=8)

        # Confianza mínima
        ctk.CTkLabel(auto_fr, text="Confianza mínima (modo auto):").grid(
            row=5, column=0, sticky="e", padx=(16, 8), pady=8)
        conf_fr = ctk.CTkFrame(auto_fr, fg_color="transparent")
        conf_fr.grid(row=5, column=1, sticky="w", pady=8)
        _conf_init = int(self.config_data.get("min_confidence", 70))
        self._conf_label = ctk.CTkLabel(conf_fr, text=f"{_conf_init}%", width=36, anchor="w")
        self._conf_label.pack(side="left", padx=(0, 6))
        self._conf_slider = ctk.CTkSlider(
            conf_fr, from_=0, to=100, number_of_steps=100, width=220,
            command=lambda v: self._conf_label.configure(text=f"{int(v)}%"))
        self._conf_slider.set(_conf_init)
        self._conf_slider.pack(side="left")
        ctk.CTkLabel(conf_fr, text="(0 = aceptar todo)", font=ctk.CTkFont(size=11),
                     text_color=PENDING_COLOR).pack(side="left", padx=(8, 0))

        # Iniciar con Windows
        self._autostart_switch = ctk.CTkSwitch(
            auto_fr, text="Iniciar con Windows (minimizado en bandeja)")
        self._autostart_switch.grid(row=6, column=0, columnspan=4, pady=(4, 4))
        if self.config_data.get("start_with_windows", False):
            self._autostart_switch.select()

        # Notificaciones de escritorio
        self._notif_switch = ctk.CTkSwitch(
            auto_fr, text="Notificaciones de escritorio al completar subidas")
        self._notif_switch.grid(row=7, column=0, columnspan=4, pady=(0, 4))
        if self.config_data.get("desktop_notifications", True):
            self._notif_switch.select()

        # Renombrar en origen / destino (aplica también a la subida manual)
        self._rename_local_switch = ctk.CTkSwitch(
            auto_fr, text="Renombrar archivos en origen (local)")
        self._rename_local_switch.grid(row=8, column=0, columnspan=4, pady=(8, 2))
        if self.config_data.get("rename_local", True):
            self._rename_local_switch.select()

        self._rename_remote_switch = ctk.CTkSwitch(
            auto_fr, text="Renombrar archivos en destino (FTP)")
        self._rename_remote_switch.grid(row=9, column=0, columnspan=4, pady=(2, 4))
        if self.config_data.get("rename_remote", True):
            self._rename_remote_switch.select()

        ctk.CTkLabel(auto_fr,
                     text="Si desactivas ambos, los archivos conservan su nombre original — solo se organizan\n"
                          "en carpetas por serie/temporada según TMDB. Afecta a la subida automática y manual.",
                     font=ctk.CTkFont(size=11), text_color=PENDING_COLOR, justify="center").grid(
            row=10, column=0, columnspan=4, pady=(0, 12))

        # ── Conexion FTP ──
        conn = ctk.CTkFrame(scroll)
        conn.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=0, pady=8)
        conn.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(conn, text="Conexión FTP",
                     font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, columnspan=2, pady=(12, 8))
        self._ftp_entries = {}
        for i, (label, key, secret) in enumerate([
            ("Servidor (host):", "ftp_host",     False),
            ("Puerto:",          "ftp_port",     False),
            ("Usuario:",         "ftp_user",     False),
            ("Contraseña:",      "ftp_password", True),
        ], start=1):
            ctk.CTkLabel(conn, text=label).grid(row=i, column=0, sticky="e", padx=10, pady=6)
            e = ctk.CTkEntry(conn, show="*" if secret else "", width=200)
            e.insert(0, str(self.config_data.get(key, "")))
            e.grid(row=i, column=1, padx=10, pady=6, sticky="ew")
            self._ftp_entries[key] = e
        self._tls_switch = ctk.CTkSwitch(conn, text="Usar FTP con TLS (FTPS)")
        self._tls_switch.grid(row=5, column=0, columnspan=2, pady=6)
        if self.config_data.get("ftp_use_tls"):
            self._tls_switch.select()

        # Subidas simultáneas
        ctk.CTkLabel(conn, text="Subidas simultáneas:").grid(
            row=6, column=0, sticky="e", padx=10, pady=6)
        self._ftp_parallel_combo = ctk.CTkComboBox(conn, values=["1", "2", "3", "4", "5"], width=80)
        self._ftp_parallel_combo.set(str(self.config_data.get("ftp_parallel", 1)))
        self._ftp_parallel_combo.grid(row=6, column=1, sticky="w", padx=10, pady=6)

        # Límite de velocidad
        ctk.CTkLabel(conn, text="Límite de velocidad (MB/s):").grid(
            row=7, column=0, sticky="e", padx=10, pady=6)
        spd_fr = ctk.CTkFrame(conn, fg_color="transparent")
        spd_fr.grid(row=7, column=1, sticky="w", padx=10, pady=6)
        self._ftp_speed_entry = ctk.CTkEntry(spd_fr, width=100)
        self._ftp_speed_entry.insert(0, str(self.config_data.get("ftp_speed_limit", 0)))
        self._ftp_speed_entry.pack(side="left")
        ctk.CTkLabel(spd_fr, text=" MB/s  (0 = sin límite)", text_color=PENDING_COLOR,
                     font=ctk.CTkFont(size=11)).pack(side="left", padx=(4, 0))

        # Reintentos automáticos
        ctk.CTkLabel(conn, text="Reintentos en error:").grid(
            row=8, column=0, sticky="e", padx=10, pady=6)
        self._ftp_retries_entry = ctk.CTkEntry(conn, width=80)
        self._ftp_retries_entry.insert(0, str(self.config_data.get("ftp_retries", 3)))
        self._ftp_retries_entry.grid(row=8, column=1, sticky="w", padx=10, pady=6)

        bf = ctk.CTkFrame(conn, fg_color="transparent")
        bf.grid(row=9, column=0, columnspan=2, pady=10)
        ctk.CTkButton(bf, text="Probar conexión", command=self._test_ftp).pack(side="left", padx=4)
        self._ftp_status = ctk.CTkLabel(conn, text="", text_color=PENDING_COLOR)
        self._ftp_status.grid(row=10, column=0, columnspan=2, pady=4)

        # ── TMDB API ──
        tmdb = ctk.CTkFrame(scroll)
        tmdb.grid(row=2, column=0, sticky="nsew", padx=(0, 6), pady=8)
        tmdb.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(tmdb, text="TMDB API",
                     font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, columnspan=2, pady=(12, 8))
        ctk.CTkLabel(tmdb, text="API Key:").grid(row=1, column=0, sticky="e", padx=10, pady=6)
        self._api_key_entry = ctk.CTkEntry(
            tmdb, width=240, show="*",
            placeholder_text="Gratis en themoviedb.org/settings/api")
        self._api_key_entry.insert(0, self.config_data.get("tmdb_api_key", ""))
        self._api_key_entry.grid(row=1, column=1, padx=10, pady=6, sticky="ew")
        ctk.CTkLabel(tmdb, text="Idioma:").grid(row=2, column=0, sticky="e", padx=10, pady=6)
        self._lang_combo = ctk.CTkComboBox(tmdb,
                                            values=["es-ES", "en-US", "pt-BR", "fr-FR", "de-DE", "ja-JP"])
        self._lang_combo.set(self.config_data.get("language", "es-ES"))
        self._lang_combo.grid(row=2, column=1, padx=10, pady=6, sticky="ew")
        self._api_status = ctk.CTkLabel(tmdb, text="", text_color=PENDING_COLOR)
        self._api_status.grid(row=3, column=0, columnspan=2, pady=4)
        bf2 = ctk.CTkFrame(tmdb, fg_color="transparent")
        bf2.grid(row=4, column=0, columnspan=2, pady=8)
        ctk.CTkButton(bf2, text="Validar API Key", command=self._validate_api_key).pack(side="left", padx=4)
        ctk.CTkLabel(tmdb, text="themoviedb.org → Configuración → API",
                     text_color=PENDING_COLOR, font=ctk.CTkFont(size=11)).grid(
            row=5, column=0, columnspan=2, pady=8)

        # ── Plantillas de nombre ──
        tpl = ctk.CTkFrame(scroll)
        tpl.grid(row=2, column=1, sticky="nsew", padx=(6, 0), pady=8)
        tpl.grid_columnconfigure(1, weight=1)
        hdr_tpl = ctk.CTkFrame(tpl, fg_color="transparent")
        hdr_tpl.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(12, 4))
        ctk.CTkLabel(hdr_tpl, text="Plantillas de nombre",
                     font=ctk.CTkFont(size=14, weight="bold")).pack(side="left", padx=12)
        ctk.CTkButton(hdr_tpl, text="? Guía", width=70, height=26,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER,
                      command=self._show_template_guide).pack(side="right", padx=12)
        PRESETS = {
            "tv_template": [
                "{serie} {temporada}x{episodio:02d} {titulo}{ext}",
                "{serie} S{temporada:02d}E{episodio:02d} {titulo}{ext}",
                "{serie} - S{temporada:02d}E{episodio:02d} - {titulo}{ext}",
                "{serie} {temporada}x{episodio:02d}{ext}",
            ],
            "movie_template": [
                "{serie} ({año}){ext}",
                "{serie} [{año}]{ext}",
                "{serie}.{año}{ext}",
                "{serie}{ext}",
            ],
            "anime_template": [
                "{serie} {temporada}x{episodio:03d} {titulo}{ext}",
                "{serie} - {episodio:04d}{ext}",
                "[{serie}] {episodio:04d} {titulo}{ext}",
                "{serie} EP{episodio:04d}{ext}",
            ],
        }
        self._tpl_entries = {}
        for i, (label, key) in enumerate([
            ("TV / Series:", "tv_template"),
            ("Películas:",   "movie_template"),
            ("Anime:",       "anime_template"),
        ], start=1):
            ctk.CTkLabel(tpl, text=label).grid(row=i, column=0, sticky="e", padx=10, pady=8)
            combo = ctk.CTkComboBox(tpl, values=PRESETS[key], width=310)
            combo.set(str(self.config_data.get(key, PRESETS[key][0])))
            combo.grid(row=i, column=1, padx=10, pady=8, sticky="ew")
            self._tpl_entries[key] = combo

        # ── Categorías FTP ──
        self._build_ftp_categories_section(scroll)

        # ── Exportar / Importar configuración ──
        self._build_config_transfer_section(scroll)

        self._load_genres_async()

    def _show_template_guide(self):
        win = ctk.CTkToplevel(self)
        self._apply_icon(win)
        win.title("Guía de plantillas")
        win.geometry("700x520")
        win.grab_set()
        win.lift()
        win.focus_force()
        # Centrar en ventana padre
        win.update_idletasks()
        px = self.winfo_rootx() + self.winfo_width() // 2
        py = self.winfo_rooty() + self.winfo_height() // 2
        win.geometry(f"700x520+{px - 350}+{py - 260}")

        hdr = ctk.CTkFrame(win, fg_color="transparent")
        hdr.pack(fill="x", padx=16, pady=(12, 4))
        ctk.CTkLabel(hdr, text="Guía de plantillas",
                     font=ctk.CTkFont(size=14, weight="bold")).pack(side="left")

        sf = ctk.CTkScrollableFrame(win)
        sf.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self._build_guide_content(sf)

    # ── Exportar / Importar configuración ──

    def _build_config_transfer_section(self, scroll):
        fr = ctk.CTkFrame(scroll)
        fr.grid(row=4, column=0, columnspan=2, sticky="nsew", padx=0, pady=(0, 8))
        fr.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(fr, text="Copia de seguridad de la configuración",
                     font=ctk.CTkFont(size=14, weight="bold")).grid(row=0, column=0, pady=(12, 4))
        ctk.CTkLabel(fr, text="Exporta toda tu configuración (categorías FTP, plantillas, servidor...) a un "
                              "archivo, o impórtala en otra instalación. La contraseña FTP nunca se exporta "
                              "en texto plano — tendrás que volver a introducirla tras importar.",
                     font=ctk.CTkFont(size=11), text_color=PENDING_COLOR, wraplength=900).grid(
            row=1, column=0, pady=(0, 8))
        bf_transfer = ctk.CTkFrame(fr, fg_color="transparent")
        bf_transfer.grid(row=2, column=0, pady=(0, 12))
        ctk.CTkButton(bf_transfer, text="⬇ Exportar configuración", command=self._export_config,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER).pack(side="left", padx=4)
        ctk.CTkButton(bf_transfer, text="⬆ Importar configuración", command=self._import_config,
                      fg_color="transparent", border_width=1).pack(side="left", padx=4)

    def _export_config(self):
        path = filedialog.asksaveasfilename(
            title="Exportar configuración",
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
            initialfile="aRenombrar_config.json",
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.config_data.to_dict(), f, indent=2, ensure_ascii=False)
        except OSError as e:
            self._set_status(f"Error al exportar: {e}", ERROR_COLOR)
            return
        self._set_status(f"Configuración exportada: {Path(path).name}", SUCCESS_COLOR)

    def _import_config(self):
        path = filedialog.askopenfilename(
            title="Importar configuración",
            filetypes=[("JSON", "*.json")],
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            self._set_status(f"Error al leer el archivo: {e}", ERROR_COLOR)
            return
        if not isinstance(data, dict):
            self._set_status("Archivo de configuración inválido", ERROR_COLOR)
            return
        if not messagebox.askyesno(
                "Importar configuración",
                "Esto sobrescribirá tu configuración actual (excepto la contraseña FTP, "
                "que deberás volver a introducir). ¿Continuar?"):
            return
        data.pop("ftp_password", None)   # nunca importar contraseñas en texto plano
        self.config_data.set_many(data)
        self.config_data.save()
        self._reload_settings_widgets()
        self._set_status("Configuración importada", SUCCESS_COLOR)

    def _reload_settings_widgets(self):
        """Refresca todos los widgets de Ajustes con los valores actuales de
        self.config_data — usado tras importar configuración, para que se vea
        reflejada sin tener que reiniciar la aplicación."""
        def _set_entry(entry, value):
            entry.delete(0, "end")
            entry.insert(0, str(value))

        for key, entry in self._ftp_entries.items():
            _set_entry(entry, self.config_data.get(key, ""))
        if self.config_data.get("ftp_use_tls"):
            self._tls_switch.select()
        else:
            self._tls_switch.deselect()
        self._ftp_parallel_combo.set(str(self.config_data.get("ftp_parallel", 1)))
        _set_entry(self._ftp_speed_entry, self.config_data.get("ftp_speed_limit", 0))
        _set_entry(self._ftp_retries_entry, self.config_data.get("ftp_retries", 3))

        _set_entry(self._api_key_entry, self.config_data.get("tmdb_api_key", ""))
        self._lang_combo.set(self.config_data.get("language", "es-ES"))
        self.tmdb.set_api_key(self.config_data.get("tmdb_api_key", ""))
        self.tmdb.set_language(self.config_data.get("language", "es-ES"))

        for key, combo in self._tpl_entries.items():
            combo.set(str(self.config_data.get(key, "")))

        _set_entry(self._watch_folder_entry, self.config_data.get("watch_folder", ""))
        _set_entry(self._poll_interval_entry, self.config_data.get("poll_interval", 10))
        self._auto_action_combo.set(self.config_data.get("auto_action", "Mantener original"))
        conf = int(self.config_data.get("min_confidence", 70))
        self._conf_slider.set(conf)
        self._conf_label.configure(text=f"{conf}%")
        if self.config_data.get("start_with_windows"):
            self._autostart_switch.select()
        else:
            self._autostart_switch.deselect()
        if self.config_data.get("desktop_notifications", True):
            self._notif_switch.select()
        else:
            self._notif_switch.deselect()
        if self.config_data.get("rename_local", True):
            self._rename_local_switch.select()
        else:
            self._rename_local_switch.deselect()
        if self.config_data.get("rename_remote", True):
            self._rename_remote_switch.select()
        else:
            self._rename_remote_switch.deselect()

        saved = self.config_data.get("ftp_categories", {"tv": [], "movie": []})
        self._tv_categories    = [dict(c) for c in saved.get("tv", [])]
        self._movie_categories = [dict(c) for c in saved.get("movie", [])]
        self._render_category_list("tv")
        self._render_category_list("movie")

    # ── Categorías FTP (rutas raíz + clasificación automática por género) ──

    def _build_ftp_categories_section(self, scroll):
        saved = self.config_data.get("ftp_categories", {"tv": [], "movie": []})
        self._tv_categories    = [dict(c) for c in saved.get("tv", [])]
        self._movie_categories = [dict(c) for c in saved.get("movie", [])]
        self._genres_cache     = {"tv": [], "movie": []}

        cats_fr = ctk.CTkFrame(scroll)
        cats_fr.grid(row=3, column=0, columnspan=2, sticky="nsew", padx=0, pady=(0, 8))
        cats_fr.grid_columnconfigure(0, weight=1)
        cats_fr.grid_columnconfigure(1, weight=1)

        hdr_cats = ctk.CTkFrame(cats_fr, fg_color="transparent")
        hdr_cats.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(12, 4))
        hdr_cats.grid_columnconfigure(0, weight=1)
        hdr_cats.grid_columnconfigure(2, weight=1)
        ctk.CTkLabel(hdr_cats, text="Categorías FTP",
                     font=ctk.CTkFont(size=14, weight="bold")).grid(row=0, column=1)
        ctk.CTkButton(hdr_cats, text="🔄 Recargar géneros", width=160, height=26,
                      command=self._load_genres_async).grid(row=0, column=2, sticky="e", padx=12)
        ctk.CTkLabel(cats_fr,
                     text="Cada categoría busca y sube contenido en su propia ruta del servidor. "
                          "La app elige la categoría sola según el género de TMDB — el orden importa "
                          "(la primera que coincida gana); una categoría sin géneros marcados actúa "
                          "como categoría por defecto para lo que no encaje en ninguna otra.",
                     font=ctk.CTkFont(size=11), text_color=PENDING_COLOR, wraplength=900).grid(
            row=1, column=0, columnspan=2, padx=12, pady=(0, 10))

        self._tv_cats_container = ctk.CTkFrame(cats_fr, fg_color="transparent")
        self._tv_cats_container.grid(row=2, column=0, sticky="nsew", padx=(12, 6), pady=(0, 12))
        self._movie_cats_container = ctk.CTkFrame(cats_fr, fg_color="transparent")
        self._movie_cats_container.grid(row=2, column=1, sticky="nsew", padx=(6, 12), pady=(0, 12))

        self._render_category_list("tv")
        self._render_category_list("movie")

    def _new_ftp_category(self) -> dict:
        return {
            "id": new_category_id(),
            "name": "Nueva categoría",
            "genre_ids": [],
            "root": "",
            "template": "{serie}/",
        }

    def _categories_list(self, media_type: str) -> list:
        return self._tv_categories if media_type == "tv" else self._movie_categories

    def _genre_options_for(self, media_type: str, cat: dict):
        """[(id, nombre), ...] a partir de los géneros TMDB ya cargados; si aún
        no han cargado, muestra al menos los ids que la categoría ya tuviera
        guardados (como "ID {n}") para no perder datos existentes."""
        loaded = self._genres_cache.get(media_type) or []
        if loaded:
            return [(g["id"], g["name"]) for g in loaded]
        return [(gid, f"ID {gid}") for gid in (cat.get("genre_ids") or [])]

    def _sync_category_widgets_to_data(self, media_type: str):
        """Vuelca lo que haya tecleado el usuario en los widgets de cada
        tarjeta a los dicts de categoría, antes de reordenar/añadir/quitar o
        repintar la lista (evita perder ediciones no guardadas todavía)."""
        for cat in self._categories_list(media_type):
            if "_name_entry" not in cat:
                continue   # tarjeta recién creada, aún no pintada
            cat["name"] = cat["_name_entry"].get().strip() or cat.get("name", "")
            cat["root"] = cat["_root_entry"].get().strip()
            cat["template"] = cat["_template_entry"].get().strip()
            cat["genre_ids"] = sorted(gid for gid, var in cat["_genre_vars"].items() if var.get())

    def _render_category_list(self, media_type: str):
        container  = self._tv_cats_container if media_type == "tv" else self._movie_cats_container
        categories = self._categories_list(media_type)
        for w in container.winfo_children():
            w.destroy()
        label = "Series" if media_type == "tv" else "Películas"
        ctk.CTkLabel(container, text=f"Categorías de {label}",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", pady=(0, 4))
        n = len(categories)
        for idx, cat in enumerate(categories):
            self._build_category_card(container, media_type, cat, idx, n)
        ctk.CTkButton(container, text="+ Añadir categoría", width=160,
                      command=lambda mt=media_type: self._add_category(mt)).pack(pady=(4, 0), anchor="w")

    def _build_category_card(self, parent, media_type: str, cat: dict, idx: int, total: int):
        card = ctk.CTkFrame(parent, corner_radius=8)
        card.pack(fill="x", pady=4)
        card.grid_columnconfigure(1, weight=1)

        top = ctk.CTkFrame(card, fg_color="transparent")
        top.grid(row=0, column=0, columnspan=2, sticky="ew", padx=8, pady=(8, 4))
        name_entry = ctk.CTkEntry(top, placeholder_text="Nombre de la categoría")
        name_entry.insert(0, cat.get("name", ""))
        name_entry.pack(side="left", fill="x", expand=True)
        if idx > 0:
            ctk.CTkButton(top, text="▲", width=28,
                          command=lambda: self._move_category(media_type, cat, -1)).pack(side="left", padx=(4, 0))
        if idx < total - 1:
            ctk.CTkButton(top, text="▼", width=28,
                          command=lambda: self._move_category(media_type, cat, 1)).pack(side="left", padx=(4, 0))
        ctk.CTkButton(top, text="✕", width=28, fg_color="transparent", border_width=1,
                      border_color=ERROR_COLOR, text_color=ERROR_COLOR,
                      command=lambda: self._remove_category(media_type, cat)).pack(side="left", padx=(4, 0))
        cat["_name_entry"] = name_entry

        ctk.CTkLabel(card, text="Géneros (ninguno marcado = categoría por defecto):",
                     font=ctk.CTkFont(size=11), text_color=PENDING_COLOR).grid(
            row=1, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 2))
        genre_frame = ctk.CTkFrame(card, fg_color="transparent")
        genre_frame.grid(row=2, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 6))
        cat["_genre_vars"] = {}
        options = self._genre_options_for(media_type, cat)
        if not options:
            ctk.CTkLabel(genre_frame, text="(géneros no cargados aún — pulsa 'Recargar géneros')",
                         font=ctk.CTkFont(size=11), text_color=PENDING_COLOR).pack(anchor="w")
        else:
            cur_genre_ids = set(cat.get("genre_ids") or [])
            cols = 3
            for i, (gid, gname) in enumerate(options):
                var = tk.BooleanVar(value=gid in cur_genre_ids)
                ctk.CTkCheckBox(genre_frame, text=gname, variable=var, width=140,
                                font=ctk.CTkFont(size=11)).grid(
                    row=i // cols, column=i % cols, sticky="w", padx=4, pady=2)
                cat["_genre_vars"][gid] = var

        ctk.CTkLabel(card, text="Ruta en el servidor:",
                     font=ctk.CTkFont(size=11), text_color=PENDING_COLOR).grid(
            row=3, column=0, sticky="w", padx=8, pady=(0, 2))
        root_entry = ctk.CTkEntry(card, placeholder_text="/datos2/series")
        root_entry.insert(0, cat.get("root", ""))
        root_entry.grid(row=3, column=1, sticky="ew", padx=8, pady=(0, 2))
        cat["_root_entry"] = root_entry

        ctk.CTkLabel(card, text="Plantilla (relativa a la ruta):",
                     font=ctk.CTkFont(size=11), text_color=PENDING_COLOR).grid(
            row=4, column=0, sticky="w", padx=8, pady=(0, 8))
        tpl_entry = ctk.CTkEntry(card)
        tpl_entry.insert(0, cat.get("template", "{serie}/"))
        tpl_entry.grid(row=4, column=1, sticky="ew", padx=8, pady=(0, 8))
        cat["_template_entry"] = tpl_entry

    def _add_category(self, media_type: str):
        self._sync_category_widgets_to_data(media_type)
        self._categories_list(media_type).append(self._new_ftp_category())
        self._render_category_list(media_type)

    def _remove_category(self, media_type: str, cat: dict):
        self._sync_category_widgets_to_data(media_type)
        lst = self._categories_list(media_type)
        lst[:] = [c for c in lst if c is not cat]
        self._render_category_list(media_type)

    def _move_category(self, media_type: str, cat: dict, delta: int):
        self._sync_category_widgets_to_data(media_type)
        lst = self._categories_list(media_type)
        idx = lst.index(cat)
        new_idx = max(0, min(len(lst) - 1, idx + delta))
        if new_idx != idx:
            lst.pop(idx)
            lst.insert(new_idx, cat)
            self._render_category_list(media_type)

    def _load_genres_async(self):
        def worker():
            try:
                tv_genres    = self.tmdb.get_genres("tv")
                movie_genres = self.tmdb.get_genres("movie")
            except Exception:
                return
            self._genres_cache = {"tv": tv_genres, "movie": movie_genres}
            self.after(0, self._rebuild_all_category_checkboxes)
        threading.Thread(target=worker, daemon=True).start()

    def _rebuild_all_category_checkboxes(self):
        self._sync_category_widgets_to_data("tv")
        self._sync_category_widgets_to_data("movie")
        self._render_category_list("tv")
        self._render_category_list("movie")

    def _build_guide_content(self, scroll):
        def section(title):
            ctk.CTkLabel(scroll, text=title, font=ctk.CTkFont(size=13, weight="bold"),
                         anchor="w").pack(fill="x", pady=(12, 2))
            ctk.CTkFrame(scroll, height=1, fg_color=ACCENT).pack(fill="x", pady=(0, 4))

        def row(var, desc, example=""):
            f = ctk.CTkFrame(scroll, fg_color="transparent")
            f.pack(fill="x", pady=1)
            ctk.CTkLabel(f, text=var, font=ctk.CTkFont(family="Courier", size=12),
                         text_color=ACCENT, width=210, anchor="w").pack(side="left")
            col = ctk.CTkFrame(f, fg_color="transparent")
            col.pack(side="left", fill="x", expand=True)
            ctk.CTkLabel(col, text=desc, anchor="w", font=ctk.CTkFont(size=12)).pack(fill="x")
            if example:
                ctk.CTkLabel(col, text=f"  ej: {example}", anchor="w",
                             font=ctk.CTkFont(size=11), text_color=PENDING_COLOR).pack(fill="x")

        def example_block(template, result):
            f = ctk.CTkFrame(scroll, fg_color=("gray90", "gray20"), corner_radius=6)
            f.pack(fill="x", pady=2, padx=4)
            ctk.CTkLabel(f, text=f"  {template}", anchor="w",
                         font=ctk.CTkFont(family="Courier", size=11),
                         text_color=ACCENT).pack(fill="x", padx=8, pady=(5, 0))
            ctk.CTkLabel(f, text=f"  -> {result}", anchor="w",
                         font=ctk.CTkFont(family="Courier", size=11),
                         text_color=("gray30", "gray70")).pack(fill="x", padx=8, pady=(0, 5))

        section("Variables disponibles")
        row("{serie}",       "Nombre de la serie o pelicula", "Breaking Bad")
        row("{titulo}",      "Titulo del episodio",           "One Minute")
        row("{temporada}",   "Numero de temporada",           "3")
        row("{episodio}",    "Numero de episodio",            "7")
        row("{año}",        "Año de estreno",               "2008")
        row("{ext}",         "Extension (incluye el punto)",  ".mkv")
        section("Formato de numeros")
        row("{episodio:02d}", "Minimo 2 digitos", "07")
        row("{episodio:03d}", "Minimo 3 digitos", "007")
        row("{episodio:04d}", "Minimo 4 digitos", "0007")
        row("{temporada:02d}","Temporada 2 dig.", "03")
        section("Ejemplos - TV")
        example_block("{serie} {temporada}x{episodio:02d} {titulo}{ext}", "Breaking Bad 3x07 One Minute.mkv")
        example_block("{serie} S{temporada:02d}E{episodio:02d} {titulo}{ext}", "Breaking Bad S03E07 One Minute.mkv")
        section("Ejemplos - Peliculas")
        example_block("{serie} ({año}){ext}",  "The Dark Knight (2008).mkv")
        example_block("{año} - {serie}{ext}",  "2008 - The Dark Knight.mkv")
        section("Ejemplos - Anime")
        example_block("{serie} {temporada}x{episodio:03d} {titulo}{ext}", "One Piece 1x078 Nami.mkv")
        example_block("{serie} - {episodio:04d}{ext}", "One Piece - 1078.mkv")

    # -------------------------------------------------------- File actions

    def _on_drop(self, event):
        """Maneja archivos soltados por drag & drop."""
        paths = self._parse_drop_data(event.data)
        videos = [p for p in paths if os.path.isfile(p) and is_video_file(p)]
        folders = [p for p in paths if os.path.isdir(p)]
        for folder in folders:
            videos += [str(f) for f in Path(folder).rglob("*")
                       if f.is_file() and is_video_file(str(f))]
        if videos:
            self._add_entries(videos)
        elif paths:
            self._set_status("No se encontraron archivos de vídeo en lo que soltaste", WARNING_COLOR)

    @staticmethod
    def _parse_drop_data(data):
        """
        Parsea los paths del evento DnD de tkinterdnd2.
        En Windows los paths con espacios vienen entre llaves: {C:\\ruta con espacios\\file.mkv}
        """
        import re
        result = []
        for match in re.finditer(r'\{([^}]+)\}|(\S+)', data):
            path = match.group(1) or match.group(2)
            result.append(path)
        return result

    def _add_files(self):
        paths = filedialog.askopenfilenames(
            title="Seleccionar archivos de video",
            filetypes=[("Video", "*.mkv *.mp4 *.avi *.mov *.m4v *.wmv *.flv *.ts *.m2ts *.webm"),
                       ("Todos", "*.*")],
            initialdir=self.config_data.get("last_dir") or os.path.expanduser("~"),
        )
        if paths:
            self.config_data["last_dir"] = str(Path(paths[0]).parent)
            self._add_entries(list(paths))

    def _add_folder(self):
        folder = filedialog.askdirectory(
            title="Seleccionar carpeta",
            initialdir=self.config_data.get("last_dir") or os.path.expanduser("~"),
        )
        if folder:
            self.config_data["last_dir"] = folder
            videos = [str(f) for f in Path(folder).rglob("*")
                      if f.is_file() and is_video_file(str(f))]
            self._add_entries(videos)

    def _add_entries(self, paths):
        existing = {e.path for e in self.files}
        added = []
        for p in paths:
            if p not in existing:
                entry = FileEntry(p)
                self.files.append(entry)
                added.append(entry)
        self._refresh_table()
        if added:
            self.after(50, self._search_new_entries, added)

    def _clear_files(self):
        if self._upload_running:
            pendientes = [e for e in self.files if e.status != "subido"]
            if pendientes:
                dlg = _ClearDialog(self, len(pendientes))
                if dlg.result == "solo_subidos":
                    self.files = [e for e in self.files if e.status == "subido"]
                elif dlg.result == "todo":
                    self._upload_cancel.set()
                    self.files.clear()
                else:
                    return   # diálogo cerrado sin elegir -> no tocar la lista
                self._refresh_table()   # destruye widgets usando _file_rows antes de vaciarlo
                self._clear_detail()
                return
        self.files.clear()
        self._refresh_table()   # destruye widgets usando _file_rows antes de vaciarlo
        self._clear_detail()

    def _refresh_table(self):
        for row_widgets in self._file_rows:
            for w in row_widgets.values():
                if isinstance(w, (ctk.CTkBaseClass, tk.BaseWidget)):
                    try:
                        w.destroy()
                    except Exception:
                        pass
        self._file_rows.clear()
        if not self.files:
            self._drop_zone.pack(pady=40)
            self._update_status_bar()
            return
        self._drop_zone.pack_forget()

        cw = self._col_widths
        for i, entry in enumerate(self.files):
            rf = ctk.CTkFrame(
                self._file_list_frame,
                fg_color=SELECTED_ROW_COLOR if entry is self._selected_entry else "transparent")
            rf.pack(fill="x", pady=1)

            det = entry.detected
            det_text = (f"{det.get('title','')} S{det.get('season',0):02d}E{det.get('episode',0):02d}"
                        if det.get("season") else det.get("title", ""))

            sc = {"pendiente": PENDING_COLOR, "buscando": WARNING_COLOR, "listo": SUCCESS_COLOR,
                  "error": ERROR_COLOR, "renombrado": ACCENT, "en_cola": QUEUED_COLOR,
                  "subiendo": WARNING_COLOR, "subido": SUCCESS_COLOR,
                  "auto": ACCENT, "omitido": WARNING_COLOR,
                  "esperando_confirmacion": WARNING_COLOR}

            _BF = ctk.CTkFont(size=12)

            # ── Todo pack desde la IZQUIERDA en orden visual; nn expande para llenar el hueco ──
            name_lbl = ctk.CTkLabel(rf, text=_fit_text(entry.name, cw["name"], self._font_name), anchor="w",
                                     font=self._font_name, width=cw["name"])
            name_lbl.pack(side="left", padx=0, pady=2)

            det_lbl = ctk.CTkLabel(rf, text=_fit_text(det_text, cw["det"], self._font_det), anchor="w",
                                    font=self._font_det, text_color=PENDING_COLOR,
                                    width=cw["det"])
            det_lbl.pack(side="left", padx=(4, 0), pady=2)   # mirror sash name|det

            nn_lbl = ctk.CTkLabel(rf, text=_fit_text(entry.new_name, cw["nn"], self._font_nn), anchor="w",
                                   font=self._font_nn,
                                   text_color=ACCENT if entry.new_name else PENDING_COLOR)
            nn_lbl.pack(side="left", padx=(4, 0), pady=2, fill="x", expand=True)  # mirror sash det|nn

            st_lbl = ctk.CTkLabel(rf, text=_status_label(entry.status), width=cw["stat"],
                                   anchor="w", font=ctk.CTkFont(size=11),
                                   text_color=sc.get(entry.status, PENDING_COLOR))
            st_lbl.pack(side="left", padx=(4, 0), pady=2)    # mirror sash nn|stat

            ftp_bar = ctk.CTkProgressBar(rf, height=8, width=cw["bar"], corner_radius=0)
            ftp_bar.set(entry.ftp_progress)
            ftp_bar.pack(side="left", padx=(4, 0), pady=2)   # mirror sash stat|bar

            spd_text = _fmt_speed(entry.ftp_speed) if entry.ftp_speed > 0 else ""
            ftp_speed = ctk.CTkLabel(rf, text=spd_text, width=cw["spd"],
                                      font=ctk.CTkFont(size=11), text_color=PENDING_COLOR)
            ftp_speed.pack(side="left", padx=(4, 0), pady=2) # mirror sash bar|spd

            ftp_up = ctk.CTkButton(rf, text="▲", width=cw["btn"], height=26,
                                    font=_BF, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                    command=lambda e=entry: self._upload_one(e))
            ftp_up.pack(side="left", padx=(4, 0), pady=2)    # mirror sash spd|btns

            play_btn = ctk.CTkButton(rf, text="▶", width=cw["btn"], height=26,
                                      font=_BF, fg_color="transparent", border_width=1,
                                      command=lambda e=entry: self._play_file(e))
            play_btn.pack(side="left", padx=(2, 0), pady=2)

            del_btn = ctk.CTkButton(rf, text="✕", width=cw["btn"], height=26,
                                     font=_BF, fg_color="transparent", border_width=1,
                                     border_color=ERROR_COLOR, text_color=ERROR_COLOR,
                                     hover_color=("gray85", "#3d1010"),
                                     command=lambda e=entry: self._remove_entry(e))
            del_btn.pack(side="left", padx=(2, 2), pady=2)

            for w in (rf, name_lbl, det_lbl, nn_lbl, st_lbl):
                w.bind("<Button-1>", lambda ev, e=entry: self._select_entry(e))
                w.bind("<Button-3>", lambda ev, e=entry: self._show_row_menu(ev, e))

            self._file_rows.append({
                "frame": rf, "name": name_lbl, "detected": det_lbl,
                "new_name": nn_lbl, "status": st_lbl,
                "ftp_bar": ftp_bar, "ftp_speed": ftp_speed,
                "ftp_up": ftp_up, "play_btn": play_btn,
                "entry": entry,
                "_raw_name": entry.name,
                "_raw_det":  det_text,
            })

        self._update_status_bar()

    def _update_row(self, entry):
        sc = {"pendiente": PENDING_COLOR, "buscando": WARNING_COLOR, "listo": SUCCESS_COLOR,
              "error": ERROR_COLOR, "renombrado": ACCENT, "en_cola": QUEUED_COLOR,
              "subiendo": WARNING_COLOR, "subido": SUCCESS_COLOR,
              "auto": ACCENT, "omitido": WARNING_COLOR}
        for row in self._file_rows:
            if row["entry"] is entry:
                row["new_name"].configure(
                    text=_fit_text(entry.new_name, self._col_widths["nn"], self._font_nn),
                    text_color=ACCENT if entry.new_name else PENDING_COLOR)
                row["status"].configure(text=_status_label(entry.status),
                                         text_color=sc.get(entry.status, PENDING_COLOR))
                break
        self._update_status_bar()

    def _upload_one(self, entry):
        """Añade el archivo a la cola. Si hay subida activa lo encola; si no, la inicia."""
        if self._upload_running:
            if entry not in self._upload_queue:
                self._upload_queue.append(entry)
                entry.ftp_progress = 0.0
                entry.ftp_speed    = 0.0
                entry.ftp_status   = "En espera"
                entry.status       = "en_cola"
                self._update_row(entry)
                self.after(0, self._refresh_ftp_columns)
                self._set_status(f"Encolado: {entry.name[:50]}", PENDING_COLOR)
            return
        self._start_ftp_upload([entry])

    def _remove_entry(self, entry):
        self.files = [e for e in self.files if e is not entry]
        self._refresh_table()
        if self._selected_entry is entry:
            self._clear_detail()

    def _select_entry(self, entry):
        prev = self._selected_entry
        self._selected_entry = entry
        for row in self._file_rows:
            if row["entry"] is prev:
                row["frame"].configure(fg_color="transparent")
            if row["entry"] is entry:
                row["frame"].configure(fg_color=SELECTED_ROW_COLOR)
        self._update_detail(entry)
        self._update_status_bar()

    # --------------------------------------------------------- TMDB search

    def _search_new_entries(self, entries: list):
        """Lanza búsqueda TMDB solo para los archivos recién añadidos."""
        if not self.config_data.get("tmdb_api_key"):
            self._set_status("Configura tu API Key de TMDB en Configuración", WARNING_COLOR)
            return
        threading.Thread(
            target=self._search_all_worker,
            args=(entries,),
            daemon=True,
        ).start()

    def _search_all(self):
        if not self.config_data.get("tmdb_api_key"):
            self._set_status("Configura tu API Key de TMDB en Configuracion", WARNING_COLOR)
            return
        threading.Thread(target=self._search_all_worker, daemon=True).start()

    def _search_all_worker(self, entries=None):
        from core.api_client import TMDBClient as _TMDBClient

        if entries is not None:
            pending = [e for e in entries if e.status not in ("renombrado", "subido")]
        else:
            pending = [e for e in self.files if e.status not in ("renombrado", "subido")]
        if not pending:
            return

        api_key = self.config_data["tmdb_api_key"]
        lang    = self.config_data.get("language", "es-ES")
        client  = _TMDBClient(api_key)
        client.set_language(lang)

        for entry in pending:
            # Marcar como buscando
            entry.status = "buscando"
            self.after(0, lambda e=entry: self._update_row(e))
            try:
                self._search_entry(entry, tmdb=client)
            except Exception as ex:
                entry.status    = "error"
                entry.error_msg = str(ex)
            self.after(0, lambda e=entry: self._update_row(e))

        n_ok = sum(1 for e in self.files if e.status == "listo")
        self.after(0, lambda: self._set_status(
            f"Búsqueda completada — {n_ok} encontrados", SUCCESS_COLOR))

    def _search_entry(self, entry, tmdb=None):
        if tmdb is None:
            tmdb = self.tmdb
        det   = entry.detected
        query = det.get("title", "")
        if not query:
            entry.status    = "error"
            entry.error_msg = "No se pudo detectar el nombre"
            return
        results = tmdb.search_multi(query)
        if not results:
            entry.status    = "error"
            entry.error_msg = "Sin resultados en TMDB"
            return
        top = results[0]

        # Calcular confianza: similitud entre el título detectado y el resultado TMDB
        result_title = (top.get("name", "") or top.get("title", "")).lower()
        confidence   = difflib.SequenceMatcher(None, query.lower(), result_title).ratio()
        entry.confidence = round(confidence * 100)

        info = tmdb.build_media_info(top, season=det.get("season"), episode=det.get("episode"))
        entry.media_info = info
        entry.new_name   = self._build_name(info, entry.ext)
        entry.status     = "listo"
        entry.error_msg  = ""
        # Igual que en la búsqueda manual: evita que un reintento automático
        # posterior de AutoWatcher pise este resultado si el archivo venía de
        # la carpeta vigilada y seguía fallando por su cuenta.
        self._mark_auto_processed(entry.path, "identificado_manual", entry.new_name)

    def _build_name(self, info, ext):
        if info.media_type == "movie":
            tpl = self.config_data.get("movie_template")
        elif info.media_type == "anime":
            tpl = self.config_data.get("anime_template")
        else:
            tpl = self.config_data.get("tv_template")
        return build_new_name(info, tpl, ext)

    def _manual_search(self):
        query = self._manual_entry.get().strip()
        if not query or not self._selected_entry:
            return
        self._set_status("Buscando...", WARNING_COLOR)
        threading.Thread(target=self._manual_search_worker, args=(query,), daemon=True).start()

    def _manual_search_worker(self, query):
        try:
            results = self.tmdb.search_multi(query)
            self._tmdb_results = results
            labels = []
            for r in results[:10]:
                if r.get("media_type") == "tv":
                    name = r.get("name", "")
                    year = (r.get("first_air_date", "") or "")[:4]
                else:
                    name = r.get("title", "")
                    year = (r.get("release_date", "") or "")[:4]
                labels.append(f"{name} ({year}) [{r.get('media_type', '')}]")
            self.after(0, lambda: self._result_combo.configure(values=labels))
            if labels:
                # Deja el primer resultado seleccionado y lo previsualiza
                # (póster, sinopsis...) para poder revisarlo antes de decidir
                # — pero NO lo asigna a la entrada todavía, hace falta pulsar
                # "Asignar" explícitamente.
                self.after(0, lambda: self._result_combo.set(labels[0]))
                self.after(0, lambda: self._preview_result(0))
                self.after(0, lambda n=len(labels): self._set_status(
                    f"{n} resultado(s) — revisa y pulsa Asignar", SUCCESS_COLOR))
            else:
                self.after(0, lambda: self._set_status("Sin resultados", WARNING_COLOR))
        except Exception as e:
            self.after(0, lambda: self._set_status(f"Error: {e}", ERROR_COLOR))

    def _on_result_preview(self, value):
        """Al elegir un resultado del desplegable: solo lo muestra en el
        panel de detalles, sin aplicarlo todavía a la entrada seleccionada."""
        vals = self._result_combo.cget("values")
        idx  = vals.index(value) if value in vals else -1
        self._preview_result(idx)

    def _preview_result(self, idx: int):
        if idx < 0 or idx >= len(self._tmdb_results):
            return
        result = self._tmdb_results[idx]
        entry  = self._selected_entry
        det    = entry.detected if entry else {}
        info = self.tmdb.build_media_info(result, season=det.get("season"), episode=det.get("episode"))
        self._set_textbox_text(self._detail_title, info.title)
        ep_text = (f"S{info.season:02d}E{info.episode:02d} - {info.episode_title}"
                   if info.season and info.episode else "")
        self._detail_episode.configure(text=ep_text)
        self._detail_year.configure(text=info.year or "")
        self._detail_confidence.configure(text="")
        self._set_textbox_text(self._detail_error, "")
        self._set_overview_text(info.overview or "")
        if info.poster_url:
            token = object()
            self._poster_token = token
            self._poster_label.configure(image=None, text="…")
            threading.Thread(target=self._load_poster, args=(info.poster_url, token), daemon=True).start()
        else:
            self._poster_token = None
            self._poster_label.configure(image=None, text="Sin poster")

    def _assign_selected_result(self):
        """Aplica a la entrada seleccionada el resultado actualmente elegido
        en el desplegable — el paso explícito que faltaba entre "buscar" y
        "aceptar", para no asignar automáticamente el primero que aparece."""
        entry = self._selected_entry
        if not entry:
            return
        value = self._result_combo.get()
        vals  = self._result_combo.cget("values")
        idx   = vals.index(value) if value in vals else -1
        if idx < 0 or idx >= len(self._tmdb_results):
            self._set_status("Busca y elige un resultado primero", WARNING_COLOR)
            return
        result = self._tmdb_results[idx]
        det  = entry.detected
        info = self.tmdb.build_media_info(result, season=det.get("season"), episode=det.get("episode"))
        entry.media_info = info
        entry.new_name   = self._build_name(info, entry.ext)
        entry.status     = "listo"
        entry.error_msg  = ""
        # Avisar a AutoWatcher de que este archivo ya está identificado a
        # mano: si seguía fallando por su cuenta (p.ej. detección local mala),
        # su reintento automático podía pisar este estado poco después
        # ("omitido" volviendo a aparecer aunque ya lo hubieras arreglado o
        # incluso mientras ya se estaba subiendo).
        self._mark_auto_processed(entry.path, "identificado_manual", entry.new_name)
        self._update_row(entry)
        self._update_detail(entry)
        self._set_status(f"Asignado: {info.title}", SUCCESS_COLOR)

    # -------------------------------------------------------- Detail panel

    @staticmethod
    def _set_textbox_text(widget, text: str):
        """Actualiza el contenido de un CTkTextbox de solo lectura (hay que
        reactivarlo temporalmente para poder cambiar el texto) — usado para
        título, sinopsis y error del panel de detalles, todos seleccionables
        y copiables a diferencia de un CTkLabel normal."""
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        if text:
            widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _set_overview_text(self, text: str):
        self._set_textbox_text(self._detail_overview, text)

    def _update_detail(self, entry):
        info = entry.media_info
        reason = entry.error_msg if entry.status in ("error", "omitido") and entry.error_msg else ""
        prefix = "⚠ Error: " if entry.status == "error" else "⏭ Omitido: "
        self._set_textbox_text(self._detail_error, (prefix + reason) if reason else "")
        if not info:
            self._set_textbox_text(self._detail_title, entry.detected.get("title", entry.name))
            self._detail_episode.configure(text="Sin informacion de TMDB")
            self._detail_year.configure(text="")
            self._detail_confidence.configure(text="")
            self._set_overview_text("")
            self._poster_label.configure(image=None, text="Sin poster")
            return
        self._set_textbox_text(self._detail_title, info.title)
        ep_text = (f"S{info.season:02d}E{info.episode:02d} - {info.episode_title}"
                   if info.season and info.episode else "")
        self._detail_episode.configure(text=ep_text)
        self._detail_year.configure(text=info.year or "")
        # Confianza
        conf = getattr(entry, "confidence", 0)
        if conf > 0:
            if conf >= 85:
                conf_color = SUCCESS_COLOR
            elif conf >= 65:
                conf_color = WARNING_COLOR
            else:
                conf_color = ERROR_COLOR
            self._detail_confidence.configure(
                text=f"Confianza: {conf}%", text_color=conf_color)
        else:
            self._detail_confidence.configure(text="")
        self._set_overview_text(info.overview or "")
        if info.poster_url:
            token = object()                    # token único por carga
            self._poster_token = token
            self._poster_label.configure(image=None, text="…")
            threading.Thread(target=self._load_poster, args=(info.poster_url, token), daemon=True).start()
        else:
            self._poster_token = None
            self._poster_label.configure(image=None, text="Sin poster")

    def _load_poster(self, url, token):
        try:
            r   = requests.get(url, timeout=8)
            img = Image.open(BytesIO(r.content)).resize((180, 260), Image.LANCZOS)
            ctk_img = ctk.CTkImage(img, size=(180, 260))
            # Solo aplicar si no se ha limpiado/cambiado de selección mientras cargaba
            def _apply(t=token, i=ctk_img):
                if getattr(self, "_poster_token", None) == t:
                    self._poster_label.configure(image=i, text="")
                    self._current_poster = i
            self.after(0, _apply)
        except Exception:
            pass

    def _clear_detail(self):
        if self._selected_entry is not None:
            for row in self._file_rows:
                if row["entry"] is self._selected_entry:
                    row["frame"].configure(fg_color="transparent")
                    break
        self._selected_entry = None
        self._poster_token = None          # cancela cualquier carga de póster en curso
        self._current_poster = None
        for lbl in (self._detail_episode, self._detail_year, self._detail_confidence):
            lbl.configure(text="")
        self._set_textbox_text(self._detail_title, "")
        self._set_textbox_text(self._detail_error, "")
        self._set_overview_text("")
        self._poster_label.configure(image=None, text="—")
        self._update_status_bar()

    # ----------------------------------------------------------- Rename

    def _rename_all(self):
        # También reintenta los que fallaron ("error"): p.ej. el modo
        # automático no pudo renombrar porque el destino ya existía en local
        # (episodio mal detectado que coincide con otro ya renombrado) — al
        # reintentarlo a mano ahora sí se pregunta si sobrescribir en vez de
        # fallar en silencio otra vez (ver _rename_worker).
        ready = [e for e in self.files if e.status in ("listo", "error") and e.new_name]
        if not ready:
            self._set_status("No hay archivos listos -- usa Buscar TMDB primero", WARNING_COLOR)
            return
        if not messagebox.askyesno("Confirmar", f"Renombrar {len(ready)} archivo(s)?"):
            return
        self._rename_overwrite_all = False
        threading.Thread(target=self._rename_worker, args=(ready,), daemon=True).start()

    def _rename_worker(self, entries):
        for entry in entries:
            ok, msg = rename_file(entry.path, entry.new_name)
            if not ok and msg.startswith("Ya existe:") and not self._rename_overwrite_all:
                # El destino ya existe en LOCAL (no en el servidor FTP — eso
                # es el otro diálogo, _OverwriteDialog para subidas). El
                # usuario está presente y ha pedido esto explícitamente
                # (botón "Renombrar"), así que aquí sí tiene sentido
                # preguntar en vez de fallar sin más, a diferencia del modo
                # automático desatendido.
                answer = [None]
                ev = threading.Event()
                def _ask(fn=entry.new_name, ans=answer, e=ev):
                    dlg = _OverwriteDialog(
                        self, fn, title="El archivo ya existe en local",
                        message="Ya existe un archivo con ese nombre en la misma carpeta:")
                    ans[0] = dlg.result
                    e.set()
                self.after(0, _ask)
                ev.wait()
                if answer[0] == "all":
                    self._rename_overwrite_all = True
                    ok, msg = rename_file(entry.path, entry.new_name, force_overwrite=True)
                elif answer[0] == "overwrite":
                    ok, msg = rename_file(entry.path, entry.new_name, force_overwrite=True)
                else:   # "skip" (o cerrado sin elegir)
                    entry.status    = "omitido"
                    entry.error_msg = "Omitido: ya existía un archivo local con ese nombre"
                    self.after(0, lambda e=entry: self._update_row(e))
                    continue
            elif not ok and msg.startswith("Ya existe:") and self._rename_overwrite_all:
                ok, msg = rename_file(entry.path, entry.new_name, force_overwrite=True)

            if ok:
                self._mark_auto_processed(entry.path, "renombrado", entry.new_name)
                entry.path   = msg
                entry.status = "renombrado"
            else:
                entry.status    = "error"
                entry.error_msg = msg
            self.after(0, lambda e=entry: self._update_row(e))
        self.after(0, self._sort_files_by_episode)
        self.after(0, lambda: self._set_status("Renombrado completado", SUCCESS_COLOR))

    def _auto_processed_status(self, original_path: str) -> str:
        """Estado registrado en auto_processed.json para original_path, o "" si no hay nada."""
        import json as _json
        try:
            from core.auto_watcher import _processed_db_path
            p = _processed_db_path()
            if not p.exists():
                return ""
            db = _json.loads(p.read_text(encoding="utf-8"))
            return db.get(original_path, {}).get("status", "")
        except Exception:
            return ""

    def _mark_auto_processed(self, original_path: str, status: str, new_name: str = ""):
        """Marca un archivo como procesado en auto_processed.json para que el watcher lo ignore."""
        import json as _json, time as _t
        try:
            from core.auto_watcher import _processed_db_path
            p = _processed_db_path()
            db = {}
            if p.exists():
                try:
                    db = _json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    pass
            db[original_path] = {"status": status, "new_name": new_name, "ts": _t.time()}
            p.write_text(_json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _unmark_auto_processed(self, original_path: str):
        """Quita la marca de auto_processed.json para este archivo, si la
        hay — usado cuando una subida manual empieza (se marca "subiendo")
        pero no llega a completarse, para no dejarlo bloqueado para
        AutoWatcher para siempre."""
        import json as _json
        try:
            from core.auto_watcher import _processed_db_path
            p = _processed_db_path()
            if not p.exists():
                return
            db = _json.loads(p.read_text(encoding="utf-8"))
            if original_path in db:
                del db[original_path]
                p.write_text(_json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _cleanup_stale_uploading_marks(self):
        """Al arrancar, ninguna subida manual puede estar realmente "en
        marcha" todavía (acabamos de abrir la app) — cualquier marca
        "subiendo" que quede en auto_processed.json es forzosamente de una
        sesión anterior cerrada a medias (cierre forzado, cuelgue, etc.).
        Sin esto, ese archivo se quedaría bloqueado para AutoWatcher para
        siempre, porque nada más lo desmarcaría."""
        import json as _json
        try:
            from core.auto_watcher import _processed_db_path
            p = _processed_db_path()
            if not p.exists():
                return
            db = _json.loads(p.read_text(encoding="utf-8"))
            stale = [k for k, v in db.items() if v.get("status") == "subiendo"]
            if not stale:
                return
            for k in stale:
                del db[k]
            p.write_text(_json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _sort_files_by_episode(self):
        """Reordena la lista por temporada y episodio (series primero, luego películas por título)."""
        def _sort_key(e):
            det = e.detected or {}
            season  = det.get("season")  or 0
            episode = det.get("episode") or 0
            # Si tiene temporada/episodio → serie: orden numérico
            # Si no → película u otro: orden alfabético al final
            is_series = bool(season or episode)
            return (0 if is_series else 1, season, episode, e.name.lower())
        self.files.sort(key=_sort_key)
        self._refresh_table()

    def _rename_selected(self):
        entry = self._selected_entry
        if not entry or not entry.new_name:
            self._set_status("Selecciona un archivo con nombre detectado", WARNING_COLOR)
            return
        ok, msg = rename_file(entry.path, entry.new_name)
        if ok:
            entry.path   = msg
            entry.status = "renombrado"
            self._set_status(f"Renombrado: {entry.new_name}", SUCCESS_COLOR)
        else:
            entry.status    = "error"
            entry.error_msg = msg
            self._set_status(f"Error: {msg}", ERROR_COLOR)
        self._update_row(entry)

    # ---------------------------------------------------------------- FTP

    def _upload_all_ftp(self):
        ready = [e for e in self.files if e.status in ("listo", "renombrado") and e.media_info]
        if not ready:
            self._set_status("No hay archivos listos para subir", WARNING_COLOR)
            return
        self._start_ftp_upload(ready)

    def _upload_selected_ftp(self):
        entry = self._selected_entry
        if not entry or not entry.media_info:
            self._set_status("Selecciona un archivo con informacion de TMDB", WARNING_COLOR)
            return
        self._start_ftp_upload([entry])

    def _ensure_ftp(self):
        if self.ftp.is_connected():
            return True
        host = self.config_data.get("ftp_host", "")
        if not host:
            self._set_status("Configura el servidor FTP en la pestania FTP", WARNING_COLOR)
            return False
        self._set_status("Conectando al servidor FTP...", WARNING_COLOR)
        ok, msg = self.ftp.connect(
            host, int(self.config_data.get("ftp_port", 21)),
            self.config_data.get("ftp_user", ""),
            self.config_data.get("ftp_password", ""),
            self.config_data.get("ftp_use_tls", False))
        self._set_status(msg, SUCCESS_COLOR if ok else ERROR_COLOR)
        if ok:
            self.after(100, self._refresh_ftp_space)
        return ok

    def _test_ftp(self):
        host = self._ftp_entries["ftp_host"].get().strip()
        port = int(self._ftp_entries["ftp_port"].get() or 21)
        user = self._ftp_entries["ftp_user"].get().strip()
        pwd  = self._ftp_entries["ftp_password"].get()
        tls  = self._tls_switch.get() in (True, "1", 1)
        self._ftp_status.configure(text="Conectando...", text_color=WARNING_COLOR)
        def worker():
            ok, msg = self.ftp.connect(host, port, user, pwd, tls)
            self.after(0, lambda: self._ftp_status.configure(
                text=msg, text_color=SUCCESS_COLOR if ok else ERROR_COLOR))
            if ok:
                self.after(0, self._refresh_ftp_space)
        threading.Thread(target=worker, daemon=True).start()

    def _browse_watch_folder(self):
        folder = filedialog.askdirectory(
            title="Seleccionar carpeta a vigilar",
            initialdir=self._watch_folder_entry.get().strip() or os.path.expanduser("~"),
        )
        if folder:
            self._watch_folder_entry.delete(0, "end")
            self._watch_folder_entry.insert(0, folder)

    # -------------------------------------------------------- Config actions

    def _validate_api_key(self):
        key = self._api_key_entry.get().strip()
        if not key:
            self._api_status.configure(text="Ingresa una API Key", text_color=ERROR_COLOR)
            return
        self.tmdb.set_api_key(key)
        self._api_status.configure(text="Validando...", text_color=WARNING_COLOR)
        def worker():
            ok  = self.tmdb.validate_key()
            msg = "API Key válida" if ok else "API Key inválida"
            self.after(0, lambda: self._api_status.configure(
                text=msg, text_color=SUCCESS_COLOR if ok else ERROR_COLOR))
        threading.Thread(target=worker, daemon=True).start()

    def _category_to_plain_dict(self, cat: dict) -> dict:
        """Quita las referencias a widgets de un dict de categoría, dejando
        solo los campos que se persisten en config."""
        return {
            "id":        cat.get("id") or self._new_ftp_category()["id"],
            "name":      cat.get("name", ""),
            "genre_ids": cat.get("genre_ids", []),
            "root":      cat.get("root", ""),
            "template":  cat.get("template", "{serie}/"),
        }

    def _collect_settings(self) -> dict:
        """Recoge de los widgets de Ajustes todos los valores actuales, sin
        guardarlos. Se usa tanto para el guardado único como para detectar
        cambios sin guardar al salir de la pestaña."""
        try:
            poll = int(self._poll_interval_entry.get().strip() or 10)
            if poll < 5:
                poll = 5
        except ValueError:
            poll = 10
        try:
            parallel = max(1, min(5, int(self._ftp_parallel_combo.get() or 1)))
        except ValueError:
            parallel = 1
        try:
            speed = max(0.0, float(self._ftp_speed_entry.get().replace(",", ".") or 0))
        except ValueError:
            speed = 0.0
        try:
            retries = max(0, min(10, int(self._ftp_retries_entry.get() or 3)))
        except ValueError:
            retries = 3

        self._sync_category_widgets_to_data("tv")
        self._sync_category_widgets_to_data("movie")

        return {
            "watch_folder":          self._watch_folder_entry.get().strip(),
            "poll_interval":         poll,
            "auto_action":           self._auto_action_combo.get(),
            "start_with_windows":    self._autostart_switch.get() in (True, "1", 1),
            "desktop_notifications": self._notif_switch.get() in (True, "1", 1),
            "min_confidence":        int(self._conf_slider.get()),
            "rename_local":          self._rename_local_switch.get() in (True, "1", 1),
            "rename_remote":         self._rename_remote_switch.get() in (True, "1", 1),

            "ftp_host":        self._ftp_entries["ftp_host"].get().strip(),
            "ftp_port":        int(self._ftp_entries["ftp_port"].get() or 21),
            "ftp_user":        self._ftp_entries["ftp_user"].get().strip(),
            "ftp_password":    self._ftp_entries["ftp_password"].get(),
            "ftp_use_tls":     self._tls_switch.get() in (True, "1", 1),
            "ftp_parallel":    parallel,
            "ftp_speed_limit": speed,
            "ftp_retries":     retries,

            "tmdb_api_key": self._api_key_entry.get().strip(),
            "language":     self._lang_combo.get(),

            "tv_template":    self._tpl_entries["tv_template"].get().strip(),
            "movie_template": self._tpl_entries["movie_template"].get().strip(),
            "anime_template": self._tpl_entries["anime_template"].get().strip(),

            "ftp_categories": {
                "tv":    [self._category_to_plain_dict(c) for c in self._tv_categories],
                "movie": [self._category_to_plain_dict(c) for c in self._movie_categories],
            },
        }

    def _settings_dirty(self) -> bool:
        """True si algún campo de Ajustes difiere de lo último guardado."""
        current = self._collect_settings()
        return any(self.config_data.get(key) != value for key, value in current.items())

    def _save_all_settings(self):
        data = self._collect_settings()
        self.config_data.set_many(data)
        self.config_data.save()
        self._set_autostart(data["start_with_windows"])
        self.tmdb.set_api_key(data["tmdb_api_key"])
        self.tmdb.set_language(data["language"])
        if self._watcher and self._watcher.running:
            self._watcher.poll_interval = data["poll_interval"]
        self._set_status("✓ Configuración guardada", SUCCESS_COLOR)

    def _set_status(self, msg, color=None):
        self._status_lbl.configure(text=msg, text_color=color or ACCENT)

    # ──────────────────────────────────────── Bandeja del sistema ──

    def _get_tray_image(self):
        try:
            if self._icon_path:
                return _PILImage.open(self._icon_path).convert("RGBA")
        except Exception:
            pass
        img = _PILImage.new("RGBA", (64, 64), (0, 0, 0, 0))
        from PIL import ImageDraw
        ImageDraw.Draw(img).ellipse([4, 4, 60, 60], fill=(52, 152, 219, 255))
        return img

    def _setup_tray(self):
        if not _PYSTRAY_AVAILABLE:
            return
        try:
            img  = self._get_tray_image()
            menu = pystray.Menu(
                pystray.MenuItem("Abrir aRenombrar", self._tray_show, default=True),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Salir", self._tray_quit),
            )
            self._tray = pystray.Icon("aRenombrar", img, "aRenombrar", menu)
        except Exception:
            self._tray = None

    def _minimize_to_tray(self):
        if self._tray is None:
            self.iconify()
            return
        self.withdraw()
        if not self._tray_running:
            self._tray_running = True
            threading.Thread(target=self._tray.run, daemon=True).start()

    def _tray_show(self, icon=None, item=None):
        if self._tray and self._tray_running:
            self._tray_running = False
            self._tray.stop()
        self.after(0, self.deiconify)
        self.after(50, self.lift)
        self.after(50, self.focus_force)

    def _tray_quit(self, icon=None, item=None):
        if self._tray:
            self._tray_running = False
            self._tray.stop()
        self.after(0, self._force_quit)

    def _set_autostart(self, enabled: bool):
        """Añade o elimina la entrada de inicio automático en el registro de Windows."""
        if os.name != "nt":
            return
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0, winreg.KEY_SET_VALUE)
            if enabled:
                if getattr(sys, "frozen", False):
                    exe_cmd = f'"{sys.executable}" --minimized'
                else:
                    main_py = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "main.py"))
                    exe_cmd = f'"{sys.executable}" "{main_py}" --minimized'
                winreg.SetValueEx(key, "aRenombrar", 0, winreg.REG_SZ, exe_cmd)
            else:
                try:
                    winreg.DeleteValue(key, "aRenombrar")
                except FileNotFoundError:
                    pass
            winreg.CloseKey(key)
        except Exception as e:
            self._set_status(f"Error registro: {e}", ERROR_COLOR)

    # ──────────────────────────────────────────────────────────────────

    def _on_close(self):
        if self._upload_running:
            dlg = _CloseDialog(self)
            if dlg.result == "close":
                self._upload_cancel.set()
                self._force_quit()
            # "cancel" → no hace nada, sigue subiendo
            return
        self._force_quit()

    def _force_quit(self):
        if self._watcher:
            self._watcher.stop()
        self.ftp.disconnect()
        self.config_data.save()
        self._save_session()
        self.destroy()


    # ──────────────────────── Menú contextual de fila (reordenar / resetear) ──

    def _show_row_menu(self, event, entry):
        menu = tk.Menu(self, tearoff=0)
        try:
            idx = self.files.index(entry)
        except ValueError:
            return
        n = len(self.files)
        if idx > 0:
            menu.add_command(label="▲  Mover arriba",     command=lambda: self._move_entry(entry, -1))
            menu.add_command(label="⏫ Ir al principio",  command=lambda: self._move_entry_to(entry, 0))
        if idx < n - 1:
            menu.add_command(label="▼  Mover abajo",      command=lambda: self._move_entry(entry, 1))
            menu.add_command(label="⏬ Ir al final",       command=lambda: self._move_entry_to(entry, n - 1))
        if entry.status in ("subido", "renombrado", "error"):
            if menu.index("end") is not None:
                menu.add_separator()
            menu.add_command(label="↺  Restablecer (volver a pendiente)",
                             command=lambda: self._reset_entry(entry))
        if menu.index("end") is None:
            return  # nada que mostrar
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _move_entry(self, entry, delta: int):
        try:
            idx = self.files.index(entry)
        except ValueError:
            return
        new_idx = max(0, min(len(self.files) - 1, idx + delta))
        if new_idx != idx:
            self.files.pop(idx)
            self.files.insert(new_idx, entry)
            self._refresh_table()

    def _move_entry_to(self, entry, new_idx: int):
        try:
            self.files.remove(entry)
        except ValueError:
            return
        self.files.insert(new_idx, entry)
        self._refresh_table()

    def _reset_entry(self, entry):
        entry.status       = "pendiente"
        entry.ftp_progress = 0.0
        entry.ftp_speed    = 0.0
        entry.ftp_status   = ""
        self._update_row(entry)
        self._refresh_ftp_columns()

    # ──────────────────────────────────────── Notificaciones de escritorio ──

    def _send_notification(self, title: str, msg: str):
        """Envía una notificación de escritorio si está habilitada en config."""
        if not self.config_data.get("desktop_notifications", True):
            return
        # Primero intentar via pystray (cuando la app está minimizada en bandeja)
        if _PYSTRAY_AVAILABLE and self._tray and self._tray_running:
            try:
                self._tray.notify(msg, title)
                return
            except Exception:
                pass
        # Fallback: globo de la barra de tareas vía PowerShell (Windows)
        if os.name == "nt":
            try:
                safe_title = title.replace("'", "").replace('"', "")[:64]
                safe_msg   = msg.replace("'", "").replace('"', "")[:128]
                ps = (
                    "Add-Type -AssemblyName System.Windows.Forms; "
                    "$n = New-Object System.Windows.Forms.NotifyIcon; "
                    "$n.Icon = [System.Drawing.SystemIcons]::Information; "
                    "$n.Visible = $true; "
                    f"$n.ShowBalloonTip(4000, '{safe_title}', '{safe_msg}', 1); "
                    "Start-Sleep -Seconds 5; $n.Dispose()"
                )
                subprocess.Popen(
                    ["powershell", "-WindowStyle", "Hidden", "-Command", ps],
                    creationflags=0x08000000,   # CREATE_NO_WINDOW
                )
            except Exception:
                pass

    # ──────────────────────────────────────────── Historial de subidas ──

    def _history_path(self) -> Path:
        return _appdata_dir() / "upload_history.json"

    def _load_history(self) -> list:
        try:
            p = self._history_path()
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
        return []

    def _save_history_entry(self, filename: str, remote: str, status: str, size: int, error_msg: str = ""):
        with self._history_lock:
            history = self._load_history()
            history.append({
                "ts":        _time.time(),
                "filename":  filename,
                "remote":    remote,
                "status":    status,
                "size":      size,
                "error_msg": error_msg,
            })
            # Mantener solo los últimos 500 registros
            if len(history) > 500:
                history = history[-500:]
            try:
                self._history_path().write_text(
                    json.dumps(history, ensure_ascii=False, indent=2),
                    encoding="utf-8")
            except Exception:
                pass

    def _show_history(self):
        import datetime
        history = self._load_history()

        win = ctk.CTkToplevel(self)
        self._apply_icon(win)
        win.title("Historial de subidas")
        win.geometry("800x520")
        win.grab_set()
        win.lift()
        win.focus_force()
        # Centrar en ventana padre
        win.update_idletasks()
        px = self.winfo_rootx() + self.winfo_width() // 2
        py = self.winfo_rooty() + self.winfo_height() // 2
        win.geometry(f"800x520+{px - 400}+{py - 260}")

        hdr = ctk.CTkFrame(win, fg_color="transparent")
        hdr.pack(fill="x", padx=16, pady=(12, 4))
        ctk.CTkLabel(hdr, text=f"Historial de subidas  ({len(history)} registros)",
                     font=ctk.CTkFont(size=14, weight="bold")).pack(side="left")
        ctk.CTkButton(hdr, text="🗑 Limpiar historial", width=150,
                      fg_color="transparent", border_width=1,
                      border_color=ERROR_COLOR, text_color=ERROR_COLOR,
                      hover_color=("gray85", "#3d1010"),
                      command=lambda: self._clear_history(win)).pack(side="right")

        sf = ctk.CTkScrollableFrame(win)
        sf.pack(fill="both", expand=True, padx=16, pady=(0, 16))

        # Cabecera
        hdr2 = ctk.CTkFrame(sf, corner_radius=0)
        hdr2.pack(fill="x", pady=(0, 2))
        for text, w in [("Fecha", 130), ("Archivo", 280), ("Destino FTP / motivo", 200), ("Tamaño", 70), ("Estado", 80)]:
            ctk.CTkLabel(hdr2, text=text, font=ctk.CTkFont(weight="bold"),
                         width=w, anchor="w", padx=2).pack(side="left", padx=(2, 1), pady=4)

        sc = {"ok": SUCCESS_COLOR, "error": ERROR_COLOR}
        for entry in reversed(history):
            try:
                ts = datetime.datetime.fromtimestamp(entry.get("ts", 0)).strftime("%d/%m/%Y %H:%M")
            except Exception:
                ts = "—"
            sz = entry.get("size", 0)
            if sz >= 1024 * 1024:
                sz_str = f"{sz / (1024*1024):.1f} MB"
            elif sz >= 1024:
                sz_str = f"{sz // 1024} KB"
            else:
                sz_str = f"{sz} B"
            st = entry.get("status", "ok")
            # Si falló, en esta columna es más útil el motivo que la ruta remota
            error_msg = entry.get("error_msg", "")
            showing_error = st == "error" and bool(error_msg)
            third_col = error_msg if showing_error else entry.get("remote", "")
            row = ctk.CTkFrame(sf, fg_color="transparent")
            row.pack(fill="x", pady=1)
            for text, w in [(ts, 130), (entry.get("filename", "")[:45], 280)]:
                ctk.CTkLabel(row, text=text, width=w, anchor="w",
                             font=ctk.CTkFont(size=11), padx=2).pack(side="left", padx=(2, 1), pady=2)
            third_kwargs = {"text_color": ERROR_COLOR} if showing_error else {}
            ctk.CTkLabel(row, text=third_col[:60], width=200, anchor="w",
                         font=ctk.CTkFont(size=11), padx=2, **third_kwargs
                         ).pack(side="left", padx=(2, 1), pady=2)
            ctk.CTkLabel(row, text=sz_str, width=70, anchor="w",
                         font=ctk.CTkFont(size=11), padx=2).pack(side="left", padx=(2, 1), pady=2)
            ctk.CTkLabel(row, text=st.capitalize(), width=80, anchor="w",
                         font=ctk.CTkFont(size=11), padx=2,
                         text_color=sc.get(st, PENDING_COLOR)).pack(side="left", padx=(2, 1), pady=2)

    def _clear_history(self, win):
        try:
            self._history_path().write_text("[]", encoding="utf-8")
        except Exception:
            pass
        win.destroy()
        self._set_status("Historial borrado", WARNING_COLOR)

    def _session_path(self) -> Path:
        return _appdata_dir() / "session.json"

    def _save_session(self):
        try:
            data = [e.to_dict() for e in self.files]
            self._session_path().write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _load_session(self):
        try:
            p = self._session_path()
            if not p.exists():
                return
            raw = json.loads(p.read_text(encoding="utf-8"))
            self.files = _dedupe_entries([FileEntry.from_dict(d) for d in raw])
            self._refresh_table()
        except Exception:
            pass


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
