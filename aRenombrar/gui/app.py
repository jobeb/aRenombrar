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
from core.ftp_client import FTPClient
from core.auto_watcher import AutoWatcher


ACCENT        = "#1DB954"
ACCENT_HOVER  = "#17a349"
ERROR_COLOR   = "#e74c3c"
WARNING_COLOR = "#f39c12"
SUCCESS_COLOR = "#2ecc71"
PENDING_COLOR = "#95a5a6"


def _truncate(text, max_len):
    return text if len(text) <= max_len else text[:max_len - 1] + "..."

def _fit_text(text: str, px_width: int, font_size: int = 12) -> str:
    """Trunca texto para que quepa en px_width píxeles (aproximado).
    CTkLabel usa width como mínimo, no máximo: si el texto es más ancho el widget se expande
    y desplaza las columnas. Este helper evita ese desbordamiento."""
    if not text or px_width <= 0:
        return ""
    # ~0.62 px por punto de fuente es una buena aproximación para fuentes proporcionales
    px_per_char = font_size * 0.62
    max_chars = max(1, int(px_width / px_per_char))
    if len(text) <= max_chars:
        return text
    return text[:max_chars - 1] + "…"


def _fmt_speed(bps):
    if bps >= 1048576:
        return f"{bps / 1048576:.1f} MB/s"
    return f"{bps / 1024:.0f} KB/s"


class _OverwriteDialog(ctk.CTkToplevel):
    """Diálogo modal con tres opciones: Sobreescribir / Omitir / Sobreescribir todos."""
    def __init__(self, parent, filename: str):
        super().__init__(parent)
        self.result = None          # "overwrite" | "skip" | "all"
        self.title("Archivo ya existe")
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

        ctk.CTkLabel(self, text="El archivo ya existe en el servidor:",
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


class FileEntry:
    def __init__(self, path):
        self.path         = path
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
        if status in ("buscando", "subiendo", "auto"):
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
            )
        return entry


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
        self._upload_slot_of        = {}
        self._upload_skip_events    = []

        self._watcher: AutoWatcher = None
        self._tray        = None
        self._tray_running = False
        self._upload_history: list = []   # historial de subidas en memoria

        ctk.set_appearance_mode(self.config_data.get("appearance", "dark"))
        ctk.set_default_color_theme(self.config_data.get("color_theme", "blue"))

        self.title("aRenombrar")
        self.geometry("1200x820")
        self.minsize(1000, 680)

        try:
            if getattr(sys, "frozen", False):
                base = sys._MEIPASS
            else:
                base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            ico = os.path.join(base, "iconoPrincipal.ico")
            if os.path.exists(ico):
                # iconbitmap establece el icono en la barra de titulo de Windows
                self.iconbitmap(default=ico)
                # CTk puede resetear el icono; re-aplicar tras el primer frame
                self.after(50, lambda: self.iconbitmap(ico))
        except Exception:
            pass

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._setup_tray()
        self._load_session()   # restaurar lista de archivos de la sesión anterior
        if "--minimized" in sys.argv:
            self.after(200, self._minimize_to_tray)
            # Arrancar modo automático si hay carpeta configurada
            self.after(400, self._auto_start_if_configured)

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
        self._config_btn.grid(row=0, column=2, padx=(0, 8))
        self._auto_btn = ctk.CTkButton(
            header, text="⚡ Auto", width=90, height=30,
            fg_color="transparent", border_width=1,
            command=self._toggle_auto)
        self._auto_btn.grid(row=0, column=3, padx=(0, 8))
        self._tray_btn = ctk.CTkButton(
            header, text="⊟", width=36, height=30,
            fg_color="transparent", border_width=1,
            command=self._minimize_to_tray)
        self._tray_btn.grid(row=0, column=4, padx=(0, 16))


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
                self._on_auto_event, self._on_auto_file_event)
            self._watcher.start()
            self._auto_btn.configure(
                text="⏹ Detener", width=90, fg_color="#c0392b", hover_color="#96281b",
                border_width=0)
            self._set_status(f"Vigilando: {folder}", SUCCESS_COLOR)

    def _on_auto_event(self, tipo, msg):
        colors = {"info": ACCENT, "ok": SUCCESS_COLOR,
                  "skip": WARNING_COLOR, "error": ERROR_COLOR}
        self.after(0, lambda: self._set_status(msg, colors.get(tipo, ACCENT)))

    def _on_auto_file_event(self, path, tipo, new_name=None, progress=None, speed=None):
        """Recibe eventos de archivo del AutoWatcher y actualiza la tabla."""
        def _update():
            # Buscar entrada existente por path
            entry = next((e for e in self.files if e.path == path), None)

            if tipo == "start":
                if entry is None:
                    entry = FileEntry(path)
                    entry.status = "auto"
                    self.files.append(entry)
                    self._refresh_table()
                else:
                    entry.status = "auto"
                    self._update_row(entry)
                return

            if entry is None:
                return  # archivo no en lista, ignorar

            if tipo == "renamed":
                entry.new_name = new_name or ""
                entry.status   = "renombrado"
                # Actualizar el path por si el archivo fue renombrado en disco
                self._update_row(entry)

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
                entry.status   = "omitido"
                entry.new_name = new_name or entry.new_name
                self._update_row(entry)

            elif tipo == "error":
                entry.status = "error"
                self._update_row(entry)

        self.after(0, _update)

    def _toggle_config(self):
        if self._config_visible:
            self._config_panel_frame.grid_remove()
            self._files_frame.grid(row=0, column=0, sticky="nsew")
            self._config_visible = False
            self._config_btn.configure(text="⚙  Configuración",
                                        fg_color="transparent", border_width=1)
        else:
            self._files_frame.grid_remove()
            self._config_panel_frame.grid(row=0, column=0, sticky="nsew")
            self._config_visible = True
            self._config_btn.configure(text="\u2190 Volver",
                                        fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                        border_width=0)

    def _build_files_tab(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)

        toolbar = ctk.CTkFrame(parent, fg_color="transparent")
        toolbar.grid(row=0, column=0, sticky="ew", pady=(8, 8))
        ctk.CTkButton(toolbar, text="+ Archivos", command=self._add_files,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER, width=110).pack(side="left", padx=(0, 4))
        ctk.CTkButton(toolbar, text="+ Carpeta", command=self._add_folder,
                      width=100).pack(side="left", padx=(0, 4))
        ctk.CTkButton(toolbar, text="Limpiar", command=self._clear_files,
                      fg_color="transparent", border_width=1, width=80).pack(side="left", padx=(0, 16))
        ctk.CTkButton(toolbar, text="Renombrar", command=self._rename_all,
                      width=100).pack(side="left", padx=(0, 16))
        ctk.CTkButton(toolbar, text="Subir todo", command=self._upload_all_ftp,
                      width=100).pack(side="left", padx=(0, 4))
        ctk.CTkButton(toolbar, text="📋 Historial", command=self._show_history,
                      fg_color="transparent", border_width=1, width=100).pack(side="left", padx=(16, 0))

        body = ctk.CTkFrame(parent, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=0)
        body.grid_rowconfigure(0, weight=1)

        self._file_list_frame = ctk.CTkScrollableFrame(body, label_text="Archivos")
        self._file_list_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self._file_list_frame.grid_columnconfigure(0, weight=1)
        self._CW = self._compute_cw()
        self._hdr_labels = []
        # Enlazar redimensionado adaptativo al canvas interno del CTkScrollableFrame
        self._file_list_frame._parent_canvas.bind(
            "<Configure>", self._on_table_resize)
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
        """Calcula anchos de columna adaptativos. avail_w = px disponibles en la tabla."""
        FIXED = 74 + 110 + 80 + 28*3  # stat+bar+spd+3 botones = 348
        PAD   = 16                     # padding mínimo de CTkScrollableFrame
        if avail_w is None:
            avail_w = 900
        flex = max(0, avail_w - FIXED - PAD)
        name = max(110, int(flex * 0.42))
        det  = max(60,  int(flex * 0.26))
        nn   = max(0,   flex - name - det)
        return dict(name=name, det=det, nn=nn, stat=74, bar=110, spd=80, btn=28)

    def _on_table_resize(self, event):
        new_cw = self._compute_cw(event.width)
        if new_cw == self._CW:
            return
        self._CW = new_cw
        for lbl, key in self._hdr_labels:
            w = new_cw[key] if key != "btns" else new_cw["btn"]*3
            lbl.configure(width=w)
        for row in self._file_rows:
            row["name"].configure(
                width=new_cw["name"],
                text=_fit_text(row.get("_raw_name", ""), new_cw["name"], 12))
            row["detected"].configure(
                width=new_cw["det"],
                text=_fit_text(row.get("_raw_det", ""), new_cw["det"], 11))
            row["new_name"].configure(
                width=new_cw["nn"],
                text=_fit_text(row["entry"].new_name, new_cw["nn"], 11))

    def _build_table_header(self, parent):
        cw = self._CW
        hdr = ctk.CTkFrame(parent, corner_radius=0)
        hdr.pack(fill="x", pady=(0, 2))
        for text, key in [
            ("Nombre original", "name"),
            ("Detectado",       "det"),
            ("Nuevo nombre",    "nn"),
            ("Estado",          "stat"),
            ("Subida FTP",      "bar"),
            ("Vel.",            "spd"),
            ("",                "btns"),
        ]:
            w = cw[key] if key != "btns" else cw["btn"]*3
            lbl = ctk.CTkLabel(hdr, text=text, font=ctk.CTkFont(weight="bold"),
                               width=w, anchor="w", padx=2)
            lbl.pack(side="left", padx=0, pady=4)
            self._hdr_labels.append((lbl, key))

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
        self._result_combo = ctk.CTkComboBox(search_top, values=[],
                                              command=self._on_result_selected)
        self._result_combo.set("")
        self._result_combo.grid(row=3, column=0, columnspan=2, sticky="ew")
        self._tmdb_results = []

        # -- Zona inferior: detalles con scroll --
        scroll = ctk.CTkScrollableFrame(panel, fg_color="transparent", label_text="")
        scroll.grid(row=1, column=0, sticky="nsew", padx=4, pady=(4, 6))
        scroll.columnconfigure(0, weight=1)

        self._poster_label = ctk.CTkLabel(scroll, text="", width=180, height=220)
        self._poster_label.pack(pady=(4, 2))
        self._detail_title = ctk.CTkLabel(scroll, text="", wraplength=215,
                                           font=ctk.CTkFont(size=13, weight="bold"))
        self._detail_title.pack(pady=(4, 0))
        self._detail_episode = ctk.CTkLabel(scroll, text="", wraplength=215,
                                             font=ctk.CTkFont(size=12))
        self._detail_episode.pack()
        self._detail_year = ctk.CTkLabel(scroll, text="", text_color=PENDING_COLOR)
        self._detail_year.pack()
        self._detail_confidence = ctk.CTkLabel(scroll, text="", font=ctk.CTkFont(size=11))
        self._detail_confidence.pack()
        self._detail_overview = ctk.CTkLabel(scroll, text="", wraplength=215,
                                              font=ctk.CTkFont(size=11), text_color=PENDING_COLOR)
        self._detail_overview.pack(pady=4)

        return panel

    # ------------------------------------------- Close warning bar



    # ------------------------------------------------- Queue / upload

    def _start_ftp_upload(self, entries):
        if self._upload_running:
            self._set_status("Ya hay una subida en progreso", WARNING_COLOR)
            return
        if not self._ensure_ftp():
            return
        self._upload_queue = list(entries)
        self._upload_cancel.clear()
        self._upload_skip.clear()
        self._upload_current_idx    = -1
        self._upload_current_remote = ""
        self._upload_overwrite_all  = False
        # Resetear columnas FTP de los archivos en cola
        for entry in entries:
            entry.ftp_progress = 0.0
            entry.ftp_speed    = 0.0
            entry.ftp_status   = "En espera"
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

    def _upload_entry_with(self, entry, ftp_conn, speed_kbs, skip_ev):
        """Sube un único archivo usando la conexión ftp_conn dada. Devuelve (ok, msg)."""
        info = entry.media_info
        if not info:
            self._ftp_row_set(entry, "Sin info TMDB", 0, 0)
            return True, "sin_info"

        tpl = (self.config_data.get("ftp_movie_path_template")
               if info.media_type == "movie"
               else self.config_data.get("ftp_path_template", "/{serie}/Temporada {temporada}/"))
        remote_dir  = ftp_conn.build_remote_path(tpl, info.title, info.season, info.year, info.media_type)
        remote_file = f"{remote_dir.rstrip('/')}/{entry.new_name or Path(entry.path).name}"

        if ftp_conn.file_exists(remote_file) and not self._upload_overwrite_all:
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
            elif answer[0] == "skip":
                self._ftp_row_set(entry, "Omitido", 0, 0)
                return True, "omitido"
            # "overwrite" → continuar

        try:
            local_size = Path(entry.path).stat().st_size
        except OSError:
            local_size = 0
        free = ftp_conn.get_free_space()
        if free is not None and free < local_size:
            self._ftp_row_set(entry, "Sin espacio", 0, 0)
            self.after(0, lambda gb=free/(1024**3): self._set_status(
                f"Disco lleno — libre: {gb:.1f} GB", ERROR_COLOR))
            self._upload_cancel.set()
            return False, "disco_lleno"

        entry.status     = "subiendo"
        entry.ftp_status = "Subiendo..."
        self.after(0, lambda e=entry: self._update_row(e))
        self.after(0, lambda e=entry: self._ftp_row_uploading(e))

        def progress(sent, total_b, spd, e=entry):
            e.ftp_progress = sent / total_b if total_b > 0 else 0
            e.ftp_speed    = spd
            self.after(0, lambda p=e.ftp_progress, s=spd, en=e: self._ftp_row_live(en, p, s))

        ok, msg = ftp_conn.upload_file(
            entry.path, remote_dir, progress,
            cancel_event=self._upload_cancel,
            skip_event=skip_ev,
            speed_limit_kbs=speed_kbs,
            try_resume=True,
        )

        if ok:
            entry.status = "subido"
            self._ftp_row_set(entry, "Subido", 1, 0)
            # Guardar en historial
            try:
                size = Path(entry.path).stat().st_size
            except OSError:
                size = 0
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
            entry.status = "error"
            self._ftp_row_set(entry, "Disco lleno", 0, 0)
            self.after(0, lambda: self._set_status("Disco lleno en servidor", ERROR_COLOR))
            self._upload_cancel.set()
        else:
            entry.status    = "error"
            entry.error_msg = msg
            self._ftp_row_set(entry, "Error", 0, 0)

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
                    # No reintentar si cancelado/saltado/omitido o éxito
                    if ok or msg in ("cancelado", "saltado", "omitido", "sin_info"):
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

        # Iniciar con Windows
        self._autostart_switch = ctk.CTkSwitch(
            auto_fr, text="Iniciar con Windows (minimizado en bandeja)")
        self._autostart_switch.grid(row=5, column=0, columnspan=4, pady=(4, 4))
        if self.config_data.get("start_with_windows", False):
            self._autostart_switch.select()

        # Notificaciones de escritorio
        self._notif_switch = ctk.CTkSwitch(
            auto_fr, text="Notificaciones de escritorio al completar subidas")
        self._notif_switch.grid(row=6, column=0, columnspan=4, pady=(0, 4))
        if self.config_data.get("desktop_notifications", True):
            self._notif_switch.select()

        # Botones guardar
        bf_auto = ctk.CTkFrame(auto_fr, fg_color="transparent")
        bf_auto.grid(row=7, column=0, columnspan=4, pady=(4, 12))
        ctk.CTkButton(bf_auto, text="Guardar configuración auto", command=self._save_auto_config,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER).pack(side="left", padx=4)
        self._auto_config_status = ctk.CTkLabel(bf_auto, text="", text_color=SUCCESS_COLOR)
        self._auto_config_status.pack(side="left", padx=8)

        # ── Conexion FTP ──
        conn = ctk.CTkFrame(scroll)
        conn.grid(row=1, column=0, sticky="nsew", padx=(0, 6), pady=8)
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
        ctk.CTkButton(bf, text="Guardar", command=self._save_ftp_config,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER).pack(side="left", padx=4)
        self._ftp_status = ctk.CTkLabel(conn, text="", text_color=PENDING_COLOR)
        self._ftp_status.grid(row=10, column=0, columnspan=2, pady=4)

        # ── Rutas FTP ──
        paths = ctk.CTkFrame(scroll)
        paths.grid(row=1, column=1, sticky="nsew", padx=(6, 0), pady=8)
        paths.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(paths, text="Rutas en el servidor",
                     font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, columnspan=2, pady=(12, 8))
        ctk.CTkLabel(paths, text="Variables: {serie}, {temporada}, {temporada:02d}, {año}, {tipo}",
                     font=ctk.CTkFont(size=11), text_color=PENDING_COLOR).grid(
            row=1, column=0, columnspan=2, pady=(0, 8))
        self._ftp_path_entries = {}
        for i, (label, key) in enumerate([
            ("Ruta para Series/TV:", "ftp_path_template"),
            ("Ruta para Películas:", "ftp_movie_path_template"),
        ], start=2):
            ctk.CTkLabel(paths, text=label).grid(row=i, column=0, sticky="e", padx=10, pady=8)
            e = ctk.CTkEntry(paths, width=280)
            e.insert(0, str(self.config_data.get(key, "")))
            e.grid(row=i, column=1, padx=10, pady=8, sticky="ew")
            self._ftp_path_entries[key] = e
        ctk.CTkLabel(paths, text="Previsualización:").grid(row=4, column=0, sticky="e", padx=10, pady=4)
        self._ftp_path_preview = ctk.CTkLabel(paths, text="", text_color=ACCENT, wraplength=280)
        self._ftp_path_preview.grid(row=4, column=1, padx=10)
        bf_paths = ctk.CTkFrame(paths, fg_color="transparent")
        bf_paths.grid(row=5, column=0, columnspan=2, pady=8)
        ctk.CTkButton(bf_paths, text="Previsualizar", command=self._preview_ftp_path).pack(side="left", padx=4)
        ctk.CTkButton(bf_paths, text="Guardar rutas", command=self._save_ftp_paths,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER).pack(side="left", padx=4)

        # ── TMDB API ──
        tmdb = ctk.CTkFrame(scroll)
        tmdb.grid(row=2, column=0, sticky="nsew", padx=(0, 6), pady=8)
        tmdb.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(tmdb, text="TMDB API",
                     font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, columnspan=2, pady=(12, 8))
        ctk.CTkLabel(tmdb, text="API Key:").grid(row=1, column=0, sticky="e", padx=10, pady=6)
        self._api_key_entry = ctk.CTkEntry(tmdb, width=240, show="*")
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
        ctk.CTkButton(bf2, text="Guardar", command=self._save_tmdb_config,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER).pack(side="left", padx=4)
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
                      command=self._toggle_template_guide).pack(side="right", padx=12)
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
        ctk.CTkButton(tpl, text="Guardar plantillas", command=self._save_templates,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER).grid(
            row=4, column=0, columnspan=2, pady=8)

        # ── Guia de plantillas (desplegable) ──
        self._guide_scroll_parent = scroll
        self._guide_panel = ctk.CTkScrollableFrame(scroll, height=300)
        self._guide_visible = False
        self._build_guide_content(self._guide_panel)

    def _toggle_template_guide(self):
        if self._guide_visible:
            self._guide_panel.grid_remove()
            self._guide_visible = False
        else:
            self._guide_panel.grid(row=3, column=0, columnspan=2,
                                    sticky="nsew", padx=8, pady=(0, 8))
            self._guide_visible = True

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
        self.files.clear()
        self._file_rows.clear()
        self._refresh_table()
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
            return
        self._drop_zone.pack_forget()

        cw = self._CW
        for i, entry in enumerate(self.files):
            rf = ctk.CTkFrame(self._file_list_frame, fg_color="transparent")
            rf.pack(fill="x", pady=1)

            det = entry.detected
            det_text = (f"{det.get('title','')} S{det.get('season',0):02d}E{det.get('episode',0):02d}"
                        if det.get("season") else det.get("title", ""))

            sc = {"pendiente": PENDING_COLOR, "buscando": WARNING_COLOR, "listo": SUCCESS_COLOR,
                  "error": ERROR_COLOR, "renombrado": ACCENT, "subiendo": WARNING_COLOR, "subido": SUCCESS_COLOR,
                  "auto": ACCENT, "omitido": WARNING_COLOR}

            # padx=2 en el CTkLabel elimina el padding interno del widget (no el de pack)
            name_lbl = ctk.CTkLabel(rf, text=_fit_text(entry.name, cw["name"], 12), anchor="w",
                                     font=ctk.CTkFont(size=12), width=cw["name"], padx=2)
            name_lbl.pack(side="left", padx=0, pady=2)

            det_lbl = ctk.CTkLabel(rf, text=_fit_text(det_text, cw["det"], 11), anchor="w",
                                    font=ctk.CTkFont(size=11), text_color=PENDING_COLOR,
                                    width=cw["det"], padx=2)
            det_lbl.pack(side="left", padx=0, pady=2)

            nn_lbl = ctk.CTkLabel(rf, text=_fit_text(entry.new_name, cw["nn"], 11), anchor="w",
                                   font=ctk.CTkFont(size=11), width=cw["nn"], padx=2,
                                   text_color=ACCENT if entry.new_name else PENDING_COLOR)
            nn_lbl.pack(side="left", padx=0, pady=2)

            st_lbl = ctk.CTkLabel(rf, text=entry.status.capitalize(), width=cw["stat"],
                                   anchor="w", font=ctk.CTkFont(size=11), padx=2,
                                   text_color=sc.get(entry.status, PENDING_COLOR))
            st_lbl.pack(side="left", padx=0, pady=2)

            # Columna barra de progreso FTP
            ftp_bar = ctk.CTkProgressBar(rf, height=8, width=cw["bar"], corner_radius=0)
            ftp_bar.set(entry.ftp_progress)
            ftp_bar.pack(side="left", padx=(4, 4), pady=2)

            # Columna velocidad (solo muestra velocidad, nunca texto de estado)
            spd_text = _fmt_speed(entry.ftp_speed) if entry.ftp_speed > 0 else ""
            ftp_speed = ctk.CTkLabel(rf, text=spd_text, width=cw["spd"],
                                      font=ctk.CTkFont(size=11), text_color=PENDING_COLOR, padx=2)
            ftp_speed.pack(side="left", padx=0, pady=2)

            _BF = ctk.CTkFont(size=12)
            # Botones — padx=1 para que no queden pegados entre sí
            ftp_up = ctk.CTkButton(rf, text="▲", width=cw["btn"], height=26,
                                    font=_BF, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                    command=lambda e=entry: self._upload_one(e))
            ftp_up.pack(side="left", padx=(2, 1), pady=2)

            play_btn = ctk.CTkButton(rf, text="▶", width=cw["btn"], height=26,
                                      font=_BF, fg_color="transparent", border_width=1,
                                      command=lambda e=entry: self._play_file(e))
            play_btn.pack(side="left", padx=1, pady=2)

            del_btn = ctk.CTkButton(rf, text="✕", width=cw["btn"], height=26,
                                     font=_BF, fg_color="transparent", border_width=1,
                                     border_color=ERROR_COLOR, text_color=ERROR_COLOR,
                                     hover_color=("gray85", "#3d1010"),
                                     command=lambda e=entry: self._remove_entry(e))
            del_btn.pack(side="left", padx=(1, 2), pady=2)

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

    def _update_row(self, entry):
        sc = {"pendiente": PENDING_COLOR, "buscando": WARNING_COLOR, "listo": SUCCESS_COLOR,
              "error": ERROR_COLOR, "renombrado": ACCENT, "subiendo": WARNING_COLOR, "subido": SUCCESS_COLOR,
              "auto": ACCENT, "omitido": WARNING_COLOR}
        for row in self._file_rows:
            if row["entry"] is entry:
                row["new_name"].configure(
                    text=_fit_text(entry.new_name, self._CW["nn"], 11),
                    text_color=ACCENT if entry.new_name else PENDING_COLOR)
                row["status"].configure(text=entry.status.capitalize(),
                                         text_color=sc.get(entry.status, PENDING_COLOR))
                break

    def _upload_one(self, entry):
        """Añade el archivo a la cola. Si hay subida activa lo encola; si no, la inicia."""
        if self._upload_running:
            if entry not in self._upload_queue:
                self._upload_queue.append(entry)
                entry.ftp_progress = 0.0
                entry.ftp_speed    = 0.0
                entry.ftp_status   = "En espera"
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
        self._selected_entry = entry
        self._update_detail(entry)

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
        from concurrent.futures import ThreadPoolExecutor
        from core.api_client import TMDBClient as _TMDBClient

        if entries is not None:
            pending = [e for e in entries if e.status not in ("renombrado", "subido")]
        else:
            pending = [e for e in self.files if e.status not in ("renombrado", "subido")]
        if not pending:
            return

        # Marcar todos como "buscando" antes de lanzar hilos
        for entry in pending:
            entry.status = "buscando"
            self.after(0, lambda e=entry: self._update_row(e))

        api_key = self.config_data["tmdb_api_key"]
        lang    = self.config_data.get("language", "es-ES")

        def search_one(entry):
            # Cada hilo tiene su propio cliente para evitar problemas con requests.Session
            client = _TMDBClient(api_key)
            client.set_language(lang)
            try:
                self._search_entry(entry, tmdb=client)
            except Exception as ex:
                entry.status    = "error"
                entry.error_msg = str(ex)
            self.after(0, lambda e=entry: self._update_row(e))

        max_workers = min(5, max(1, len(pending)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            list(executor.map(search_one, pending))

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
                self.after(0, lambda: self._result_combo.set(labels[0]))
                self.after(0, lambda: self._on_result_selected(labels[0]))
                self.after(0, lambda n=len(labels): self._set_status(
                    f"{n} resultado(s)", SUCCESS_COLOR))
            else:
                self.after(0, lambda: self._set_status("Sin resultados", WARNING_COLOR))
        except Exception as e:
            self.after(0, lambda: self._set_status(f"Error: {e}", ERROR_COLOR))

    def _on_result_selected(self, value):
        vals = self._result_combo.cget("values")
        idx  = vals.index(value) if value in vals else 0
        if idx >= len(self._tmdb_results):
            return
        result = self._tmdb_results[idx]
        entry  = self._selected_entry
        if not entry:
            return
        det  = entry.detected
        info = self.tmdb.build_media_info(result, season=det.get("season"), episode=det.get("episode"))
        entry.media_info = info
        entry.new_name   = self._build_name(info, entry.ext)
        entry.status     = "listo"
        self.after(0, lambda: self._update_row(entry))
        self.after(0, lambda: self._update_detail(entry))

    # -------------------------------------------------------- Detail panel

    def _update_detail(self, entry):
        info = entry.media_info
        if not info:
            self._detail_title.configure(text=entry.detected.get("title", entry.name))
            self._detail_episode.configure(text="Sin informacion de TMDB")
            self._detail_year.configure(text="")
            self._detail_confidence.configure(text="")
            self._detail_overview.configure(text="")
            self._poster_label.configure(image=None, text="Sin poster")
            return
        self._detail_title.configure(text=info.title)
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
        self._detail_overview.configure(text=(info.overview or "")[:200])
        if info.poster_url:
            threading.Thread(target=self._load_poster, args=(info.poster_url,), daemon=True).start()
        else:
            self._poster_label.configure(image=None, text="Sin poster")

    def _load_poster(self, url):
        try:
            r   = requests.get(url, timeout=8)
            img = Image.open(BytesIO(r.content)).resize((180, 260), Image.LANCZOS)
            ctk_img = ctk.CTkImage(img, size=(180, 260))
            self.after(0, lambda: self._poster_label.configure(image=ctk_img, text=""))
            self._current_poster = ctk_img
        except Exception:
            pass

    def _clear_detail(self):
        self._selected_entry = None
        for lbl in (self._detail_title, self._detail_episode,
                    self._detail_year, self._detail_confidence, self._detail_overview):
            lbl.configure(text="")
        self._poster_label.configure(image=None, text="")

    # ----------------------------------------------------------- Rename

    def _rename_all(self):
        ready = [e for e in self.files if e.status == "listo" and e.new_name]
        if not ready:
            self._set_status("No hay archivos listos -- usa Buscar TMDB primero", WARNING_COLOR)
            return
        if not messagebox.askyesno("Confirmar", f"Renombrar {len(ready)} archivo(s)?"):
            return
        threading.Thread(target=self._rename_worker, args=(ready,), daemon=True).start()

    def _rename_worker(self, entries):
        for entry in entries:
            ok, msg = rename_file(entry.path, entry.new_name)
            if ok:
                entry.path   = msg
                entry.status = "renombrado"
            else:
                entry.status    = "error"
                entry.error_msg = msg
            self.after(0, lambda e=entry: self._update_row(e))
        self.after(0, self._sort_files_by_episode)
        self.after(0, lambda: self._set_status("Renombrado completado", SUCCESS_COLOR))

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
        threading.Thread(target=worker, daemon=True).start()

    def _browse_watch_folder(self):
        folder = filedialog.askdirectory(
            title="Seleccionar carpeta a vigilar",
            initialdir=self._watch_folder_entry.get().strip() or os.path.expanduser("~"),
        )
        if folder:
            self._watch_folder_entry.delete(0, "end")
            self._watch_folder_entry.insert(0, folder)

    def _save_auto_config(self):
        folder = self._watch_folder_entry.get().strip()
        try:
            interval = int(self._poll_interval_entry.get().strip() or 10)
            if interval < 5:
                interval = 5
        except ValueError:
            interval = 10
        action    = self._auto_action_combo.get()
        autostart = self._autostart_switch.get() in (True, "1", 1)
        notifs    = self._notif_switch.get() in (True, "1", 1)
        self.config_data.set_many({
            "watch_folder":          folder,
            "poll_interval":         interval,
            "auto_action":           action,
            "start_with_windows":    autostart,
            "desktop_notifications": notifs,
        })
        self.config_data.save()
        self._set_autostart(autostart)
        self._auto_config_status.configure(text="✓ Guardado", text_color=SUCCESS_COLOR)
        self.after(3000, lambda: self._auto_config_status.configure(text=""))
        # Si el watcher está activo, actualiza el intervalo
        if self._watcher and self._watcher.running:
            self._watcher.poll_interval = interval

    def _save_ftp_config(self):
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
        self.config_data.set_many({
            "ftp_host":        self._ftp_entries["ftp_host"].get().strip(),
            "ftp_port":        int(self._ftp_entries["ftp_port"].get() or 21),
            "ftp_user":        self._ftp_entries["ftp_user"].get().strip(),
            "ftp_password":    self._ftp_entries["ftp_password"].get(),
            "ftp_use_tls":     self._tls_switch.get() in (True, "1", 1),
            "ftp_parallel":    parallel,
            "ftp_speed_limit": speed,
            "ftp_retries":     retries,
        })
        self.config_data.save()
        self._ftp_status.configure(text="Guardado", text_color=SUCCESS_COLOR)

    def _save_ftp_paths(self):
        for key, entry in self._ftp_path_entries.items():
            self.config_data.set(key, entry.get().strip())
        self.config_data.save()
        self._set_status("Rutas FTP guardadas", SUCCESS_COLOR)

    def _preview_ftp_path(self):
        tpl       = self._ftp_path_entries.get("ftp_path_template")
        movie_tpl = self._ftp_path_entries.get("ftp_movie_path_template")
        path  = self.ftp.build_remote_path(tpl.get(), "Breaking Bad", 3, "2008", "tv") if tpl else ""
        mpath = self.ftp.build_remote_path(movie_tpl.get(), "The Dark Knight", None, "2008", "movie") if movie_tpl else ""
        self._ftp_path_preview.configure(text=f"Serie: {path}\nPelicula: {mpath}")

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

    def _save_tmdb_config(self):
        key  = self._api_key_entry.get().strip()
        lang = self._lang_combo.get()
        self.config_data.set_many({"tmdb_api_key": key, "language": lang})
        self.config_data.save()
        self.tmdb.set_api_key(key)
        self.tmdb.set_language(lang)
        self._api_status.configure(text="Guardado", text_color=SUCCESS_COLOR)

    def _save_templates(self):
        for key, combo in self._tpl_entries.items():
            self.config_data.set(key, combo.get().strip())
        self.config_data.save()
        self._set_status("Plantillas guardadas", SUCCESS_COLOR)

    def _set_status(self, msg, color=None):
        self._status_lbl.configure(text=msg, text_color=color or ACCENT)

    # ──────────────────────────────────────── Bandeja del sistema ──

    def _get_tray_image(self):
        try:
            base = sys._MEIPASS if getattr(sys, "frozen", False) else \
                   os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            ico = os.path.join(base, "iconoPrincipal.ico")
            if os.path.exists(ico):
                return _PILImage.open(ico).convert("RGBA")
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

    def _save_history_entry(self, filename: str, remote: str, status: str, size: int):
        history = self._load_history()
        history.append({
            "ts":       _time.time(),
            "filename": filename,
            "remote":   remote,
            "status":   status,
            "size":     size,
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

        scroll = ctk.CTkScrollableFrame(win, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        scroll.grid_columnconfigure(0, weight=1)

        # Cabecera
        ch = ctk.CTkFrame(scroll, corner_radius=0)
        ch.pack(fill="x", pady=(0, 2))
        for text, w in [("Fecha", 140), ("Archivo", 300), ("Ruta remota", 240), ("Tam.", 90)]:
            ctk.CTkLabel(ch, text=text, font=ctk.CTkFont(weight="bold"),
                         width=w, anchor="w").pack(side="left", padx=4, pady=4)

        if not history:
            ctk.CTkLabel(scroll, text="Sin registros de subidas",
                         text_color=PENDING_COLOR).pack(pady=40)
        else:
            for entry in reversed(history):
                ts  = datetime.datetime.fromtimestamp(entry.get("ts", 0))
                dt  = ts.strftime("%d/%m/%Y %H:%M")
                fn  = entry.get("filename", "")
                rp  = entry.get("remote",   "")
                sz  = entry.get("size", 0)
                sts = entry.get("status", "")
                sz_txt = (f"{sz/(1024**3):.1f} GB" if sz >= 1073741824
                          else f"{sz/1048576:.1f} MB" if sz >= 1048576 else "")
                color = SUCCESS_COLOR if sts == "ok" else ERROR_COLOR

                rf = ctk.CTkFrame(scroll, fg_color="transparent")
                rf.pack(fill="x", pady=1)
                ctk.CTkLabel(rf, text=dt,               width=140, anchor="w",
                             font=ctk.CTkFont(size=11), text_color=color).pack(side="left", padx=4)
                ctk.CTkLabel(rf, text=_fit_text(fn, 290, 11), width=300, anchor="w",
                             font=ctk.CTkFont(size=11)).pack(side="left", padx=2)
                ctk.CTkLabel(rf, text=_fit_text(rp, 230, 11), width=240, anchor="w",
                             font=ctk.CTkFont(size=11), text_color=PENDING_COLOR).pack(side="left", padx=2)
                ctk.CTkLabel(rf, text=sz_txt,           width=90, anchor="w",
                             font=ctk.CTkFont(size=11), text_color=PENDING_COLOR).pack(side="left", padx=2)

    def _clear_history(self, parent_win):
        try:
            self._history_path().write_text("[]", encoding="utf-8")
        except Exception:
            pass
        parent_win.destroy()
        self._set_status("Historial borrado", WARNING_COLOR)

    # ──────────────────────────────────── Persistencia de sesión ──

    def _session_path(self) -> Path:
        return _appdata_dir() / "session.json"

    def _save_session(self):
        try:
            data = []
            for e in self.files:
                if not os.path.exists(e.path):
                    continue
                data.append(e.to_dict())
            self._session_path().write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception:
            pass

    def _load_session(self):
        try:
            p = self._session_path()
            if not p.exists():
                return
            data = json.loads(p.read_text(encoding="utf-8"))
            loaded = []
            existing_paths = {e.path for e in self.files}
            for d in data:
                path = d.get("path", "")
                if not path or not os.path.exists(path):
                    continue
                if path in existing_paths:
                    continue
                entry = FileEntry.from_dict(d)
                loaded.append(entry)
            if loaded:
                self.files.extend(loaded)
                self._refresh_table()
                self._set_status(
                    f"Sesión restaurada — {len(loaded)} archivo(s)", ACCENT)
        except Exception:
            pass


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
