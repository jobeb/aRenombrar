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

import customtkinter as ctk
from PIL import Image, ImageTk
import requests
from io import BytesIO

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    _DND_AVAILABLE = True
except ImportError:
    _DND_AVAILABLE = False

from config import Config, CONFIG_EXPORT_SCHEMA_VERSION
from core.api_client import TMDBClient, detect_episode, MediaInfo, TMDB_IMAGE
from core.renamer import build_new_name, rename_file, is_video_file, get_extension
from core.ftp_client import FTPClient, _ftp_safe, sizes_by_top_level_folder, files_by_top_level_folder
from core.auto_watcher import AutoWatcher
from core.series_match import best_match, match_names_exclusively
from core.ftp_categories import choose_category, new_category_id
from core.upload_slots import UploadSlotManager
from gui.table_view import TableView, ColumnSpec
from core.appdirs import app_data_dir, is_windows, is_macos
from core.applog import get_logger
from core.version import __version__
from core.trending import trending_score, format_trending_score

_log = get_logger("aRenombrar.gui", "app.log")


ACCENT        = "#1DB954"
ACCENT_HOVER  = "#17a349"
ERROR_COLOR   = "#e74c3c"
WARNING_COLOR = "#f39c12"
SUCCESS_COLOR = "#2ecc71"
PENDING_COLOR = "#95a5a6"
CONTAINER_GAP = 8   # separación estándar entre contenedores principales de la UI
QUEUED_COLOR  = "#3498db"
SELECTED_ROW_COLOR = ("#c8e6d0", "#204a34")   # (modo claro, modo oscuro) — fila seleccionada
STATUS_LABELS = {"en_cola": "En cola",
                  "esperando_confirmacion": "Espera"}   # textos de estado que no quedan bien con .capitalize()


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


def _truncate_dropdown_labels(labels: list, px_width: int, font) -> list:
    """Recorta cada etiqueta a px_width con _fit_text, y desambigua las que
    queden idénticas tras recortar (p.ej. dos series con el mismo prefijo
    largo) añadiendo un contador — necesario porque el desplegable y las
    búsquedas posteriores identifican el resultado elegido por el texto
    exacto de la etiqueta (vals.index(value)), y dos etiquetas iguales
    harían que siempre se eligiera la primera."""
    seen = {}
    result = []
    for label in labels:
        short = _fit_text(label, px_width, font)
        if short in seen:
            seen[short] += 1
            suffix = f" ({seen[short]})"
            short = _fit_text(label, max(px_width - font.measure(suffix), 0), font) + suffix
        else:
            seen[short] = 1
        result.append(short)
    return result


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
                 message="El archivo ya existe en el servidor:",
                 overwrite_label="Sobreescribir",
                 all_label="Sobreescribir todos",
                 close_result="skip",
                 show_all_button=True):
        super().__init__(parent)
        parent._apply_icon(self)
        self.result = None          # "overwrite" | "skip" | "all" (u otro valor de close_result)
        self.title(title)
        self.resizable(False, False)
        self.grab_set()
        self.lift()
        self.attributes("-topmost", True)

        ctk.CTkLabel(self, text=message,
                     font=ctk.CTkFont(size=13), wraplength=380, justify="left").pack(padx=24, pady=(20, 4))
        ctk.CTkLabel(self, text=filename, font=ctk.CTkFont(size=11),
                     text_color="#95a5a6", wraplength=380).pack(padx=24, pady=(0, 16))

        bf = ctk.CTkFrame(self, fg_color="transparent")
        bf.pack(padx=24, pady=(0, 20))
        ctk.CTkButton(bf, text=overwrite_label,
                      fg_color="#e67e22", hover_color="#ca6f1e", width=130,
                      command=lambda: self._close("overwrite")).pack(side="left", padx=4)
        ctk.CTkButton(bf, text="Omitir",
                      fg_color="transparent", border_width=1, width=100,
                      command=lambda: self._close("skip")).pack(side="left", padx=4)
        if show_all_button:
            # Solo tiene sentido si hay más de un archivo en la tanda actual
            # — con uno solo, "todos" no aporta nada frente al botón normal.
            ctk.CTkButton(bf, text=all_label,
                          fg_color="#c0392b", hover_color="#96281b", width=150,
                          command=lambda: self._close("all")).pack(side="left", padx=4)

        # El alto se calcula a partir del contenido ya empaquetado (en vez
        # de una altura fija) — con un mensaje largo (varias líneas), una
        # altura fija aplastaba los botones contra el texto, dejándolos
        # ilegibles. Centrado en la ventana padre.
        self.update_idletasks()
        dw = max(430, self.winfo_reqwidth())
        dh = self.winfo_reqheight()
        pw = parent.winfo_rootx() + parent.winfo_width() // 2
        ph = parent.winfo_rooty() + parent.winfo_height() // 2
        self.geometry(f"{dw}x{dh}+{pw - dw//2}+{ph - dh//2}")

        self.protocol("WM_DELETE_WINDOW", lambda: self._close(close_result))
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


class _DubHiddenDialog(ctk.CTkToplevel):
    """Lista de series ocultas por "Ocultar sin doblaje ES" -- para poder
    auditarlas y detectar un veredicto de doblaje equivocado (de TMDB o de
    la IA) que las esconde sin dejar ningún rastro visible en la tabla
    principal (ver App._missing_ep_dub_hidden_rows, caso real: Kung Fu
    Panda -- la IA dijo que los episodios que faltaban no tenían doblaje
    castellano, y sí lo tenían). Solo lectura -- para corregirlo, el
    usuario debe buscar la serie a mano (con el interruptor desactivado)
    y volver a preguntarle a la IA, o desactivar el interruptor entero.

    Se construye una sola vez y se reutiliza (ver
    App._show_missing_ep_dub_hidden_dialog y self._dub_hidden_win) en vez
    de crear un CTkToplevel nuevo cada apertura: customtkinter nunca
    deshace los bind_all(<MouseWheel>/...) globales que registra
    CTkScrollableFrame al construirse, así que recrearlo en cada apertura
    iba acumulando handlers para siempre y ralentizando el scroll de toda
    la app, no solo de este diálogo."""
    def __init__(self, parent, hidden: list):
        super().__init__(parent)
        self._parent = parent
        parent._apply_icon(self)
        self.title("Series ocultas por doblaje")
        self.attributes("-topmost", True)

        self._title_lbl = ctk.CTkLabel(self, font=ctk.CTkFont(size=13, weight="bold"), wraplength=440)
        self._title_lbl.pack(padx=20, pady=(20, 4))
        ctk.CTkLabel(self, text="Búscalas con el interruptor desactivado si crees que el "
                                "doblaje indicado no es correcto.",
                     font=ctk.CTkFont(size=11), text_color=PENDING_COLOR, wraplength=440).pack(padx=20, pady=(0, 12))

        self._scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self._scroll.pack(fill="both", expand=True, padx=20, pady=(0, 12))

        ctk.CTkButton(self, text="Cerrar", width=100, command=self._hide).pack(pady=(0, 16))
        self.protocol("WM_DELETE_WINDOW", self._hide)

        self.refresh(hidden)

    def _hide(self):
        self.grab_release()
        self.withdraw()

    def refresh(self, hidden: list):
        """Repuebla la lista con los datos actuales -- se llama tanto al
        construir el diálogo como cada vez que se reabre (ver
        App._show_missing_ep_dub_hidden_dialog), en vez de crear un
        CTkScrollableFrame nuevo por apertura (ver docstring de la clase)."""
        for w in self._scroll.winfo_children():
            w.destroy()

        plural = "s" if len(hidden) != 1 else ""
        self._title_lbl.configure(text=f"{len(hidden)} serie{plural} con hueco real, "
                                        f"ocultada{plural} por \"Ocultar sin doblaje ES\"")

        for r in hidden:
            row = ctk.CTkFrame(self._scroll, fg_color=("gray90", "gray20"), corner_radius=6)
            row.pack(fill="x", pady=3)
            top = ctk.CTkFrame(row, fg_color="transparent")
            top.pack(fill="x", padx=10, pady=(6, 0))
            ctk.CTkLabel(top, text=r["name"], font=ctk.CTkFont(size=12, weight="bold"),
                        anchor="w").pack(side="left", fill="x", expand=True)
            # Solo tiene sentido repasar el lado TMDB -- un veredicto de la
            # IA (ver App._ask_ai_about_current_missing_ep_show) solo se
            # corrige volviendo a preguntarle a mano, no se puede forzar
            # desde aquí.
            if "doblaje_castellano" not in (r.get("ai_verdict") or {}):
                ctk.CTkLabel(top, text="🔄", cursor="hand2", font=ctk.CTkFont(size=13)).pack(side="right")
                # Oculta el diálogo antes de disparar el repaso -- es modal
                # (grab_set), así que el usuario no vería la barra de
                # progreso de la ventana principal mientras siga abierto, y
                # la lista que muestra quedaría obsoleta en cuanto empiece.
                def _recheck(event, tmdb_id=r["tmdb_id"]):
                    self._hide()
                    self._parent._on_force_recheck(tmdb_id)
                top.winfo_children()[-1].bind("<Button-1>", _recheck)
            ai_verdict = r.get("ai_verdict") or {}
            motivo = ai_verdict.get("motivo", "")
            fuente = "según la IA" if "doblaje_castellano" in ai_verdict else "según TMDB"
            detalle = f"{fuente}" + (f": {motivo}" if motivo else "")
            ctk.CTkLabel(row, text=detalle, font=ctk.CTkFont(size=10), text_color=PENDING_COLOR,
                        anchor="w", wraplength=400).pack(fill="x", padx=10, pady=(0, 6))

        self.update_idletasks()
        pw = self._parent.winfo_rootx() + self._parent.winfo_width() // 2
        ph = self._parent.winfo_rooty() + self._parent.winfo_height() // 2
        dw, dh = 480, 420
        self.geometry(f"{dw}x{dh}+{pw - dw//2}+{ph - dh//2}")
        self.deiconify()
        self.lift()
        self.grab_set()


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


class _RenameReservationsDialog(ctk.CTkToplevel):
    """Diálogo modal al cambiar "Tu nombre" en Ajustes teniendo reservas
    propias con el nombre anterior -- ver App._resolve_app_user_name_change.
    "Traspasarlas" solo se ofrece si hay un nombre nuevo real (no al
    vaciar el campo, no tiene sentido traspasar a "nadie")."""
    def __init__(self, parent, old_name: str, new_name: str):
        super().__init__(parent)
        parent._apply_icon(self)
        self.result = None   # "transfer" | "unprotect" | None (cancelado)
        self.title("Cambio de nombre")
        self.resizable(False, False)
        self.grab_set()
        self.lift()
        self.attributes("-topmost", True)
        self.update_idletasks()
        pw = parent.winfo_rootx() + parent.winfo_width() // 2
        ph = parent.winfo_rooty() + parent.winfo_height() // 2
        dw, dh = 460, 230
        self.geometry(f"{dw}x{dh}+{pw - dw//2}+{ph - dh//2}")

        ctk.CTkLabel(self, text=f"Tienes contenido reservado como \"{old_name}\".",
                     font=ctk.CTkFont(size=13, weight="bold"), wraplength=400).pack(padx=24, pady=(22, 4))
        msg = (f"¿Qué quieres hacer con esas reservas al pasar a llamarte \"{new_name}\"?" if new_name else
               "Vas a quitar tu nombre de Ajustes. ¿Qué quieres hacer con esas reservas?")
        ctk.CTkLabel(self, text=msg, font=ctk.CTkFont(size=11),
                     text_color="#95a5a6", wraplength=400, justify="left").pack(padx=24, pady=(0, 18))

        bf = ctk.CTkFrame(self, fg_color="transparent")
        bf.pack(padx=24, pady=(0, 10))
        if new_name:
            ctk.CTkButton(bf, text="Traspasarlas al nombre nuevo", width=190,
                          fg_color=ACCENT, hover_color=ACCENT_HOVER,
                          command=lambda: self._close("transfer")).pack(side="left", padx=6)
        ctk.CTkButton(bf, text="Desproteger todas", width=150,
                      fg_color="#c0392b", hover_color="#96281b",
                      command=lambda: self._close("unprotect")).pack(side="left", padx=6)
        ctk.CTkButton(self, text="Cancelar", fg_color="transparent", border_width=1, width=120,
                      command=lambda: self._close(None)).pack(pady=(0, 16))

        self.protocol("WM_DELETE_WINDOW", lambda: self._close(None))
        self.wait_window()

    def _close(self, result):
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


class _ConfirmDeleteDialog(ctk.CTkToplevel):
    """Diálogo modal de confirmación antes de borrar algo del servidor de
    verdad -- la acción más peligrosa de toda la app. Deliberadamente sin
    ningún atajo de "aplicar a todos": se pregunta una vez por cada
    elemento, mostrando la ruta exacta y el motivo por el que se propuso
    como candidato, para que la decisión de borrar sea siempre informada."""
    def __init__(self, parent, name: str, ftp_path: str, size_bytes: int, reason: str):
        super().__init__(parent)
        parent._apply_icon(self)
        self.result = False
        self.title("Confirmar borrado")
        self.resizable(False, False)
        self.grab_set()
        self.lift()
        self.attributes("-topmost", True)

        ctk.CTkLabel(self, text="¿Eliminar esto del servidor?",
                     font=ctk.CTkFont(size=14, weight="bold")).pack(padx=24, pady=(22, 6))
        ctk.CTkLabel(self, text=name, font=ctk.CTkFont(size=13, weight="bold"),
                     wraplength=420, justify="left").pack(padx=24, pady=(0, 2))
        ctk.CTkLabel(self, text=ftp_path, font=ctk.CTkFont(size=11), text_color=PENDING_COLOR,
                     wraplength=420, justify="left").pack(padx=24, pady=(0, 8))
        ctk.CTkLabel(self, text=f"{_fmt_size(size_bytes)} -- {reason}",
                     font=ctk.CTkFont(size=12), wraplength=420, justify="left").pack(padx=24, pady=(0, 10))
        ctk.CTkLabel(self, text="Esta acción NO se puede deshacer.",
                     font=ctk.CTkFont(size=12, weight="bold"), text_color=ERROR_COLOR).pack(padx=24, pady=(0, 18))

        bf = ctk.CTkFrame(self, fg_color="transparent")
        bf.pack(padx=24, pady=(0, 20))
        ctk.CTkButton(bf, text="Cancelar", width=120, fg_color="transparent", border_width=1,
                      command=lambda: self._close(False)).pack(side="left", padx=6)
        ctk.CTkButton(bf, text="Sí, eliminar", width=140,
                      fg_color=ERROR_COLOR, hover_color="#96281b",
                      command=lambda: self._close(True)).pack(side="left", padx=6)

        self.update_idletasks()
        dw = max(470, self.winfo_reqwidth())
        dh = self.winfo_reqheight()
        pw = parent.winfo_rootx() + parent.winfo_width() // 2
        ph = parent.winfo_rooty() + parent.winfo_height() // 2
        self.geometry(f"{dw}x{dh}+{pw - dw//2}+{ph - dh//2}")

        self.protocol("WM_DELETE_WINDOW", lambda: self._close(False))
        self.wait_window()

    def _close(self, result: bool):
        self.result = result
        self.destroy()


class _UpdateAvailableDialog(ctk.CTkToplevel):
    """Diálogo modal al detectar una versión más nueva en GitHub Releases
    (ver core/update_check.py). Solo enlaza a la página de la release --
    nunca descarga ni reemplaza nada en sitio."""
    def __init__(self, parent, tag: str):
        super().__init__(parent)
        parent._apply_icon(self)
        self.result = None   # "open" | "skip" | None (cerrado sin elegir)
        self.title("Actualización disponible")
        self.resizable(False, False)
        self.grab_set()
        self.lift()
        self.attributes("-topmost", True)
        self.update_idletasks()
        pw = parent.winfo_rootx() + parent.winfo_width() // 2
        ph = parent.winfo_rooty() + parent.winfo_height() // 2
        dw, dh = 400, 160
        self.geometry(f"{dw}x{dh}+{pw - dw//2}+{ph - dh//2}")

        ctk.CTkLabel(self, text=f"Hay una nueva actualización {tag}.",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(padx=24, pady=(22, 4))
        ctk.CTkLabel(self, text="¿Quieres descargarla?",
                     font=ctk.CTkFont(size=11), text_color="#95a5a6").pack(padx=24, pady=(0, 18))

        bf = ctk.CTkFrame(self, fg_color="transparent")
        bf.pack(padx=24, pady=(0, 20))
        ctk.CTkButton(bf, text="Ir a la release", width=150,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER,
                      command=lambda: self._close("open")).pack(side="left", padx=6)
        ctk.CTkButton(bf, text="Saltar esta versión", width=150,
                      fg_color="transparent", border_width=1,
                      command=lambda: self._close("skip")).pack(side="left", padx=6)

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
        self.remote_dir_override = None   # carpeta remota elegida a mano, sustituye a la calculada por categoría/género
        self._last_known_size_text = ""   # ver App._file_size_text -- último tamaño leído con éxito
        self._last_known_size_bytes = None   # ver App._update_status_bar -- caché para no re-stat()ear todo self.files en cada fila actualizada

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
            "remote_dir_override": self.remote_dir_override,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FileEntry":
        entry = cls(d["path"])
        entry.new_name   = d.get("new_name", "")
        entry.status     = d.get("status", "pendiente")
        entry.confidence = d.get("confidence", 0)
        entry.remote_dir_override = d.get("remote_dir_override")
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
    return app_data_dir()


if _DND_AVAILABLE:
    _AppBase = type("_AppBase", (ctk.CTk, TkinterDnD.DnDWrapper), {})
else:
    _AppBase = ctk.CTk


class App(_AppBase):
    def __init__(self):
        # Instrumentación temporal de arranque -- para localizar qué fase
        # concreta tarda, en vez de seguir adivinando (ver _t: usado en
        # todo __init__). Quitar cuando se identifique y resuelva la
        # causa real de la lentitud.
        _t0 = _time.perf_counter()
        _t_last = [_t0]

        def _mark(label):
            now = _time.perf_counter()
            _log.info("Arranque: %-40s %6.0f ms (total %6.0f ms)",
                      label, (now - _t_last[0]) * 1000, (now - _t0) * 1000)
            _t_last[0] = now

        super().__init__()
        _mark("super().__init__()")
        # Activar drag & drop si tkinterdnd2 esta disponible
        if _DND_AVAILABLE:
            self.TkdndVersion = TkinterDnD._require(self)
        _mark("TkinterDnD._require")
        self.config_data = Config()
        _mark("Config()")
        self.tmdb = TMDBClient(self.config_data["tmdb_api_key"])
        self.ftp  = FTPClient()
        # self.ftp es una única conexión de control compartida con AutoWatcher
        # (ver _toggle_auto) y con varias comprobaciones de fondo de la propia
        # GUI (espacio libre, precarga de carpetas, "Probar conexión", cruce
        # FTP del detector de huecos) -- ftplib no es seguro entre hilos, así
        # que TODAS ellas deben serializarse con este mismo candado, no cada
        # una con el suyo (AutoWatcher tenía uno propio que no protegía nada
        # fuera de sí mismo -- visto de verdad: "550 Failed to change
        # directory" y "200 Switching to Binary mode" tratados como error,
        # ambos síntomas clásicos de respuestas cruzadas entre hilos en la
        # misma conexión).
        self._ftp_cmd_lock = threading.Lock()
        _mark("TMDBClient/FTPClient")
        from core.favorites import load_local_cache
        self._favorites = load_local_cache()
        from core.reservations import load_local_cache as _load_reservations_cache
        self._reservations = _load_reservations_cache()
        self.files = []
        self._selected_entry = None

        self._upload_queue          = []
        self._upload_cancel         = threading.Event()
        self._upload_skip           = threading.Event()
        self._upload_running        = False
        self._upload_current_idx    = -1
        self._upload_current_remote = ""
        self._upload_overwrite_all  = False
        self._upload_duplicate_ignore_all = False
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

        self.title(f"aRenombrar v{__version__}")
        self.geometry("1200x820")
        self.minsize(1000, 680)

        self._icon_path  = None   # .ico — solo Windows, vía iconbitmap()
        self._icon_photo = None   # PNG — macOS/Linux, vía iconphoto(); hay
                                   # que mantener la referencia viva o Tk la
                                   # recolecta y el icono desaparece
        self._icon_png_path = None   # PNG en disco — usado por _get_tray_image()
                                      # en macOS/Linux, donde self._icon_path
                                      # (el .ico) no existe
        try:
            if getattr(sys, "frozen", False):
                base = sys._MEIPASS
            else:
                base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if is_windows():
                # Tk en macOS/Linux no soporta .ico vía iconbitmap.
                ico = os.path.join(base, "iconoPrincipal.ico")
                if os.path.exists(ico):
                    self._icon_path = ico
            else:
                png = os.path.join(base, "IconoSinFondo.png")
                if os.path.exists(png):
                    self._icon_png_path = png
                    self._icon_photo = ImageTk.PhotoImage(Image.open(png))
        except Exception:
            pass
        self._apply_icon(self)
        if self._icon_path or self._icon_photo:
            # CTk puede resetear el icono; re-aplicar tras el primer frame
            self.after(50, lambda: self._apply_icon(self))
        _mark("icono")

        # Logos de Plex/Jellyfin -- para distinguir de un vistazo, en la
        # fila de cada serie de "Episodios que faltan", de qué servidor
        # viene esa información (antes eran los emoji 📺/🎬, sin relación
        # visual con ninguno de los dos servicios). CTkImage escala él
        # solo según el "widget scaling" activo, así que basta con darle
        # el tamaño final deseado (ver origin_source_icon_size más abajo).
        self._plex_logo_img = None
        self._jellyfin_logo_img = None
        try:
            base = sys._MEIPASS if getattr(sys, "frozen", False) else \
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            plex_png = os.path.join(base, "plex_logo.png")
            jellyfin_png = os.path.join(base, "jellyfin_logo.png")
            if os.path.exists(plex_png):
                self._plex_logo_img = ctk.CTkImage(Image.open(plex_png), size=(18, 18))
            if os.path.exists(jellyfin_png):
                self._jellyfin_logo_img = ctk.CTkImage(Image.open(jellyfin_png), size=(18, 18))
        except Exception:
            pass

        self._startup_mark = _mark   # usado dentro de _build_ui, ver ahí
        self._build_ui()
        del self._startup_mark
        _mark("_build_ui() TOTAL")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if is_macos():
            # Cmd+Q (y "Salir de aRenombrar" del menú de la app en la barra
            # superior del sistema) los gestiona Tk directamente en macOS,
            # sin pasar por WM_DELETE_WINDOW — sin este remapeo, se saltaría
            # el diálogo de "hay una subida en curso" y el guardado de
            # sesión/config de _on_close, cerrando la app en seco.
            try:
                self.createcommand('tk::mac::Quit', self._on_close)
            except Exception:
                pass
            try:
                # Clic en el icono del Dock con la ventana oculta (tras
                # minimizar a bandeja): sin esto, la app se queda sin
                # ventana visible y sin forma obvia de recuperarla salvo
                # encontrar el icono en la barra de menú.
                self.createcommand('tk::mac::ReopenApplication', self._tray_show)
            except Exception:
                pass
        self._setup_tray()
        _mark("_setup_tray()")
        self._load_session()   # restaurar lista de archivos de la sesión anterior
        _mark("_load_session()")
        # Sincronización de visionado programada -- a diferencia de
        # AutoWatcher (que solo arranca si se inicia minimizado o al
        # pulsar "Auto"), este hilo se arranca SIEMPRE: una hora
        # programada tiene que poder saltar sin importar cómo se abrió
        # la ventana esta vez. No hace nada si no hay programación
        # activada (comprueba config_data en caliente cada minuto, ver
        # _check_watch_sync_schedule).
        self._start_watch_sync_scheduler()
        _mark("_start_watch_sync_scheduler()")
        self._cleanup_stale_uploading_marks()   # ver docstring: limpia marcas "subiendo" de una sesión anterior cerrada a medias
        _mark("_cleanup_stale_uploading_marks()")
        # Antes, el indicador de espacio libre del toolbar solo se rellenaba
        # tras pulsar "Probar conexión" en Ajustes o al empezar una subida
        # -- así que en un arranque normal se veía vacío sin más. Se conecta
        # solo en segundo plano al arrancar (si hay servidor configurado)
        # para que ya esté relleno desde el primer momento. 3s (no 400ms):
        # con la app arrancando junto a Windows, un intento demasiado
        # pronto puede toparse con la red todavía sin terminar de
        # inicializarse -- visto de verdad: funcionaba al empezar una
        # subida más tarde (red ya lista) pero no en el primer intento.
        # Instrumentación temporal (ver _mark arriba): estas dos miden lo
        # que queda FUERA de __init__ -- el primer ciclo de mainloop() y
        # el primer pintado real de la ventana en pantalla. Todo lo de
        # arriba mide "cuánto tarda en construirse", esto mide "cuánto
        # tarda en verse de verdad" -- si hay un hueco grande entre el
        # total de __init__ y estas dos marcas, el tiempo se va DENTRO de
        # mainloop(), no en nada de lo ya medido.
        self.after(0, lambda: _log.info(
            "Arranque: primer ciclo de mainloop()               total %6.0f ms desde __init__",
            (_time.perf_counter() - _t0) * 1000))

        def _mark_first_map(event=None):
            self.unbind("<Map>", _map_bind_id[0])
            _log.info("Arranque: primera vez visible en pantalla (<Map>)   total %6.0f ms desde __init__",
                      (_time.perf_counter() - _t0) * 1000)
        _map_bind_id = [None]
        _map_bind_id[0] = self.bind("<Map>", _mark_first_map, add="+")

        self.after(3000, self._refresh_ftp_space_at_startup)
        self.after(3500, self._check_for_updates_at_startup)
        # Configuración compartida del servidor (TMDB/IA, plantillas,
        # categorías, servidores de medios, enlaces, cuota de reservas...)
        # -- ver core/server_config.py. Silencioso si no hay ruta
        # configurada o la descarga falla; los valores locales se quedan
        # como estaban.
        self.after(2200, self._sync_server_config_from_ftp)
        # Detector de episodios que faltan: comprobación periódica en
        # segundo plano (silenciosa, sin diálogo) para que la caché esté ya
        # al día cuando el usuario abra el diálogo a mano -- ver
        # _maybe_background_missing_scan.
        self.after(60_000, self._maybe_background_missing_scan)
        # Reanudar el modo automático si estaba en marcha al cerrar la app
        # la última vez (ver _restore_auto_watcher_state) -- siempre, no
        # solo con --minimized: el botón "⚡ Auto" debe recordar su estado
        # tanto si la app se abre a mano como por el autoarranque.
        self.after(400, self._restore_auto_watcher_state)
        if "--minimized" in sys.argv:
            self.after(200, self._minimize_to_tray)

    def _apply_icon(self, window):
        """Aplica el icono de la app a *window* (ventana principal o cualquier
        popup/diálogo Toplevel) — iconbitmap(default=...) no se propaga de
        forma fiable a los Toplevel hijos, así que se aplica explícitamente.
        Windows usa el .ico vía iconbitmap(); macOS/Linux usan el PNG vía
        iconphoto(), que es lo que Tk soporta ahí."""
        try:
            if self._icon_path:
                window.iconbitmap(self._icon_path)
            elif self._icon_photo:
                window.iconphoto(True, self._icon_photo)
        except Exception:
            pass

    # ------------------------------------------------------------------ UI --

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(2, weight=0)

        self._build_header()
        self._startup_mark("  _build_header()")

        self._main_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._main_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=(CONTAINER_GAP, CONTAINER_GAP))
        self._main_frame.grid_columnconfigure(0, weight=1)
        self._main_frame.grid_rowconfigure(0, weight=1)

        # Vista de archivos (siempre presente, se oculta al abrir las otras)
        self._files_frame = ctk.CTkFrame(self._main_frame, fg_color="transparent")
        self._files_frame.grid(row=0, column=0, sticky="nsew")
        self._files_frame.grid_columnconfigure(0, weight=1)
        self._build_files_tab(self._files_frame)
        self._startup_mark("  _build_files_tab()")

        # Vista de episodios que faltan (oculta por defecto) -- pantalla
        # completa igual que Archivos, no un diálogo aparte: es una sección
        # tan importante como la principal.
        self._missing_ep_frame = ctk.CTkFrame(self._main_frame, fg_color="transparent")
        self._missing_ep_frame.grid_columnconfigure(0, weight=1)
        # El peso de fila lo pone _build_missing_episodes_tab sobre su
        # propia fila 2 (la tabla) -- ponerlo aquí también en la fila 0
        # hacía que la barra de título se centrase verticalmente en un
        # hueco enorme (dos filas peleando por el espacio sobrante).
        self._missing_ep_visible = False
        self._build_missing_episodes_tab(self._missing_ep_frame)
        self._startup_mark("  _build_missing_episodes_tab()")

        # Panel de configuracion (oculto por defecto) -- construcción
        # DIFERIDA a la primera vez que se entra de verdad (ver
        # _show_view), no aquí. Medido en real: con las 7 pestañas de
        # Configuración ya construidas (aunque ocultas), Tk tarda ~2.5s
        # extra la primera vez que mapea la ventana en pantalla -- ese
        # coste solo aparece al arrancar mainloop(), no durante la propia
        # construcción de los widgets, así que ninguna instrumentación
        # de tiempo DENTRO de __init__ lo detectaba. Confirmado
        # experimentalmente: saltarse solo esta pestaña (dejando
        # Episodios/Historial/Liberar espacio como estaban) quita casi
        # todo ese hueco; las otras tres apenas contribuían.
        self._config_panel_frame = ctk.CTkFrame(self._main_frame, fg_color="transparent")
        self._config_panel_frame.grid_columnconfigure(0, weight=1)
        self._config_panel_frame.grid_rowconfigure(0, weight=1)
        self._config_visible = False
        self._config_panel_built = False
        self._startup_mark("  _build_config_panel() [diferida]")

        # Diálogos reabribles que se construyen una sola vez y se reutilizan
        # (ver _DubHiddenDialog, _show_template_guide, _open_learned_terms_dialog)
        # en vez de crear un CTkToplevel/CTkScrollableFrame nuevo en cada
        # apertura: customtkinter nunca deshace los bind_all(<MouseWheel>/...)
        # globales que registra CTkScrollableFrame al construirse, así que
        # recrearlo cada vez iba acumulando handlers para siempre y
        # ralentizando el scroll de TODA la app, no solo de ese diálogo.
        self._dub_hidden_win = None
        self._template_guide_win = None
        self._learned_terms_win = None
        self._learned_terms_refresh = None

        # Frenar la sincronización FTP redundante -- ver
        # _sync_favorites_from_ftp/_sync_reservations_from_ftp, ambas
        # llamadas desde _show_view_impl en cada visita a Archivos/
        # Episodios/Liberar espacio/Protegidos.
        self._last_favorites_sync_ts = 0.0
        self._last_reservations_sync_ts = 0.0
        self._last_dub_verdicts_sync_ts = 0.0
        self._last_activity_sync_ts = 0.0

        # Mirror local de los veredictos de doblaje compartidos (ver
        # core/shared_dub_verdicts.py) -- último estado conocido antes de
        # la primera sincronización real, para poder mostrar algo sin
        # esperar a conectar.
        from core.shared_dub_verdicts import load_local_cache as _load_dub_verdicts_cache
        self._shared_dub_verdicts = _load_dub_verdicts_cache()

        # Historial de actividad compartido (ver Historial → "Ver todo el
        # servidor") -- solo vive en memoria, se sincroniza de verdad al
        # entrar en esa pestaña (ver _sync_activity_history_from_ftp);
        # vacío hasta la primera sincronización real de esta sesión.
        self._shared_activity_history = []

        # Historial de subidas -- integrado como una vista más (igual que
        # Episodios/Configuración), no una ventana emergente aparte.
        self._history_frame = ctk.CTkFrame(self._main_frame, fg_color="transparent")
        self._history_frame.grid_columnconfigure(0, weight=1)
        self._history_visible = False
        self._build_history_tab(self._history_frame)
        self._startup_mark("  _build_history_tab()")

        # Liberar espacio -- la vista más delicada de toda la app (el
        # primer paso hacia un borrado real e irreversible en el
        # servidor). Nunca borra nada por su cuenta: solo lista
        # candidatas según los filtros que el usuario active a propósito,
        # y cada borrado exige confirmación individual (ver
        # _build_cleanup_tab).
        self._cleanup_frame = ctk.CTkFrame(self._main_frame, fg_color="transparent")
        self._cleanup_frame.grid_columnconfigure(0, weight=1)
        self._cleanup_visible = False
        self._build_cleanup_tab(self._cleanup_frame)
        self._startup_mark("  _build_cleanup_tab()")

        # Protegidos -- gestión de todo lo reservado (ver core/reservations.py):
        # cuota del usuario actual y botón "Liberar" por fila, tanto para lo
        # reservado desde Archivos como desde Liberar espacio.
        self._protected_frame = ctk.CTkFrame(self._main_frame, fg_color="transparent")
        self._protected_frame.grid_columnconfigure(0, weight=1)
        self._protected_visible = False
        self._build_protected_tab(self._protected_frame)
        self._startup_mark("  _build_protected_tab()")

        # Sincronizar visionado -- pestaña principal, igual que Archivos/
        # Episodios/etc (el emparejamiento de usuarios y la programación
        # horaria viven aparte, en Configuración → Cliente -- ver
        # _build_watch_sync_config_tab).
        self._watch_sync_top_frame = ctk.CTkFrame(self._main_frame, fg_color="transparent")
        self._watch_sync_top_frame.grid_columnconfigure(0, weight=1)
        self._watch_sync_top_visible = False
        self._build_watch_sync_top_tab(self._watch_sync_top_frame)
        self._startup_mark("  _build_watch_sync_top_tab()")

        self._build_status_bar()
        self._startup_mark("  _build_status_bar()")

        # Barra de aviso de cierre (oculta normalmente)


    def _build_header(self):
        header = ctk.CTkFrame(self, height=56, corner_radius=0)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(2, weight=1)
        ctk.CTkLabel(header, text="  aRenombrar",
                     font=ctk.CTkFont(size=20, weight="bold")).grid(
            row=0, column=0, padx=(20, 12), pady=10)

        # Navegación: las 4 vistas como pestañas conectadas (una activa a
        # la vez), en vez de botones de alternancia sueltos -- Archivos
        # tiene su propia pestaña ahora, igual que las demás.
        self._nav_segmented = ctk.CTkSegmentedButton(
            header, values=list(self._NAV_LABELS.values()), height=30, border_width=1,
            command=self._on_nav_segment_changed)
        self._nav_segmented.set(self._NAV_LABELS["files"])
        self._nav_segmented.grid(row=0, column=1, padx=(0, 16))

        self._status_lbl = ctk.CTkLabel(header, text="", text_color=ACCENT,
                                         font=ctk.CTkFont(size=13), anchor="w")
        self._status_lbl.grid(row=0, column=2, padx=10, sticky="w")
        self._ftp_space_lbl = ctk.CTkLabel(
            header, text="", font=ctk.CTkFont(size=12),
            text_color=PENDING_COLOR)
        self._ftp_space_lbl.grid(row=0, column=3, padx=10)

        # Separador visual: a partir de aquí son ACCIONES (guardar,
        # automático, minimizar), no vistas a las que navegar.
        ctk.CTkFrame(header, width=1, height=30, fg_color=("gray70", "gray35")).grid(
            row=0, column=4, sticky="ns", padx=(4, 12))

        self._save_settings_btn = ctk.CTkButton(
            header, text="💾 Guardar configuración", width=170, height=30,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            command=self._save_all_settings)
        self._save_settings_btn.grid(row=0, column=5, padx=(0, 8))
        self._save_settings_btn.grid_remove()   # solo visible dentro de Ajustes
        self._auto_btn = ctk.CTkButton(
            header, text="⚡ Auto", width=90, height=30,
            fg_color="transparent", border_width=1,
            command=self._toggle_auto)
        self._auto_btn.grid(row=0, column=6, padx=(0, 8))
        self._tray_btn = ctk.CTkButton(
            header, text="⊟", width=36, height=30,
            fg_color="transparent", border_width=1,
            command=self._minimize_to_tray)
        self._tray_btn.grid(row=0, column=7, padx=(0, 16))

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
                # Cacheado tras el primer stat() con éxito (ver
                # FileEntry._last_known_size_bytes) -- esta función se llama
                # tras CADA actualización de fila (subida, renombrado,
                # AutoWatcher...), y antes volvía a leer el tamaño en disco
                # de TODOS los archivos de la lista en cada llamada: con una
                # cola de cientos de archivos, cada actualización de fila
                # disparaba un barrido O(n) completo -- O(n²) stat() en
                # total para un lote grande. El tamaño de un vídeo ya en
                # disco no cambia bajo esta app (AutoWatcher espera a que se
                # estabilice antes de procesarlo), así que reutilizarlo es
                # seguro; mismo criterio ya aceptado para
                # _last_known_size_text.
                if e._last_known_size_bytes is None:
                    try:
                        e._last_known_size_bytes = Path(e.path).stat().st_size
                    except OSError:
                        pass
                if e._last_known_size_bytes is not None:
                    total_size += e._last_known_size_bytes
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
            # Suma de la velocidad de TODAS las subidas en curso ahora mismo
            # (entry.ftp_speed, ya lo mantiene vivo el propio callback de
            # progreso de cada subida, ver _upload_one::progress) -- con
            # "Subidas simultáneas" > 1 esto es justo lo que no se ve fila a
            # fila, cuánto se está aprovechando de verdad el ancho de banda
            # disponible en conjunto.
            total_speed = sum(e.ftp_speed for e in self.files if e.status == "subiendo")
            if total_speed > 0:
                parts.append(f"Subiendo: {_fmt_speed(total_speed)}")
            self._status_bar_left.configure(text="   ·   ".join(parts))

        # El texto de la derecha refleja la selección de la vista ACTIVA
        # ahora mismo, no siempre self._selected_entry (que es solo de
        # Archivos) -- antes, estando en Episodios o Liberar espacio con
        # algo pulsado, aquí seguía poniendo "Ningún archivo seleccionado"
        # sin más, aunque sí hubiera algo seleccionado en esa vista.
        view = self._current_view_key()
        if view == "cleanup":
            item = self._cleanup_selected_item
            if item is not None:
                self._status_bar_right.configure(
                    text=f"Seleccionado: {item.name}  ({_fmt_size(item.size_bytes)})")
            else:
                self._status_bar_right.configure(text="Ninguna candidata seleccionada")
        elif view == "missing_ep":
            row = self._missing_ep_current_row
            if row is not None:
                # No hay tamaño que mostrar de verdad -- son episodios que
                # faltan, ni siquiera se han descargado -- pero se deja el
                # mismo formato "Nombre (dato)" que Liberar espacio en vez
                # de omitirlo sin más, para no parecer que falta algo.
                self._status_bar_right.configure(text=f"Seleccionado: {row['name']}  (tamaño desconocido)")
            else:
                self._status_bar_right.configure(text="Ninguna serie seleccionada")
        elif view == "files":
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
        else:
            self._status_bar_right.configure(text="")

    def _preview_remote_path(self, entry) -> str:
        """Ruta FTP de destino para *entry* -- si el usuario la ha fijado a
        mano (columna "Destino" de la tabla), esa gana; si no, se prueba
        primero si ya hay carpeta en OTRA categoría (usando solo lo que ya
        esté en caché -- ver _find_category_with_existing_folder, y
        _prefetch_ftp_category_dirs para cuándo se rellena esa caché) y,
        si no, la que le correspondería por categoría/género. No se
        conecta al servidor por su cuenta ni bloquea la interfaz -- si
        hace falta listar una carpeta que no está ya en caché, eso solo
        pasa de verdad al subir (ver _upload_entry_with)."""
        filename = entry.new_name or Path(entry.path).name
        if entry.remote_dir_override:
            return f"{entry.remote_dir_override.rstrip('/')}/{filename}"
        info = entry.media_info
        if not info:
            return ""
        category = self._category_for(info)
        serie_name = info.title
        if info.media_type == "tv":
            existing_category, existing_name = self._find_category_with_existing_folder(
                self.ftp, info, use_cache_only=True)
            if existing_category:
                category = existing_category
                serie_name = existing_name   # reutilizar el nombre EXACTO ya existente, no el título tal cual de TMDB
        if not category:
            return ""
        root = category.get("root", "")
        if not root:
            return ""
        full_tpl = root.rstrip("/") + "/" + category.get("template", "{serie}/")
        remote_dir = self.ftp.build_remote_path(
            full_tpl, serie_name, info.season, info.year, info.media_type)
        return f"{remote_dir.rstrip('/')}/{filename}"

    # ---------------------------------------------------------------- Files tab

    def _restore_auto_watcher_state(self):
        """Reanuda el modo automático al arrancar SI ya estaba en marcha la
        última vez que se cerró la app (ver "auto_watcher_running" en
        DEFAULTS, config.py) -- sustituye a la lógica anterior, que
        arrancaba siempre que hubiera una carpeta configurada (con
        --minimized, para el autoarranque con Windows/macOS): eso podía
        volver a encender el modo automático solo porque alguna vez se
        configuró una carpeta, aunque el usuario lo hubiera parado a mano
        antes de cerrar. Ahora se llama SIEMPRE al arrancar (no solo con
        --minimized), para que el botón "⚡ Auto" recuerde su estado entre
        reinicios igual que cualquier otro ajuste persistente."""
        was_running = self.config_data.get("auto_watcher_running", False)
        folder = self.config_data.get("watch_folder", "").strip()
        if was_running and folder and not (self._watcher and self._watcher.running):
            self._toggle_auto()

    def _toggle_auto(self):
        if self._watcher and self._watcher.running:
            self._watcher.stop()
            self._watcher = None
            self._auto_btn.configure(
                text="⚡ Auto", width=90, fg_color="transparent", border_width=1)
            self._set_status("Modo automático detenido", PENDING_COLOR)
            self.config_data.set("auto_watcher_running", False)
            self.config_data.save()
        else:
            folder = self.config_data.get("watch_folder", "").strip()
            if not folder:
                self._set_status(
                    "Configura la carpeta vigilada en ⚙ Configuración", WARNING_COLOR)
                return
            self._watcher = AutoWatcher(
                folder, self.config_data, self.tmdb, self.ftp,
                self._on_auto_event, self._on_auto_file_event,
                upload_slots=self._upload_slots, ftp_lock=self._ftp_cmd_lock)
            self._watcher.start()
            self._auto_btn.configure(
                text="⏹ Detener", width=90, fg_color="#c0392b", hover_color="#96281b",
                border_width=0)
            self._set_status(f"Vigilando: {folder}", SUCCESS_COLOR)
            self.config_data.set("auto_watcher_running", True)
            self.config_data.save()

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

            elif tipo == "queued":
                # Ya se resolvió categoría/duplicado/espacio, pero todavía
                # no hay turno de "Subidas simultáneas" -- si varios
                # archivos se detectaron a la vez, todos pasan por aquí casi
                # al mismo tiempo, y solo uno de ellos conseguirá turno real
                # (ver "uploading" más abajo, que si eso sí pisa este
                # estado en cuanto empieza a transferir de verdad).
                entry.status       = "en_cola"
                entry.new_name     = new_name or entry.new_name
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
                self._save_history_entry(fname, path, "ok", 0, local_path=path)
                self._remove_uploaded_episode_from_missing_list(entry.media_info)
                self._refresh_ftp_space()

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

    # Navegación unificada (barra de pestañas segmentada en el header,
    # ver _build_header) -- las 4 vistas (Archivos/Episodios/Historial/
    # Configuración) se tratan igual, una activa a la vez, en vez de
    # botones de alternancia independientes cada uno con su propia lógica.
    _NAV_LABELS = {
        "files": "📁 Archivos",
        "missing_ep": "🔍 Episodios",
        "cleanup": "🗑 Liberar espacio",
        "protected": "🔒 Protegidos",
        "history": "📋 Historial",
        "watch_sync": "🔄 Sincronizar visionado",
        "config": "⚙ Configuración",
    }

    def _current_view_key(self) -> str:
        if self._config_visible:
            return "config"
        if self._missing_ep_visible:
            return "missing_ep"
        if self._history_visible:
            return "history"
        if self._cleanup_visible:
            return "cleanup"
        if self._protected_visible:
            return "protected"
        if self._watch_sync_top_visible:
            return "watch_sync"
        return "files"

    def _on_nav_segment_changed(self, value):
        key = next((k for k, v in self._NAV_LABELS.items() if v == value), "files")
        self._show_view(key)

    def _show_view(self, view_key: str):
        """Cambia a la vista *view_key* -- envoltorio fino sobre
        _show_view_impl solo para medir cuánto tarda un cambio de pantalla
        de verdad (ver app.log, mismo hábito que el "Arranque: ..." de
        __init__) sin tener que instrumentar cada return anticipado de
        _show_view_impl por separado."""
        if view_key == self._current_view_key():
            return
        _t0 = _time.perf_counter()
        try:
            self._show_view_impl(view_key)
        finally:
            _log.info("Vista: _show_view('%s') %6.0f ms", view_key, (_time.perf_counter() - _t0) * 1000)

    def _show_view_impl(self, view_key: str):
        """Cambia a la vista *view_key* ("files"/"missing_ep"/"history"/
        "config"), ocultando la que estuviera activa. Si se sale de
        Ajustes con cambios sin guardar y el usuario cancela, la pestaña
        segmentada vuelve a marcar Configuración en vez de quedarse en el
        valor a medio pulsar."""
        if self._config_visible:
            try:
                dirty = self._settings_dirty()
            except Exception:
                # Si no se puede comprobar con seguridad, asumir que SÍ hay
                # cambios sin guardar -- lo contrario (asumir que no los
                # hay) se probó y arriesga perder cambios de verdad en
                # silencio, que es mucho peor que preguntar de más. Se
                # registra para poder diagnosticar la causa real si vuelve
                # a pasar, en vez de seguir adivinando.
                _log.warning("_settings_dirty() falló al comprobar cambios sin guardar, "
                             "se asume que sí los hay", exc_info=True)
                dirty = True
            if dirty:
                try:
                    dlg_result = _UnsavedSettingsDialog(self).result
                except Exception:
                    # Si el propio diálogo falla al construirse, quedarse en
                    # Configuración (no perder cambios) en vez de salir sin
                    # preguntar -- el usuario sigue pudiendo guardar a mano
                    # con el botón "Guardar configuración", que sigue visible.
                    _log.warning("_UnsavedSettingsDialog falló al construirse, "
                                 "se mantiene la vista en Configuración", exc_info=True)
                    self._nav_segmented.set(self._NAV_LABELS["config"])
                    return
                if dlg_result == "save":
                    self._save_all_settings()
                elif dlg_result != "discard":
                    self._nav_segmented.set(self._NAV_LABELS["config"])
                    return
            self._config_panel_frame.grid_remove()
            self._config_visible = False
            self._save_settings_btn.grid_remove()
        elif self._missing_ep_visible:
            self._missing_ep_frame.grid_remove()
            self._missing_ep_visible = False
        elif self._history_visible:
            self._history_frame.grid_remove()
            self._history_visible = False
        elif self._cleanup_visible:
            self._cleanup_frame.grid_remove()
            self._cleanup_visible = False
        elif self._protected_visible:
            self._protected_frame.grid_remove()
            self._protected_visible = False
        elif self._watch_sync_top_visible:
            self._watch_sync_top_frame.grid_remove()
            self._watch_sync_top_visible = False
        else:
            self._files_frame.grid_remove()

        if view_key == "files":
            self._files_frame.grid(row=0, column=0, sticky="nsew")
            self._sync_favorites_from_ftp()
            self._sync_reservations_from_ftp()
        elif view_key == "missing_ep":
            self._missing_ep_frame.grid(row=0, column=0, sticky="nsew")
            self._missing_ep_visible = True
            self._sync_favorites_from_ftp()
            self._sync_shared_dub_verdicts_from_ftp()
        elif view_key == "history":
            self._history_frame.grid(row=0, column=0, sticky="nsew")
            self._history_visible = True
            self._sync_activity_history_from_ftp()
            self._refresh_history_view()   # por si hay subidas nuevas desde la última vez
        elif view_key == "cleanup":
            self._cleanup_frame.grid(row=0, column=0, sticky="nsew")
            self._cleanup_visible = True
            self._sync_favorites_from_ftp()
            self._sync_reservations_from_ftp()
        elif view_key == "protected":
            self._protected_frame.grid(row=0, column=0, sticky="nsew")
            self._protected_visible = True
            self._render_protected_table()   # con lo que haya en el mirror local, sin esperar al FTP
            self._sync_reservations_from_ftp()
        elif view_key == "watch_sync":
            self._watch_sync_top_frame.grid(row=0, column=0, sticky="nsew")
            self._watch_sync_top_visible = True
            self._refresh_watch_sync_history_view()   # por si hay sincronizaciones nuevas desde la última vez
        elif view_key == "config":
            if not self._config_panel_built:
                # Construcción diferida hasta la primera visita real --
                # ver el comentario en _build_ui sobre por qué. Un pequeño
                # respiro visible aquí (la app ya está abierta y en uso)
                # es preferible al mismo coste escondido en cada arranque.
                self._build_config_panel(self._config_panel_frame)
                self._config_panel_built = True
            self._config_panel_frame.grid(row=0, column=0, sticky="nsew")
            self._config_visible = True
            self._save_settings_btn.grid()

        self._nav_segmented.set(self._NAV_LABELS[view_key])
        self._update_status_bar()   # el texto de la derecha depende de la vista activa (ver _update_status_bar)

    def _build_files_tab(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(0, weight=1)

        body = ctk.CTkFrame(parent, fg_color="transparent")
        body.grid(row=0, column=0, sticky="nsew")
        # Columna 0: tabla de archivos (se expande). Columna 1: separador
        # arrastrable. Columna 2: panel "Buscar en TMDB" (ancho fijo, pero
        # ajustable a mano) -- al arrastrar el separador, la tabla se
        # ensancha o se estrecha sola porque es la única columna con peso.
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=0)
        body.grid_columnconfigure(2, weight=0)
        body.grid_rowconfigure(1, weight=1)

        # Barra de título de la tabla: el propio label_text="Archivos" del
        # CTkScrollableFrame se desactiva (label_text="") para no duplicar
        # el título — esta barra hace también de título, con los botones de
        # gestión de archivos integrados en ella en vez de en un toolbar
        # aparte por encima de la tabla. Tono distinto (mismo gris que las
        # tarjetas de categoría en Ajustes) para diferenciarla del resto.
        # Grid a 3 columnas con las dos exteriores en el mismo grupo
        # "uniform": así se fuerza el mismo ancho en las dos aunque el
        # contenido de cada lado pese distinto (2 botones vs. 3) -- con
        # weight igual pero sin uniform, cada columna solo iguala el
        # ancho SOBRANTE, no el mínimo de partida, y el título queda
        # descentrado hacia el lado con menos botones.
        table_header = ctk.CTkFrame(body, fg_color=("gray90", "gray20"), corner_radius=8)
        table_header.grid(row=0, column=0, sticky="ew", padx=(0, CONTAINER_GAP), pady=(0, CONTAINER_GAP))
        table_header.grid_columnconfigure(0, weight=1, uniform="header_sides")
        table_header.grid_columnconfigure(1, weight=0)
        table_header.grid_columnconfigure(2, weight=1, uniform="header_sides")

        left_fr = ctk.CTkFrame(table_header, fg_color="transparent")
        left_fr.grid(row=0, column=0, sticky="w")
        ctk.CTkButton(left_fr, text="+ Archivos", command=self._add_files,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER, width=110).pack(side="left", padx=(12, 4), pady=8)
        ctk.CTkButton(left_fr, text="+ Carpeta", command=self._add_folder,
                      width=100).pack(side="left", padx=(0, 4), pady=8)

        ctk.CTkLabel(table_header, text="Archivos", font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=1, padx=16, pady=8)

        right_fr = ctk.CTkFrame(table_header, fg_color="transparent")
        right_fr.grid(row=0, column=2, sticky="e")
        # Derecha: Subir todo, Renombrar, Limpiar (pack right en orden inverso al visual)
        ctk.CTkButton(right_fr, text="Subir todo", command=self._upload_all_ftp,
                      width=100).pack(side="right", padx=(4, 12), pady=8)
        ctk.CTkButton(right_fr, text="Renombrar", command=self._rename_all,
                      width=100).pack(side="right", padx=4, pady=8)
        ctk.CTkButton(right_fr, text="Limpiar", command=self._clear_files,
                      fg_color="transparent", border_width=1, width=80).pack(side="right", padx=4, pady=8)

        # Fuentes compartidas por las columnas truncadas — se reutilizan tanto para
        # dibujar el texto como para medirlo en _fit_text, así ambos ancho coinciden.
        self._font_name = ctk.CTkFont(size=12)
        self._font_det  = ctk.CTkFont(size=11)
        self._font_nn   = ctk.CTkFont(size=11)
        # Igual que las de arriba -- compartidas para no crear un CTkFont
        # nuevo (llamada a Tcl) por cada fila y cada botón de cada fila;
        # con cientos de archivos, esto se notaba en la velocidad de
        # dibujado de la tabla entera.
        self._font_btn   = ctk.CTkFont(size=12)   # botones ▲/▶/✕ de cada fila
        self._font_small = ctk.CTkFont(size=11)   # columnas Estado y Vel.

        # TableView: mismo componente que Episodios/Liberar espacio/
        # Historial (ver gui/table_view.py) -- cabecera fija con los
        # mismos anchos que leen las filas. name/det/nn se recalculan
        # según el ancho disponible (ver _compute_cw/_on_table_resize,
        # comportamiento propio de esta tabla, no del componente).
        sw0 = self._saved_col_widths("archivos")   # anchos guardados de una sesión anterior, si hay
        cw0 = self._compute_cw(dest_w=sw0.get("dest", 140))
        self._col_widths = cw0   # mirror de self._file_table para el código de filas -- ver _apply_col_widths
        self._file_table = TableView(body, columns=[
            ColumnSpec("name", "Nombre original", width=sw0.get("name", cw0["name"]), min_width=80, resizable=True),
            ColumnSpec("det", "Detectado", width=sw0.get("det", cw0["det"]), min_width=55, resizable=True),
            ColumnSpec("nn", "Nuevo nombre", width=cw0["nn"], expand=True, resizable=True),
            ColumnSpec("dest", "Destino", width=sw0.get("dest", 140), min_width=60, resizable=True),
            ColumnSpec("stat", "Estado", width=sw0.get("stat", 74), min_width=50, resizable=True),
            ColumnSpec("bar", "Subida FTP", width=sw0.get("bar", 110), min_width=50, resizable=True),
            ColumnSpec("spd", "Vel.", width=sw0.get("spd", 80), min_width=50, resizable=True),
            ColumnSpec("size", "Peso", width=sw0.get("size", 60), min_width=40),
            ColumnSpec("fav", "", width=28),
            ColumnSpec("lock", "", width=28),
            ColumnSpec("btns", "", width=28 * 3 + 6),
        ])
        self._file_table.grid(row=1, column=0, sticky="nsew", padx=(0, CONTAINER_GAP))
        self._file_table.on_column_resize = self._apply_col_widths
        self._file_table.on_widths_changed = lambda w: self._save_table_col_widths("archivos", w)
        # Alias -- el resto de la app (drag&drop, _refresh_table, etc.)
        # ya usaba self._file_list_frame como el contenedor con scroll de
        # las filas; TableView.body es justo eso.
        self._file_list_frame = self._file_table.body

        # Enlazar redimensionado adaptativo al canvas interno del CTkScrollableFrame
        # add="+" preserva el binding nativo _fit_frame_dimensions_to_canvas de CTkScrollableFrame
        # que ajusta el ancho del frame interno al canvas; sin él las filas no llenan el ancho completo
        self._file_list_frame._parent_canvas.bind(
            "<Configure>", self._on_table_resize, add="+")
        self._file_table.enable_dynamic_page_size(lambda _size: self._refresh_table())
        self._file_rows = []
        self._files_page = 0
        self._apply_col_widths()   # sincroniza self._col_widths con los anchos guardados (sw0), si había

        # Paginado (ver TableView.page_size, calculado dinámicamente según
        # el alto disponible -- ver enable_dynamic_page_size arriba) --
        # mismo motivo y mismo patrón que Episodios/Liberar espacio/
        # Historial: con muchos archivos en cola (auto-watcher llevando un
        # rato, o una carpeta grande arrastrada de golpe) dibujar todas las
        # filas a la vez obliga a hacer scroll dentro de la ventana en vez
        # de solo dentro de la tabla, y con cientos de filas se acerca al
        # límite de objetos GUI de Windows (ver
        # [[project_pagination_user_object_limit]]). Debajo de la tabla,
        # centrada (sin sticky="ew", la columna 0 de "body" ya tiene
        # weight=1).
        files_nav_fr = ctk.CTkFrame(body, fg_color="transparent")
        files_nav_fr.grid(row=2, column=0, pady=(6, 0))
        self._files_prev_btn = ctk.CTkButton(
            files_nav_fr, text="< Anterior", width=100, fg_color="transparent", border_width=1,
            command=lambda: self._files_change_page(-1))
        self._files_prev_btn.pack(side="left")
        self._files_page_lbl = ctk.CTkLabel(files_nav_fr, text="", text_color=PENDING_COLOR)
        self._files_page_lbl.pack(side="left", padx=12)
        self._files_next_btn = ctk.CTkButton(
            files_nav_fr, text="Siguiente >", width=100, fg_color="transparent", border_width=1,
            command=lambda: self._files_change_page(1))
        self._files_next_btn.pack(side="left")

        self._drop_zone = ctk.CTkLabel(self._file_list_frame,
                                        text="Arrastra archivos aquí\no usa + Archivos",
                                        font=ctk.CTkFont(size=15), text_color=PENDING_COLOR, height=200)
        self._drop_zone.pack(pady=40)

        # Separador arrastrable entre la tabla y el panel de TMDB -- mismo
        # patrón visual que los separadores de columna de TableView (ver
        # gui/table_view.py), pero independiente de esos (ajusta el ancho
        # del panel, no columnas de la tabla).
        self._detail_sash = tk.Frame(body, width=4, bg="#505060",
                                      cursor="sb_h_double_arrow")
        self._detail_sash.grid(row=0, column=1, rowspan=2, sticky="ns", pady=8)
        self._detail_sash.bind("<ButtonPress-1>", self._detail_sash_press)
        self._detail_sash.bind("<B1-Motion>", self._detail_sash_motion)
        self._detail_sash.bind("<ButtonRelease-1>", self._detail_sash_release)
        self._detail_sash_state = None
        self._detail_resize_debounce_id = None
        self._detail_panel_width = 255   # igual que el width= inicial de _build_detail_panel

        self._detail_panel = self._build_detail_panel(body)
        self._detail_panel.grid(row=0, column=2, rowspan=2, sticky="nsew")

        # Drag & drop — registrar la ventana entera como destino
        if _DND_AVAILABLE:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_drop)

    def _saved_col_widths(self, table_id: str) -> dict:
        """Anchos de columna guardados para una tabla (ver
        _save_table_col_widths) -- {} si nunca se guardó nada, p.ej. en el
        primer arranque o tras un reset de configuración."""
        return dict(self.config_data.get("table_col_widths", {}).get(table_id, {}))

    def _save_table_col_widths(self, table_id: str, widths: dict):
        """Callback de TableView.on_widths_changed: se guarda en disco
        inmediatamente al soltar un separador (no espera a "Guardar
        configuración"), como pidió el usuario."""
        all_saved = dict(self.config_data.get("table_col_widths", {}))
        all_saved[table_id] = widths
        self.config_data.set("table_col_widths", all_saved)
        self.config_data.save()

    def _compute_cw(self, avail_w=None, dest_w=140):
        """Calcula anchos de columna adaptativos. avail_w = px disponibles en la tabla.
        FIXED incluye los 6 sashes de 4px + padx de botones (4+2+2+2) = 30px constantes,
        más la columna "Destino" y su separador (4px). "Destino" SÍ tiene
        sash (ColumnSpec resizable=True) y su ancho se guarda entre
        sesiones -- dest_w debe ser el ancho REAL actual (guardado o
        arrastrado a mano), nunca un 140 fijo: si el usuario ensanchó
        "Destino" y luego solo redimensiona la ventana (_on_table_resize),
        recalcular "Nuevo nombre" asumiendo 140 aquí le daba más espacio
        flexible del que en realidad quedaba libre, así que el texto se
        truncaba con un ancho mayor del que la columna ocupa de verdad en
        pantalla -- se salía visualmente en vez de cortarse con "…".
        28*5 (no *3): 5 botones por fila -- favorito, reservar, subir,
        reproducir, quitar -- desde que se añadieron favorito y reservar;
        cada uno de esos dos añade su propio padx aparte de los 30px de
        los tres originales (ver fav_btn/lock_btn.pack en _refresh_table)."""
        FIXED = 74 + 110 + 80 + 60 + 28*5 + 4 + 4 + 30 + dest_w + 4  # widgets fijos + spacing constante
        PAD   = 16                           # padding interno CTkScrollableFrame
        if avail_w is None:
            avail_w = 900
        flex = max(0, avail_w - FIXED - PAD)
        name = max(80, int(flex * 0.26))
        det  = max(55, int(flex * 0.12))
        nn   = max(0,  flex - name - det)
        return dict(name=name, det=det, nn=nn, dest=dest_w, stat=74, bar=110, spd=80, size=60, btn=28)

    def _on_table_resize(self, event):
        """Reescala columnas al cambiar el ancho del canvas."""
        # Ancho REAL actual de "Destino" (puede haberse arrastrado a mano
        # por su propio sash desde el último cálculo, ver _compute_cw) --
        # nunca el valor por defecto.
        dest_w = self._file_table.col_width("dest")
        new_cw = self._compute_cw(event.width, dest_w=dest_w)
        # "name"/"det" tienen un mínimo (max(80, ...)/max(55, ...)) -- con la
        # ventana ya estrecha, ambos se quedan clavados en ese mínimo aunque
        # siga estrechándose más, así que compararlos SOLO a ellos para
        # decidir "¿hizo falta cambiar algo?" dejaba "nn" (sin mínimo propio,
        # max(0, flex - name - det)) congelado con un ancho viejo demasiado
        # generoso -- el texto dejaba de truncarse lo suficiente para el
        # hueco real y desbordaba la columna (CTkLabel: width es un mínimo,
        # no un máximo, así que el label se ensanchaba y desplazaba el resto
        # de la fila). Hay que comprobar también "nn".
        if (new_cw["name"] == self._col_widths["name"] and new_cw["det"] == self._col_widths["det"]
                and new_cw["nn"] == self._col_widths["nn"]):
            return
        for key in ("name", "det", "nn"):
            self._file_table.set_width(key, new_cw[key], refresh=False)
        self._file_table.refresh_header()
        self._apply_col_widths()

    def _apply_col_widths(self):
        """Sincroniza self._col_widths desde self._file_table (que es la
        fuente real de los anchos, ver TableView.set_width/col_width) y
        actualiza las filas ya pintadas -- se llama tanto tras
        _on_table_resize como tras arrastrar un separador a mano (ver
        self._file_table.on_column_resize)."""
        for key in ("name", "det", "nn", "dest", "stat", "bar", "spd", "size"):
            self._col_widths[key] = self._file_table.col_width(key)
        cw = self._col_widths
        for row in self._file_rows:
            row["name"].configure(
                width=cw["name"],
                text=_fit_text(row.get("_raw_name", ""), cw["name"], self._font_name))
            row["detected"].configure(
                width=cw["det"],
                text=_fit_text(row.get("_raw_det", ""), cw["det"], self._font_det))
            row["new_name"].configure(
                text=_fit_text(row["entry"].new_name, cw["nn"], self._font_nn))
            row["dest"].configure(
                width=cw["dest"],
                text=_fit_text(self._preview_remote_path(row["entry"]), cw["dest"], self._font_det))
            row["status"].configure(width=cw["stat"])
            row["ftp_bar"].configure(width=cw["bar"])
            row["ftp_speed"].configure(width=cw["spd"])
            row["size"].configure(width=cw["size"])

    _DETAIL_PANEL_MIN_W = 180
    _DETAIL_PANEL_MAX_W = 500
    _DETAIL_RESIZE_DEBOUNCE_MS = 150

    def _detail_sash_press(self, event):
        self._detail_sash_state = {"x0": event.x_root, "w0": self._detail_panel_width}

    def _detail_sash_motion(self, event):
        if not self._detail_sash_state:
            return
        # El panel está a la derecha del separador: arrastrar hacia la
        # izquierda (x decrece) lo ensancha, hacia la derecha lo estrecha.
        delta = self._detail_sash_state["x0"] - event.x_root
        new_w = max(self._DETAIL_PANEL_MIN_W,
                    min(self._DETAIL_PANEL_MAX_W, self._detail_sash_state["w0"] + delta))
        if new_w == self._detail_panel_width:
            return   # mismo ancho (redondeo) -- no repetir trabajo de layout
        self._detail_panel_width = new_w
        # Durante el arrastre en vivo SOLO se mueve el borde del panel
        # (barato, ~1ms) -- reconfigurar el ancho de un CTkTextbox es caro
        # de por sí (~20-30ms cada uno, es un widget compuesto con canvas +
        # scrollbar interno), y hacerlo en cada píxel de movimiento del
        # ratón resultaba muy lento. El reajuste completo de los cuadros de
        # texto se aplaza con antirrebote: solo se hace de verdad cuando el
        # arrastre se detiene un momento o se suelta el botón.
        self._detail_panel.configure(width=new_w)
        if self._detail_resize_debounce_id is not None:
            self.after_cancel(self._detail_resize_debounce_id)
        self._detail_resize_debounce_id = self.after(
            self._DETAIL_RESIZE_DEBOUNCE_MS, self._commit_detail_panel_width)

    def _detail_sash_release(self, event):
        self._detail_sash_state = None
        if self._detail_resize_debounce_id is not None:
            self.after_cancel(self._detail_resize_debounce_id)
            self._detail_resize_debounce_id = None
        self._commit_detail_panel_width()

    def _commit_detail_panel_width(self):
        """Aplica self._detail_panel_width de verdad a los widgets de texto
        de dentro del panel (el póster se queda a tamaño fijo -- es una
        imagen ya renderizada a una resolución concreta, no se puede
        reescalar solo cambiando el ancho declarado del widget sin perder
        calidad). Es la parte cara (ver _detail_sash_motion) -- se llama con
        antirrebote durante el arrastre, no en cada evento de movimiento."""
        self._detail_resize_debounce_id = None
        w = self._detail_panel_width
        self._detail_panel.configure(width=w)
        content_w = max(100, w - 40)   # descontar el padding interno del panel
        self._detail_title.configure(width=content_w)
        self._detail_episode.configure(wraplength=content_w)
        self._detail_overview.configure(width=content_w)
        self._detail_error.configure(width=content_w)
        # El wrap cambia con el ancho, así que el número de líneas (y por
        # tanto el alto que necesitan) también cambia.
        self._autosize_textbox(self._detail_title)
        self._autosize_textbox(self._detail_overview)
        self._autosize_textbox(self._detail_error)

    def _build_detail_panel(self, parent):
        panel = ctk.CTkFrame(parent, width=255)
        panel.grid_propagate(False)
        panel.grid_rowconfigure(1, weight=1)
        panel.grid_columnconfigure(0, weight=1)

        # -- Zona superior: busqueda (siempre visible) --
        search_top = ctk.CTkFrame(panel, fg_color="transparent")
        search_top.grid(row=0, column=0, sticky="ew", padx=8, pady=(10, 4))
        search_top.columnconfigure(0, weight=1)

        ctk.CTkLabel(search_top, text="Buscar en TMDB:",
                     font=ctk.CTkFont(size=11), anchor="w").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 2))
        # Un único campo hace de buscador y de desplegable de resultados:
        # se escribe el título y se pulsa Enter/"Buscar", y el propio campo
        # se convierte en el desplegable para elegir entre los resultados
        # (antes eran dos widgets separados — un CTkEntry para buscar y un
        # CTkComboBox aparte para el resultado). Elegir un resultado del
        # desplegable solo lo PREVISUALIZA (póster, sinopsis...) — no se
        # aplica a la entrada hasta pulsar "Asignar".
        self._search_dropdown_font = ctk.CTkFont()   # usada para recortar las etiquetas del desplegable a su ancho
        self._result_combo = ctk.CTkComboBox(search_top, values=[],
                                              command=self._on_result_preview)
        self._result_combo.set("")
        self._result_combo.grid(row=1, column=0, sticky="ew")
        self._result_combo.bind("<Return>", lambda _: self._manual_search(use_ai_fallback=True))
        # Búsqueda automática mientras se escribe (con un pequeño retardo
        # para no lanzar una petición a TMDB en cada pulsación).
        self._result_combo.bind("<KeyRelease>", self._on_search_key_release)
        self._search_debounce_id = None
        ctk.CTkButton(search_top, text="Buscar", width=60,
                      command=lambda: self._manual_search(use_ai_fallback=True)).grid(row=1, column=1, padx=(4, 0))
        ctk.CTkButton(search_top, text="Asignar", width=200,
                      command=self._assign_selected_result).grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))
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
            scroll, width=215, height=1, wrap="word",
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
            scroll, width=215, height=1, wrap="word",
            font=ctk.CTkFont(size=11), fg_color="transparent",
            activate_scrollbars=False)
        self._detail_overview.configure(state="disabled")
        self._detail_overview.pack(pady=4, fill="x")
        self._detail_error = ctk.CTkTextbox(
            scroll, width=215, height=1, wrap="word",
            font=ctk.CTkFont(size=11), fg_color="transparent",
            text_color=ERROR_COLOR, activate_scrollbars=False)
        self._detail_error.configure(state="disabled")
        self._detail_error.pack(pady=(0, 4), fill="x")

        return panel

    # ------------------------------------------- Close warning bar



    # ------------------------------------------------- Queue / upload

    def _get_free_space_with_jellyfin_fallback(self, ftp_conn, root: str):
        """Espacio libre (bytes) para *root* usando el FTP; si el servidor no
        soporta ningún comando de espacio (p.ej. vsftpd), cae a Jellyfin si
        está configurado (>= 10.11, System/Info/Storage), emparejando por la
        raíz de la categoría -- ver core/jellyfin_storage_match.py. None si
        ninguna de las dos vías da un dato."""
        free = ftp_conn.get_free_space(root)
        if free is None and self.config_data.get("jellyfin_enabled"):
            from core.media_server_refresh import get_jellyfin_free_space_for_root
            free = get_jellyfin_free_space_for_root(
                root, self.config_data.get("jellyfin_host", ""),
                self.config_data.get("jellyfin_api_key", ""))
        return free

    @staticmethod
    def _fmt_free_space(free: int) -> str:
        # Coma como separador decimal (es-ES), no punto.
        for unit, divisor in (("TB", 1024**4), ("GB", 1024**3), ("MB", 1024**2)):
            if free >= divisor:
                return f"{free / divisor:.1f} {unit}".replace(".", ",")
        return f"{free} B"

    def _disks_from_categories(self) -> dict:
        """Un disco por cada primer segmento de ruta distinto entre todas
        las categorías FTP configuradas (p.ej. "datos" en "/datos/peliculas/",
        "datos2" en "/datos2/series/") -- así, si varias categorías comparten
        disco (Series y SeriesPeques bajo /datos2/), no se consulta ni se
        muestra dos veces. Devuelve {nombre_disco: root_representativo}."""
        cats = self.config_data.get("ftp_categories", {"tv": [], "movie": []})
        roots = [c.get("root", "") for c in cats.get("tv", []) + cats.get("movie", [])]
        disks = {}
        for root in roots:
            segments = [s for s in (root or "").replace("\\", "/").split("/") if s]
            if not segments:
                continue
            disks.setdefault(segments[0], root)
        return disks

    def _refresh_ftp_space(self):
        """Consulta el espacio libre de cada disco distinto usado en las
        categorías FTP configuradas y actualiza la etiqueta de la cabecera
        con un resumen por disco (p.ej. "Espacio disponible: Datos-30,0 GB
        · Datos2-40,0 GB")."""
        disks = self._disks_from_categories()
        if not disks:
            self._ftp_space_lbl.configure(text="")
            return

        def worker():
            # Conexión propia, NUNCA self.ftp -- este chequeo es periódico
            # (cada 5 min) y con el automático subiendo sin pausa durante un
            # buen rato, self.ftp (y el candado que lo protege) puede estar
            # ocupado casi todo el tiempo: no es solo un bloqueo puntual, es
            # inanición real del hilo de este chequeo, que nunca consigue su
            # turno. Con conexión propia no depende de lo ocupado que esté
            # el automático -- mismo patrón que ya usa el detector de huecos.
            from core.ftp_client import FTPClient as _FTPClient
            own_ftp = _FTPClient()
            try:
                ok, _msg = own_ftp.connect(
                    self.config_data.get("ftp_host", ""),
                    int(self.config_data.get("ftp_port", 21)),
                    self.config_data.get("ftp_user", ""),
                    self.config_data.get("ftp_password", ""),
                    self.config_data.get("ftp_use_tls", False))
                if not ok:
                    self.after(0, lambda: self._ftp_space_lbl.configure(text=""))
                    return
                parts = []
                any_low = False
                for disk_name, root in disks.items():
                    free = self._get_free_space_with_jellyfin_fallback(own_ftp, root)
                    if free is None:
                        continue
                    parts.append(f"{disk_name.capitalize()}-{self._fmt_free_space(free)}")
                    if free <= 1024**3:   # < 1 GB -- mismo umbral de aviso que antes
                        any_low = True
            finally:
                own_ftp.disconnect()
            if not parts:
                self.after(0, lambda: self._ftp_space_lbl.configure(text=""))
                return
            text = "Espacio disponible: " + " · ".join(parts)
            color = WARNING_COLOR if any_low else SUCCESS_COLOR
            self.after(0, lambda t=text, c=color: self._ftp_space_lbl.configure(text=t, text_color=c))
        threading.Thread(target=worker, daemon=True).start()

    _FTP_SPACE_PERIODIC_MS = 5 * 60 * 1000   # 5 minutos

    def _refresh_ftp_space_at_startup(self):
        """Al arrancar (si hay servidor FTP configurado), rellena el
        indicador de espacio del toolbar desde el principio -- sin esto, se
        quedaba vacío hasta pulsar "Probar conexión" en Ajustes o empezar
        una subida. _refresh_ftp_space() y _prefetch_ftp_category_dirs() ya
        conectan con su propia conexión dedicada cada una, en su propio
        hilo -- no hace falta preparar nada de self.ftp aquí."""
        if not self.config_data.get("ftp_host", ""):
            return
        self._refresh_ftp_space()
        self._prefetch_ftp_category_dirs()

    def _shared_data_path(self, filename: str) -> str:
        """Ruta remota de *filename* dentro de la carpeta compartida (ver
        "Carpeta compartida (datos)" en Ajustes -> Cliente -> Conexión
        FTP, shared_data_ftp_path) -- favoritos, reservas y configuración
        de servidor son 3 archivos independientes con nombre fijo dentro
        de esa misma carpeta, no derivados unos de otros por sufijo (así
        se llamaban todos "favoritos_algo.json" antes, aunque no tuvieran
        nada que ver con favoritos -- ver el nombre de archivo de cada
        uno en _favorites_remote_path/_reservations_remote_path/
        _server_config_remote_path). "" si no hay carpeta configurada
        (las 3 funciones quedan deshabilitadas, cada mirror local se
        queda con el último estado conocido)."""
        folder = self.config_data.get("shared_data_ftp_path", "").strip()
        if not folder:
            return ""
        return f"{folder.rstrip('/')}/{filename}"

    def _favorites_remote_path(self) -> str:
        return self._shared_data_path("aRenombrar_favoritos.json")

    def _is_favorite(self, media_type: str, tmdb_id: int) -> bool:
        from core.favorites import is_favorite
        return is_favorite(self._favorites, media_type, tmdb_id)

    def _toggle_favorite(self, media_type: str, tmdb_id: int, name: str, on_done=None):
        """Marca/desmarca favorito -- optimista en local (se ve al instante
        en las 3 pestañas) y luego intenta sincronizar con el FTP en segundo
        plano: descarga el JSON remoto fresco, aplica el MISMO cambio sobre
        ESE contenido (no sobre lo que había en memoria) y lo vuelve a
        subir, para minimizar la carrera si otro cliente conectado al mismo
        servidor cambió algo distinto mientras tanto. Si no hay ruta
        configurada o la subida falla, el cambio se queda en el mirror local
        nada más -- no es una operación que deba bloquear ni fallar la UI."""
        from core.favorites import add_favorite, remove_favorite, save_local_cache
        turning_on = not self._is_favorite(media_type, tmdb_id)
        op = add_favorite if turning_on else remove_favorite
        args = (media_type, tmdb_id, name) if turning_on else (media_type, tmdb_id)

        self._favorites = op(self._favorites, *args)
        save_local_cache(self._favorites)
        if on_done:
            on_done()

        remote_path = self._favorites_remote_path()
        if not remote_path:
            return

        def worker():
            from core.ftp_client import FTPClient as _FTPClient
            import json as _json
            own_ftp = _FTPClient()
            try:
                ok, _msg = own_ftp.connect(
                    self.config_data.get("ftp_host", ""),
                    int(self.config_data.get("ftp_port", 21)),
                    self.config_data.get("ftp_user", ""),
                    self.config_data.get("ftp_password", ""),
                    self.config_data.get("ftp_use_tls", False))
                if not ok:
                    return
                raw = own_ftp.download_bytes(remote_path)
                try:
                    remote_data = _json.loads(raw.decode("utf-8")) if raw else {}
                except ValueError:
                    remote_data = {}
                if not isinstance(remote_data, dict):
                    remote_data = {}
                merged = op(remote_data, *args)
                data = _json.dumps(merged, ensure_ascii=False, indent=2).encode("utf-8")
                up_ok, _up_msg = own_ftp.upload_bytes(data, remote_path)
                if up_ok:
                    self.after(0, lambda: self._apply_synced_favorites(merged, on_done))
            finally:
                own_ftp.disconnect()
        threading.Thread(target=worker, daemon=True).start()

    def _apply_synced_favorites(self, merged: dict, on_done=None):
        from core.favorites import save_local_cache
        changed = merged != self._favorites
        self._favorites = merged
        save_local_cache(self._favorites)
        if on_done:
            on_done()
        elif changed:
            # Sin un callback puntual (p.ej. tras _sync_favorites_from_ftp
            # al abrir una pestaña, en vez de tras marcar/desmarcar una
            # fila concreta) -- refrescar la vista activa para que las
            # estrellas reflejen lo que haya cambiado desde otro cliente.
            # Solo si de verdad cambió algo: _sync_favorites_from_ftp se
            # dispara cada vez que se entra en una pestaña (pasado
            # _FAVORITES_SYNC_MIN_INTERVAL), y redibujar la tabla entera
            # (con Liberar espacio, además, volviendo a la página 1) por
            # una sincronización que no trajo nada nuevo se veía como un
            # "carga y enseguida recarga" sin motivo real.
            if self._current_view_key() == "files":
                self._refresh_table()
            elif self._missing_ep_visible:
                self._render_missing_episodes_table(reset_page=False)
            elif self._cleanup_visible:
                self._apply_cleanup_filters()

    _FAVORITES_SYNC_MIN_INTERVAL = 20   # segundos, ver _sync_favorites_from_ftp

    def _sync_favorites_from_ftp(self):
        """Refresca el mirror local desde el FTP en segundo plano -- se
        llama al abrir cada una de las 3 pestañas que muestran favoritos,
        para reflejar cambios hechos desde otro cliente del mismo servidor.
        Silencioso si no hay ruta configurada o la conexión falla: el mirror
        local ya tiene el último estado conocido. Si se saltó rápido entre
        varias de esas pestañas, no repite la consulta al FTP dentro de
        _FAVORITES_SYNC_MIN_INTERVAL -- los datos no pueden haber cambiado
        en ese margen y cada visita repetía tráfico innecesario."""
        now = _time.time()
        if now - self._last_favorites_sync_ts < self._FAVORITES_SYNC_MIN_INTERVAL:
            return
        self._last_favorites_sync_ts = now

        remote_path = self._favorites_remote_path()
        if not remote_path:
            return

        def worker():
            from core.ftp_client import FTPClient as _FTPClient
            import json as _json
            own_ftp = _FTPClient()
            try:
                ok, _msg = own_ftp.connect(
                    self.config_data.get("ftp_host", ""),
                    int(self.config_data.get("ftp_port", 21)),
                    self.config_data.get("ftp_user", ""),
                    self.config_data.get("ftp_password", ""),
                    self.config_data.get("ftp_use_tls", False))
                if not ok:
                    return
                raw = own_ftp.download_bytes(remote_path)
                if raw is None:
                    return
                try:
                    remote_data = _json.loads(raw.decode("utf-8"))
                except ValueError:
                    return
                if not isinstance(remote_data, dict):
                    return
                self.after(0, lambda: self._apply_synced_favorites(remote_data))
            finally:
                own_ftp.disconnect()
        threading.Thread(target=worker, daemon=True).start()

    def _reservations_remote_path(self) -> str:
        """Ruta remota del JSON de reservas -- archivo propio dentro de la
        carpeta compartida (ver _shared_data_path), no un campo de ajustes
        aparte: reservas, favoritos y configuración de servidor comparten
        la misma conexión/carpeta FTP, así que basta con una única ruta
        (de carpeta) configurada por el usuario."""
        return self._shared_data_path("aRenombrar_reservas.json")

    def _server_config_remote_path(self) -> str:
        """Ruta remota de la configuración compartida del servidor (ver
        core/server_config.py) -- archivo propio dentro de la misma
        carpeta compartida, mismo motivo que _reservations_remote_path."""
        return self._shared_data_path("aRenombrar_config_servidor.json")

    def _shared_dub_verdicts_remote_path(self) -> str:
        """Ruta remota de los veredictos de doblaje castellano obtenidos
        por IA, compartidos entre clientes (ver core/shared_dub_verdicts.py)
        -- archivo propio dentro de la misma carpeta compartida, mismo
        motivo que _reservations_remote_path."""
        return self._shared_data_path("aRenombrar_doblaje_ia.json")

    def _activity_remote_path(self) -> str:
        """Ruta remota del historial de actividad compartido (subidas y
        borrados de todos los clientes, ver Historial → "Ver todo el
        servidor") -- archivo propio dentro de la misma carpeta
        compartida, mismo motivo que _reservations_remote_path."""
        return self._shared_data_path("aRenombrar_actividad.json")

    def _sync_server_config_from_ftp(self):
        """Descarga la configuración compartida del servidor y la aplica en
        local (TMDB/IA, plantillas, categorías, servidores de medios,
        enlaces, cuota de reservas... ver
        core/server_config.py::SHARED_CONFIG_KEYS) -- se llama una vez al
        arrancar. Silencioso si no hay ruta configurada, la descarga falla,
        o el archivo remoto todavía no existe (nadie lo ha publicado
        nunca): los valores locales se quedan como estaban, no es un
        error."""
        remote_path = self._server_config_remote_path()
        if not remote_path:
            return

        def worker():
            from core.ftp_client import FTPClient as _FTPClient
            from core.server_config import filter_shared_config
            import json as _json
            own_ftp = _FTPClient()
            try:
                ok, _msg = own_ftp.connect(
                    self.config_data.get("ftp_host", ""),
                    int(self.config_data.get("ftp_port", 21)),
                    self.config_data.get("ftp_user", ""),
                    self.config_data.get("ftp_password", ""),
                    self.config_data.get("ftp_use_tls", False))
                if not ok:
                    return
                raw = own_ftp.download_bytes(remote_path)
                if raw is None:
                    return
                try:
                    remote_data = _json.loads(raw.decode("utf-8"))
                except ValueError:
                    return
                if not isinstance(remote_data, dict):
                    return
                updates = filter_shared_config(remote_data)
                # learned_junk_terms no es una clave de Config -- vive en
                # core/learned_terms.py, se gestiona aparte (igual que en
                # _export_config/_import_config).
                learned_terms = remote_data.get("learned_junk_terms")
                if updates or learned_terms is not None:
                    self.after(0, lambda: self._apply_synced_server_config(updates, learned_terms))
            finally:
                own_ftp.disconnect()
        threading.Thread(target=worker, daemon=True).start()

    def _apply_synced_server_config(self, updates: dict, learned_terms=None):
        self.config_data.set_many(updates)
        self.config_data.save()
        if learned_terms is not None:
            from core.learned_terms import set_learned_terms
            set_learned_terms(learned_terms)
        # El panel de Ajustes se construye diferido (ver _build_ui) -- si
        # el usuario nunca lo ha abierto todavía, no hay widgets que
        # refrescar; se leerán ya actualizados de self.config_data la
        # primera vez que se construya.
        if self._config_panel_built:
            self._reload_settings_widgets()
        self._set_status(
            f"Configuración de servidor actualizada ({len(updates)} parámetro(s))", PENDING_COLOR)

    def _publish_server_config(self):
        """Sube la configuración de ESTE equipo como la configuración
        compartida del servidor -- a diferencia de favoritos/reservas
        (que se fusionan), esto es una publicación deliberada que
        SOBRESCRIBE por completo lo que hubiera, así que pide confirmación
        explícita: afecta a cualquier otra persona que use aRenombrar
        contra este mismo servidor la próxima vez que arranque. Incluye
        credenciales (TMDB/IA/Plex/Jellyfin) a propósito -- ver la nota de
        seguridad en core/server_config.py: viajan en texto plano dentro
        del JSON del FTP, protegidas solo por la contraseña FTP."""
        remote_path = self._server_config_remote_path()
        if not remote_path:
            messagebox.showwarning(
                "Falta la carpeta compartida",
                "Configura \"Carpeta compartida (datos)\" en Ajustes → Cliente → Conexión FTP "
                "antes de publicar la configuración del servidor.")
            return
        if not messagebox.askyesno(
                "Publicar configuración del servidor",
                "Esto sobrescribirá la configuración compartida (TMDB/IA -- incluidas las claves de "
                "API --, plantillas de nombre, categorías FTP, Plex/Jellyfin -- incluidos sus tokens --, "
                "enlaces y la cuota de reservas) con la de este equipo. Cualquier otra persona que use "
                "aRenombrar contra este servidor la adoptará la próxima vez que abra la app. "
                "¿Continuar?"):
            return

        from core.server_config import extract_shared_config
        from core.learned_terms import load_learned_terms
        data = extract_shared_config(self.config_data.get)
        data["learned_junk_terms"] = load_learned_terms()
        self._set_status("Publicando configuración del servidor...", PENDING_COLOR)

        def worker():
            from core.ftp_client import FTPClient as _FTPClient
            import json as _json
            own_ftp = _FTPClient()
            try:
                ok, msg = own_ftp.connect(
                    self.config_data.get("ftp_host", ""),
                    int(self.config_data.get("ftp_port", 21)),
                    self.config_data.get("ftp_user", ""),
                    self.config_data.get("ftp_password", ""),
                    self.config_data.get("ftp_use_tls", False))
                if not ok:
                    self.after(0, lambda: self._set_status(f"No se pudo conectar: {msg}", ERROR_COLOR))
                    return
                payload = _json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
                up_ok, up_msg = own_ftp.upload_bytes(payload, remote_path)
            finally:
                own_ftp.disconnect()
            if up_ok:
                self.after(0, lambda: self._set_status("Configuración de servidor publicada", SUCCESS_COLOR))
            else:
                self.after(0, lambda: self._set_status(f"No se pudo publicar: {up_msg}", ERROR_COLOR))
        threading.Thread(target=worker, daemon=True).start()

    def _is_reserved(self, media_type: str, tmdb_id: int) -> bool:
        from core.reservations import is_reserved
        return is_reserved(self._reservations, media_type, tmdb_id)

    def _reservation_owner(self, media_type: str, tmdb_id: int):
        from core.reservations import reserved_by
        return reserved_by(self._reservations, media_type, tmdb_id)

    def _reservation_quota_bytes(self) -> int:
        """Cuota de reservas configurada, en bytes -- configuración de
        SERVIDOR (ver core/server_config.py), 100GB por defecto si nunca
        se ha sincronizado/publicado nada (config.py::DEFAULTS
        ["reservation_quota_gb"])."""
        return int(self.config_data.get("reservation_quota_gb", 100)) * 1024 ** 3

    def _toggle_reservation(self, media_type: str, tmdb_id: int, name: str,
                             size_bytes: int, on_done=None):
        """Reserva/libera un ítem -- mismo patrón optimista+sync que
        _toggle_favorite, pero con dos comprobaciones previas que favoritos
        no necesita: (1) hace falta un nombre de usuario configurado, para
        saber a quién cargarle la cuota; (2) al reservar, el usuario debe
        tener sitio en su cuota (configurable, ver _reservation_quota_bytes);
        al liberar, solo el usuario que la reservó puede hacerlo (sin esto,
        cualquier cliente podría liberar por error el hueco de otra
        persona)."""
        from core.reservations import (
            add_reservation, remove_reservation, fits_in_quota, remaining_bytes, reserved_by)

        user = self.config_data.get("app_user_name", "").strip()
        if not user:
            messagebox.showwarning(
                "Falta tu nombre",
                "Configura \"Tu nombre\" en Ajustes → Conexión FTP antes de reservar espacio, "
                "así se sabe a quién cargarle la cuota.")
            return

        quota_bytes = self._reservation_quota_bytes()
        turning_on = not self._is_reserved(media_type, tmdb_id)
        if turning_on:
            if not fits_in_quota(self._reservations, user, size_bytes, quota_bytes):
                remaining_gb = remaining_bytes(self._reservations, user, quota_bytes) / (1024 ** 3)
                quota_gb = quota_bytes / (1024 ** 3)
                messagebox.showwarning(
                    "Cuota de reservas agotada",
                    f"Te quedan {remaining_gb:.1f}GB libres de tu cuota de {quota_gb:.0f}GB -- "
                    f"\"{name}\" no cabe. Libera alguna reserva antes de añadir otra.")
                return
            op, args = add_reservation, (media_type, tmdb_id, name, size_bytes, user)
        else:
            owner = reserved_by(self._reservations, media_type, tmdb_id)
            if owner and owner != user:
                messagebox.showwarning(
                    "Reservado por otra persona",
                    f"\"{name}\" lo reservó {owner} -- solo esa persona puede liberarlo.")
                return
            op, args = remove_reservation, (media_type, tmdb_id)

        self._reservations = op(self._reservations, *args)
        from core.reservations import save_local_cache as _save_reservations_cache
        _save_reservations_cache(self._reservations)
        if on_done:
            on_done()

        self._push_reservations_to_ftp(lambda remote_data: op(remote_data, *args), on_done)

    def _push_reservations_to_ftp(self, transform, on_done=None):
        """Sube el cambio de *transform* al FTP en segundo plano --
        descarga el JSON remoto fresco, le aplica *transform* (no lo que
        hubiera en memoria) y lo vuelve a subir, para minimizar la carrera
        si otro cliente conectado al mismo servidor cambió algo distinto
        mientras tanto. *transform* recibe el dict remoto y devuelve el
        dict nuevo -- mismo callable tanto para un cambio de una sola
        clave (_toggle_reservation) como para uno que toca varias a la vez
        (_transfer_reservations/_unprotect_all_reservations). Si no hay
        ruta configurada, el cambio se queda en el mirror local nada más."""
        remote_path = self._reservations_remote_path()
        if not remote_path:
            return

        def worker():
            from core.ftp_client import FTPClient as _FTPClient
            import json as _json
            own_ftp = _FTPClient()
            try:
                ok, _msg = own_ftp.connect(
                    self.config_data.get("ftp_host", ""),
                    int(self.config_data.get("ftp_port", 21)),
                    self.config_data.get("ftp_user", ""),
                    self.config_data.get("ftp_password", ""),
                    self.config_data.get("ftp_use_tls", False))
                if not ok:
                    return
                raw = own_ftp.download_bytes(remote_path)
                try:
                    remote_data = _json.loads(raw.decode("utf-8")) if raw else {}
                except ValueError:
                    remote_data = {}
                if not isinstance(remote_data, dict):
                    remote_data = {}
                merged = transform(remote_data)
                data = _json.dumps(merged, ensure_ascii=False, indent=2).encode("utf-8")
                up_ok, _up_msg = own_ftp.upload_bytes(data, remote_path)
                if up_ok:
                    self.after(0, lambda: self._apply_synced_reservations(merged, on_done))
            finally:
                own_ftp.disconnect()
        threading.Thread(target=worker, daemon=True).start()

    def _transfer_reservations(self, old_owner: str, new_owner: str):
        """"Traspasarlas al nombre nuevo" en _RenameReservationsDialog --
        misma entrada, mismo tamaño, solo cambia reserved_by."""
        from core.reservations import transfer_reservations, save_local_cache as _save_reservations_cache
        self._reservations = transfer_reservations(self._reservations, old_owner, new_owner)
        _save_reservations_cache(self._reservations)
        self._push_reservations_to_ftp(lambda data: transfer_reservations(data, old_owner, new_owner))

    def _unprotect_all_reservations(self, owner: str):
        """"Desproteger todas" en _RenameReservationsDialog -- la otra
        opción al cambiar de nombre, empezar de nuevo en vez de traspasar."""
        from core.reservations import remove_all_by_owner, save_local_cache as _save_reservations_cache
        self._reservations = remove_all_by_owner(self._reservations, owner)
        _save_reservations_cache(self._reservations)
        self._push_reservations_to_ftp(lambda data: remove_all_by_owner(data, owner))

    def _apply_synced_reservations(self, merged: dict, on_done=None):
        from core.reservations import save_local_cache as _save_reservations_cache
        changed = merged != self._reservations
        self._reservations = merged
        _save_reservations_cache(self._reservations)
        if on_done:
            on_done()
        elif not changed:
            pass   # ver _apply_synced_favorites: no redibujar si no cambió nada de verdad
        elif self._cleanup_visible:
            self._apply_cleanup_filters()
        elif self._protected_visible:
            self._render_protected_table()
        elif self._current_view_key() == "files":
            self._refresh_table()

    _RESERVATIONS_SYNC_MIN_INTERVAL = 20   # segundos, ver _sync_reservations_from_ftp

    def _sync_reservations_from_ftp(self):
        """Refresca el mirror local desde el FTP en segundo plano -- mismo
        motivo y patrón que _sync_favorites_from_ftp, incluido el freno de
        _RESERVATIONS_SYNC_MIN_INTERVAL. Se llama al abrir Archivos (los
        candados de las filas ya subidas), Liberar espacio (protección +
        cuota) y Protegidos (gestión)."""
        now = _time.time()
        if now - self._last_reservations_sync_ts < self._RESERVATIONS_SYNC_MIN_INTERVAL:
            return
        self._last_reservations_sync_ts = now

        remote_path = self._reservations_remote_path()
        if not remote_path:
            return

        def worker():
            from core.ftp_client import FTPClient as _FTPClient
            import json as _json
            own_ftp = _FTPClient()
            try:
                ok, _msg = own_ftp.connect(
                    self.config_data.get("ftp_host", ""),
                    int(self.config_data.get("ftp_port", 21)),
                    self.config_data.get("ftp_user", ""),
                    self.config_data.get("ftp_password", ""),
                    self.config_data.get("ftp_use_tls", False))
                if not ok:
                    return
                raw = own_ftp.download_bytes(remote_path)
                if raw is None:
                    return
                try:
                    remote_data = _json.loads(raw.decode("utf-8"))
                except ValueError:
                    return
                if not isinstance(remote_data, dict):
                    return
                self.after(0, lambda: self._apply_synced_reservations(remote_data))
            finally:
                own_ftp.disconnect()
        threading.Thread(target=worker, daemon=True).start()

    def _apply_synced_shared_dub_verdicts(self, merged: dict):
        """Aplica los veredictos de doblaje compartidos (ver
        core/shared_dub_verdicts.py) a las filas ya cargadas de Episodios
        que faltan -- solo los que sean más recientes que la última vez
        que se sincronizó (comparando checked_at contra la copia anterior
        de self._shared_dub_verdicts), para no reprocesar/redibujar en
        cada sincronización si nada cambió de verdad desde la última.
        Reutiliza _persist_ai_verdicts tal cual: series sin entrada en
        missing_episodes_cache.json (no salieron de un escaneo local)
        simplemente no tienen dónde guardarlo todavía -- se aplicarán la
        próxima vez que esa serie se escanee y vuelva a sincronizar."""
        previous = getattr(self, "_shared_dub_verdicts", {}) or {}
        self._shared_dub_verdicts = merged
        from core.shared_dub_verdicts import save_local_cache as _save_dub_verdicts_cache
        _save_dub_verdicts_cache(merged)

        to_apply = {}
        for r in self._missing_ep_results:
            key = str(r["tmdb_id"])
            shared = merged.get(key)
            if not shared:
                continue
            prev_checked_at = (previous.get(key) or {}).get("checked_at", 0)
            if shared.get("checked_at", 0) <= prev_checked_at:
                continue
            verdict = {k: v for k, v in shared.items() if k not in ("checked_at", "checked_by")}
            r["ai_verdict"] = verdict
            to_apply[r["tmdb_id"]] = verdict
        if to_apply:
            self._persist_ai_verdicts(to_apply)
            self._render_missing_episodes_table(reset_page=False)

    def _push_shared_dub_verdict_to_ftp(self, tmdb_id: int, verdict: dict):
        """Comparte con el resto de clientes del mismo servidor el
        veredicto de doblaje que la IA acaba de dar para una serie --
        llamado solo tras una consulta manual real (ver
        _apply_single_missing_ep_ai_verdict), nunca desde el chequeo
        automático por lotes (ese no pasa por aquí, se queda solo local).
        Mismo patrón lectura-modificación-escritura que
        _push_reservations_to_ftp: descarga el remoto fresco, le fija esta
        sola entrada y lo vuelve a subir, para minimizar la carrera si
        otro cliente compartió un veredicto de OTRA serie casi a la vez."""
        remote_path = self._shared_dub_verdicts_remote_path()
        if not remote_path:
            return
        app_user_name = self.config_data.get("app_user_name", "")

        def worker():
            from core.ftp_client import FTPClient as _FTPClient
            from core.shared_dub_verdicts import set_verdict
            import json as _json
            own_ftp = _FTPClient()
            try:
                ok, _msg = own_ftp.connect(
                    self.config_data.get("ftp_host", ""),
                    int(self.config_data.get("ftp_port", 21)),
                    self.config_data.get("ftp_user", ""),
                    self.config_data.get("ftp_password", ""),
                    self.config_data.get("ftp_use_tls", False))
                if not ok:
                    return
                raw = own_ftp.download_bytes(remote_path)
                try:
                    remote_data = _json.loads(raw.decode("utf-8")) if raw else {}
                except ValueError:
                    remote_data = {}
                if not isinstance(remote_data, dict):
                    remote_data = {}
                merged = set_verdict(remote_data, tmdb_id, verdict, app_user_name)
                data = _json.dumps(merged, ensure_ascii=False, indent=2).encode("utf-8")
                up_ok, _up_msg = own_ftp.upload_bytes(data, remote_path)
                if up_ok:
                    self.after(0, lambda: self._apply_synced_shared_dub_verdicts(merged))
            finally:
                own_ftp.disconnect()
        threading.Thread(target=worker, daemon=True).start()

    _DUB_VERDICTS_SYNC_MIN_INTERVAL = 20   # segundos, ver _sync_shared_dub_verdicts_from_ftp

    def _sync_shared_dub_verdicts_from_ftp(self):
        """Refresca el mirror local de veredictos de doblaje compartidos
        desde el FTP en segundo plano -- mismo patrón y mismo freno que
        _sync_reservations_from_ftp. Se llama al abrir Episodios que
        faltan."""
        now = _time.time()
        if now - self._last_dub_verdicts_sync_ts < self._DUB_VERDICTS_SYNC_MIN_INTERVAL:
            return
        self._last_dub_verdicts_sync_ts = now

        remote_path = self._shared_dub_verdicts_remote_path()
        if not remote_path:
            return

        def worker():
            from core.ftp_client import FTPClient as _FTPClient
            import json as _json
            own_ftp = _FTPClient()
            try:
                ok, _msg = own_ftp.connect(
                    self.config_data.get("ftp_host", ""),
                    int(self.config_data.get("ftp_port", 21)),
                    self.config_data.get("ftp_user", ""),
                    self.config_data.get("ftp_password", ""),
                    self.config_data.get("ftp_use_tls", False))
                if not ok:
                    return
                raw = own_ftp.download_bytes(remote_path)
                if raw is None:
                    return
                try:
                    remote_data = _json.loads(raw.decode("utf-8"))
                except ValueError:
                    return
                if not isinstance(remote_data, dict):
                    return
                self.after(0, lambda: self._apply_synced_shared_dub_verdicts(remote_data))
            finally:
                own_ftp.disconnect()
        threading.Thread(target=worker, daemon=True).start()

    def _push_activity_entry_to_ftp(self, entry: dict, kind: str):
        """Añade *entry* (una subida o un borrado, ya guardada en el
        historial LOCAL antes de llamar a esto) al historial de actividad
        compartido entre clientes del mismo servidor -- silencioso si no
        hay carpeta compartida configurada o la subida falla, mismo
        criterio que el resto de sincronizaciones de la app: el historial
        local es la copia que de verdad importa, esto es un añadido de
        mejor esfuerzo.

        A diferencia de reservas/favoritos/veredictos de doblaje (un dict
        que se fusiona clave a clave), el remoto aquí es una LISTA que
        solo crece por añadido -- se descarga fresca, se le añade esta
        entrada con su "kind" ("subida"/"borrado"), se recorta a los
        últimos 500 igual que los históricos locales, y se vuelve a
        subir."""
        remote_path = self._activity_remote_path()
        if not remote_path:
            return

        def worker():
            from core.ftp_client import FTPClient as _FTPClient
            import json as _json
            own_ftp = _FTPClient()
            try:
                ok, _msg = own_ftp.connect(
                    self.config_data.get("ftp_host", ""),
                    int(self.config_data.get("ftp_port", 21)),
                    self.config_data.get("ftp_user", ""),
                    self.config_data.get("ftp_password", ""),
                    self.config_data.get("ftp_use_tls", False))
                if not ok:
                    return
                raw = own_ftp.download_bytes(remote_path)
                try:
                    remote_list = _json.loads(raw.decode("utf-8")) if raw else []
                except ValueError:
                    remote_list = []
                if not isinstance(remote_list, list):
                    remote_list = []
                remote_list.append({**entry, "kind": kind})
                if len(remote_list) > 500:
                    remote_list = remote_list[-500:]
                data = _json.dumps(remote_list, ensure_ascii=False, indent=2).encode("utf-8")
                up_ok, _up_msg = own_ftp.upload_bytes(data, remote_path)
                if up_ok:
                    self.after(0, lambda: self._apply_synced_activity_history(remote_list))
            finally:
                own_ftp.disconnect()
        threading.Thread(target=worker, daemon=True).start()

    def _apply_synced_activity_history(self, activity_list: list):
        self._shared_activity_history = activity_list
        if self._history_visible and self._history_show_all_var.get():
            self._history_dirty = True
            self._refresh_history_view()

    _ACTIVITY_SYNC_MIN_INTERVAL = 20   # segundos, ver _sync_activity_history_from_ftp

    def _sync_activity_history_from_ftp(self):
        """Refresca el mirror en memoria del historial de actividad
        compartido desde el FTP en segundo plano -- mismo patrón y mismo
        freno que _sync_reservations_from_ftp. Se llama al entrar en
        Historial; solo importa de verdad si "Ver todo el servidor" está
        activo, pero se sincroniza siempre que se visita la pestaña para
        que el interruptor no tenga que esperar a una conexión nueva al
        encenderlo."""
        now = _time.time()
        if now - self._last_activity_sync_ts < self._ACTIVITY_SYNC_MIN_INTERVAL:
            return
        self._last_activity_sync_ts = now

        remote_path = self._activity_remote_path()
        if not remote_path:
            return

        def worker():
            from core.ftp_client import FTPClient as _FTPClient
            import json as _json
            own_ftp = _FTPClient()
            try:
                ok, _msg = own_ftp.connect(
                    self.config_data.get("ftp_host", ""),
                    int(self.config_data.get("ftp_port", 21)),
                    self.config_data.get("ftp_user", ""),
                    self.config_data.get("ftp_password", ""),
                    self.config_data.get("ftp_use_tls", False))
                if not ok:
                    return
                raw = own_ftp.download_bytes(remote_path)
                if raw is None:
                    return
                try:
                    remote_list = _json.loads(raw.decode("utf-8"))
                except ValueError:
                    return
                if not isinstance(remote_list, list):
                    return
                self.after(0, lambda: self._apply_synced_activity_history(remote_list))
            finally:
                own_ftp.disconnect()
        threading.Thread(target=worker, daemon=True).start()

    def _check_for_updates_at_startup(self):
        """Comprueba en segundo plano si hay una versión más nueva publicada
        en GitHub Releases (core/update_check.py) y, si la hay y el usuario
        no la saltó ya, muestra un aviso para ir a la página de la release.
        Nunca descarga ni reemplaza el ejecutable en sitio. Silencioso si la
        consulta falla -- no es una acción que el usuario haya pedido
        explícitamente."""
        def worker():
            from core.update_check import check_for_update
            result = check_for_update(__version__)
            if result is None:
                return
            tag, html_url = result
            if tag == self.config_data.get("skipped_update_version", ""):
                return
            self.after(0, lambda: self._show_update_dialog(tag, html_url))
        threading.Thread(target=worker, daemon=True).start()

    def _show_update_dialog(self, tag: str, html_url: str):
        dlg = _UpdateAvailableDialog(self, tag)
        if dlg.result == "open":
            import webbrowser
            webbrowser.open(html_url)
        elif dlg.result == "skip":
            self.config_data.set("skipped_update_version", tag)
            self.config_data.save()
        # Primer reintento periódico más pronto (20s, no los 5 minutos de
        # costumbre) -- el caso típico que motivó esto es la red tardando
        # unos segundos más en estar lista al arrancar la app junto con
        # Windows; los reintentos siguientes ya usan el intervalo normal.
        self.after(20_000, self._periodic_refresh_ftp_space)

    def _periodic_refresh_ftp_space(self):
        """Reintenta cada pocos minutos, indefinidamente, mientras la app
        esté abierta -- antes, si el único intento (al arrancar, al
        subir, o al guardar Ajustes) se topaba con un fallo pasajero de
        red hacia el FTP/Jellyfin/Plex, el indicador se quedaba en blanco
        el resto de la sesión sin ninguna forma de que se corrigiera solo."""
        self._refresh_ftp_space()
        self.after(self._FTP_SPACE_PERIODIC_MS, self._periodic_refresh_ftp_space)

    def _prefetch_ftp_category_dirs(self):
        """Lista de antemano (en segundo plano, con su propia conexión
        dedicada -- NUNCA self.ftp, ver _refresh_ftp_space) las carpetas de
        todas las categorías FTP configuradas -- así la columna "Destino"
        de la tabla puede avisar de entrada si una serie ya tiene carpeta
        en OTRA categoría (ver _find_category_with_existing_folder), sin
        esperar a la primera subida real para tener ese dato en caché.
        Silencioso: si falla, la columna se queda con la vista previa por
        género de siempre."""
        def worker():
            from core.ftp_client import FTPClient as _FTPClient
            own_ftp = _FTPClient()
            try:
                ok, _msg = own_ftp.connect(
                    self.config_data.get("ftp_host", ""),
                    int(self.config_data.get("ftp_port", 21)),
                    self.config_data.get("ftp_user", ""),
                    self.config_data.get("ftp_password", ""),
                    self.config_data.get("ftp_use_tls", False))
                if not ok:
                    return
                cats = self.config_data.get("ftp_categories", {"tv": [], "movie": []})
                roots = {c.get("root", "") for cs in cats.values() for c in cs if c.get("root")}
                changed = False
                for root in roots:
                    if root in self._ftp_dir_cache:
                        continue
                    try:
                        # Dos listados, no uno, misma unión que
                        # _find_category_with_existing_folder -- este
                        # prefetch es quien primero rellena
                        # self._ftp_dir_cache para cada raíz, y esa función
                        # SOLO vuelve a listar si la raíz no está ya en
                        # caché: un listado único e incompleto aquí "envenena"
                        # la caché para el resto de la sesión, saltándose la
                        # protección de doble listado de esa otra función por
                        # completo. Caso real: Futurama sí existía en
                        # /datos2/series/, pero al subir un capítulo nuevo se
                        # creó una carpeta duplicada en /datos2/seriespeques
                        # -- la búsqueda de carpeta existente nunca llegó a
                        # listar de verdad esa raíz porque este prefetch ya
                        # la había dejado en caché (incompleta) nada más
                        # arrancar.
                        first = set(own_ftp.list_dirs(root))
                        second = set(own_ftp.list_dirs(root))
                        self._ftp_dir_cache[root] = list(first | second)
                        changed = True
                    except Exception:
                        continue
            finally:
                own_ftp.disconnect()
            if changed:
                self.after(0, lambda: [self._update_row(e) for e in self.files])
        threading.Thread(target=worker, daemon=True).start()

    _BACKGROUND_MISSING_SCAN_INTERVAL_H = 12    # cada cuanto se repite el escaneo de verdad
    _BACKGROUND_MISSING_SCAN_CHECK_MS = 3_600_000   # cada cuanto se comprueba si "toca" (1h)

    def _maybe_background_missing_scan(self):
        """Comprobación periódica y silenciosa (sin diálogo, sin tocar la
        UI salvo el resultado que ya queda en la caché) de si toca repetir
        el escaneo de episodios que faltan -- así, cuando el usuario abre
        el diálogo a mano, la caché ya está razonablemente al día en vez de
        tener que esperar un escaneo completo. No compite con una subida en
        marcha (ancho de banda/CPU), y se reprograma sola cada hora."""
        self.after(self._BACKGROUND_MISSING_SCAN_CHECK_MS, self._maybe_background_missing_scan)
        if not self.config_data.get("jellyfin_enabled") and not self.config_data.get("plex_enabled"):
            return
        if self._upload_running:
            return
        if self._missing_ep_scanning:
            # Un escaneo manual ("Comprobar"/"Reescaneo completo") ya está
            # en marcha -- sin este guard, los dos escaneos cargarían su
            # propia instantánea de missing_episodes_cache.json y cada uno
            # la sobrescribiría entera al terminar, perdiendo en silencio
            # el resultado del que acabase antes (last-write-wins).
            return
        from core.missing_episodes_cache import load_cache
        import time as _time
        last_ts = (load_cache().get("_meta") or {}).get("last_scan_ts", 0)
        if _time.time() - last_ts < self._BACKGROUND_MISSING_SCAN_INTERVAL_H * 3600:
            return
        threading.Thread(target=self._scan_missing_episodes, daemon=True).start()

    def _start_ftp_upload(self, entries):
        if self._upload_running:
            self._set_status("Ya hay una subida en progreso", WARNING_COLOR)
            return
        if not self.config_data.get("ftp_host", ""):
            self._set_status("Configura el servidor FTP en la pestania FTP", WARNING_COLOR)
            return
        if self._missing_ep_scanning:
            # La subida es lo prioritario: un escaneo completo hace muchas
            # llamadas seguidas a TMDB, y compartirlas con la identificación
            # de lo que se está subiendo ahora mismo solo añade tiempos de
            # espera/errores de red sin necesidad -- se cancela solo.
            self._cancel_missing_episodes_scan()
        # Marcar YA como "en marcha" (bloquea clics repetidos). La conexión
        # de verdad la resuelve _queue_worker con su propio pool dedicado
        # (con su propio manejo de error si falla) -- no hace falta probar
        # self.ftp aquí antes, sería una conexión redundante que además
        # competiría por self.ftp con el modo automático sin necesidad.
        self._upload_running = True
        self._set_status("Conectando al servidor FTP...", WARNING_COLOR)
        self._refresh_ftp_space()
        self._begin_ftp_upload(entries)

    def _begin_ftp_upload(self, entries):
        """Continuación de _start_ftp_upload en el hilo principal, una vez
        resuelta la conexión FTP en _connect_and_start_upload."""
        self._upload_queue = list(entries)
        self._upload_cancel.clear()
        self._upload_skip.clear()
        self._upload_current_idx    = -1
        self._upload_current_remote = ""
        self._upload_overwrite_all  = False
        self._upload_duplicate_ignore_all = False
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

    def _find_category_with_existing_folder(self, ftp_conn, info, use_cache_only=False, force_refresh=False):
        """Busca en TODAS las categorías configuradas para el tipo de
        *info* (no solo en la que tocaría por género) si ya existe una
        carpeta con nombre igual o prácticamente idéntico a la serie --
        para que la organización real del servidor prevalezca sobre la
        clasificación automática cuando no coinciden (series movidas a
        mano de categoría, por ejemplo por tener contenido para adultos y
        ya no encajar en una categoría infantil). Solo hace caso a
        coincidencias de alta confianza: nombre exacto tras sanear, o
        ratio >= 0.90 -- ese 0.90 en concreto es el que da series_similarity
        cuando un nombre está literalmente contenido en el otro ("Desencanto"
        dentro de "Desencanto (Disenchantment)", por ejemplo, un nombre de
        carpeta con el título original entre paréntesis) -- no es un
        parecido vago, así que aceptarlo en silencio aquí es razonable;
        cualquier cosa por debajo se deja para la confirmación de siempre
        dentro de la categoría elegida por género (ver
        _resolve_series_folder). Devuelve (categoría, nombre_de_carpeta_
        existente), o (None, None) si no hay ninguna coincidencia de esa
        confianza -- el nombre exacto de la carpeta hace falta para que la
        vista previa de "Destino" no proponga crear una carpeta nueva con
        el título tal cual lo da TMDB cuando ya existe una parecida (p.ej.
        "(Des)encanto" de TMDB vs "Desencanto" ya en el servidor).
        use_cache_only=True (para la columna "Destino", que se recalcula
        en el hilo de la GUI al redibujar filas) no listará ninguna
        carpeta que no esté ya en caché -- evita bloquear la interfaz
        haciendo una conexión/listado FTP de verdad solo por refrescar una
        vista previa; una vez haya conexión real (al subir), la caché ya
        tendrá el dato y la vista previa se pondrá al día sola.
        force_refresh=True (para el borrado desde Episodios que faltan,
        ver _resolve_missing_ep_series_path) ignora self._ftp_dir_cache
        aunque la raíz ya esté cacheada y vuelve a listarla -- self._ftp_dir_cache
        vive mientras dure la sesión de la app y nunca se invalida sola,
        así que una carpeta añadida DESPUÉS del primer listado de esa raíz
        en toda la sesión (o un listado que se cortó corto sin dar error)
        se queda sin ver hasta reiniciar la app; para una acción tan poco
        frecuente y tan seria como borrar, vale la pena pagar el listado
        de verdad en vez de arriesgarse a un "no encontrado" con la
        carpeta ahí delante.

        El propio emparejamiento (nombre exacto/folder_name conocido/
        parecido >=0.90) vive en core.ftp_categories.find_existing_category_folder,
        compartido con AutoWatcher (ver core/auto_watcher.py) -- aquí solo
        queda la parte de CÓMO listar/cachear cada raíz, que sí es propia
        de la GUI (use_cache_only para no bloquear la interfaz)."""
        cats = self.config_data.get("ftp_categories", {"tv": [], "movie": []}).get(info.media_type, [])
        known_folder_name = getattr(info, "folder_name", None)

        def dir_lookup(root):
            if root not in self._ftp_dir_cache or force_refresh:
                if use_cache_only:
                    return None
                # Dos listados, no uno -- visto de verdad con "(Des)encanto":
                # ya tenía carpeta en "Series" desde antes, pero un ÚNICO
                # NLST a esa raíz (360 carpetas) a veces vuelve incompleto
                # sin dar ningún error (mismo servidor donde LIST -R se
                # corta sistemáticamente, ver _build_ftp_episode_index) --
                # eso hacía que ESE episodio (y todos los de la misma tanda,
                # que reutilizan esta misma caché) acabaran mal clasificados
                # por género en vez de en la carpeta que ya tenían. La unión
                # de dos intentos independientes es mucho menos probable que
                # pierda la misma carpeta en los dos a la vez.
                first = set(ftp_conn.list_dirs(root))
                second = set(ftp_conn.list_dirs(root))
                if first != second:
                    _log.warning("Listado de '%s' inconsistente entre dos intentos seguidos "
                                "(%d vs %d carpetas) -- usando la unión de ambos", root, len(first), len(second))
                self._ftp_dir_cache[root] = list(first | second)
            return self._ftp_dir_cache[root]

        from core.ftp_categories import find_existing_category_folder
        return find_existing_category_folder(cats, info.title, known_folder_name, dir_lookup)

    def _resolve_series_folder(self, ftp_conn, category: dict, info, entry=None) -> str:
        """Si ya existe en la raíz de *category* una carpeta con nombre
        parecido (idioma, artículo, nombre corto vs largo, título original
        entre paréntesis...) a la serie a subir, la reutiliza directamente
        cuando la confianza es alta (ratio >= 0.90, igual que
        _find_category_with_existing_folder -- mismo criterio en los dos
        sitios, para que la vista previa de "Destino" y la subida real
        coincidan sin pedir una confirmación que ya se daba por hecha en
        la vista previa). Por debajo de eso, pregunta una sola vez por
        serie si hay que reutilizarla en vez de crear una nueva a mayores.
        La respuesta se cachea para el resto de episodios de la misma
        serie dentro de esta tanda de subida."""
        desired = info.title
        with self._series_folder_lock:
            if info.tmdb_id in self._series_folder_cache:
                return self._series_folder_cache[info.tmdb_id]

            chosen = desired
            root = category.get("root", "")
            if root:
                if root not in self._ftp_dir_cache:
                    # Dos listados, no uno, misma unión que
                    # _find_category_with_existing_folder -- esta caché es
                    # compartida (self._ftp_dir_cache) y quien la rellene
                    # primero decide lo que ven todos los demás lectores
                    # mientras dure la sesión (ninguno vuelve a listar si ya
                    # está en caché); un listado único e incompleto aquí
                    # tiene el mismo riesgo de "envenenarla" que ya se vio
                    # en _prefetch_ftp_category_dirs.
                    first = set(ftp_conn.list_dirs(root))
                    second = set(ftp_conn.list_dirs(root))
                    self._ftp_dir_cache[root] = list(first | second)
                existing = self._ftp_dir_cache[root]

                sanitized_desired = _ftp_safe(desired)
                if sanitized_desired in existing:
                    chosen = sanitized_desired
                else:
                    candidate, ratio = best_match(desired, existing, min_ratio=0.55)
                    if candidate:
                        if ratio >= 0.90:
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
            _log.warning("Subida: archivo local no encontrado %r", entry.path)
            return False, "archivo_no_encontrado"

        info = entry.media_info
        if not info:
            self._ftp_row_set(entry, "Sin info TMDB", 0, 0)
            return True, "sin_info"

        if entry.remote_dir_override:
            # El usuario fijó la carpeta a mano (doble clic en la columna
            # "Destino" de la tabla) -- prevalece sobre todo lo demás, ni
            # siquiera se consulta la categoría por género.
            remote_dir = entry.remote_dir_override
        else:
            category = self._category_for(info)
            if info.media_type == "tv":
                # Si la serie ya tiene carpeta en OTRA categoría (se movió a
                # mano, p.ej. por tener contenido para adultos y ya no
                # encajar en la categoría infantil que le tocaría por
                # género), esa organización real del servidor prevalece sobre
                # la clasificación automática -- ver _find_category_with_existing_folder.
                existing_category, _existing_name = self._find_category_with_existing_folder(ftp_conn, info)
                if existing_category:
                    if category and existing_category.get("root") != category.get("root"):
                        _log.info(
                            "Subida: '%s' (tmdb_id=%s) -- categoría por género sería '%s', "
                            "pero ya existe carpeta en '%s' -- se usa esta última",
                            info.title, info.tmdb_id, category.get("name"), existing_category.get("name"))
                    category = existing_category
                else:
                    # Sin coincidencia de alta confianza en NINGUNA categoría
                    # -- normal en una serie nueva, pero también el síntoma
                    # visto de verdad con "(Des)encanto": ya tenía carpeta en
                    # "Series", pero esta comprobación no la encontró esa vez
                    # (coincidiendo con un reescaneo pesado a la vez) y acabó
                    # en "SeriesPeques" solo por género (Animación). Con esto
                    # en el log, la próxima vez que pase se ve al momento en
                    # vez de por casualidad al revisar el FTP a mano.
                    _log.info(
                        "Subida: '%s' (tmdb_id=%s) -- sin carpeta existente encontrada en ninguna "
                        "categoría, se usa la de género: '%s'",
                        info.title, info.tmdb_id, category.get("name") if category else None)
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
            full_tpl   = root.rstrip("/") + "/" + category.get("template", "{serie}/")
            remote_dir = ftp_conn.build_remote_path(full_tpl, serie_name, info.season, info.year, info.media_type)
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

        # "Sobrescribir todos"/"Empezar de cero (todos)"/"Subir todas de
        # todas formas" solo tiene sentido si hay más de un archivo en esta
        # tanda de subida.
        is_batch = len(self._upload_queue) > 1

        # Detección de duplicados: ¿ya hay en esta misma carpeta remota un
        # archivo DISTINTO que representa el mismo contenido (mismo
        # episodio, u otra versión de la misma película)? Distinto de la
        # comprobación de "ya existe" de más abajo, que solo mira el nombre
        # remoto exacto -- esto detecta el mismo contenido llegado con un
        # nombre de archivo diferente (otra fuente/calidad/grupo). Solo se
        # comprueba una vez por entrada (no en cada reintento) para no
        # listar la carpeta remota una y otra vez ni repreguntar lo mismo.
        if not getattr(entry, "_duplicate_checked", False):
            entry._duplicate_checked = True
            from core.duplicate_detect import find_duplicate
            existing_files = ftp_conn.list_files(remote_dir)
            dup = find_duplicate(existing_files, info, remote_filename)
            if dup and not self._upload_duplicate_ignore_all:
                prev_status = entry.status
                entry.status = "esperando_confirmacion"
                self.after(0, lambda e=entry: self._update_row(e))
                answer = [None]
                ev = threading.Event()
                def _ask(d=dup, ans=answer, e=ev):
                    dlg = _OverwriteDialog(
                        self, d,
                        title="Posible contenido duplicado",
                        message=("Ya hay un archivo distinto en el servidor que parece "
                                 "ser el mismo contenido:"),
                        overwrite_label="Subir de todas formas",
                        all_label="Subir todas de todas formas",
                        close_result="skip",
                        show_all_button=is_batch)
                    ans[0] = dlg.result
                    e.set()
                self.after(0, _ask)
                ev.wait()
                if answer[0] == "all":
                    self._upload_duplicate_ignore_all = True
                elif answer[0] != "overwrite":   # "skip" (Omitir o cerrado con la X)
                    entry.status = prev_status
                    self.after(0, lambda e=entry: self._update_row(e))
                    return True, "omitido"

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
            prev_status = entry.status
            entry.status = "esperando_confirmacion"
            self.after(0, lambda e=entry: self._update_row(e))
            answer = [None]
            ev = threading.Event()
            def _ask(rf=remote_file, ans=answer, e=ev):
                dlg = _OverwriteDialog(self, rf, show_all_button=is_batch)
                ans[0] = dlg.result
                e.set()
            self.after(0, _ask)
            ev.wait()
            if answer[0] == "all":
                self._upload_overwrite_all = True
                force_overwrite = True
            elif answer[0] == "skip":
                # Omitir o cerrar el diálogo (por defecto también "skip"):
                # vuelve al estado de antes de preguntar en vez de quedarse
                # marcado "Omitido" — es un "ahora no", no una exclusión
                # permanente, así que la fila queda lista para reintentarse
                # normalmente más adelante.
                entry.status = prev_status
                self.after(0, lambda e=entry: self._update_row(e))
                return True, "omitido"
            else:   # "overwrite"
                force_overwrite = True
        elif remote_size is not None and remote_size >= local_size and self._upload_overwrite_all:
            # "Sobrescribir todos" ya activo de un archivo anterior de esta tanda
            force_overwrite = True
        elif stalled and not self._upload_overwrite_all:
            # El archivo remoto no avanzó nada desde el intento anterior:
            # seguir reanudando el mismo punto para siempre no lleva a
            # ningún sitio si ese punto está atascado (p.ej. un parcial
            # dañado de un corte anterior) — dar la opción de empezar de
            # cero en vez de reintentar sin fin sin ninguna salida.
            prev_status = entry.status
            entry.status = "esperando_confirmacion"
            self.after(0, lambda e=entry: self._update_row(e))
            answer = [None]
            ev = threading.Event()
            def _ask(rf=remote_file, ans=answer, e=ev, rs=remote_size, ls=local_size):
                dlg = _OverwriteDialog(
                    self, rf,
                    title="La subida no avanza",
                    message=(f"Este archivo lleva al menos un intento sin avanzar en "
                             f"el servidor (sigue en {_fmt_size(rs)} de {_fmt_size(ls)}). "
                             f"Puede que el punto de reanudación esté dañado."),
                    overwrite_label="Empezar de cero",
                    all_label="Empezar de cero (todos)",
                    close_result="skip",
                    show_all_button=is_batch)
                ans[0] = dlg.result
                e.set()
            self.after(0, _ask)
            ev.wait()
            if answer[0] == "all":
                self._upload_overwrite_all = True
                force_overwrite = True
            elif answer[0] == "overwrite":
                force_overwrite = True
            else:   # "skip" (Omitir o cerrado con la X)
                entry.status = prev_status
                self.after(0, lambda e=entry: self._update_row(e))
                return True, "omitido"
        elif stalled and self._upload_overwrite_all:
            force_overwrite = True

        free = self._get_free_space_with_jellyfin_fallback(ftp_conn, root)
        if free is not None and free < local_size:
            self._ftp_row_set(entry, "Sin espacio", 0, 0)
            self.after(0, lambda gb=free/(1024**3): self._set_status(
                f"Disco lleno — libre: {gb:.1f} GB", ERROR_COLOR))
            self._upload_cancel.set()
            _log.error("Subida: sin espacio antes de empezar %r (libre: %s, necesita: %s)",
                      remote_filename, _fmt_size(free), _fmt_size(local_size))
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

        _log.info("Subida: iniciando %r -> %s (resume=%s)",
                  remote_filename, remote_dir, not force_overwrite)
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
                remote_file, "ok", size, local_path=entry.path)
            _log.info("Subida: OK %r (%s)", remote_filename, _fmt_size(size))
            from core.media_server_refresh import trigger_refresh
            trigger_refresh(self.config_data)
            # Este método corre en un hilo de subida, no en el de la GUI --
            # a diferencia del mismo hook en _on_auto_file_event (que ya
            # corre dentro de self.after), aquí sí hay que agendarlo.
            self.after(0, lambda mi=entry.media_info: self._remove_uploaded_episode_from_missing_list(mi))
            self.after(0, self._refresh_ftp_space)
        elif msg == "cancelado":
            entry.status = "listo"
            self._ftp_row_set(entry, "Cancelado", entry.ftp_progress, 0)
            _log.info("Subida: cancelada por el usuario %r", remote_filename)
        elif msg == "saltado":
            entry.status = "listo"
            self._ftp_row_set(entry, "Saltado", 0, 0)
            _log.info("Subida: saltada %r", remote_filename)
        elif msg == "disco_lleno":
            entry.status    = "error"
            entry.error_msg = "Disco lleno en el servidor"
            self._ftp_row_set(entry, "Disco lleno", 0, 0)
            self.after(0, lambda: self._set_status("Disco lleno en servidor", ERROR_COLOR))
            self._upload_cancel.set()
            self._save_history_entry(
                entry.new_name or Path(entry.path).name,
                remote_file, "error", size, error_msg=entry.error_msg, local_path=entry.path)
            _log.error("Subida: disco lleno en servidor, cancelando %r", remote_filename)
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
                remote_file, "error", size, error_msg=entry.error_msg, local_path=entry.path)
            _log.error("Subida: ERROR %r — %s", remote_filename, entry.error_msg)

        if not ok:
            # La subida no llegó a completarse: quitar la marca "subiendo"
            # para no dejar el archivo bloqueado para AutoWatcher para
            # siempre — si de verdad se quedó a medias, que pueda reintentarlo
            # él también (o el usuario, a mano, otra vez).
            self._unmark_auto_processed(entry.path)

        self.after(0, lambda e=entry: self._update_row(e))
        return ok or msg in ("omitido", "saltado", "sin_info"), msg

    def _queue_worker(self):
        from concurrent.futures import ThreadPoolExecutor
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
        connect_error = ""
        for _ in range(parallel):
            c = _FTPClient()
            ok, msg = c.connect(host, port, user, password, use_tls)
            if ok:
                pool.append(c)
            else:
                connect_error = msg
                self.after(0, lambda m=msg: self._set_status(m, ERROR_COLOR))
                break

        if not pool:
            # Sin esto, las filas se quedaban mostrando "En cola" para
            # siempre: el mensaje de error solo se veía un instante en la
            # barra de estado, pero nada actualizaba las filas ya puestas en
            # cola en _begin_ftp_upload, así que parecía que la subida se
            # había quedado colgada sin explicación.
            for entry in self._upload_queue:
                entry.status    = "error"
                entry.error_msg = connect_error or "No se pudo conectar al servidor FTP"
                self.after(0, lambda e=entry: self._ftp_row_set(e, "Error", 0, 0))
                self.after(0, lambda e=entry: self._update_row(e))
            self._upload_running = False
            return

        if len(pool) < parallel:
            # Diagnóstico: "conexiones en paralelo" pedía "parallel", pero
            # si alguna conexión de más falla a mitad del bucle de arriba,
            # este queda con MENOS -- y como pool no está vacío, seguía
            # adelante en silencio con ese cupo reducido, sin avisar de que
            # las subidas iban a ir con menos paralelismo del configurado.
            _log.warning("Subida: se pidieron %d conexiones en paralelo pero solo se consiguieron %d "
                        "(%s) -- las subidas de esta tanda irán con menos paralelismo del configurado",
                        parallel, len(pool), connect_error or "sin más detalle")
        else:
            _log.info("Subida: %d conexión(es) en paralelo listas para esta tanda", len(pool))

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
            _logged_fill = False
            while not self._upload_cancel.is_set():
                # Enviar trabajos disponibles hasta llenar el pool
                while idx < len(self._upload_queue) and len(pending) < len(pool):
                    e = self._upload_queue[idx]
                    idx += 1
                    f = executor.submit(process, e)
                    pending[f] = e
                if not _logged_fill and len(self._upload_queue) > 1:
                    # Diagnóstico una sola vez, al primer llenado -- cuántos
                    # archivos se enviaron a la vez de golpe frente a los
                    # que había en cola y cuántas conexiones había
                    # disponibles. Si esto muestra 1 en vez de len(pool),
                    # el paralelismo se está perdiendo ANTES de llegar
                    # siquiera a _upload_slots (que se comprobó aparte y sí
                    # permite varias a la vez), no dentro de él.
                    _logged_fill = True
                    _log.info("Subida: %d archivo(s) enviados a la vez al arrancar (de %d en cola, "
                              "%d conexión(es) disponibles)", len(pending), len(self._upload_queue), len(pool))
                if pending:
                    done, _ = _fut_wait(list(pending), timeout=0.3, return_when=_FIRST)
                    for f in done:
                        e = pending.pop(f)
                        try:
                            f.result()
                        except Exception as exc:
                            e.status    = "error"
                            e.error_msg = str(exc)
                            _log.exception("Subida: excepcion inesperada procesando %r", e.name)
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
        # Velocidad total en la barra de estado (ver _update_status_bar) --
        # solo reconfigura una etiqueta de texto, no reconstruye ninguna
        # tabla, así que no hace falta frenar esto aunque se llame varias
        # veces por segundo con varias subidas a la vez.
        self._update_status_bar()

    def _ftp_row_set(self, entry, status_text, pct, speed_bps):
        """Fija el estado final de una fila FTP (llamado desde worker, usa after)."""
        entry.ftp_progress = pct
        entry.ftp_speed    = speed_bps
        entry.ftp_status   = status_text
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
        """Abre el archivo con la aplicación predeterminada del sistema."""
        try:
            if is_windows():
                os.startfile(entry.path)   # no existe en macOS/Linux
            elif is_macos():
                subprocess.run(["open", entry.path], check=True)
            else:
                subprocess.run(["xdg-open", entry.path], check=True)
        except Exception as e:
            self._set_status(f"No se pudo abrir: {e}", ERROR_COLOR)

    def _open_containing_folder(self, entry):
        """Abre la carpeta que contiene el archivo en el explorador del
        sistema -- en Windows/macOS, además, deja el archivo ya
        seleccionado dentro de esa carpeta (no solo abierta)."""
        try:
            if is_windows():
                # "/select," pegado sin espacio a la ruta es la sintaxis que
                # entiende explorer.exe para abrir la carpeta con el archivo
                # ya resaltado, en vez de solo abrir la carpeta a secas.
                subprocess.run(["explorer", "/select,", entry.path])
            elif is_macos():
                subprocess.run(["open", "-R", entry.path], check=True)
            else:
                subprocess.run(["xdg-open", str(Path(entry.path).parent)], check=True)
        except Exception as e:
            self._set_status(f"No se pudo abrir la carpeta: {e}", ERROR_COLOR)

    # ------------------------------------------------------------- FTP tab

    def _build_config_panel(self, panel):
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(0, weight=1)

        # Compartidas por todas las pestañas de Configuración -- mismo
        # motivo que en las tablas (_build_files_tab, etc.): decenas de
        # etiquetas repiten el mismo tamaño/peso, y crear un CTkFont
        # nuevo (llamada a Tcl) para cada una se notaba al construir este
        # panel entero de golpe al arrancar.
        self._cfg_font_title = ctk.CTkFont(size=14, weight="bold")
        self._cfg_font_subtitle = ctk.CTkFont(size=13, weight="bold")
        self._cfg_font_small_bold = ctk.CTkFont(size=12, weight="bold")
        self._cfg_font_desc = ctk.CTkFont(size=11)

        # División visual Cliente/Servidor -- ver core/server_config.py::
        # SHARED_CONFIG_KEYS para qué claves son de cada lado. "Cliente":
        # cada equipo/persona la suya, nunca se toca al sincronizar ni al
        # publicar. "Servidor": debe ser igual para todo el que use
        # aRenombrar contra este mismo FTP, se sincroniza sola al arrancar
        # y "Publicar" (cabecera de esta pestaña, ver
        # _build_server_publish_header) la sobrescribe para todos.
        tabs = ctk.CTkTabview(panel)
        tabs.grid(row=0, column=0, sticky="nsew")
        self._config_tabs = tabs

        client_tab = tabs.add("🖥 Cliente")
        server_tab = tabs.add("🌐 Servidor")

        client_tabs = ctk.CTkTabview(client_tab)
        client_tabs.pack(fill="both", expand=True)
        self._build_general_tab(client_tabs.add("General"))
        self._build_ftp_connection_tab(client_tabs.add("Conexión FTP"))
        self._build_watch_sync_config_tab(client_tabs.add("Sincronizar visionado"))
        self._build_config_transfer_section(client_tabs.add("Copia de seguridad"))

        server_wrap = ctk.CTkFrame(server_tab, fg_color="transparent")
        server_wrap.pack(fill="both", expand=True)
        self._build_server_publish_header(server_wrap)
        server_tabs = ctk.CTkTabview(server_wrap)
        server_tabs.pack(fill="both", expand=True)
        self._build_tmdb_tab(server_tabs.add("TMDB / IA"))
        self._build_templates_tab(server_tabs.add("Plantillas"))
        self._build_ftp_categories_section(server_tabs.add("Categorías"))
        self._build_media_servers_section(server_tabs.add("Servidores de medios"))
        self._build_reservation_quota_tab(server_tabs.add("Reservas"))

        ctk.CTkLabel(panel, text=f"aRenombrar v{__version__}",
                     text_color=PENDING_COLOR, font=self._cfg_font_desc).grid(
            row=1, column=0, pady=(4, 8))

        self._load_genres_async()

    def _build_general_tab(self, tab):
        scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        scroll.pack(fill="both", expand=True)

        # ── Modo Automático ──
        auto_fr = ctk.CTkFrame(scroll)
        auto_fr.pack(fill="both", expand=True, padx=0, pady=(0, 8))
        auto_fr.grid_columnconfigure(1, weight=1)
        auto_fr.grid_columnconfigure(3, weight=1)
        ctk.CTkLabel(auto_fr, text="Modo Automático",
                     font=self._cfg_font_title).grid(
            row=0, column=0, columnspan=4, pady=(12, 6))
        ctk.CTkLabel(auto_fr, text="La carpeta vigilada se monitoriza en segundo plano: detecta, renombra y sube archivos de vídeo automáticamente.",
                     font=self._cfg_font_desc, text_color=PENDING_COLOR, wraplength=600).grid(
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
        ctk.CTkLabel(conf_fr, text="(0 = aceptar todo)", font=self._cfg_font_desc,
                     text_color=PENDING_COLOR).pack(side="left", padx=(8, 0))

        # Iniciar con el sistema (registro de Windows / LaunchAgent de macOS
        # — ver _set_autostart). En Linux no hay una convención única
        # (systemd user unit vs. .desktop en autostart/), así que el switch
        # se deshabilita ahí en vez de aparentar que hace algo.
        if is_windows():
            _autostart_text = "Iniciar con Windows (minimizado en bandeja)"
        elif is_macos():
            _autostart_text = "Iniciar con macOS (minimizado en bandeja)"
        else:
            _autostart_text = "Iniciar con el sistema (no disponible en Linux)"
        self._autostart_switch = ctk.CTkSwitch(auto_fr, text=_autostart_text)
        self._autostart_switch.grid(row=6, column=0, columnspan=4, pady=(4, 4))
        if self.config_data.get("start_with_windows", False):
            self._autostart_switch.select()
        if not (is_windows() or is_macos()):
            self._autostart_switch.configure(state="disabled")

        # Notificaciones de escritorio
        self._notif_switch = ctk.CTkSwitch(
            auto_fr, text="Notificaciones de escritorio al completar subidas")
        self._notif_switch.grid(row=7, column=0, columnspan=4, pady=(0, 4))
        if self.config_data.get("desktop_notifications", True):
            self._notif_switch.select()

        # Renombrar en origen (aplica también a la subida manual) -- el
        # renombrado en destino/FTP es configuración de SERVIDOR (afecta a
        # lo que ve todo el mundo en el servidor compartido), se configura
        # junto a las plantillas en Servidor -> Plantillas, ver
        # _build_templates_tab.
        self._rename_local_switch = ctk.CTkSwitch(
            auto_fr, text="Renombrar archivos en origen (local)")
        self._rename_local_switch.grid(row=8, column=0, columnspan=4, pady=(8, 2))
        if self.config_data.get("rename_local", True):
            self._rename_local_switch.select()

        ctk.CTkLabel(auto_fr,
                     text="Si lo desactivas, los archivos locales conservan su nombre original al\n"
                          "añadirlos o subirlos. El renombrado en el servidor se configura aparte, "
                          "en Servidor → Plantillas.",
                     font=self._cfg_font_desc, text_color=PENDING_COLOR, justify="center").grid(
            row=9, column=0, columnspan=4, pady=(0, 12))

    def _build_ftp_connection_tab(self, tab):
        scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        scroll.pack(fill="both", expand=True)

        # ── Conexion FTP ──
        conn = ctk.CTkFrame(scroll)
        conn.pack(fill="both", expand=True, padx=0, pady=(0, 8))
        conn.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(conn, text="Conexión FTP",
                     font=self._cfg_font_title).grid(
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
                     font=self._cfg_font_desc).pack(side="left", padx=(4, 0))

        # Reintentos automáticos
        ctk.CTkLabel(conn, text="Reintentos en error:").grid(
            row=8, column=0, sticky="e", padx=10, pady=6)
        self._ftp_retries_entry = ctk.CTkEntry(conn, width=80)
        self._ftp_retries_entry.insert(0, str(self.config_data.get("ftp_retries", 3)))
        self._ftp_retries_entry.grid(row=8, column=1, sticky="w", padx=10, pady=6)

        # Carpeta compartida donde viven favoritos/reservas/configuración
        # de servidor -- una CARPETA, no un archivo (ver _shared_data_path):
        # dentro se crean 3 archivos con nombre fijo, uno por función
        # (aRenombrar_favoritos.json, aRenombrar_reservas.json,
        # aRenombrar_config_servidor.json). Vacío = las 3 funciones solo
        # locales, sin sincronizar.
        ctk.CTkLabel(conn, text="Carpeta compartida (datos):").grid(
            row=9, column=0, sticky="e", padx=10, pady=6)
        self._shared_data_ftp_path_entry = ctk.CTkEntry(
            conn, width=200, placeholder_text="/datos2/aRenombrar")
        self._shared_data_ftp_path_entry.insert(0, str(self.config_data.get("shared_data_ftp_path", "")))
        self._shared_data_ftp_path_entry.grid(row=9, column=1, padx=10, pady=6, sticky="ew")

        # Nombre de esta persona -- identifica su cuota de reservas (ver
        # core/reservations.py, tamaño configurable en Servidor ->
        # reservation_quota_gb) al reservar espacio en "Liberar espacio".
        # Solo hace falta si se van a usar reservas; favoritos no lo
        # necesita (es un concepto sin dueño, compartido por todos).
        ctk.CTkLabel(conn, text="Tu nombre (reservas):").grid(
            row=10, column=0, sticky="e", padx=10, pady=6)
        self._app_user_name_entry = ctk.CTkEntry(
            conn, width=200, placeholder_text="Para repartir la cuota de reservas por persona")
        self._app_user_name_entry.insert(0, str(self.config_data.get("app_user_name", "")))
        self._app_user_name_entry.grid(row=10, column=1, padx=10, pady=6, sticky="ew")

        bf = ctk.CTkFrame(conn, fg_color="transparent")
        bf.grid(row=11, column=0, columnspan=2, pady=10)
        ctk.CTkButton(bf, text="Probar conexión", command=self._test_ftp).pack(side="left", padx=4)
        self._ftp_status = ctk.CTkLabel(conn, text="", text_color=PENDING_COLOR)
        self._ftp_status.grid(row=12, column=0, columnspan=2, pady=4)

    def _build_server_publish_header(self, parent):
        """Cabecera fija de la super-pestaña "🌐 Servidor" (ver
        _build_config_panel) -- publica TODAS las claves de servidor a la
        vez (TMDB/IA, plantillas, categorías, servidores de medios,
        enlaces, cuota de reservas -- ver
        core/server_config.py::SHARED_CONFIG_KEYS, incluye credenciales a
        propósito), así que vive fuera de cualquier sub-pestaña concreta,
        visible sin importar cuál esté activa. Se sincroniza sola al
        arrancar; publicar es la única acción manual, y deliberadamente
        lo es -- sobrescribe lo de todos, no algo para hacer sin querer
        al guardar Ajustes normales."""
        ctk.CTkButton(parent, text="📤 Publicar como configuración del servidor", width=280,
                      fg_color="transparent", border_width=1,
                      command=self._publish_server_config).pack(pady=(8, 0))
        ctk.CTkLabel(parent, text="Sube TMDB/IA, plantillas, categorías FTP, Plex/Jellyfin, enlaces "
                                  "y la cuota de reservas de este equipo (con sus claves y tokens) "
                                  "para que los adopten los otros clientes de este mismo servidor.",
                     font=self._cfg_font_desc, text_color=PENDING_COLOR, wraplength=380).pack(pady=(0, 8))

    def _build_reservation_quota_tab(self, tab):
        """Cuánto puede reservar cada persona en "Protegidos" (ver
        core/reservations.py) para que su contenido nunca aparezca como
        candidato a borrar en Liberar espacio -- un único límite para
        todo el que reserve contra este servidor, no una constante fija
        en el código ni algo que cada persona decida por su cuenta."""
        scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        scroll.pack(fill="both", expand=True)
        fr = ctk.CTkFrame(scroll)
        fr.pack(fill="both", expand=True, padx=0, pady=(0, 8))
        fr.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(fr, text="Cuota de reservas",
                     font=self._cfg_font_title).grid(row=0, column=0, pady=(12, 4))
        ctk.CTkLabel(fr, text="Cuánto puede reservar cada persona en \"Protegidos\" para que su contenido\n"
                              "nunca aparezca como candidato a borrar en Liberar espacio.",
                     font=self._cfg_font_desc, text_color=PENDING_COLOR, justify="center").grid(
            row=1, column=0, pady=(0, 12))

        quota_row = ctk.CTkFrame(fr, fg_color="transparent")
        quota_row.grid(row=2, column=0, pady=(0, 16))
        ctk.CTkLabel(quota_row, text="GB por persona:").pack(side="left", padx=(0, 6))
        self._reservation_quota_entry = ctk.CTkEntry(quota_row, width=80)
        self._reservation_quota_entry.insert(0, str(self.config_data.get("reservation_quota_gb", 100)))
        self._reservation_quota_entry.pack(side="left")

    def _build_watch_sync_config_tab(self, tab):
        """Configuración de "Sincronizar visionado" (Cliente, no
        Servidor -- el emparejamiento es conocimiento personal, "qué
        persona eres tú en cada plataforma", y la config de servidor se
        aplica SIN revisión en cada arranque, lo que rompería la
        garantía de revisión humana que tiene el botón manual de esta
        función -- ver core/server_config.py, sección "Excluido a
        propósito"). Aquí solo vive el EMPAREJAMIENTO y la programación
        horaria; el botón de sincronizar y la vista previa viven en la
        pestaña principal "🔄 Sincronizar visionado" (ver
        _build_watch_sync_top_tab), igual que Archivos/Episodios/etc.

        UN único scroll para toda la pestaña -- la lista de usuarios
        emparejados NO tiene scroll propio (frame normal que crece con
        cada fila añadida, no un TableView con scrollable=True), porque
        un scroll interno diminuto para una lista de 2-3 personas no
        sirve de nada; si algún día crece mucho, la usa el scroll de la
        pestaña."""
        outer = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        outer.pack(fill="both", expand=True)
        fr = ctk.CTkFrame(outer, fg_color="transparent")
        fr.pack(fill="both", expand=True)
        fr.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(fr, text="Sincronizar visionado", font=self._cfg_font_title).grid(
            row=0, column=0, pady=(8, 2))
        ctk.CTkLabel(fr, text="Emparejamiento de usuarios y sincronización automática. El botón "
                              "manual y la vista previa están en la pestaña \"🔄 Sincronizar "
                              "visionado\".",
                     font=self._cfg_font_desc, text_color=PENDING_COLOR, wraplength=460,
                     justify="center").grid(row=1, column=0, pady=(0, 10))

        if not (self.config_data.get("plex_enabled") and self.config_data.get("jellyfin_enabled")):
            ctk.CTkLabel(fr, text="Activa Plex y Jellyfin en Ajustes → Servidor → \"Servidores de "
                                  "medios\" para poder usar esta utilidad.",
                         font=self._cfg_font_desc, text_color=WARNING_COLOR, justify="center").grid(
                row=2, column=0, pady=(0, 16))
            return

        # ── Emparejamiento -- SIEMPRE visible, sin botón "editar" (se
        # reveló como una interacción rara ocultar/mostrar todo el editor
        # tras un botón). Cada añadir/quitar se aplica y guarda al
        # instante, no hay paso "Guardar" aparte. Nunca se sugiere una
        # pareja por nombre parecido -- los nombres no tienen por qué
        # coincidir entre plataformas. La lista siempre lee directo de
        # config_data (nunca una copia en memoria aparte), así que
        # persiste entre reinicios y refleja el último estado guardado
        # sin ningún paso extra.
        self._watch_sync_plex_users = []
        self._watch_sync_jellyfin_users = []
        self._watch_sync_pending_pair = None   # (plex_user, jf_user) esperando confirmación

        # fg_color="transparent": sin esto, este panel usa el gris por
        # defecto de CTkFrame, que en modo oscuro es EL MISMO gris que las
        # filas de la tabla (gray17) -- header, panel y filas se fundían
        # en un bloque sin distinción. Igual que el "body" transparente
        # que envuelve la tabla en Episodios que faltan.
        mapping_fr = ctk.CTkFrame(fr, fg_color="transparent")
        mapping_fr.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 10))
        mapping_fr.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(mapping_fr, text="Usuarios emparejados -- se verifica la contraseña de Jellyfin "
                                      "y se confirma antes de guardar cada pareja.",
                     font=ctk.CTkFont(size=12, weight="bold"), wraplength=460, justify="center").grid(
            row=0, column=0, pady=(10, 6), padx=10)
        status_row_fr = ctk.CTkFrame(mapping_fr, fg_color="transparent")
        status_row_fr.grid(row=1, column=0, pady=(0, 6))
        self._watch_sync_mapping_status_label = ctk.CTkLabel(
            status_row_fr, text="Cargando usuarios...", text_color=WARNING_COLOR)
        self._watch_sync_mapping_status_label.pack(side="left")
        # La lista de usuarios de Plex/Jellyfin solo se carga UNA vez, al
        # construir esta pestaña -- si algo cambia después (una cuenta
        # nueva, o el propietario apareciendo por primera vez tras este
        # mismo arreglo, ver get_plex_home_users) no se refleja solo,
        # antes había que reiniciar la app entera para volver a verla.
        refresh_lbl = ctk.CTkLabel(status_row_fr, text=" 🔄 Actualizar", cursor="hand2",
                                   font=ctk.CTkFont(size=11, underline=True), text_color=PENDING_COLOR)
        refresh_lbl.pack(side="left", padx=(8, 0))
        refresh_lbl.bind("<Button-1>", lambda e: self._refresh_watch_sync_mapping_users())

        # Mismo componente TableView que el resto de la app (Archivos/
        # Episodios/Liberar espacio/Historial), pero scrollable=False --
        # crece con cada fila añadida en vez de tener su propia barra de
        # scroll, y se apoya en el scroll general de la pestaña.
        self._watch_sync_mapping_table = TableView(mapping_fr, scrollable=False, columns=[
            ColumnSpec("plex", "Usuario Plex", width=180),
            ColumnSpec("jellyfin", "Usuario Jellyfin", width=180),
            ColumnSpec("del", "", width=36),
        ])
        self._watch_sync_mapping_table.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 6))

        add_row_fr = ctk.CTkFrame(mapping_fr, fg_color="transparent")
        add_row_fr.grid(row=4, column=0, pady=(0, 4))
        self._watch_sync_plex_combo = ctk.CTkComboBox(add_row_fr, width=160, values=[], state="readonly")
        self._watch_sync_plex_combo.pack(side="left", padx=(0, 6))
        self._watch_sync_jellyfin_combo = ctk.CTkComboBox(add_row_fr, width=160, values=[], state="readonly")
        self._watch_sync_jellyfin_combo.pack(side="left", padx=(0, 6))
        self._watch_sync_jellyfin_password_entry = ctk.CTkEntry(
            add_row_fr, width=140, show="*", placeholder_text="Contraseña Jellyfin")
        self._watch_sync_jellyfin_password_entry.pack(side="left", padx=(0, 6))
        self._watch_sync_add_mapping_btn = ctk.CTkButton(
            add_row_fr, text="+ Añadir", width=90, state="disabled",
            command=self._start_add_watch_sync_mapping)
        self._watch_sync_add_mapping_btn.pack(side="left")

        # Paso de confirmación -- oculto hasta que la contraseña de
        # Jellyfin se verifica correctamente. Nunca se añade una pareja
        # sin este paso explícito, aunque la contraseña sea correcta.
        self._watch_sync_confirm_frame = ctk.CTkFrame(mapping_fr, fg_color="transparent")
        self._watch_sync_confirm_frame.grid(row=5, column=0, pady=(0, 8))
        self._watch_sync_confirm_frame.grid_remove()
        self._watch_sync_confirm_label = ctk.CTkLabel(
            self._watch_sync_confirm_frame, font=ctk.CTkFont(size=12, weight="bold"),
            wraplength=420, justify="center")
        self._watch_sync_confirm_label.pack(pady=(0, 6))
        confirm_bf = ctk.CTkFrame(self._watch_sync_confirm_frame, fg_color="transparent")
        confirm_bf.pack()
        ctk.CTkButton(confirm_bf, text="Cancelar", width=110, fg_color="transparent", border_width=1,
                      command=self._cancel_add_watch_sync_mapping).pack(side="left", padx=6)
        ctk.CTkButton(confirm_bf, text="Sí, son la misma persona", width=210,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER,
                      command=self._confirm_add_watch_sync_mapping).pack(side="left", padx=6)

        self._watch_sync_mapping_add_status_label = ctk.CTkLabel(mapping_fr, text="", text_color=ERROR_COLOR)
        self._watch_sync_mapping_add_status_label.grid(row=6, column=0, pady=(0, 10))

        # ── Sincronización programada -- a diferencia del botón manual
        # (pestaña principal), ESTA escribe sin pedir confirmación: es una
        # decisión explícita del usuario al activarla, sabiendo que
        # renuncia a la revisión previa (ver _run_scheduled_watch_sync).
        schedule_fr = ctk.CTkFrame(fr)
        schedule_fr.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 10))
        schedule_fr.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(schedule_fr, text="Sincronización automática diaria",
                     font=ctk.CTkFont(size=12, weight="bold")).grid(row=0, column=0, pady=(10, 2))
        ctk.CTkLabel(schedule_fr, text="A la hora indicada se sincroniza sola, SIN pedir "
                                       "confirmación -- a diferencia del botón manual. Solo "
                                       "funciona mientras aRenombrar esté abierto en ese "
                                       "momento -- no es una tarea del sistema operativo.",
                     font=ctk.CTkFont(size=11), text_color=WARNING_COLOR, wraplength=420,
                     justify="center").grid(row=1, column=0, pady=(0, 8), padx=10)
        self._watch_sync_schedule_switch = ctk.CTkSwitch(schedule_fr, text="Activar")
        if self.config_data.get("watch_sync_schedule_enabled", False):
            self._watch_sync_schedule_switch.select()
        self._watch_sync_schedule_switch.grid(row=2, column=0, pady=(0, 8))
        time_fr = ctk.CTkFrame(schedule_fr, fg_color="transparent")
        time_fr.grid(row=3, column=0, pady=(0, 10))
        ctk.CTkLabel(time_fr, text="Hora (HH:MM):").pack(side="left", padx=(0, 6))
        self._watch_sync_schedule_time_entry = ctk.CTkEntry(time_fr, width=80, placeholder_text="03:00")
        self._watch_sync_schedule_time_entry.insert(
            0, self.config_data.get("watch_sync_schedule_time", ""))
        self._watch_sync_schedule_time_entry.pack(side="left", padx=(0, 8))
        ctk.CTkButton(time_fr, text="Guardar", width=90,
                      command=self._save_watch_sync_schedule).pack(side="left")
        self._watch_sync_schedule_status_label = ctk.CTkLabel(schedule_fr, text="", text_color=ERROR_COLOR)
        self._watch_sync_schedule_status_label.grid(row=4, column=0, pady=(0, 10))

        self._render_watch_sync_mapping_rows()
        self._load_watch_sync_mapping_users_async()

    def _save_watch_sync_schedule(self):
        """Guarda la programación horaria -- se lee en caliente en cada
        comprobación del programador (ver _check_watch_sync_schedule),
        así que no hace falta reiniciar la app para que un cambio tenga
        efecto."""
        import re
        time_str = self._watch_sync_schedule_time_entry.get().strip()
        enabled = bool(self._watch_sync_schedule_switch.get())
        if enabled and not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", time_str):
            self._watch_sync_schedule_status_label.configure(
                text="Formato de hora no válido -- usa HH:MM (24h), p.ej. 03:00")
            return
        self.config_data.set("watch_sync_schedule_enabled", enabled)
        self.config_data.set("watch_sync_schedule_time", time_str)
        self.config_data.save()
        self._watch_sync_schedule_status_label.configure(text="Guardado", text_color=SUCCESS_COLOR)

    def _build_watch_sync_top_tab(self, parent):
        """Pestaña principal "🔄 Sincronizar visionado" (ver _NAV_LABELS/
        _show_view) -- botón manual, estado y vista previa. El
        emparejamiento de usuarios y la programación horaria viven en
        Configuración → Cliente (ver _build_watch_sync_config_tab).
        Gana "visto": si cualquiera de las dos plataformas lo tiene
        visto, la otra se marca también -- nunca se desmarca nada (ver
        core/watch_sync.py). El botón manual SIEMPRE muestra la vista
        previa y exige confirmación antes de escribir; la sincronización
        programada (Configuración) es la única vía que escribe sin
        pedirla, por decisión explícita del usuario al activarla."""
        # Fuentes propias, no self._cfg_font_* -- esas solo existen tras
        # construir el panel de Configuración (diferido a la primera
        # visita real, ver _build_ui), y esta pestaña se construye
        # SIEMPRE al arrancar, igual que Archivos/Episodios/etc.
        desc_font = ctk.CTkFont(size=11)

        parent.grid_columnconfigure(0, weight=1)

        outer = ctk.CTkScrollableFrame(parent, fg_color="transparent")
        outer.grid(row=0, column=0, sticky="nsew")
        parent.grid_rowconfigure(0, weight=1)
        fr = ctk.CTkFrame(outer, fg_color="transparent")
        fr.pack(fill="both", expand=True)
        fr.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(fr, text="Sincronizar visionado", font=ctk.CTkFont(size=20, weight="bold")).grid(
            row=0, column=0, pady=(8, 2))
        ctk.CTkLabel(fr, text="Marca como visto en una plataforma lo que ya está visto en la otra. "
                              "Nunca desmarca nada y siempre pide confirmación antes de escribir.",
                     font=desc_font, text_color=PENDING_COLOR, wraplength=460,
                     justify="center").grid(row=1, column=0, pady=(0, 10))

        if not (self.config_data.get("plex_enabled") and self.config_data.get("jellyfin_enabled")):
            ctk.CTkLabel(fr, text="Activa Plex y Jellyfin en Configuración → Servidor → \"Servidores "
                                  "de medios\" para poder usar esta utilidad.",
                         font=desc_font, text_color=WARNING_COLOR, justify="center").grid(
                row=2, column=0, pady=(0, 16))
            return

        # ── Sincronizar ──
        run_fr = ctk.CTkFrame(fr, fg_color="transparent")
        run_fr.grid(row=2, column=0, pady=(0, 10))
        last_run = self.config_data.get("watch_sync_last_run_ts", 0)
        last_run_txt = ("nunca" if not last_run
                        else self._fmt_cleanup_scan_age(_time.time() - last_run))
        self._watch_sync_last_run_label = ctk.CTkLabel(
            run_fr, text=f"Última sincronización: {last_run_txt}",
            font=desc_font, text_color=PENDING_COLOR)
        self._watch_sync_last_run_label.pack(pady=(0, 6))
        self._watch_sync_run_button = ctk.CTkButton(
            run_fr, text="🔄 Sincronizar visionado ahora", width=240,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            command=self._run_watch_sync)
        self._watch_sync_run_button.pack(pady=(0, 6))
        self._watch_sync_status_label = ctk.CTkLabel(run_fr, text="", text_color=PENDING_COLOR)
        self._watch_sync_status_label.pack()

        # ── Vista previa -- tabla paginada (25 filas, igual que
        # Episodios/Liberar espacio/Historial). Oculta hasta que hay algo
        # que revisar. Una biblioteca grande puede generar miles de
        # acciones, y construirlas todas de golpe sin paginar agota el
        # límite de objetos GUI de Windows (10000 por proceso) y rompe el
        # pintado de TODA la ventana, no solo de esta tabla.
        self._watch_sync_preview_actions = []
        self._watch_sync_preview_page = 0
        self._WATCH_SYNC_PREVIEW_PAGE_SIZE = 25

        self._watch_sync_preview_header_frame = ctk.CTkFrame(fr, fg_color="transparent")
        self._watch_sync_preview_header_frame.grid(row=3, column=0, sticky="ew", padx=12)
        ctk.CTkLabel(self._watch_sync_preview_header_frame, text="¿Aplicar estos cambios?",
                     font=ctk.CTkFont(size=14, weight="bold")).pack(pady=(4, 4))
        self._watch_sync_preview_summary_label = ctk.CTkLabel(
            self._watch_sync_preview_header_frame, font=ctk.CTkFont(size=12), justify="left")
        self._watch_sync_preview_summary_label.pack(pady=(0, 4))
        self._watch_sync_preview_failed_label = ctk.CTkLabel(
            self._watch_sync_preview_header_frame, font=ctk.CTkFont(size=11),
            text_color=WARNING_COLOR, wraplength=460)
        self._watch_sync_preview_failed_label.pack(pady=(0, 4))
        self._watch_sync_preview_header_frame.grid_remove()

        # scrollable=False: ya está paginada (25 filas por página, ver
        # _render_watch_sync_preview_page), así que no necesita su propia
        # barra de scroll -- crece con las filas de la página actual y se
        # apoya en el scroll general de la pestaña. La paginación sigue
        # siendo necesaria aunque no tenga scroll propio: sin ella, una
        # biblioteca grande podría generar miles de filas de golpe y
        # agotar el límite de objetos GUI de Windows (10000 por proceso),
        # rompiendo el pintado de TODA la ventana.
        self._watch_sync_preview_table = TableView(fr, scrollable=False, columns=[
            ColumnSpec("title", "Título", width=200, expand=True),
            ColumnSpec("season_ep", "T/E", width=60),
            ColumnSpec("target", "Se marcará en", width=110),
        ])
        self._watch_sync_preview_table.grid(row=4, column=0, sticky="ew", padx=12, pady=(0, 4))
        self._watch_sync_preview_table.grid_remove()

        self._watch_sync_preview_footer_frame = ctk.CTkFrame(fr, fg_color="transparent")
        self._watch_sync_preview_footer_frame.grid(row=5, column=0, pady=(0, 10))
        nav_fr = ctk.CTkFrame(self._watch_sync_preview_footer_frame, fg_color="transparent")
        nav_fr.pack(pady=(0, 8))
        self._watch_sync_preview_prev_btn = ctk.CTkButton(
            nav_fr, text="< Anterior", width=100, fg_color="transparent", border_width=1,
            command=lambda: self._watch_sync_change_preview_page(-1))
        self._watch_sync_preview_prev_btn.pack(side="left")
        self._watch_sync_preview_page_lbl = ctk.CTkLabel(nav_fr, text="", text_color=PENDING_COLOR)
        self._watch_sync_preview_page_lbl.pack(side="left", padx=12)
        self._watch_sync_preview_next_btn = ctk.CTkButton(
            nav_fr, text="Siguiente >", width=100, fg_color="transparent", border_width=1,
            command=lambda: self._watch_sync_change_preview_page(1))
        self._watch_sync_preview_next_btn.pack(side="left")
        preview_bf = ctk.CTkFrame(self._watch_sync_preview_footer_frame, fg_color="transparent")
        preview_bf.pack()
        ctk.CTkButton(preview_bf, text="Cancelar", width=120, fg_color="transparent", border_width=1,
                      command=self._cancel_watch_sync_preview).pack(side="left", padx=6)
        ctk.CTkButton(preview_bf, text="Confirmar y sincronizar", width=190,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER,
                      command=self._confirm_watch_sync_preview).pack(side="left", padx=6)
        self._watch_sync_preview_footer_frame.grid_remove()

        # ── Historial de sincronizaciones -- lista persistente (entre
        # reinicios, siempre lee de disco) de todo lo que ya se ha marcado
        # como visto por esta función, ok o fallido -- nunca desaparece en
        # silencio. Mismo patrón que "Historial de subidas" (ver
        # _build_history_tab): tabla paginada a 25 filas por página,
        # límite de objetos GUI de Windows por medio (10000 por proceso).
        hist_header = ctk.CTkFrame(fr, fg_color=("gray90", "gray20"), corner_radius=8)
        hist_header.grid(row=6, column=0, sticky="ew", padx=12, pady=(16, 6))
        self._watch_sync_history_title_lbl = ctk.CTkLabel(
            hist_header, text="Historial de sincronizaciones", font=ctk.CTkFont(size=14, weight="bold"))
        self._watch_sync_history_title_lbl.pack(side="left", padx=12, pady=8)
        ctk.CTkButton(hist_header, text="🗑 Limpiar historial", width=150,
                      fg_color="transparent", border_width=1,
                      border_color=ERROR_COLOR, text_color=ERROR_COLOR,
                      hover_color=("gray85", "#3d1010"),
                      command=self._clear_watch_sync_history).pack(side="right", padx=(4, 12), pady=8)

        self._WATCH_SYNC_HISTORY_PAGE_SIZE = 25
        self._watch_sync_history_all = []
        self._watch_sync_history_page = 0
        self._watch_sync_history_dirty = True   # ver _refresh_watch_sync_history_view
        self._watch_sync_history_col_order = ["fecha", "persona", "titulo", "season_ep", "destino", "estado"]
        self._watch_sync_history_font = ctk.CTkFont(size=11)
        self._watch_sync_history_empty_msg = None

        self._watch_sync_history_table = TableView(fr, scrollable=False, columns=[
            ColumnSpec("fecha", "Fecha", width=130),
            ColumnSpec("persona", "Persona", width=110),
            ColumnSpec("titulo", "Título", width=200, expand=True),
            ColumnSpec("season_ep", "T/E", width=60),
            ColumnSpec("destino", "Marcado en", width=100),
            ColumnSpec("estado", "Estado", width=80),
        ])
        self._watch_sync_history_table.grid(row=7, column=0, sticky="ew", padx=12, pady=(0, 4))

        hist_nav_fr = ctk.CTkFrame(fr, fg_color="transparent")
        hist_nav_fr.grid(row=8, column=0, pady=(0, 10))
        self._watch_sync_history_prev_btn = ctk.CTkButton(
            hist_nav_fr, text="< Anterior", width=100, fg_color="transparent", border_width=1,
            command=lambda: self._watch_sync_history_change_page(-1))
        self._watch_sync_history_prev_btn.pack(side="left")
        self._watch_sync_history_page_lbl = ctk.CTkLabel(hist_nav_fr, text="", text_color=PENDING_COLOR)
        self._watch_sync_history_page_lbl.pack(side="left", padx=12)
        self._watch_sync_history_next_btn = ctk.CTkButton(
            hist_nav_fr, text="Siguiente >", width=100, fg_color="transparent", border_width=1,
            command=lambda: self._watch_sync_history_change_page(1))
        self._watch_sync_history_next_btn.pack(side="left")

        # Diferido -- igual motivo que _build_history_tab: esta pestaña se
        # construye al arrancar aunque esté oculta, y el historial puede
        # tener hasta 500 registros (varios widgets cada uno). _show_view()
        # ya vuelve a llamar a esto cada vez que se entra de verdad en la
        # pestaña, así que esto solo afecta al primer dibujado al arrancar.
        self.after(120, self._refresh_watch_sync_history_view)

    def _refresh_watch_sync_history_view(self):
        """Recarga el historial desde disco y redibuja la página actual --
        se llama al construir la pestaña y cada vez que se entra en ella
        (_show_view), y también justo después de cada sincronización
        (manual o programada) por si acaba de haber entradas nuevas.
        No-op si Plex/Jellyfin no están activos: _build_watch_sync_top_tab
        no llega a construir esta sección en ese caso (ver el "return"
        temprano), así que no hay tabla que redibujar. También no-op si no
        hubo sincronizaciones/borrados de historial nuevos desde la última
        vez (self._watch_sync_history_dirty) -- releer el JSON y
        reconstruir la tabla en cada cambio de pestaña era trabajo
        desperdiciado la mayoría de las veces."""
        if not hasattr(self, "_watch_sync_history_table"):
            return
        _t0 = _time.perf_counter()
        skipped = not self._watch_sync_history_dirty
        try:
            if skipped:
                return
            history = self._load_watch_sync_history()
            self._watch_sync_history_all = list(reversed(history))   # más reciente primero
            self._watch_sync_history_title_lbl.configure(
                text=f"Historial de sincronizaciones  ({len(history)} registros)")
            self._watch_sync_history_page = 0
            self._watch_sync_history_render_page()
            self._watch_sync_history_dirty = False
        finally:
            _log.info("Vista: _refresh_watch_sync_history_view %6.0f ms%s",
                       (_time.perf_counter() - _t0) * 1000, " (sin cambios, omitido)" if skipped else "")

    def _watch_sync_history_change_page(self, delta: int):
        n_pages = max(1, -(-len(self._watch_sync_history_all) // self._WATCH_SYNC_HISTORY_PAGE_SIZE))
        new_page = max(0, min(n_pages - 1, self._watch_sync_history_page + delta))
        if new_page == self._watch_sync_history_page:
            return
        self._watch_sync_history_page = new_page
        self._watch_sync_history_render_page()

    def _watch_sync_history_render_page(self):
        _t0 = _time.perf_counter()
        try:
            self._watch_sync_history_render_page_impl()
        finally:
            _log.info("Vista: _watch_sync_history_render_page %6.0f ms", (_time.perf_counter() - _t0) * 1000)

    def _watch_sync_history_render_page_impl(self):
        """Dibuja solo la página actual (self._watch_sync_history_page) de
        self._watch_sync_history_all -- ver _WATCH_SYNC_HISTORY_PAGE_SIZE."""
        import datetime

        table = self._watch_sync_history_table
        table.clear_rows()
        self._watch_sync_history_empty_msg = None

        total = len(self._watch_sync_history_all)
        n_pages = max(1, -(-total // self._WATCH_SYNC_HISTORY_PAGE_SIZE))
        start = self._watch_sync_history_page * self._WATCH_SYNC_HISTORY_PAGE_SIZE
        page_items = self._watch_sync_history_all[start:start + self._WATCH_SYNC_HISTORY_PAGE_SIZE]

        self._watch_sync_history_page_lbl.configure(
            text=f"Página {self._watch_sync_history_page + 1} de {n_pages}" if total else "")
        self._watch_sync_history_prev_btn.configure(
            state="normal" if self._watch_sync_history_page > 0 else "disabled")
        self._watch_sync_history_next_btn.configure(
            state="normal" if self._watch_sync_history_page < n_pages - 1 else "disabled")
        table.scroll_to_top()

        if not page_items:
            self._watch_sync_history_empty_msg = ctk.CTkLabel(
                table.body, text="Sin sincronizaciones registradas todavía.", text_color=PENDING_COLOR)
            self._watch_sync_history_empty_msg.pack(pady=30)
            return

        cw = {key: table.col_width(key) for key in self._watch_sync_history_col_order}
        font = self._watch_sync_history_font
        sc = {"ok": SUCCESS_COLOR, "error": ERROR_COLOR}
        target_lbl = {"plex": "Plex", "jellyfin": "Jellyfin"}
        for entry in page_items:
            try:
                ts = datetime.datetime.fromtimestamp(entry.get("ts", 0)).strftime("%d/%m/%Y %H:%M")
            except Exception:
                ts = "—"
            season, episode = entry.get("season"), entry.get("episode")
            season_ep = f"{season}x{episode:02d}" if season is not None and episode is not None else "—"
            status = entry.get("status", "ok")
            row = ctk.CTkFrame(table.body, fg_color=("gray95", "gray17"), corner_radius=8)
            row.pack(fill="x", pady=2, padx=2)
            ctk.CTkLabel(row, text=ts, width=cw["fecha"], anchor="w", font=font).pack(side="left")
            ctk.CTkLabel(row, text=entry.get("person", "?"), width=cw["persona"], anchor="w",
                        font=font).pack(side="left")
            ctk.CTkLabel(row, text=entry.get("name", ""), width=cw["titulo"], anchor="w",
                        font=font).pack(side="left", fill="x", expand=True)
            ctk.CTkLabel(row, text=season_ep, width=cw["season_ep"], anchor="w", font=font).pack(side="left")
            ctk.CTkLabel(row, text=target_lbl.get(entry.get("target"), "?"), width=cw["destino"],
                        anchor="w", font=font).pack(side="left")
            ctk.CTkLabel(row, text="✓ Ok" if status == "ok" else "✕ Error", width=cw["estado"],
                        anchor="w", font=font, text_color=sc.get(status, PENDING_COLOR)).pack(side="left")

    def _clear_watch_sync_history(self):
        try:
            self._watch_sync_history_path().write_text("[]", encoding="utf-8")
        except Exception:
            pass
        self._watch_sync_history_dirty = True   # escribe el JSON directamente, no pasa por _save_watch_sync_history_entry
        self._refresh_watch_sync_history_view()

    def _refresh_watch_sync_mapping_users(self):
        """Botón "🔄 Actualizar" junto a la lista -- reutiliza
        _load_watch_sync_mapping_users_async tal cual, solo pone el
        estado en "Actualizando..." antes de lanzarlo para que quede
        claro que está pasando algo (si no, con la lista ya cargada
        antes, no había ninguna señal visible del refresco)."""
        self._watch_sync_mapping_status_label.configure(text="Actualizando usuarios...", text_color=WARNING_COLOR)
        self._load_watch_sync_mapping_users_async()

    def _load_watch_sync_mapping_users_async(self):
        """Carga los usuarios de ambas plataformas al construir la
        pestaña -- siempre visible, ya no hace falta un botón "Editar"
        para desencadenarlo. También reutilizado por
        _refresh_watch_sync_mapping_users para volver a pedirlos a mano
        sin reiniciar la app."""
        host = self.config_data.get("plex_host", "")
        owner_token = self.config_data.get("plex_token", "")
        jf_host = self.config_data.get("jellyfin_host", "")
        jf_key = self.config_data.get("jellyfin_api_key", "")

        def worker():
            from core.media_server_refresh import get_plex_home_users, get_jellyfin_users
            plex_users = get_plex_home_users(host, owner_token) or []
            jf_users = get_jellyfin_users(jf_host, jf_key) or []
            self.after(0, lambda: self._on_watch_sync_mapping_users_loaded(plex_users, jf_users))

        threading.Thread(target=worker, daemon=True).start()

    def _on_watch_sync_mapping_users_loaded(self, plex_users, jf_users):
        self._watch_sync_plex_users = plex_users
        self._watch_sync_jellyfin_users = jf_users
        if not plex_users or not jf_users:
            self._watch_sync_mapping_status_label.configure(
                text="No se pudieron cargar los usuarios -- comprueba la conexión", text_color=ERROR_COLOR)
            return
        self._watch_sync_mapping_status_label.configure(text="")
        self._watch_sync_plex_combo.configure(values=[u["title"] for u in plex_users], state="readonly")
        self._watch_sync_jellyfin_combo.configure(values=[u["name"] for u in jf_users], state="readonly")
        self._watch_sync_plex_combo.set(plex_users[0]["title"])
        self._watch_sync_jellyfin_combo.set(jf_users[0]["name"])
        self._watch_sync_add_mapping_btn.configure(state="normal")

    def _start_add_watch_sync_mapping(self):
        """Paso 1: verificar la contraseña de Jellyfin de la persona
        elegida -- Plex no tiene nada equivalente para usuarios de Plex
        Home (no tienen contraseña propia), así que esto solo confirma
        la mitad Jellyfin. El paso 2 (_confirm_add_watch_sync_mapping)
        exige además una confirmación explícita de que ambas cuentas son
        la misma persona antes de guardar nada."""
        plex_title = self._watch_sync_plex_combo.get()
        jf_name = self._watch_sync_jellyfin_combo.get()
        password = self._watch_sync_jellyfin_password_entry.get()
        plex_user = next((u for u in self._watch_sync_plex_users if u["title"] == plex_title), None)
        jf_user = next((u for u in self._watch_sync_jellyfin_users if u["name"] == jf_name), None)
        if plex_user is None or jf_user is None:
            return
        if not password:
            self._watch_sync_mapping_add_status_label.configure(
                text="Escribe la contraseña de Jellyfin de esa persona para continuar")
            return

        existing = self.config_data.get("watch_sync_user_mappings", [])
        if any(m["plex_user_id"] == plex_user["id"] or m["jellyfin_user_id"] == jf_user["id"]
               for m in existing):
            self._watch_sync_mapping_add_status_label.configure(
                text="Uno de los dos usuarios ya está emparejado con otra persona")
            return

        self._watch_sync_add_mapping_btn.configure(state="disabled")
        self._watch_sync_mapping_add_status_label.configure(text="Verificando contraseña...",
                                                             text_color=WARNING_COLOR)
        jf_host = self.config_data.get("jellyfin_host", "")

        def worker():
            from core.media_server_refresh import verify_jellyfin_password
            ok = verify_jellyfin_password(jf_host, jf_user["name"], password)
            self.after(0, lambda: self._on_watch_sync_password_verified(ok, plex_user, jf_user))

        threading.Thread(target=worker, daemon=True).start()

    def _on_watch_sync_password_verified(self, ok: bool, plex_user: dict, jf_user: dict):
        self._watch_sync_jellyfin_password_entry.delete(0, "end")
        if not ok:
            self._watch_sync_add_mapping_btn.configure(state="normal")
            self._watch_sync_mapping_add_status_label.configure(
                text=f"Contraseña incorrecta para '{jf_user['name']}' en Jellyfin", text_color=ERROR_COLOR)
            return
        self._watch_sync_mapping_add_status_label.configure(text="")
        self._watch_sync_pending_pair = (plex_user, jf_user)
        self._watch_sync_confirm_label.configure(
            text=f"¿Confirmas que \"{plex_user['title']}\" (Plex) y \"{jf_user['name']}\" (Jellyfin) "
                 f"son la misma persona?")
        self._watch_sync_confirm_frame.grid()

    def _cancel_add_watch_sync_mapping(self):
        self._watch_sync_pending_pair = None
        self._watch_sync_confirm_frame.grid_remove()
        self._watch_sync_add_mapping_btn.configure(state="normal")

    def _confirm_add_watch_sync_mapping(self):
        if self._watch_sync_pending_pair is None:
            return
        plex_user, jf_user = self._watch_sync_pending_pair
        mappings = list(self.config_data.get("watch_sync_user_mappings", []))
        mappings.append({
            "plex_user_id": plex_user["id"], "plex_user_name": plex_user["title"],
            "jellyfin_user_id": jf_user["id"], "jellyfin_user_name": jf_user["name"],
        })
        self.config_data.set("watch_sync_user_mappings", mappings)
        self.config_data.save()
        self._watch_sync_pending_pair = None
        self._watch_sync_confirm_frame.grid_remove()
        self._watch_sync_add_mapping_btn.configure(state="normal")
        self._render_watch_sync_mapping_rows()

    def _remove_watch_sync_mapping(self, index):
        mappings = list(self.config_data.get("watch_sync_user_mappings", []))
        del mappings[index]
        self.config_data.set("watch_sync_user_mappings", mappings)
        self.config_data.save()
        self._render_watch_sync_mapping_rows()

    def _render_watch_sync_mapping_rows(self):
        """Reconstruye la lista de emparejados -- TableView(scrollable=False):
        crece con cada fila, sin scroll propio (ver _build_watch_sync_config_tab)."""
        table = self._watch_sync_mapping_table
        table.clear_rows()
        for i, m in enumerate(self.config_data.get("watch_sync_user_mappings", [])):
            row = ctk.CTkFrame(table.body, fg_color=("gray95", "gray17"), corner_radius=8)
            row.pack(fill="x", pady=2, padx=2)
            ctk.CTkLabel(row, text=m["plex_user_name"], width=table.col_width("plex"),
                        anchor="w").pack(side="left")
            ctk.CTkLabel(row, text=m["jellyfin_user_name"], width=table.col_width("jellyfin"),
                        anchor="w").pack(side="left")
            ctk.CTkButton(row, text="✕", width=table.col_width("del"), height=22,
                         fg_color="transparent", border_width=1, text_color=ERROR_COLOR,
                         command=lambda idx=i: self._remove_watch_sync_mapping(idx)).pack(side="left")

    def _watch_sync_collect_actions(self, mappings, status_cb=None):
        """Lee el estado de visionado de ambas plataformas para
        *mappings* y calcula las acciones pendientes
        (core.watch_sync.diff_watched_items). Pura I/O, pensada para
        ejecutarse en CUALQUIER hilo (nunca toca widgets directamente) --
        tanto el botón manual (_run_watch_sync) como la sincronización
        programada (_run_scheduled_watch_sync) la comparten, para no
        duplicar esta lógica en dos sitios que podrían acabar
        divergiendo. status_cb(texto), opcional, se llama con
        actualizaciones de progreso -- quien lo pase decide si lo
        reenvía a la UI vía self.after() (el programador automático no
        pasa ninguno, no hay UI que actualizar). Devuelve (actions,
        failed_users)."""
        from core.watch_sync import WatchedItem, diff_watched_items
        from core.media_server_refresh import (
            get_plex_user_token, get_plex_movie_watched, get_plex_episode_watched,
            get_jellyfin_movie_watched, get_jellyfin_episode_watched,
            get_plex_series, get_jellyfin_series,
        )
        from concurrent.futures import ThreadPoolExecutor

        plex_host = self.config_data.get("plex_host", "")
        plex_owner_token = self.config_data.get("plex_token", "")
        jellyfin_host = self.config_data.get("jellyfin_host", "")
        jellyfin_key = self.config_data.get("jellyfin_api_key", "")

        all_items = []
        failed_users = []
        # Peliculas + listado de series: rapido (un unico listado por
        # plataforma, no una llamada por titulo) -- se hace por usuario,
        # sin necesitar reparto especial.
        per_mapping_shows = []   # [(mapping, plex_token, [(show, jf_show), ...]), ...]

        for m in mappings:
            plex_token = get_plex_user_token(plex_host, plex_owner_token, m["plex_user_id"])
            if plex_token is None:
                failed_users.append(m)
                continue

            plex_movies = get_plex_movie_watched(plex_host, plex_token) or {}
            jf_movies = get_jellyfin_movie_watched(
                jellyfin_host, jellyfin_key, m["jellyfin_user_id"]) or {}
            for tmdb_id in set(plex_movies) | set(jf_movies):
                p = plex_movies.get(tmdb_id, {})
                j = jf_movies.get(tmdb_id, {})
                name = p.get("name") or j.get("name") or f"(película {tmdb_id})"
                all_items.append(WatchedItem(
                    media_type="movie", tmdb_id=tmdb_id, name=name,
                    plex_watched=p.get("watched", False), jellyfin_watched=j.get("watched", False),
                    plex_ref=p.get("ref"), jellyfin_ref=j.get("ref"),
                    plex_user_id=m["plex_user_id"], jellyfin_user_id=m["jellyfin_user_id"]))

            plex_shows = get_plex_series(plex_host, plex_token) or []
            jf_shows = get_jellyfin_series(jellyfin_host, jellyfin_key) or []
            jf_shows_by_tmdb = {s["tmdb_id"]: s for s in jf_shows if s["tmdb_id"] is not None}
            matched = [(show, jf_shows_by_tmdb[show["tmdb_id"]]) for show in plex_shows
                      if show["tmdb_id"] is not None and show["tmdb_id"] in jf_shows_by_tmdb]
            per_mapping_shows.append((m, plex_token, matched))

        # Episodios: la parte lenta (una llamada por serie a cada
        # plataforma) -- repartida entre hasta 8 hilos SIEMPRE, sin
        # importar cuantos usuarios haya (antes solo se paralelizaba por
        # usuario, así que con 1 solo usuario no paralelizaba nada en
        # absoluto). Progreso visible cada pocas series para que no
        # parezca colgada durante los varios minutos que puede tardar.
        work_items = [(m, plex_token, show, jf_show)
                     for m, plex_token, shows in per_mapping_shows
                     for show, jf_show in shows]
        total = len(work_items)
        done = [0]

        def _fetch_episodes(work_item):
            m, plex_token, show, jf_show = work_item
            plex_eps = get_plex_episode_watched(plex_host, plex_token, show["rating_key"]) or {}
            jf_eps = get_jellyfin_episode_watched(
                jellyfin_host, jellyfin_key, m["jellyfin_user_id"], jf_show["id"]) or {}
            items = []
            for se in set(plex_eps) | set(jf_eps):
                p = plex_eps.get(se, {})
                j = jf_eps.get(se, {})
                items.append(WatchedItem(
                    media_type="episode", tmdb_id=show["tmdb_id"], name=show["name"],
                    season=se[0], episode=se[1],
                    plex_watched=p.get("watched", False), jellyfin_watched=j.get("watched", False),
                    plex_ref=p.get("ref"), jellyfin_ref=j.get("ref"),
                    plex_user_id=m["plex_user_id"], jellyfin_user_id=m["jellyfin_user_id"]))
            done[0] += 1
            if status_cb and (done[0] % 10 == 0 or done[0] == total):
                status_cb(f"Leyendo episodios: serie {done[0]}/{total}...")
            return items

        if work_items:
            with ThreadPoolExecutor(max_workers=8) as pool:
                for items in pool.map(_fetch_episodes, work_items):
                    all_items.extend(items)

        return diff_watched_items(all_items), failed_users

    def _run_watch_sync(self):
        mappings = self.config_data.get("watch_sync_user_mappings", [])
        if not mappings:
            self._watch_sync_status_label.configure(
                text="Empareja al menos un usuario primero, en Configuración → Cliente",
                text_color=ERROR_COLOR)
            return

        self._watch_sync_run_button.configure(state="disabled")
        self._watch_sync_status_label.configure(text="Leyendo estado de visionado...",
                                                 text_color=WARNING_COLOR)

        def worker():
            actions, failed_users = self._watch_sync_collect_actions(
                mappings,
                status_cb=lambda t: self.after(0, lambda t=t: self._watch_sync_status_label.configure(
                    text=t, text_color=WARNING_COLOR)))
            self.after(0, lambda: self._show_watch_sync_preview(actions, failed_users))

        threading.Thread(target=worker, daemon=True).start()

    def _show_watch_sync_preview(self, actions, failed_users):
        """Rellena y muestra la sección de vista previa integrada en la
        pestaña (no un diálogo emergente) -- nada se escribe todavía,
        solo al pulsar "Confirmar y sincronizar" (ver
        _confirm_watch_sync_preview)."""
        self._watch_sync_run_button.configure(state="normal")
        self._watch_sync_status_label.configure(text="")
        if not actions and not failed_users:
            self._watch_sync_preview_header_frame.grid_remove()
            self._watch_sync_preview_table.grid_remove()
            self._watch_sync_preview_footer_frame.grid_remove()
            self._watch_sync_status_label.configure(
                text="Nada que sincronizar, ya está todo al día", text_color=SUCCESS_COLOR)
            return

        from core.watch_sync import summarize_actions
        summary = summarize_actions(actions)
        self._watch_sync_preview_summary_label.configure(
            text=(f"Plex: {summary['plex']['movies']} película(s), "
                  f"{summary['plex']['episodes']} episodio(s)\n"
                  f"Jellyfin: {summary['jellyfin']['movies']} película(s), "
                  f"{summary['jellyfin']['episodes']} episodio(s)"))
        if failed_users:
            names = ", ".join(m.get("plex_user_name", "?") for m in failed_users)
            self._watch_sync_preview_failed_label.configure(text=f"No verificado (se omite): {names}")
        else:
            self._watch_sync_preview_failed_label.configure(text="")

        self._watch_sync_preview_actions = sorted(
            actions, key=lambda a: (a.item.name, a.item.season or 0, a.item.episode or 0))
        self._watch_sync_preview_page = 0
        self._render_watch_sync_preview_page()
        self._watch_sync_preview_header_frame.grid()
        self._watch_sync_preview_table.grid()
        self._watch_sync_preview_footer_frame.grid()

    def _watch_sync_change_preview_page(self, delta: int):
        page_size = self._WATCH_SYNC_PREVIEW_PAGE_SIZE
        n_pages = max(1, -(-len(self._watch_sync_preview_actions) // page_size))
        new_page = max(0, min(n_pages - 1, self._watch_sync_preview_page + delta))
        if new_page == self._watch_sync_preview_page:
            return
        self._watch_sync_preview_page = new_page
        self._render_watch_sync_preview_page()

    def _render_watch_sync_preview_page(self):
        page_size = self._WATCH_SYNC_PREVIEW_PAGE_SIZE
        actions = self._watch_sync_preview_actions
        n_pages = max(1, -(-len(actions) // page_size))
        start = self._watch_sync_preview_page * page_size
        page_actions = actions[start:start + page_size]

        table = self._watch_sync_preview_table
        table.clear_rows()
        for a in page_actions:
            row = ctk.CTkFrame(table.body, fg_color=("gray95", "gray17"), corner_radius=8)
            row.pack(fill="x", pady=1, padx=2)
            se_txt = f"{a.item.season}x{a.item.episode:02d}" if a.item.media_type == "episode" else ""
            ctk.CTkLabel(row, text=a.item.name, width=table.col_width("title"), anchor="w").pack(
                side="left", fill="x", expand=True)
            ctk.CTkLabel(row, text=se_txt, width=table.col_width("season_ep"), anchor="w").pack(side="left")
            ctk.CTkLabel(row, text=a.target.capitalize(), width=table.col_width("target"), anchor="w").pack(
                side="left")
        table.scroll_to_top()

        self._watch_sync_preview_page_lbl.configure(
            text=f"Página {self._watch_sync_preview_page + 1} de {n_pages} ({len(actions)} en total)")
        self._watch_sync_preview_prev_btn.configure(
            state="normal" if self._watch_sync_preview_page > 0 else "disabled")
        self._watch_sync_preview_next_btn.configure(
            state="normal" if self._watch_sync_preview_page < n_pages - 1 else "disabled")

    def _cancel_watch_sync_preview(self):
        self._watch_sync_preview_actions = []
        self._watch_sync_preview_header_frame.grid_remove()
        self._watch_sync_preview_table.grid_remove()
        self._watch_sync_preview_footer_frame.grid_remove()
        self._watch_sync_status_label.configure(text="Cancelado, no se ha sincronizado nada",
                                                 text_color=PENDING_COLOR)

    def _watch_sync_apply(self, actions, status_cb=None):
        """Escribe *actions* en Plex/Jellyfin y actualiza
        watch_sync_last_run_ts. Pura I/O, pensada para ejecutarse en
        CUALQUIER hilo -- compartida entre el botón manual
        (_confirm_watch_sync_preview) y la sincronización programada
        (_run_scheduled_watch_sync). status_cb(texto), opcional, mismo
        contrato que en _watch_sync_collect_actions. Devuelve (ok, fail)."""
        from core.media_server_refresh import get_plex_user_token, mark_plex_watched, mark_jellyfin_watched

        plex_host = self.config_data.get("plex_host", "")
        plex_owner_token = self.config_data.get("plex_token", "")
        jellyfin_host = self.config_data.get("jellyfin_host", "")
        jellyfin_key = self.config_data.get("jellyfin_api_key", "")

        # Un token por CADA usuario de Plex que aparezca entre las
        # acciones pendientes (no uno por accion) -- se re-deriva aqui,
        # nunca se reutiliza el que se pidio durante la lectura (ver el
        # plan: los tokens por usuario nunca se cachean).
        plex_user_ids = {a.item.plex_user_id for a in actions
                         if a.target == "plex" and a.item.plex_user_id}
        plex_tokens = {uid: get_plex_user_token(plex_host, plex_owner_token, uid)
                      for uid in plex_user_ids}

        # Para el historial persistente (ver _save_watch_sync_history_entry):
        # los WatchedItem solo llevan el id de usuario, no el nombre --
        # se resuelve aquí una vez contra el emparejamiento actual, en vez
        # de por cada acción.
        mappings = self.config_data.get("watch_sync_user_mappings", [])
        plex_name_by_id = {m["plex_user_id"]: m["plex_user_name"] for m in mappings}
        jf_name_by_id = {m["jellyfin_user_id"]: m["jellyfin_user_name"] for m in mappings}

        ok, fail = 0, 0
        total = len(actions)
        for i, action in enumerate(actions, 1):
            item = action.item
            if action.target == "jellyfin":
                success = bool(item.jellyfin_user_id and item.jellyfin_ref) and mark_jellyfin_watched(
                    jellyfin_host, jellyfin_key, item.jellyfin_user_id, item.jellyfin_ref)
            else:
                token = plex_tokens.get(item.plex_user_id)
                success = bool(token and item.plex_ref) and mark_plex_watched(
                    plex_host, token, item.plex_ref)
            if success:
                ok += 1
            else:
                fail += 1
                _log.warning("Sincronizar visionado: fallo al marcar '%s' en %s (usuario plex=%s, jellyfin=%s)",
                            item.name, action.target, item.plex_user_id, item.jellyfin_user_id)

            person = (plex_name_by_id.get(item.plex_user_id)
                     or jf_name_by_id.get(item.jellyfin_user_id) or "?")
            self._save_watch_sync_history_entry(action.target, item, "ok" if success else "error", person)

            if status_cb and (i % 10 == 0 or i == total):
                status_cb(f"Sincronizando: {i}/{total} ({ok} aplicados, {fail} fallidos)...")

        self.config_data.set("watch_sync_last_run_ts", _time.time())
        self.config_data.save()
        return ok, fail

    # ── Historial de sincronizaciones -- lista persistente (entre
    # reinicios) de cada ítem marcado como visto en Plex/Jellyfin por esta
    # función, ok o fallido -- nunca desaparece en silencio, mismo patrón
    # que el historial de subidas/borrados (ver _history_path/
    # _deletion_history_path más abajo). Archivo aparte, no una clave de
    # config: puede crecer hasta 500 registros, y config.json no es sitio
    # para listas que crecen sin límite fijo.

    def _watch_sync_history_path(self) -> Path:
        return _appdata_dir() / "watch_sync_history.json"

    def _load_watch_sync_history(self) -> list:
        try:
            p = self._watch_sync_history_path()
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
        return []

    def _save_watch_sync_history_entry(self, target: str, item, status: str, person: str):
        with self._history_lock:
            history = self._load_watch_sync_history()
            history.append({
                "ts":         _time.time(),
                "target":     target,       # "plex" | "jellyfin" -- dónde se escribió
                "media_type": item.media_type,
                "name":       item.name,
                "season":     item.season,
                "episode":    item.episode,
                "person":     person,
                "status":     status,       # "ok" | "error"
            })
            if len(history) > 500:
                history = history[-500:]
            try:
                self._watch_sync_history_path().write_text(
                    json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
            self._watch_sync_history_dirty = True

    def _confirm_watch_sync_preview(self):
        """Aplica EXACTAMENTE la lista de SyncAction ya mostrada en la
        vista previa -- nunca se recalcula aquí, para que lo confirmado
        sea siempre lo que se escribe."""
        actions = self._watch_sync_preview_actions
        self._watch_sync_preview_header_frame.grid_remove()
        self._watch_sync_preview_table.grid_remove()
        self._watch_sync_preview_footer_frame.grid_remove()
        self._watch_sync_status_label.configure(text="Sincronizando...", text_color=WARNING_COLOR)

        def worker():
            ok, fail = self._watch_sync_apply(
                actions,
                status_cb=lambda t: self.after(0, lambda t=t: self._watch_sync_status_label.configure(
                    text=t, text_color=WARNING_COLOR)))
            self.after(0, lambda: self._watch_sync_status_label.configure(
                text=f"Sincronización completada: {ok} aplicados, {fail} fallidos",
                text_color=SUCCESS_COLOR if fail == 0 else WARNING_COLOR))
            self.after(0, lambda: self._watch_sync_last_run_label.configure(
                text="Última sincronización: hace un momento"))
            self.after(0, self._refresh_watch_sync_history_view)

        threading.Thread(target=worker, daemon=True).start()

    def _run_scheduled_watch_sync(self):
        """Sincronización automática programada (ver
        _start_watch_sync_scheduler/_check_watch_sync_schedule) -- a
        diferencia del botón manual, aplica DIRECTAMENTE sin mostrar
        vista previa ni pedir confirmación: decisión explícita del
        usuario al activar la programación en Configuración → Cliente,
        sabiendo que renuncia a la revisión humana previa que tiene el
        resto de esta función. Se ejecuta ya en el hilo del programador
        (ver _watch_sync_scheduler_loop), no lanza otro hilo aparte."""
        mappings = self.config_data.get("watch_sync_user_mappings", [])
        if not mappings:
            _log.info("Sincronización programada: omitida, no hay usuarios emparejados")
            return
        _log.info("Sincronización programada: iniciando (%d usuario(s) emparejado(s))", len(mappings))
        try:
            actions, failed_users = self._watch_sync_collect_actions(mappings)
            ok, fail = self._watch_sync_apply(actions) if actions else (0, 0)
        except Exception:
            _log.exception("Sincronización programada: fallo inesperado")
            return
        _log.info("Sincronización programada: completada -- %d aplicado(s), %d fallido(s), "
                  "%d usuario(s) no verificado(s)", ok, fail, len(failed_users))
        if not actions:
            # Nada que aplicar -- watch_sync_apply no se llamó, así que
            # watch_sync_last_run_ts tampoco se actualizó; se hace aquí
            # para que "ya se sincronizó hoy" siga siendo cierto y el
            # programador no reintente en el siguiente minuto.
            self.config_data.set("watch_sync_last_run_ts", _time.time())
            self.config_data.save()
        self.after(0, lambda: self._send_notification(
            "aRenombrar — Sincronización de visionado",
            f"{ok} cambio(s) aplicados" + (f", {fail} fallido(s)" if fail else "")))
        self.after(0, lambda: self._watch_sync_last_run_label.configure(
            text="Última sincronización: hace un momento"))
        self.after(0, self._refresh_watch_sync_history_view)

    def _start_watch_sync_scheduler(self):
        """Hilo en segundo plano que comprueba cada minuto si toca la
        sincronización programada (Configuración → Cliente →
        Sincronizar visionado) -- mismo patrón que AutoWatcher (hilo
        daemon + threading.Event().wait(), nunca time.sleep, para poder
        pararlo al instante si hiciera falta). A diferencia de
        AutoWatcher (que solo arranca si se inicia minimizado o al pulsar
        "Auto"), este se arranca SIEMPRE al abrir la app -- una
        sincronización programada tiene que poder saltar sin importar
        cómo se abrió la ventana esta vez."""
        stop_event = threading.Event()
        self._watch_sync_scheduler_stop = stop_event

        def loop():
            while not stop_event.is_set():
                try:
                    self._check_watch_sync_schedule()
                except Exception:
                    _log.exception("Programador de sincronización: error inesperado")
                stop_event.wait(60)

        threading.Thread(target=loop, daemon=True, name="WatchSyncScheduler").start()

    def _check_watch_sync_schedule(self):
        """Se llama una vez por minuto desde _start_watch_sync_scheduler.
        Todo se lee en caliente de config_data (nunca cacheado), así que
        activar/editar la hora desde Configuración tiene efecto de
        inmediato, sin reiniciar la app."""
        if not self.config_data.get("watch_sync_schedule_enabled", False):
            return
        schedule_time = self.config_data.get("watch_sync_schedule_time", "")
        if not schedule_time or _time.strftime("%H:%M") != schedule_time:
            return
        last_run = self.config_data.get("watch_sync_last_run_ts", 0)
        if last_run and _time.strftime("%Y-%m-%d", _time.localtime(last_run)) == _time.strftime("%Y-%m-%d"):
            return   # ya se sincronizó hoy (manual o programada) -- no repetir dentro del mismo minuto/día
        self._run_scheduled_watch_sync()

    def _build_tmdb_tab(self, tab):
        scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        scroll.pack(fill="both", expand=True)

        # ── TMDB API ──
        tmdb = ctk.CTkFrame(scroll)
        tmdb.pack(fill="both", expand=True, padx=0, pady=(0, 8))
        tmdb.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(tmdb, text="TMDB API",
                     font=self._cfg_font_title).grid(
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
                     text_color=PENDING_COLOR, font=self._cfg_font_desc).grid(
            row=5, column=0, columnspan=2, pady=8)

        # ── Fallback de IA (opcional) ──
        # Último recurso solo cuando TMDB no encuentra nada con el título
        # limpiado localmente — desactivado por defecto, no se envía nada a
        # terceros hasta que el usuario lo active explícitamente aquí.
        ctk.CTkLabel(tmdb, text="IA como último recurso",
                     font=self._cfg_font_subtitle).grid(
            row=6, column=0, columnspan=2, pady=(16, 4))
        self._ai_fallback_switch = ctk.CTkSwitch(
            tmdb, text="Usar IA si TMDB no encuentra resultados")
        if self.config_data.get("ai_fallback_enabled"):
            self._ai_fallback_switch.select()
        self._ai_fallback_switch.grid(row=7, column=0, columnspan=2, pady=4)
        ctk.CTkLabel(tmdb, text="API Key (Groq):").grid(row=8, column=0, sticky="e", padx=10, pady=6)
        self._ai_key_entry = ctk.CTkEntry(
            tmdb, width=240, show="*",
            placeholder_text="Gratis en console.groq.com/keys")
        self._ai_key_entry.insert(0, self.config_data.get("ai_api_key", ""))
        self._ai_key_entry.grid(row=8, column=1, padx=10, pady=6, sticky="ew")
        bf3 = ctk.CTkFrame(tmdb, fg_color="transparent")
        bf3.grid(row=9, column=0, columnspan=2, pady=8)
        ctk.CTkButton(bf3, text="Validar API Key (Groq)", command=self._validate_ai_key).pack(side="left", padx=4)
        ctk.CTkButton(bf3, text="Términos aprendidos", fg_color="transparent", border_width=1,
                      command=self._open_learned_terms_dialog).pack(side="left", padx=4)
        self._ai_key_status = ctk.CTkLabel(tmdb, text="", text_color=PENDING_COLOR)
        self._ai_key_status.grid(row=10, column=0, columnspan=2, pady=4)
        ctk.CTkLabel(tmdb, text="Solo se consulta cuando TMDB falla — cada consulta\nqueda registrada en ai_fallback.log",
                     text_color=PENDING_COLOR, font=self._cfg_font_desc, justify="left").grid(
            row=11, column=0, columnspan=2, pady=(4, 8))

    def _build_templates_tab(self, tab):
        scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        scroll.pack(fill="both", expand=True)

        # ── Plantillas de nombre ──
        tpl = ctk.CTkFrame(scroll)
        tpl.pack(fill="both", expand=True, padx=0, pady=(0, 8))
        tpl.grid_columnconfigure(1, weight=1)
        hdr_tpl = ctk.CTkFrame(tpl, fg_color="transparent")
        hdr_tpl.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(12, 4))
        ctk.CTkLabel(hdr_tpl, text="Plantillas de nombre",
                     font=self._cfg_font_title).pack(side="left", padx=12)
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

        # Renombrar en destino/FTP -- junto a las plantillas porque decide
        # si de verdad se aplican al subir (si está desactivado, se sube
        # con el nombre original aunque la carpeta se organice igual por
        # serie/temporada). Configuración de SERVIDOR: afecta al nombre
        # que queda en el servidor compartido, no solo a este equipo (el
        # renombrado en origen/local es de Cliente -> General).
        self._rename_remote_switch = ctk.CTkSwitch(tpl, text="Renombrar archivos en destino (FTP)")
        self._rename_remote_switch.grid(row=4, column=0, columnspan=2, pady=(4, 10))
        if self.config_data.get("rename_remote", True):
            self._rename_remote_switch.select()

        # ── Enlaces personalizables (detector de episodios que faltan) ──
        # Una lista independiente por nivel -- serie, temporada y episodio
        # cada uno con sentido para una plantilla distinta (p.ej. la
        # ficha de TMDB cambia de URL según el nivel), así que cada botón
        # se configura por separado en vez de compartir una única lista.
        links_sec = ctk.CTkFrame(scroll)
        links_sec.pack(fill="both", expand=True, padx=0, pady=(0, 8))
        ctk.CTkLabel(links_sec, text="Enlaces personalizables (episodios que faltan)",
                     font=self._cfg_font_title).pack(padx=12, pady=(12, 4), anchor="w")
        ctk.CTkLabel(links_sec, text=("Botones para abrir una URL con variables sustituidas -- solo "
                                      "eso, nunca se conectan a nada por su cuenta. Variables "
                                      "disponibles: {serie}, {tmdb_id}, {temporada}, {episodio}, "
                                      "{titulo}, {nombre_archivo}, {ruta} (carpeta de la serie en el "
                                      "FTP, si ya existe) -- las que no apliquen al nivel del botón "
                                      "-- p.ej. {episodio} a nivel serie -- quedan vacías. Marca "
                                      "\"Segundo plano\" para que, en vez de abrir una pestaña del "
                                      "navegador, se haga una petición silenciosa a la URL (útil para "
                                      "webhooks o disparar una búsqueda en otra herramienta)."),
                     font=self._cfg_font_desc, text_color=PENDING_COLOR,
                     wraplength=520, justify="left").pack(padx=12, pady=(0, 8), anchor="w")

        self._custom_links_rows_frame = {}
        self._custom_links_widgets = {}
        for level, config_key, title in (
            ("show", "custom_links_show", "Nivel serie"),
            ("season", "custom_links_season", "Nivel temporada"),
            ("episode", "custom_links_episode", "Nivel episodio"),
        ):
            ctk.CTkLabel(links_sec, text=title, font=self._cfg_font_small_bold).pack(
                padx=12, pady=(4, 2), anchor="w")
            rows_fr = ctk.CTkFrame(links_sec, fg_color="transparent")
            rows_fr.pack(fill="x", padx=12)
            self._custom_links_rows_frame[level] = rows_fr
            self._custom_links_widgets[level] = []
            for link in self.config_data.get(config_key, []):
                self._add_custom_link_row(level, link.get("name", ""), link.get("url_template", ""),
                                          link.get("background", False))
            ctk.CTkButton(links_sec, text="+ Añadir enlace", width=140,
                          command=lambda lvl=level: self._add_custom_link_row(lvl, "", "")).pack(
                pady=(4, 12), padx=12, anchor="w")

    def _add_custom_link_row(self, level: str, name: str, url_template: str, background: bool = False):
        row = ctk.CTkFrame(self._custom_links_rows_frame[level], fg_color="transparent")
        row.pack(fill="x", pady=2)
        name_entry = ctk.CTkEntry(row, placeholder_text="Nombre", width=140)
        name_entry.insert(0, name)
        name_entry.pack(side="left", padx=(0, 4))
        url_entry = ctk.CTkEntry(row, placeholder_text="https://.../{serie}/{tmdb_id}")
        url_entry.insert(0, url_template)
        url_entry.pack(side="left", padx=(0, 4), fill="x", expand=True)
        # "En segundo plano": en vez de abrir una pestaña del navegador,
        # hace una petición GET silenciosa a la URL -- para botones que
        # disparan un webhook o una búsqueda en otra herramienta sin
        # necesitar ver nada ni que se abra una ventana nueva.
        bg_var = ctk.BooleanVar(value=background)
        ctk.CTkCheckBox(row, text="Segundo plano", variable=bg_var, width=20).pack(
            side="left", padx=(4, 4))
        ctk.CTkButton(row, text="✕", width=28, fg_color="transparent", border_width=1,
                      border_color=ERROR_COLOR, text_color=ERROR_COLOR,
                      hover_color=("gray85", "#3d1010"),
                      command=lambda: self._remove_custom_link_row(level, row)).pack(side="left")
        self._custom_links_widgets[level].append(
            {"row": row, "name": name_entry, "url": url_entry, "background": bg_var})

    def _remove_custom_link_row(self, level: str, row):
        self._custom_links_widgets[level] = [w for w in self._custom_links_widgets[level] if w["row"] is not row]
        row.destroy()

    def _show_template_guide(self):
        # Contenido estático (ver _build_guide_content) -- se construye una
        # sola vez y se reutiliza (self._template_guide_win) en vez de crear
        # un CTkToplevel/CTkScrollableFrame nuevo cada apertura, por la
        # misma fuga de bind_all(<MouseWheel>/...) de customtkinter
        # documentada en _DubHiddenDialog.
        if self._template_guide_win is not None and self._template_guide_win.winfo_exists():
            self._template_guide_win.grab_set()
            self._template_guide_win.lift()
            self._template_guide_win.focus_force()
            return

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
                     font=self._cfg_font_title).pack(side="left")

        sf = ctk.CTkScrollableFrame(win)
        sf.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self._build_guide_content(sf)

        def _hide():
            win.grab_release()
            win.withdraw()
        win.protocol("WM_DELETE_WINDOW", _hide)

        self._template_guide_win = win

    # ── Servidores de medios (Plex/Jellyfin) ──

    def _build_media_servers_section(self, tab):
        scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        scroll.pack(fill="both", expand=True)
        fr = ctk.CTkFrame(scroll)
        fr.pack(fill="both", expand=True, padx=0, pady=(0, 8))
        fr.grid_columnconfigure(0, weight=1)
        fr.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(fr, text="Servidores de medios",
                     font=self._cfg_font_title).grid(
            row=0, column=0, columnspan=2, pady=(12, 4))
        ctk.CTkLabel(fr, text="Refresca la biblioteca justo tras subir, en vez de esperar\n"
                              "al siguiente escaneo periódico. Cada uno es independiente.",
                     font=self._cfg_font_desc, text_color=PENDING_COLOR, justify="center").grid(
            row=1, column=0, columnspan=2, pady=(0, 8))

        # -- Plex --
        plex = ctk.CTkFrame(fr, fg_color="transparent")
        plex.grid(row=2, column=0, sticky="nsew", padx=(12, 6), pady=(0, 12))
        plex.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(plex, text="Plex", font=self._cfg_font_subtitle).grid(
            row=0, column=0, columnspan=2, pady=(0, 6))
        self._plex_switch = ctk.CTkSwitch(plex, text="Activar")
        if self.config_data.get("plex_enabled"):
            self._plex_switch.select()
        self._plex_switch.grid(row=1, column=0, columnspan=2, pady=4)
        ctk.CTkLabel(plex, text="URL:").grid(row=2, column=0, sticky="e", padx=6, pady=4)
        self._plex_host_entry = ctk.CTkEntry(plex, placeholder_text="http://192.168.1.10:32400")
        self._plex_host_entry.insert(0, self.config_data.get("plex_host", ""))
        self._plex_host_entry.grid(row=2, column=1, padx=6, pady=4, sticky="ew")
        ctk.CTkLabel(plex, text="Token:").grid(row=3, column=0, sticky="e", padx=6, pady=4)
        self._plex_token_entry = ctk.CTkEntry(plex, show="*")
        self._plex_token_entry.insert(0, self.config_data.get("plex_token", ""))
        self._plex_token_entry.grid(row=3, column=1, padx=6, pady=4, sticky="ew")
        ctk.CTkButton(plex, text="Validar", width=80, command=self._validate_plex).grid(
            row=4, column=0, columnspan=2, pady=8)
        self._plex_status = ctk.CTkLabel(plex, text="", text_color=PENDING_COLOR)
        self._plex_status.grid(row=5, column=0, columnspan=2)

        # -- Jellyfin --
        jf = ctk.CTkFrame(fr, fg_color="transparent")
        jf.grid(row=2, column=1, sticky="nsew", padx=(6, 12), pady=(0, 12))
        jf.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(jf, text="Jellyfin", font=self._cfg_font_subtitle).grid(
            row=0, column=0, columnspan=2, pady=(0, 6))
        self._jellyfin_switch = ctk.CTkSwitch(jf, text="Activar")
        if self.config_data.get("jellyfin_enabled"):
            self._jellyfin_switch.select()
        self._jellyfin_switch.grid(row=1, column=0, columnspan=2, pady=4)
        ctk.CTkLabel(jf, text="URL:").grid(row=2, column=0, sticky="e", padx=6, pady=4)
        self._jellyfin_host_entry = ctk.CTkEntry(jf, placeholder_text="http://192.168.1.10:8096")
        self._jellyfin_host_entry.insert(0, self.config_data.get("jellyfin_host", ""))
        self._jellyfin_host_entry.grid(row=2, column=1, padx=6, pady=4, sticky="ew")
        ctk.CTkLabel(jf, text="API Key:").grid(row=3, column=0, sticky="e", padx=6, pady=4)
        self._jellyfin_key_entry = ctk.CTkEntry(jf, show="*")
        self._jellyfin_key_entry.insert(0, self.config_data.get("jellyfin_api_key", ""))
        self._jellyfin_key_entry.grid(row=3, column=1, padx=6, pady=4, sticky="ew")
        ctk.CTkLabel(jf, text="Usuario (opcional):").grid(row=4, column=0, sticky="e", padx=6, pady=4)
        self._jellyfin_username_entry = ctk.CTkEntry(
            jf, placeholder_text="vacío = visionado de TODOS los usuarios")
        self._jellyfin_username_entry.insert(0, self.config_data.get("jellyfin_username", ""))
        self._jellyfin_username_entry.grid(row=4, column=1, padx=6, pady=4, sticky="ew")
        ctk.CTkLabel(jf, text=("Vacío (recomendado): se combina el visionado de TODOS los "
                               "usuarios del servidor -- algo se considera visto si CUALQUIERA "
                               "lo ha visto. Rellena esto solo si quieres restringirlo a una "
                               "sola persona en concreto."),
                     font=ctk.CTkFont(size=10), text_color=PENDING_COLOR,
                     wraplength=260, justify="left").grid(row=5, column=0, columnspan=2, padx=6)
        ctk.CTkButton(jf, text="Validar", width=80, command=self._validate_jellyfin).grid(
            row=6, column=0, columnspan=2, pady=8)
        self._jellyfin_status = ctk.CTkLabel(jf, text="", text_color=PENDING_COLOR)
        self._jellyfin_status.grid(row=7, column=0, columnspan=2)

        ctk.CTkLabel(fr, text="El detector de episodios que faltan tiene su propia pantalla:\n"
                              'botón "🔍 Episodios" arriba, junto a Configuración.',
                     font=self._cfg_font_desc, text_color=PENDING_COLOR, justify="center").grid(
            row=3, column=0, columnspan=2, pady=(4, 12))

    def _validate_plex(self):
        host  = self._plex_host_entry.get().strip()
        token = self._plex_token_entry.get().strip()
        if not host or not token:
            self._plex_status.configure(text="Rellena URL y token", text_color=ERROR_COLOR)
            return
        self._plex_status.configure(text="Validando...", text_color=WARNING_COLOR)
        def worker():
            from core.media_server_refresh import validate_plex
            ok  = validate_plex(host, token)
            msg = "Conexión válida" if ok else "No se pudo conectar"
            self.after(0, lambda: self._plex_status.configure(
                text=msg, text_color=SUCCESS_COLOR if ok else ERROR_COLOR))
        threading.Thread(target=worker, daemon=True).start()

    def _validate_jellyfin(self):
        host = self._jellyfin_host_entry.get().strip()
        key  = self._jellyfin_key_entry.get().strip()
        if not host or not key:
            self._jellyfin_status.configure(text="Rellena URL y API Key", text_color=ERROR_COLOR)
            return
        self._jellyfin_status.configure(text="Validando...", text_color=WARNING_COLOR)
        def worker():
            from core.media_server_refresh import validate_jellyfin
            ok  = validate_jellyfin(host, key)
            msg = "Conexión válida" if ok else "No se pudo conectar"
            self.after(0, lambda: self._jellyfin_status.configure(
                text=msg, text_color=SUCCESS_COLOR if ok else ERROR_COLOR))
        threading.Thread(target=worker, daemon=True).start()

    # ── Detector de episodios que faltan (Plex/Jellyfin + TMDB) ──
    # Pantalla completa (_build_missing_episodes_tab), tan importante como
    # la de Archivos -- no un diálogo aparte.

    def _build_missing_episodes_tab(self, parent):
        parent.grid_columnconfigure(0, weight=1)

        # -- Barra de título, mismo estilo que la de la tabla de Archivos --
        header = ctk.CTkFrame(parent, fg_color=("gray90", "gray20"), corner_radius=8)
        header.grid(row=0, column=0, sticky="ew", pady=(0, CONTAINER_GAP))
        header.grid_columnconfigure(0, weight=1, uniform="missing_ep_sides")
        header.grid_columnconfigure(1, weight=0)
        header.grid_columnconfigure(2, weight=1, uniform="missing_ep_sides")

        left_fr = ctk.CTkFrame(header, fg_color="transparent")
        left_fr.grid(row=0, column=0, sticky="w")
        self._missing_ep_scan_btn = ctk.CTkButton(
            left_fr, text="🔍 Comprobar", width=120,
            command=lambda: self._start_missing_episodes_scan(force_full=False))
        self._missing_ep_scan_btn.pack(side="left", padx=(12, 4), pady=8)
        self._missing_ep_full_btn = ctk.CTkButton(
            left_fr, text="Reescaneo completo", width=140, fg_color="transparent", border_width=1,
            command=lambda: self._start_missing_episodes_scan(force_full=True))
        self._missing_ep_full_btn.pack(side="left", padx=(0, 4), pady=8)
        self._missing_ep_cancel_btn = ctk.CTkButton(
            left_fr, text="Cancelar", width=90, fg_color=ERROR_COLOR, hover_color="#96281b",
            command=self._cancel_missing_episodes_scan)
        # (empaquetado solo mientras escanea, ver _set_missing_ep_scanning_ui)
        # Nota: "Preguntar a la IA" es un botón por serie (panel lateral,
        # ver _build_missing_ep_side_panel), no uno general aquí -- el
        # veredicto automático al terminar el escaneo sigue siendo por
        # lotes (una sola llamada), esto solo afecta al botón manual.

        ctk.CTkLabel(header, text="Episodios que faltan",
                     font=ctk.CTkFont(size=14, weight="bold")).grid(row=0, column=1, padx=16, pady=8)

        right_fr = ctk.CTkFrame(header, fg_color="transparent")
        right_fr.grid(row=0, column=2, sticky="e")
        # Los 3 interruptores recuerdan su estado entre reinicios (ver
        # DEFAULTS en config.py) -- cada uno persiste al cambiar de estado
        # (ver _on_toggle_missing_ep_switch), no solo al cerrar la app.
        self._missing_ep_show_ignored_var = ctk.BooleanVar(
            value=self.config_data.get("missing_ep_show_ignored", False))
        self._missing_ep_show_ignored_switch = ctk.CTkSwitch(
            right_fr, text="Mostrar ignoradas", variable=self._missing_ep_show_ignored_var,
            command=lambda: self._on_toggle_missing_ep_switch(
                "missing_ep_show_ignored", self._missing_ep_show_ignored_var,
                self._render_missing_episodes_table))
        self._missing_ep_show_ignored_switch.pack(side="right", padx=(4, 12), pady=8)
        self._missing_ep_hide_ai_var = ctk.BooleanVar(
            value=self.config_data.get("missing_ep_hide_ai_dismissed", False))
        self._missing_ep_hide_ai_switch = ctk.CTkSwitch(
            right_fr, text="Ocultar descartados por IA", variable=self._missing_ep_hide_ai_var,
            command=lambda: self._on_toggle_missing_ep_switch(
                "missing_ep_hide_ai_dismissed", self._missing_ep_hide_ai_var,
                self._render_missing_episodes_table))
        self._missing_ep_hide_ai_switch.pack(side="right", padx=4, pady=8)
        self._missing_ep_hide_no_dub_var = ctk.BooleanVar(
            value=self.config_data.get("missing_ep_hide_no_dub", False))
        self._missing_ep_hide_no_dub_switch = ctk.CTkSwitch(
            right_fr, text="Ocultar sin doblaje ES", variable=self._missing_ep_hide_no_dub_var,
            command=lambda: self._on_toggle_missing_ep_switch(
                "missing_ep_hide_no_dub", self._missing_ep_hide_no_dub_var,
                self._on_toggle_hide_no_dub))
        self._missing_ep_hide_no_dub_switch.pack(side="right", padx=4, pady=8)
        # El recuento de series ocultas se funde en el propio texto del
        # interruptor (ver _update_missing_ep_dub_hidden_counter) en vez de
        # una etiqueta aparte al lado -- esa ocupaba demasiado sitio.
        # Clic derecho (no toca el toggle normal, que sigue siendo clic
        # izquierdo) abre el detalle de qué series se ocultaron y por qué.
        self._missing_ep_hide_no_dub_switch.bind(
            "<Button-3>", lambda e: self._show_missing_ep_dub_hidden_dialog())
        self._missing_ep_search_entry = ctk.CTkEntry(right_fr, width=180, placeholder_text="Filtrar por nombre...")
        self._missing_ep_search_entry.pack(side="right", padx=4, pady=8)
        self._missing_ep_search_entry.bind("<KeyRelease>", lambda e: self._render_missing_episodes_table())

        # -- Barra de estado / progreso (una u otra, nunca las dos) --
        self._missing_ep_status_lbl = ctk.CTkLabel(
            parent, text="Sin comprobar todavía", text_color=PENDING_COLOR,
            font=ctk.CTkFont(size=12), anchor="w")
        self._missing_ep_status_lbl.grid(row=1, column=0, sticky="w", pady=(0, 6))
        self._missing_ep_progress = ctk.CTkProgressBar(parent)
        self._missing_ep_progress.set(0)
        # (empaquetado solo mientras escanea)

        # -- Cuerpo: tabla a la izquierda, ficha de TMDB a la derecha --
        body = ctk.CTkFrame(parent, fg_color="transparent")
        body.grid(row=2, column=0, sticky="nsew")
        parent.grid_rowconfigure(2, weight=1)
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=0)
        body.grid_rowconfigure(0, weight=1)

        self._missing_ep_empty_msg = None   # label "sin resultados"/"pulsa Comprobar", ver _render_missing_episodes_table
        # Compartidas entre todas las filas -- crear un CTkFont nuevo por
        # cada serie (llamada a Tcl) se notaba con muchas series en la
        # tabla; ver el mismo arreglo en _build_files_tab.
        self._missing_ep_name_font = ctk.CTkFont(size=13, weight="bold")
        self._missing_ep_summary_font = ctk.CTkFont(size=11)
        self._missing_ep_detail_font = ctk.CTkFont(size=11)         # lista de episodios al desplegar
        self._missing_ep_season_font = ctk.CTkFont(size=12, weight="bold")   # cabecera de cada temporada

        # TableView: mismo componente que Archivos/Liberar espacio/
        # Historial (ver gui/table_view.py). "Serie" es la columna
        # expand=True; el sash entre ella y "Episodios que faltan" solo
        # necesita resizable=True en "name" (TableView ya sabe que el
        # lado expand no tiene ancho propio que tocar).
        sw0 = self._saved_col_widths("episodios")
        self._missing_ep_table = TableView(body, columns=[
            ColumnSpec("toggle", "", width=28),
            ColumnSpec("logo", "", width=28),
            ColumnSpec("fav", "", width=28),
            ColumnSpec("name", "Serie", expand=True, resizable=True),
            ColumnSpec("summary", "Episodios que faltan", width=sw0.get("summary", 180), min_width=100),
            ColumnSpec("trending", "Tendencia", width=70),
            ColumnSpec("ignore", "", width=90),
            ColumnSpec("rescan", "", width=36),
            ColumnSpec("delete", "", width=36),
        ])
        self._missing_ep_table.grid(row=0, column=0, sticky="nsew", padx=(0, CONTAINER_GAP))
        self._missing_ep_table.on_column_resize = self._on_missing_ep_column_resize
        self._missing_ep_table.on_widths_changed = lambda w: self._save_table_col_widths("episodios", w)
        self._missing_ep_table.enable_dynamic_page_size(lambda _size: self._missing_ep_render_page())

        self._missing_ep_side_panel = self._build_missing_ep_side_panel(body)
        self._missing_ep_side_panel.grid(row=0, column=1, sticky="nsew")

        # Paginado (ver TableView.page_size, calculado dinámicamente) -- con varios cientos de
        # series cacheadas, dibujar la tabla entera de golpe (o incluso en
        # lotes vía after(), que solo reparte el trabajo en el tiempo pero
        # no reduce el total) llega a agotar el límite de objetos GUI de
        # Windows (10000 por proceso) y rompe el pintado de TODA la
        # ventana, no solo de esta tabla. Mismo paginado en Liberar
        # espacio/Historial. Debajo de la tabla (no encima) y centrada --
        # sin sticky="ew", para que el frame ocupe solo su tamaño natural
        # y quede centrado en la columna (que sí tiene weight=1).
        nav_fr = ctk.CTkFrame(parent, fg_color="transparent")
        nav_fr.grid(row=3, column=0, pady=(6, 0))
        self._missing_ep_prev_btn = ctk.CTkButton(
            nav_fr, text="< Anterior", width=100, fg_color="transparent", border_width=1,
            command=lambda: self._missing_ep_change_page(-1))
        self._missing_ep_prev_btn.pack(side="left")
        self._missing_ep_page_lbl = ctk.CTkLabel(nav_fr, text="", text_color=PENDING_COLOR)
        self._missing_ep_page_lbl.pack(side="left", padx=12)
        self._missing_ep_next_btn = ctk.CTkButton(
            nav_fr, text="Siguiente >", width=100, fg_color="transparent", border_width=1,
            command=lambda: self._missing_ep_change_page(1))
        self._missing_ep_next_btn.pack(side="left")

        self._missing_ep_results = self._load_missing_episodes_from_cache()
        from core.spanish_dub_cache import load_cache as _load_spanish_dub_cache
        self._spanish_dub_cache = _load_spanish_dub_cache()
        self._missing_ep_cancel_event = None
        self._missing_ep_scanning = False
        self._missing_ep_row_widgets = {}
        self._missing_ep_page = 0
        self._missing_ep_selected_tmdb_id = None   # serie pulsada, ver _show_missing_ep_poster
        self._missing_ep_expanded = set()   # tmdb_ids con la fila desplegada
        self._missing_ep_expanded_seasons = set()   # (tmdb_id, temporada) con la temporada desplegada
        self._missing_ep_poster_token = None
        self._update_missing_ep_status_text()
        # Diferido, no al construir la app: dibujar esta tabla implica
        # crear un widget por cada serie con hueco cacheada, y esta
        # pestaña se construye al arrancar aunque esté oculta (se
        # empieza en Archivos) -- antes esto se hacía ya mismo, sumando
        # trabajo real al arranque por algo que el usuario ni ve todavía.
        self.after(50, self._render_missing_episodes_table)

    def _on_missing_ep_column_resize(self):
        """Tras arrastrar el separador de "Episodios que faltan" (ver
        TableView.on_column_resize), reconfigura solo el ancho de las
        filas ya pintadas -- no hace falta reconstruir la página entera
        para esto, a diferencia de Historial/Liberar espacio."""
        w = self._missing_ep_table.col_width("summary")
        for widgets in self._missing_ep_row_widgets.values():
            lbl = widgets.get("summary_lbl")
            if lbl is not None:
                lbl.configure(width=w)

    def _build_missing_ep_side_panel(self, parent):
        """Ficha de TMDB de la serie pulsada -- póster + sinopsis, mismo
        estilo que el panel "Buscar en TMDB" de Archivos, pero de solo
        lectura (aquí no se identifica nada, solo se consulta)."""
        panel = ctk.CTkFrame(parent, width=240)
        panel.grid_propagate(False)
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(0, weight=1)

        scroll = ctk.CTkScrollableFrame(panel, fg_color="transparent", label_text="")
        scroll.grid(row=0, column=0, sticky="nsew", padx=4, pady=6)
        scroll.columnconfigure(0, weight=1)

        self._missing_ep_poster_label = ctk.CTkLabel(
            scroll, text="Pulsa una serie\npara ver su ficha",
            width=180, height=220, text_color=PENDING_COLOR)
        self._missing_ep_poster_label.pack(pady=(4, 2))
        self._missing_ep_detail_title = ctk.CTkTextbox(
            scroll, width=200, height=1, wrap="word",
            font=ctk.CTkFont(size=13, weight="bold"), fg_color="transparent",
            activate_scrollbars=False)
        self._missing_ep_detail_title.configure(state="disabled")
        self._missing_ep_detail_title.pack(pady=(4, 0), fill="x")
        self._missing_ep_detail_overview = ctk.CTkTextbox(
            scroll, width=200, height=1, wrap="word",
            font=ctk.CTkFont(size=11), fg_color="transparent",
            activate_scrollbars=False)
        self._missing_ep_detail_overview.configure(state="disabled")
        self._missing_ep_detail_overview.pack(pady=4, fill="x")

        # Ruta de la serie en el FTP -- se ve directamente al pulsar la
        # serie, sin tocar ningún botón (a diferencia de los enlaces
        # personalizables, que son independientes de esto). Ver
        # _update_missing_ep_path_label.
        self._missing_ep_path_lbl = ctk.CTkLabel(
            scroll, text="", font=ctk.CTkFont(size=11), text_color=PENDING_COLOR,
            wraplength=200, justify="left", anchor="w")
        self._missing_ep_path_lbl.pack(pady=(0, 4), fill="x")

        # Botón por serie (no uno general): pregunta a la IA solo por la
        # serie que se está viendo ahora mismo en el panel.
        self._missing_ep_ai_ask_btn = ctk.CTkButton(
            scroll, text="🤖 Preguntar a la IA", width=200, state="disabled",
            command=self._ask_ai_about_current_missing_ep_show)
        self._missing_ep_ai_ask_btn.pack(pady=(0, 4), fill="x")
        # Si la IA detecta algún conflicto (hueco real vs. numeración
        # distinta), la explicación aparece aquí, justo debajo de la
        # sinopsis de la serie.
        self._missing_ep_ai_verdict_lbl = ctk.CTkLabel(
            scroll, text="", font=ctk.CTkFont(size=11), text_color=WARNING_COLOR,
            wraplength=200, justify="left", anchor="w")
        self._missing_ep_ai_verdict_lbl.pack(pady=(0, 4), fill="x")

        # Enlaces personalizables (Ajustes > Plantillas) -- a nivel serie
        # (sin episodio concreto): "Ver en TMDB" por defecto, o los que el
        # usuario haya configurado. Solo abren una URL, nunca se conectan
        # a nada por su cuenta.
        self._missing_ep_links_frame = ctk.CTkFrame(scroll, fg_color="transparent")
        self._missing_ep_links_frame.pack(pady=(0, 4), fill="x")

        self._missing_ep_current_row = None
        return panel

    @staticmethod
    def _ai_verdict_from_cache_entry(entry: dict):
        """Reconstruye el "ai_verdict" persistido de una entrada cruda de
        missing_episodes_cache.json (ver _persist_ai_verdicts) -- None si
        nunca se preguntó. "doblaje_castellano" se guarda con claves de
        texto (JSON no permite claves int), aquí se reconvierten a int
        para que encajen con "missing"/"expected_episodes" (también int)
        al filtrar (ver filter_missing_by_dub_cutoff)."""
        ai_verdict = entry.get("ai_verdict")
        if not ai_verdict:
            return None
        if "doblaje_castellano" in ai_verdict:
            ai_verdict = dict(ai_verdict)
            ai_verdict["doblaje_castellano"] = {int(s): ep for s, ep in ai_verdict["doblaje_castellano"].items()}
        return ai_verdict

    def _load_missing_episodes_from_cache(self) -> list:
        """Al abrir la vista, mostrar lo que ya se sabía del último
        escaneo (persistido en disco) en vez de una tabla vacía hasta que
        el usuario pulse "Comprobar" a mano."""
        from core.missing_episodes import format_missing_summary, apply_season_split_filter
        from core.missing_episodes_cache import load_cache
        cache = load_cache()
        results = []
        for key, entry in cache.items():
            if key == "_meta":
                continue
            missing = {int(k): v for k, v in (entry.get("missing") or {}).items()}
            unknown_seasons = set(entry.get("unknown_seasons", []))
            if not missing and not unknown_seasons:
                continue
            name = entry.get("name", "")
            episode_titles = {int(s): {int(e): t for e, t in eps.items()}
                              for s, eps in (entry.get("episode_titles") or {}).items()}
            expected = {int(k): v for k, v in (entry.get("expected") or {}).items()}
            present_season_counts = {int(k): v for k, v in (entry.get("present_season_counts") or {}).items()}
            # La caché en disco guarda el hueco SIN filtrar (ver
            # apply_season_split_filter) -- el filtro se aplica aquí, al
            # reconstruir la fila para mostrar, igual que en el resto de
            # sitios donde se calcula "missing" para pantalla.
            missing, split_seasons = apply_season_split_filter(missing, expected)
            # Veredicto de la IA persistido (ver _persist_ai_verdicts) --
            # antes se perdía al cerrar la app y había que volver a
            # preguntarle a la IA en cada sesión.
            ai_verdict = self._ai_verdict_from_cache_entry(entry)
            results.append({
                "tmdb_id": int(key), "name": name, "source": entry.get("source", ""),
                "server_id": entry.get("server_id"),
                "missing": missing, "summary": format_missing_summary(name, missing),
                "ignored": entry.get("ignored", False), "episode_titles": episode_titles,
                "split_seasons": split_seasons, "unknown_seasons": unknown_seasons,
                "expected_episodes": expected,
                "tmdb_season_counts": {s: len(eps) for s, eps in expected.items()},
                "server_season_counts": present_season_counts,
                "ai_verdict": ai_verdict,
                "absolute_numbering": entry.get("absolute_numbering", False),
                "play_count": entry.get("play_count", 0),
                "last_played_ts": entry.get("last_played_ts"),
                "folder_name": entry.get("folder_name"),
            })
        return results

    def _remove_uploaded_episode_from_missing_list(self, media_info) -> None:
        """Tras subir un episodio (automático o manual), si ese episodio
        concreto aparecía en "Episodios que faltan", lo quita de ahí sin
        esperar a un nuevo escaneo completo -- la propia app ya sabe que
        acaba de subirlo, no hace falta volver a preguntarle a Jellyfin/Plex
        y esperar a que ellos mismos reindexen la biblioteca. Solo aplica a
        series con temporada/episodio identificados; en películas no hay
        "episodios que faltan" que actualizar. Debe llamarse desde el hilo
        de la GUI (los sitios en un hilo de subida ya lo agendan con
        self.after)."""
        if media_info is None or media_info.media_type != "tv":
            return
        if media_info.season is None or media_info.episode is None:
            return
        tmdb_id, season, episode = media_info.tmdb_id, media_info.season, media_info.episode

        from core.missing_episodes import remove_missing_episode
        if not remove_missing_episode(self._missing_ep_results, tmdb_id, season, episode):
            return

        from core.missing_episodes_cache import load_cache, save_cache, remove_missing_episode_from_cache
        cache = load_cache()
        if remove_missing_episode_from_cache(cache, tmdb_id, season, episode):
            save_cache(cache)

        self._render_missing_episodes_table(reset_page=False)

    def _remove_series_from_missing_episodes(self, tmdb_id: int):
        """Quita una serie ENTERA de "Episodios que faltan" -- mismo
        mecanismo que _remove_uploaded_from_missing_episodes (quitar de
        self._missing_ep_results + del caché en disco + redibujar sin
        resetear la página), pero para la fila completa en vez de un solo
        episodio. Usado tanto al borrar la serie desde el botón de esta
        misma pantalla (ver _finish_delete_missing_ep_series) como al
        borrarla desde Liberar espacio (ver _finish_delete_cleanup_item):
        en ambos casos la serie ya no está en el servidor, así que no
        tiene sentido seguir listándola como "con episodios pendientes".
        Debe llamarse desde el hilo de la GUI."""
        from core.missing_episodes import remove_series
        if not remove_series(self._missing_ep_results, tmdb_id):
            return

        from core.missing_episodes_cache import load_cache, save_cache, remove_series_from_cache
        cache = load_cache()
        if remove_series_from_cache(cache, tmdb_id):
            save_cache(cache)

        self._render_missing_episodes_table(reset_page=False)

    def _start_missing_episodes_scan(self, force_full: bool = False):
        if not self.config_data.get("jellyfin_enabled") and not self.config_data.get("plex_enabled"):
            self._set_status("Activa Plex o Jellyfin en Ajustes para usar el detector de huecos", WARNING_COLOR)
            return
        if self._missing_ep_scanning:
            return
        if self._upload_running:
            # No competir con una subida en curso: un escaneo completo hace
            # muchas llamadas seguidas a TMDB, y una subida manual también
            # necesita TMDB para identificar el archivo -- mejor esperar a
            # que termine una de las dos antes de lanzar la otra.
            self._set_status("Espera a que termine la subida en curso antes de comprobar huecos", WARNING_COLOR)
            return
        self._missing_ep_scanning = True
        self._missing_ep_cancel_event = threading.Event()
        self._set_missing_ep_scanning_ui(True)
        self._missing_ep_progress.set(0)

        # En un reescaneo completo, la tabla vieja se queda mostrando
        # datos ya obsoletos durante todo el escaneo (que puede tardar) --
        # mejor vaciarla al empezar y que las series vayan apareciendo
        # según se detectan, en vez de esperar a tener el resultado
        # entero. En un "Comprobar" normal no aplica: es rápido (usa
        # caché, se salta series sin cambios) y muchas series que ya
        # estaban en la tabla ni siquiera se reevalúan en esta pasada, así
        # que vaciarla daría una falsa sensación de que desaparecieron.
        on_result_cb = None
        if force_full:
            self._missing_ep_results = []
            self._missing_ep_live_render_counter = 0
            self._render_missing_episodes_table()
            # Todo lo que toca self._missing_ep_results (incluido añadir
            # cada fila nueva) se agenda vía self.after(0, ...) para que
            # ocurra en el hilo de la GUI -- el escaneo corre en un hilo
            # aparte, y mutar la lista desde ahí a la vez que se lee para
            # redibujar podría pisarse.
            on_result_cb = lambda row: self.after(0, lambda r=row: self._append_missing_ep_result_live(r))

        def worker():
            # Sin este try/except, cualquier fallo dentro de
            # _scan_missing_episodes (un hueco de red, un dato inesperado
            # de TMDB/Jellyfin/Plex...) mataba el hilo en silencio ANTES
            # de llegar a _finish_missing_episodes_scan -- los botones se
            # quedaban deshabilitados y la barra de progreso llena para
            # siempre, aunque un reescaneo completo ya hubiera mostrado
            # todas las filas en vivo (ver on_result_cb) y pareciera
            # "terminado". self._missing_ep_results ya tiene lo que se
            # llegó a encontrar (en vivo, si force_full) o lo de antes (si
            # no) -- se usa igual para no dejar la UI colgada, y el error
            # se avisa en vez de fingir que no pasó nada.
            try:
                results = self._scan_missing_episodes(
                    progress_cb=lambda c, t, n: self.after(
                        0, lambda c=c, t=t, n=n: self._update_missing_ep_progress(c, t, n)),
                    cancel_event=self._missing_ep_cancel_event, force_full=force_full,
                    on_result_cb=on_result_cb)
            except Exception:
                _log.exception("Comprobación de episodios que faltan: fallo inesperado durante el escaneo")
                self.after(0, lambda: self._set_status(
                    "El escaneo terminó con un error -- revisa app.log", ERROR_COLOR))
                self.after(0, lambda: self._finish_missing_episodes_scan(self._missing_ep_results))
                return
            self.after(0, lambda: self._finish_missing_episodes_scan(results))
        threading.Thread(target=worker, daemon=True).start()

    _MISSING_EP_LIVE_RENDER_EVERY = 15   # ver _append_missing_ep_result_live

    def _append_missing_ep_result_live(self, row: dict):
        """Registra una fila recién encontrada durante un reescaneo
        completo. Con páginas de tamaño acotado (ver TableView.page_size)
        no tiene sentido repintar en CADA fila (sería tan caro como el
        problema que la paginación evitó) -- pero tampoco dejar la tabla
        completamente en blanco hasta el final: con un cruce FTP lento
        (ver _build_ftp_episode_index) un reescaneo completo puede tardar
        muchos minutos, y una barra de progreso subiendo sin que aparezca
        NADA en la lista parece un cuelgue aunque no lo sea -- solo se
        detectaba viendo el log, o por casualidad si se salía de la
        pestaña y se volvía a entrar (dispara una sincronización de
        favoritos que de rebote repinta). Cada _MISSING_EP_LIVE_RENDER_EVERY
        filas nuevas, si la pestaña está visible, se repinta de verdad."""
        self._missing_ep_results.append(row)
        self._missing_ep_live_render_counter = getattr(self, "_missing_ep_live_render_counter", 0) + 1
        if self._missing_ep_live_render_counter >= self._MISSING_EP_LIVE_RENDER_EVERY:
            self._missing_ep_live_render_counter = 0
            if self._missing_ep_visible:
                self._render_missing_episodes_table(reset_page=False)

    def _cancel_missing_episodes_scan(self):
        if self._missing_ep_cancel_event:
            self._missing_ep_cancel_event.set()

    def _set_missing_ep_scanning_ui(self, scanning: bool):
        state = "disabled" if scanning else "normal"
        self._missing_ep_scan_btn.configure(state=state)
        self._missing_ep_full_btn.configure(state=state)
        if scanning:
            self._missing_ep_cancel_btn.pack(side="left", padx=(0, 4), pady=8)
            self._missing_ep_status_lbl.grid_remove()
            self._missing_ep_progress.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        else:
            self._missing_ep_cancel_btn.pack_forget()
            self._missing_ep_progress.grid_remove()
            self._missing_ep_status_lbl.grid(row=1, column=0, sticky="w", pady=(0, 6))

    def _update_missing_ep_progress(self, current: int, total: int, show_name: str):
        if total > 0:
            self._missing_ep_progress.set(current / total)
        self._missing_ep_status_lbl.configure(text=f"Comprobando ({current}/{total}): {show_name}")

    def _finish_missing_episodes_scan(self, results: list):
        self._missing_ep_scanning = False
        self._missing_ep_results = results
        self._set_missing_ep_scanning_ui(False)
        self._update_missing_ep_status_text()
        self._render_missing_episodes_table()
        # Segunda opinión automática de la IA -- solo si el usuario la tiene
        # activada y configurada (mismo interruptor que el fallback de
        # títulos en Ajustes); por lotes, una sola llamada para todas las
        # series con huecos. El botón "🤖 Preguntar a la IA" del panel
        # lateral es aparte, por serie, para cuando quiera repetirla a mano.
        if self.config_data.get("ai_fallback_enabled") and self.config_data.get("ai_api_key"):
            self._ask_ai_for_missing_ep_verdicts()

    def _on_toggle_missing_ep_switch(self, config_key: str, var: "ctk.BooleanVar", then):
        """Persiste el nuevo estado de uno de los 3 interruptores de
        "Episodios que faltan" (Mostrar ignoradas/Ocultar descartados por
        IA/Ocultar sin doblaje ES) y ejecuta *then*, su comportamiento
        normal al cambiar -- así la próxima vez que se abra la app se
        recuerda cómo se dejó, en vez de resetear siempre a apagado."""
        self.config_data.set(config_key, var.get())
        self.config_data.save()
        then()

    def _on_toggle_hide_no_dub(self):
        """Al activar "Ocultar sin doblaje ES": si hay episodios pendientes
        de comprobar, lanza el chequeo en segundo plano (con barra de
        progreso, ver _start_spanish_dub_check) antes de redibujar -- al
        desactivarlo, basta con redibujar (no hay nada que comprobar para
        volver a MOSTRAR episodios). Este chequeo automático (TMDB) es el
        comportamiento por defecto -- tiene un fallo conocido (falso
        positivo en series con doblaje parcial, ver el docstring de
        core.missing_episodes.episode_has_spanish_text), que el usuario
        puede corregir serie a serie pulsando "🤖 Preguntar a la IA" en el
        panel lateral (ver _ask_ai_about_current_missing_ep_show) -- nunca
        al revés, y nunca automáticamente."""
        if self._missing_ep_hide_no_dub_var.get():
            self._start_spanish_dub_check()
        else:
            self._render_missing_episodes_table()

    def _on_force_recheck(self, tmdb_id):
        """Botón "🔄" de _DubHiddenDialog -- fuerza un repaso inmediato del
        doblaje TMDB de UNA sola serie. Borra su entrada de
        self._spanish_dub_cache (is_stale() la trata como caducada al no
        tener "checked_at") y reutiliza _start_spanish_dub_check tal
        cual: como es la ÚNICA serie caducada en ese momento, el repaso
        que hace ya queda acotado a ella sola, sin tener que duplicar esa
        lógica para un caso "una sola serie"."""
        self._spanish_dub_cache.pop(str(tmdb_id), None)
        self._start_spanish_dub_check()

    def _start_spanish_dub_check(self):
        """Comprueba el doblaje ES de todos los episodios que faltan (de
        series no ignoradas) que aún no estén en self._spanish_dub_cache --
        una llamada a /watch/providers por serie (cacheada en memoria
        durante todo el chequeo, para no repetirla por cada episodio de la
        misma serie) más una a /season/.../episode/... con language=es-ES
        por episodio pendiente. Reutiliza la barra de progreso y el botón
        "Cancelar" del escaneo normal (_set_missing_ep_scanning_ui) -- ambos
        comparten self._missing_ep_scanning para no competir por el mismo
        límite de peticiones de TMDB."""
        if self._missing_ep_scanning:
            return
        from core.spanish_dub_cache import is_stale
        known_cache = self._spanish_dub_cache   # solo lectura mientras corre el worker
        now = _time.time()
        pending = []
        for r in self._missing_ep_results:
            if r.get("ignored"):
                continue
            series_entry = known_cache.get(str(r["tmdb_id"]), {})
            # Una entrada caducada (ver is_stale) se trata como si no
            # tuviera NINGÚN episodio comprobado -- se repasa la serie
            # entera, no solo los episodios nuevos desde la última vez.
            dub_episodes = {} if is_stale(series_entry, now) else series_entry.get("episodes", {})
            for season, eps in r["missing"].items():
                for ep in eps:
                    if f"{season}x{ep:02d}" not in dub_episodes:
                        pending.append((r["tmdb_id"], r["name"], season, ep))

        if not pending:
            self._render_missing_episodes_table()
            return

        self._missing_ep_scanning = True
        self._missing_ep_cancel_event = threading.Event()
        cancel_event = self._missing_ep_cancel_event
        self._set_missing_ep_scanning_ui(True)
        self._missing_ep_progress.set(0)

        def worker():
            from core.missing_episodes import has_spanish_availability, episode_has_spanish_text
            updates = {}   # tmdb_id_str -> {"spanish_available": bool|None, "episodes": {...}}
            total = len(pending)
            for i, (tmdb_id, name, season, ep) in enumerate(pending):
                if cancel_event.is_set():
                    break
                self.after(0, lambda c=i + 1, t=total, n=name: self._update_missing_ep_progress(c, t, n))
                key = str(tmdb_id)
                entry = updates.get(key)
                if entry is None:
                    old_entry = known_cache.get(key, {})
                    entry = dict(old_entry)
                    entry["episodes"] = dict(entry.get("episodes", {}))
                    if is_stale(old_entry, now):
                        # Caducada -- también se vuelve a comprobar
                        # spanish_available, no solo los episodios (podría
                        # haber cambiado de plataforma desde la última vez).
                        entry["spanish_available"] = None
                    else:
                        entry.setdefault("spanish_available", None)
                    entry["checked_at"] = now   # ver core.spanish_dub_cache.is_stale
                    updates[key] = entry

                # available: solo para ESTE episodio/intento. Si la consulta
                # de disponibilidad falla, entry["spanish_available"] se deja
                # en None (no se persiste una suposición) para reintentarla
                # la próxima vez -- pero se sigue comprobando el episodio
                # ahora mismo, en vez de bloquear todo el resto del chequeo
                # por un fallo puntual de red.
                available = entry["spanish_available"]
                if available is None:
                    try:
                        providers = self.tmdb.get_watch_providers(tmdb_id)
                        available = has_spanish_availability(providers)
                        entry["spanish_available"] = available
                    except Exception:
                        available = True

                ep_key = f"{season}x{ep:02d}"
                if not available:
                    entry["episodes"][ep_key] = False
                    continue
                try:
                    info = self.tmdb.get_episode_info_es(tmdb_id, season, ep)
                    entry["episodes"][ep_key] = episode_has_spanish_text(info)
                except Exception:
                    pass   # sin dato fiable -- se deja fuera de la caché, se reintenta la próxima vez
            self.after(0, lambda: self._finish_spanish_dub_check(updates))
        threading.Thread(target=worker, daemon=True).start()

    def _finish_spanish_dub_check(self, updates: dict):
        self._missing_ep_scanning = False
        self._set_missing_ep_scanning_ui(False)
        for key, entry in updates.items():
            existing = self._spanish_dub_cache.setdefault(key, {"spanish_available": None, "episodes": {}})
            existing["spanish_available"] = entry["spanish_available"]
            existing["episodes"].update(entry["episodes"])
        from core.spanish_dub_cache import save_cache
        save_cache(self._spanish_dub_cache)
        self._update_missing_ep_status_text()
        self._render_missing_episodes_table()

    def _ask_ai_for_missing_ep_verdicts(self):
        """Manda de una sola vez (no una llamada por serie) el recuento de
        episodios por temporada de TODAS las series con huecos actuales a
        Groq, para que diga cuáles son probablemente huecos reales y
        cuáles probablemente un desajuste de numeración con TMDB (series
        publicadas por partes, ID de TMDB con menos temporadas de las
        reales...). Nunca oculta nada por su cuenta -- solo añade una
        anotación; el interruptor "Ocultar descartados por IA" es cosa del
        usuario. Se llama automáticamente tras cada escaneo completo (ver
        _finish_missing_episodes_scan) -- a propósito NUNCA pregunta por
        doblaje castellano aquí (check_spanish_dub=False, por defecto): esa
        pregunta solo se hace cuando el usuario pulsa "🤖 Preguntar a la IA"
        a mano para una serie concreta (ver
        _ask_ai_about_current_missing_ep_show), nunca de forma automática."""
        api_key = self.config_data.get("ai_api_key", "")
        if not api_key or not self._missing_ep_results:
            return
        shows_payload = [
            {"tmdb_id": r["tmdb_id"], "name": r["name"],
             "tmdb_seasons": {str(k): v for k, v in r.get("tmdb_season_counts", {}).items()},
             "server_seasons": {str(k): v for k, v in r.get("server_season_counts", {}).items()}}
            for r in self._missing_ep_results
        ]

        def worker():
            from core.missing_episodes_ai import analyze_missing_episodes
            verdicts = analyze_missing_episodes(shows_payload, api_key)
            self.after(0, lambda: self._apply_missing_ep_ai_verdicts(verdicts))
        threading.Thread(target=worker, daemon=True).start()

    def _apply_missing_ep_ai_verdicts(self, verdicts: dict):
        for r in self._missing_ep_results:
            if r["tmdb_id"] in verdicts:
                r["ai_verdict"] = verdicts[r["tmdb_id"]]
        self._persist_ai_verdicts(verdicts)
        if self._missing_ep_current_row is not None:
            self._update_missing_ep_ai_verdict_label(self._missing_ep_current_row)
        self._render_missing_episodes_table(reset_page=False)

    def _persist_ai_verdicts(self, verdicts: dict):
        """Guarda el veredicto de la IA (incluido "doblaje_castellano", si
        se preguntó) en missing_episodes_cache.json -- antes solo vivía en
        self._missing_ep_results (memoria), así que se perdía al cerrar la
        app o al hacer un "Comprobar" nuevo, y había que volver a
        preguntarle a la IA cada vez. Solo se actualizan series que YA
        tienen entrada en la caché (si no la tienen, no hay dónde
        guardarlo -- no debería pasar, ya que el veredicto siempre se pide
        sobre series que salieron de un escaneo previo)."""
        if not verdicts:
            return
        from core.missing_episodes_cache import load_cache, save_cache
        cache = dict(load_cache())
        changed = False
        for tmdb_id, verdict in verdicts.items():
            key = str(tmdb_id)
            if key not in cache:
                continue
            cache[key]["ai_verdict"] = verdict
            changed = True
        if changed:
            save_cache(cache)

    def _update_missing_ep_status_text(self):
        from core.missing_episodes_cache import load_cache
        import time as _time
        last_ts = (load_cache().get("_meta") or {}).get("last_scan_ts")
        when = ""
        if last_ts:
            mins = int((_time.time() - last_ts) / 60)
            if mins < 1:
                when = "hace un momento"
            elif mins < 60:
                when = f"hace {mins} min"
            else:
                when = f"hace {mins // 60} h"
        pending = [r for r in self._missing_ep_results if not r.get("ignored")]
        if not self._missing_ep_results:
            text = "Sin comprobar todavía" if not when else f"Sin huecos conocidos -- último escaneo {when}"
        elif not pending:
            text = "🎉 No falta ningún episodio" + (f" -- último escaneo {when}" if when else "")
        else:
            text = f"{len(pending)} serie(s) con episodios que faltan" + (f" -- último escaneo {when}" if when else "")
        self._missing_ep_status_lbl.configure(text=text)

    def _render_missing_episodes_table(self, reset_page: bool = True):
        """Punto de entrada tras cambiar filtros, terminar un escaneo, o
        cualquier otro cambio que pueda alterar QUÉ filas hay que ver --
        por defecto vuelve a la primera página. Para redibujar la página
        actual sin resetearla (p.ej. tras marcar un favorito o un
        veredicto de IA en una fila concreta) pasar reset_page=False --
        eso también evita subir el scroll (ver _missing_ep_render_page):
        un refresco en segundo plano (sync de favoritos desde el FTP,
        veredicto de IA que llega async...) no cambia qué filas hay ni
        cuántas, así que no hay ningún "hueco" que cubrir subiendo el
        scroll -- solo interrumpía al usuario mientras miraba la lista
        desplazada hacia abajo, sin ningún motivo real."""
        if reset_page:
            self._missing_ep_page = 0
        self._missing_ep_render_page(scroll_top=reset_page)

    def _missing_ep_change_page(self, delta: int):
        rows = self._missing_ep_visible_rows()
        n_pages = max(1, -(-len(rows) // self._missing_ep_table.page_size))
        new_page = max(0, min(n_pages - 1, self._missing_ep_page + delta))
        if new_page == self._missing_ep_page:
            return
        self._missing_ep_page = new_page
        self._missing_ep_render_page()

    def _missing_ep_visible_rows(self) -> list:
        rows = [row for row in (self._visible_missing_ep_row(r) for r in self._missing_ep_results)
                if row is not None]
        return sorted(rows, key=lambda r: r["name"].lower())

    def _missing_ep_render_page(self, scroll_top: bool = True):
        """Dibuja solo self._missing_ep_page de las filas que pasan los
        filtros activos -- ver TableView.page_size. Página acotada, no
        lotes acumulativos: con varios cientos de series cacheadas, el
        número de widgets vivos a la vez tiene que tener un techo fijo
        pase lo que pase con el tamaño real de la lista (ver el comentario
        largo en _build_missing_episodes_tab, junto a nav_fr).

        scroll_top=False (ver _render_missing_episodes_table con
        reset_page=False): un refresco en segundo plano no cambia el
        conjunto de filas ni cuántas hay, así que no hace falta subir el
        scroll -- solo se sube cuando de verdad puede haber quedado un
        hueco (cambio de página o de filtro, ver el comentario de más
        abajo)."""
        # Solo se destruyen las filas (y el mensaje vacío, si estaba) --
        # la cabecera vive en TableView.header_frame, aparte del cuerpo
        # con scroll (ver gui/table_view.py), así que nunca se ve tocada
        # por esto.
        for widgets in self._missing_ep_row_widgets.values():
            widgets["row_fr"].destroy()
        if self._missing_ep_empty_msg is not None:
            self._missing_ep_empty_msg.destroy()
            self._missing_ep_empty_msg = None
        # tmdb_id -> {"toggle_btn", "detail_fr" (None hasta que se expande la
        # primera vez), "r"} -- para que expandir/colapsar (_toggle_missing_ep_expand)
        # solo toque la fila afectada en vez de reconstruir la tabla entera,
        # que con muchas series era lentísimo (mismo problema que ya se dio
        # con el arrastre del panel de detalles de Archivos).
        self._missing_ep_row_widgets = {}
        # (tmdb_id, temporada) -> {"episodes_fr", "toggle_btn", "n_eps"} --
        # mismo motivo que _missing_ep_row_widgets: expandir/colapsar una
        # temporada (_toggle_missing_ep_season_expand) solo toca esa
        # temporada en vez de reconstruir el detalle de la serie entero.
        self._missing_ep_season_widgets = {}

        sorted_rows = self._missing_ep_visible_rows()
        self._update_missing_ep_switch_counters()

        if not sorted_rows:
            if self._missing_ep_results:
                msg = "Sin resultados que coincidan con el filtro."
            else:
                msg = "Pulsa \"🔍 Comprobar\" para buscar episodios que faltan."
            self._missing_ep_empty_msg = ctk.CTkLabel(self._missing_ep_table.body, text=msg, text_color=PENDING_COLOR)
            self._missing_ep_empty_msg.pack(pady=30)
            self._missing_ep_page_lbl.configure(text="")
            self._missing_ep_prev_btn.configure(state="disabled")
            self._missing_ep_next_btn.configure(state="disabled")
            return

        total = len(sorted_rows)
        page_size = self._missing_ep_table.page_size
        n_pages = max(1, -(-total // page_size))
        self._missing_ep_page = max(0, min(n_pages - 1, self._missing_ep_page))
        start = self._missing_ep_page * page_size
        page_rows = sorted_rows[start:start + page_size]

        self._missing_ep_page_lbl.configure(text=f"Página {self._missing_ep_page + 1} de {n_pages}")
        self._missing_ep_prev_btn.configure(state="normal" if self._missing_ep_page > 0 else "disabled")
        self._missing_ep_next_btn.configure(state="normal" if self._missing_ep_page < n_pages - 1 else "disabled")

        # Volver arriba del todo -- si no, al cambiar de página/filtro con
        # el scroll bajado, las filas nuevas se dibujan pero el hueco
        # (ahora vacío) por el que se había bajado se queda visible, dando
        # la sensación de "no hay resultados" aunque sí los haya. Solo si
        # scroll_top=True (ver _render_missing_episodes_table): un
        # refresco en segundo plano (p.ej. quitar un episodio recién
        # subido de la lista, ver _remove_uploaded_from_missing_episodes)
        # no cambia de página ni de filtro, así que no hay hueco que
        # cubrir -- interrumpía al usuario en mitad de la lista sin motivo.
        if scroll_top:
            self._missing_ep_table.scroll_to_top()

        # En lotes vía after() dentro de la propia página (no la lista
        # entera): self._missing_ep_render_token invalida cualquier lote
        # pendiente de una llamada anterior si esta función se vuelve a
        # llamar antes de terminar (p.ej. el usuario cambia de página o de
        # filtro a media carga).
        self._missing_ep_render_token = getattr(self, "_missing_ep_render_token", 0) + 1
        token = self._missing_ep_render_token
        _BATCH = 20

        def _render_batch(start=0):
            if token != self._missing_ep_render_token:
                return   # una llamada más reciente ya se está pintando
            for r in page_rows[start:start + _BATCH]:
                self._build_missing_ep_row(r)
            if start + _BATCH < len(page_rows):
                self.after(1, lambda: _render_batch(start + _BATCH))
            else:
                # Solo al terminar el ÚLTIMO lote -- medir con la página a
                # medio construir daría un alto medio por fila incorrecto
                # (ver TableView.note_rows_rendered).
                self._missing_ep_table.note_rows_rendered(len(page_rows))

        _render_batch()

    def _visible_missing_ep_row(self, r: dict):
        """Aplica los filtros activos (búsqueda, ignoradas, descartadas por
        IA y doblaje ES) -- devuelve la fila a pintar, o None si no debe
        verse ahora mismo. Con "Ocultar sin doblaje ES" activo, devuelve
        una COPIA con "missing" recortado a solo los episodios con doblaje
        confirmado -- por defecto según el chequeo automático de TMDB
        (self._spanish_dub_cache, ver _start_spanish_dub_check), SALVO que
        el usuario haya pulsado "🤖 Preguntar a la IA" para esta serie
        concreta (ver _ask_ai_about_current_missing_ep_show): en ese caso
        el veredicto de la IA (r["ai_verdict"]["doblaje_castellano"])
        sustituye por completo al de TMDB para esta serie, nunca al revés.
        Si tras recortar no queda ningún hueco (ni unknown_seasons), la
        fila entera se oculta. Usado tanto por _render_missing_episodes_table
        como por _append_missing_ep_result_live, para que ambos apliquen
        exactamente el mismo criterio."""
        query = self._missing_ep_search_entry.get().strip().lower()
        show_ignored = self._missing_ep_show_ignored_var.get()
        hide_ai_dismissed = self._missing_ep_hide_ai_var.get()
        ai_verdict = r.get("ai_verdict")
        ai_dismissed = bool(ai_verdict) and ai_verdict.get("veredicto") == "numeracion_distinta"
        if not (show_ignored or not r.get("ignored")):
            return None
        if query and query not in r["name"].lower():
            return None
        if hide_ai_dismissed and ai_dismissed:
            return None
        if not self._missing_ep_hide_no_dub_var.get():
            return r

        from core.missing_episodes import format_missing_summary
        filtered_missing = self._missing_ep_dub_filtered(r)
        if not filtered_missing and not r.get("unknown_seasons"):
            return None
        if filtered_missing == r["missing"]:
            return r
        display = dict(r)
        display["missing"] = filtered_missing
        display["summary"] = format_missing_summary(r["name"], filtered_missing) if filtered_missing else r["summary"]
        return display

    def _missing_ep_dub_filtered(self, r: dict) -> dict:
        """El "missing" de *r* recortado a solo los episodios sin doblaje
        castellano confirmado -- IA si la serie tiene doblaje_castellano
        (ver _ask_ai_about_current_missing_ep_show), si no TMDB
        (self._spanish_dub_cache). Extraído de _visible_missing_ep_row para
        reutilizarlo también en _missing_ep_dub_hidden_rows (el contador
        "N series ocultas por doblaje") sin duplicar el criterio."""
        ai_verdict = r.get("ai_verdict")
        if ai_verdict and "doblaje_castellano" in ai_verdict:
            from core.missing_episodes import filter_missing_by_dub_cutoff
            return filter_missing_by_dub_cutoff(r["missing"], ai_verdict["doblaje_castellano"])
        from core.missing_episodes import filter_missing_by_spanish_dub
        dub_episodes = self._spanish_dub_cache.get(str(r["tmdb_id"]), {}).get("episodes", {})
        return filter_missing_by_spanish_dub(r["missing"], dub_episodes)

    def _missing_ep_dub_hidden_rows(self) -> list:
        """Series que "Ocultar sin doblaje ES" deja sin nada visible --
        independiente de otros filtros (búsqueda, ignoradas, descartadas
        por IA), para el contador pulsable junto al interruptor. Un
        veredicto de la IA equivocado sobre el doblaje (visto de verdad:
        Kung Fu Panda -- la IA dijo que solo tenían doblaje los episodios
        ya presentes, y no era así) esconde la serie ENTERA sin dejar
        ningún rastro visible de por qué -- este contador es la única
        forma de revisar qué se ocultó y detectarlo."""
        if not self._missing_ep_hide_no_dub_var.get():
            return []
        hidden = []
        for r in self._missing_ep_results:
            filtered_missing = self._missing_ep_dub_filtered(r)
            if not filtered_missing and not r.get("unknown_seasons"):
                hidden.append(r)
        return sorted(hidden, key=lambda r: r["name"].lower())

    def _missing_ep_ai_dismissed_rows(self) -> list:
        """Series que "Ocultar descartados por IA" oculta ahora mismo --
        mismo criterio que _missing_ep_dub_hidden_rows, para fundir el
        recuento en el propio texto del interruptor."""
        if not self._missing_ep_hide_ai_var.get():
            return []
        hidden = [r for r in self._missing_ep_results
                 if bool(r.get("ai_verdict")) and r["ai_verdict"].get("veredicto") == "numeracion_distinta"]
        return sorted(hidden, key=lambda r: r["name"].lower())

    def _missing_ep_ignored_count(self) -> int:
        """A diferencia de los otros dos contadores, "ignorada" es un dato
        propio de cada fila (r["ignored"]), no algo que dependa de si el
        interruptor está activo -- tiene sentido mostrarlo pase lo que
        pase (con el interruptor apagado, cuántas hay para revelar; con
        él encendido, cuántas se están mostrando ahora)."""
        return sum(1 for r in self._missing_ep_results if r.get("ignored"))

    def _update_missing_ep_switch_counters(self):
        """Funde el recuento de cada filtro en el propio texto de su
        interruptor (Mostrar/Ocultar N ...) en vez de una etiqueta aparte
        al lado -- menos sitio ocupado en la cabecera."""
        n_ignored = self._missing_ep_ignored_count()
        self._missing_ep_show_ignored_switch.configure(
            text=f"Mostrar {n_ignored} ignoradas" if n_ignored else "Mostrar ignoradas")

        n_ai = len(self._missing_ep_ai_dismissed_rows())
        self._missing_ep_hide_ai_switch.configure(
            text=f"Ocultar {n_ai} descartados por IA" if n_ai else "Ocultar descartados por IA")

        n_dub = len(self._missing_ep_dub_hidden_rows())
        self._missing_ep_hide_no_dub_switch.configure(
            text=f"Ocultar {n_dub} sin doblaje ES" if n_dub else "Ocultar sin doblaje ES")

    def _show_missing_ep_dub_hidden_dialog(self):
        hidden = self._missing_ep_dub_hidden_rows()
        if not hidden:
            return
        if self._dub_hidden_win is not None and self._dub_hidden_win.winfo_exists():
            self._dub_hidden_win.refresh(hidden)
        else:
            self._dub_hidden_win = _DubHiddenDialog(self, hidden)

    def _build_missing_ep_row(self, r: dict):
        """Construye y empaqueta la fila de una serie en la tabla --
        extraído para poder usarse tanto al redibujar la tabla entera
        (_render_missing_episodes_table) como al añadir una sola fila
        nueva en vivo durante un reescaneo (_append_missing_ep_row_live),
        sin reconstruir las demás. Las filas se empaquetan con pack() (no
        grid con índice), así que añadir una nueva al final es barato:
        no hace falta saber ni tocar la posición de las que ya había."""
        tmdb_id = r["tmdb_id"]
        expanded = tmdb_id in self._missing_ep_expanded
        is_selected = tmdb_id == getattr(self, "_missing_ep_selected_tmdb_id", None)

        # row_fr apila verticalmente (con pack) su fila de cabecera y,
        # si está desplegada, el detalle debajo -- header_row es un
        # frame aparte porque sus propias celdas van en horizontal
        # (también con pack): no se puede mezclar pack y grid dentro de
        # un mismo padre, pero sí anidar frames que usen cada uno el
        # suyo.
        cw = self._missing_ep_table.col_width
        row_fr = ctk.CTkFrame(self._missing_ep_table.body,
                              fg_color=SELECTED_ROW_COLOR if is_selected else ("gray95", "gray17"))
        row_fr.pack(fill="x", pady=3, padx=2)

        header_row = ctk.CTkFrame(row_fr, fg_color="transparent")
        header_row.pack(fill="x")

        # CTkLabel + bind (no CTkButton), sin marco -- mismo estilo que el
        # triángulo de "Temporada X" al expandir una serie (ver más abajo).
        # "v"/">" en vez de ▼/▶: esos caracteres Unicode (BLACK DOWN/RIGHT-
        # POINTING TRIANGLE) se renderizan con un recuadro visible en este
        # sistema -- confirmado por introspección directa del canvas (el
        # relleno coincide exactamente con el de la fila, no es un fallo de
        # color de customtkinter, es un artefacto de la fuente con ESE
        # glifo concreto). Mismo motivo en los botones "Anterior"/"Siguiente".
        toggle_btn = ctk.CTkLabel(header_row, text=("v" if expanded else ">"), width=cw("toggle"), cursor="hand2")
        toggle_btn.pack(side="left", padx=(6, 0), pady=6)
        toggle_btn.bind("<Button-1>", lambda ev, tid=tmdb_id: self._toggle_missing_ep_expand(tid))

        source_img = self._plex_logo_img if r["source"] == "plex" else self._jellyfin_logo_img
        logo_lbl = ctk.CTkLabel(header_row, image=source_img, text="", width=cw("logo"), cursor="hand2")
        logo_lbl.pack(side="left", pady=6)
        logo_lbl.bind("<Button-1>", lambda ev, row=r: self._open_source_server_link(row))

        # "Episodios que faltan" es siempre sobre series (huecos de
        # temporada/episodio) -- no aplica a películas.
        # CTkLabel + bind (no CTkButton): esta fila se construye para las
        # ~500 series cacheadas de golpe al arrancar (ver más abajo, "self.
        # after(50, self._render_missing_episodes_table)") -- un CTkButton
        # por fila (con su dibujo de hover/relleno) multiplicaba tanto el
        # trabajo síncrono de ese arranque que llegaba a impedir que el
        # resto de la ventana (cabecera incluida) terminase de pintarse.
        fav_btn = ctk.CTkLabel(
            header_row, text="★" if self._is_favorite("tv", tmdb_id) else "☆",
            width=cw("fav"), cursor="hand2",
            text_color=ACCENT if self._is_favorite("tv", tmdb_id) else PENDING_COLOR)
        fav_btn.pack(side="left", pady=6)
        fav_btn.bind("<Button-1>", lambda ev, tid=tmdb_id, name=r["name"]:
                      self._toggle_missing_ep_favorite(tid, name))

        name_color = PENDING_COLOR if r.get("ignored") else None
        name_lbl = ctk.CTkLabel(header_row, text=r["name"], font=self._missing_ep_name_font,
                                anchor="w", text_color=name_color, cursor="hand2")
        name_lbl.pack(side="left", padx=4, pady=6, fill="x", expand=True)
        name_lbl.bind("<Button-1>", lambda e, tid=tmdb_id, row=r: self._on_missing_ep_name_click(tid, row))

        n_missing = sum(len(eps) for eps in r["missing"].values())
        n_seasons = len(r["missing"])
        ai_verdict = r.get("ai_verdict")
        has_warning = (bool(r.get("split_seasons")) or bool(r.get("unknown_seasons"))
                      or bool(r.get("absolute_numbering")))
        if n_missing:
            summary = f"{n_missing} episodio{'s' if n_missing != 1 else ''}"
            if n_seasons > 1:
                summary += f" ({n_seasons} temporadas)"
        else:
            summary = "Posible ID equivocado"   # sin huecos, pero con temporadas que TMDB no conoce
        if has_warning:
            summary += " ⚠"
        if ai_verdict:
            summary += " 🤖"
        if ai_verdict and ai_verdict.get("veredicto") == "numeracion_distinta":
            summary_color = PENDING_COLOR   # la IA lo descarta -- ya no hace falta el color de aviso
        elif has_warning:
            summary_color = WARNING_COLOR
        else:
            summary_color = PENDING_COLOR
        # padx=(4, 4) espeja el separador de 4px de la cabecera (TableView,
        # ver gui/table_view.py) para que ambas queden alineadas.
        summary_lbl = ctk.CTkLabel(header_row, text=summary, font=self._missing_ep_summary_font,
                                   width=cw("summary"),
                                   text_color=summary_color, anchor="w")
        summary_lbl.pack(side="left", padx=(4, 4), pady=6)

        score = trending_score(r.get("play_count", 0), r.get("last_played_ts"), _time.time())
        trending_lbl = ctk.CTkLabel(header_row, text=format_trending_score(score), width=cw("trending"),
                                    font=self._missing_ep_summary_font, text_color=PENDING_COLOR)
        trending_lbl.pack(side="left", padx=(0, 4), pady=6)

        btn_text = "Restaurar" if r.get("ignored") else "Ignorar"
        ctk.CTkButton(header_row, text=btn_text, width=cw("ignore"), fg_color="transparent", border_width=1,
                      command=lambda tid=tmdb_id, ig=not r.get("ignored"):
                      self._toggle_missing_ep_ignore(tid, ig)).pack(side="left", padx=(4, 4), pady=6)

        rescan_btn = ctk.CTkButton(header_row, text="🔄", width=cw("rescan"), height=24,
                      fg_color="transparent", border_width=1,
                      command=lambda row=r: self._rescan_single_missing_ep_series(row))
        rescan_btn.pack(side="left", padx=(0, 4), pady=6)

        ctk.CTkButton(header_row, text="🗑", width=cw("delete"), height=24,
                      fg_color="transparent", border_width=1,
                      text_color=ERROR_COLOR, hover_color=("gray85", "#3d1010"),
                      command=lambda row=r: self._confirm_delete_missing_ep_series(row)
                      ).pack(side="left", padx=(0, 12), pady=6)

        detail_fr = None
        if expanded:
            detail_fr = self._build_missing_ep_detail_frame(row_fr, r)
            detail_fr.pack(fill="x", padx=(36, 12), pady=(0, 8))

        self._missing_ep_row_widgets[tmdb_id] = {
            "row_fr": row_fr, "toggle_btn": toggle_btn, "detail_fr": detail_fr, "r": r,
            "summary_lbl": summary_lbl, "trending_lbl": trending_lbl, "fav_btn": fav_btn,
            "rescan_btn": rescan_btn,
        }

    def _toggle_missing_ep_favorite(self, tmdb_id: int, name: str):
        def _refresh():
            widgets = self._missing_ep_row_widgets.get(tmdb_id)
            if not widgets:
                return
            is_fav = self._is_favorite("tv", tmdb_id)
            widgets["fav_btn"].configure(text="★" if is_fav else "☆",
                                          text_color=ACCENT if is_fav else PENDING_COLOR)
        self._toggle_favorite("tv", tmdb_id, name, on_done=_refresh)

    def _open_source_server_link(self, r: dict):
        """Al pulsar el logo de Plex/Jellyfin de una fila en Episodios que
        faltan, abre esa serie en la web del servidor correspondiente --
        solo informativo, no descarga ni cambia nada. server_id es el Id
        de Jellyfin o el ratingKey de Plex (ver _scan_missing_episodes/
        _load_missing_episodes_from_cache); si falta (series cacheadas
        antes de que se empezara a guardar este campo), pide reanalizar."""
        server_id = r.get("server_id")
        if not server_id:
            self._set_status(
                "No se pudo abrir: vuelve a analizar para que esta serie tenga el enlace guardado",
                WARNING_COLOR)
            return

        if r["source"] == "jellyfin":
            host = self.config_data.get("jellyfin_host", "").rstrip("/")
            if not host:
                return
            import webbrowser
            webbrowser.open(f"{host}/web/#/details?id={server_id}")
        elif r["source"] == "plex":
            host = self.config_data.get("plex_host", "").rstrip("/")
            token = self.config_data.get("plex_token", "")
            if not host or not token:
                return
            self._set_status("Abriendo en Plex...", PENDING_COLOR)
            threading.Thread(target=self._open_plex_link_worker,
                             args=(host, token, server_id), daemon=True).start()

    def _open_plex_link_worker(self, host: str, token: str, rating_key: str):
        # A diferencia de Jellyfin, la URL de Plex necesita el identificador
        # del servidor además del ítem -- de ahí la llamada de red extra
        # antes de poder abrir el navegador (ver get_plex_machine_identifier).
        from core.media_server_refresh import get_plex_machine_identifier
        machine_id = get_plex_machine_identifier(host, token)
        if not machine_id:
            self.after(0, lambda: self._set_status(
                "No se pudo conectar con Plex para abrir el enlace", ERROR_COLOR))
            return
        url = f"https://app.plex.tv/desktop/#!/server/{machine_id}/details?key=%2Flibrary%2Fmetadata%2F{rating_key}"

        def _open():
            import webbrowser
            webbrowser.open(url)
            self._set_status("", PENDING_COLOR)
        self.after(0, _open)

    def _build_missing_ep_detail_frame(self, parent, r: dict):
        """Construye (una sola vez por fila, la primera vez que se expande)
        el bloque con la lista completa de episodios -- ver
        _toggle_missing_ep_expand, que reutiliza este frame en vez de
        reconstruirlo en cada clic. Una línea por episodio (en vez de un
        único bloque de texto) para poder copiar cada nombre suelto."""
        detail_fr = ctk.CTkFrame(parent, fg_color="transparent")
        detail_fr.grid_columnconfigure(0, weight=1)
        next_row = 0

        if r.get("split_seasons"):
            seasons_txt = ", ".join(f"T{s}" for s in sorted(r["split_seasons"]))
            ctk.CTkLabel(detail_fr, text=(f"⚠ {seasons_txt}: podría no faltar de verdad -- TMDB cuenta "
                                          "como una sola temporada algo que Netflix publicó en dos partes"),
                         font=ctk.CTkFont(size=10), text_color=WARNING_COLOR, anchor="w",
                         wraplength=600, justify="left").grid(
                row=next_row, column=0, columnspan=2, sticky="w", pady=(0, 4))
            next_row += 1

        if r.get("unknown_seasons"):
            seasons_txt = ", ".join(f"T{s}" for s in sorted(r["unknown_seasons"]))
            ctk.CTkLabel(detail_fr, text=(f"⚠ Tu servidor tiene la {seasons_txt} pero TMDB no la tiene "
                                          "registrada para esta serie -- es muy probable que el ID de "
                                          "TMDB emparejado en Jellyfin/Plex sea el equivocado. Revisa la "
                                          "identificación de esta serie en el propio servidor."),
                         font=ctk.CTkFont(size=10), text_color=WARNING_COLOR, anchor="w",
                         wraplength=600, justify="left").grid(
                row=next_row, column=0, columnspan=2, sticky="w", pady=(0, 4))
            next_row += 1

        if r.get("absolute_numbering"):
            ctk.CTkLabel(detail_fr, text=("⚠ Tu servidor parece numerar los episodios de corrido "
                                          "(numeración absoluta, típico de anime largo como Naruto "
                                          "Shippuden) en vez de reiniciar en cada temporada -- se "
                                          "convirtió automáticamente para comparar, pero conviene "
                                          "revisar el resultado."),
                         font=ctk.CTkFont(size=10), text_color=WARNING_COLOR, anchor="w",
                         wraplength=600, justify="left").grid(
                row=next_row, column=0, columnspan=2, sticky="w", pady=(0, 4))
            next_row += 1

        # El veredicto de la IA (si lo hay) se muestra en el panel lateral,
        # debajo de la sinopsis de la serie -- ver
        # _update_missing_ep_ai_verdict_label -- no aquí, para no duplicarlo.

        import itertools
        season_font = self._missing_ep_season_font
        season_links = self.config_data.get("custom_links_season", [])
        tmdb_id = r["tmdb_id"]
        lines_by_season = itertools.groupby(self._missing_episode_lines(r), key=lambda t: t[0])

        for season, lines in lines_by_season:
            lines = list(lines)

            # Cabecera de temporada -- colapsada por defecto, igual que las
            # series en la tabla principal -- con sus propios botones de
            # enlaces personalizables (sin episodio concreto: {episodio}/
            # {titulo}/{nombre_archivo} quedan vacíos si la plantilla los usa).
            header_fr = ctk.CTkFrame(detail_fr, fg_color="transparent")
            header_fr.grid(row=next_row, column=0, columnspan=10, sticky="w", pady=(8, 2))
            season_key = (tmdb_id, season)
            expanded = season_key in self._missing_ep_expanded_seasons
            arrow = "v" if expanded else ">"
            # CTkLabel + bind (no CTkButton), sin marco -- mismo motivo que
            # el triángulo de cada serie (ver _build_missing_ep_row): un
            # CTkButton, aunque fg_color="transparent", sigue dibujando su
            # rectángulo redondeado en un canvas propio, y ese redondeado
            # deja un halo/marco visible alrededor del texto. CTkLabel no
            # dibuja ningún rectángulo, solo el texto.
            toggle_btn = ctk.CTkLabel(
                header_fr, text=f"{arrow} Temporada {season} ({len(lines)} episodios)",
                font=season_font, anchor="w", width=220, cursor="hand2")
            toggle_btn.pack(side="left")
            toggle_btn.bind("<Button-1>", lambda ev, tid=tmdb_id, s=season:
                            self._toggle_missing_ep_season_expand(tid, s))
            season_vars = {"serie": r["name"], "tmdb_id": tmdb_id, "temporada": season}
            for link in season_links:
                template = link.get("url_template", "")
                if not template:
                    continue
                short_label = (link.get("name", "").split() or ["🔗"])[-1]
                ctk.CTkButton(header_fr, text=short_label, width=70, height=22,
                              fg_color="transparent", border_width=1,
                              command=lambda t=template, v=season_vars, bg=link.get("background", False):
                              self._resolve_missing_ep_ruta_and_open(t, v, r, bg)
                              ).pack(side="left", padx=(6, 0))
            next_row += 1

            episodes_fr = ctk.CTkFrame(detail_fr, fg_color="transparent")
            episodes_fr.grid_columnconfigure(0, weight=1)
            episodes_fr.grid(row=next_row, column=0, columnspan=10, sticky="ew", padx=(20, 0))
            if not expanded:
                episodes_fr.grid_remove()
            next_row += 1

            # Filas de episodio construidas de forma perezosa (ver
            # _toggle_missing_ep_season_expand): con series de muchos
            # capítulos pendientes (p.ej. "Bleach" con 307), construir aquí
            # mismo ~4 widgets por episodio para TODAS las temporadas de
            # golpe -- incluidas las que siguen colapsadas -- bloqueaba la
            # ventana un momento entero al expandir la serie. Ahora solo se
            # construyen las de una temporada la primera vez que esa
            # temporada en concreto se expande (las que ya estaban
            # expandidas de antes SÍ se construyen aquí, para no perder su
            # estado al reconstruir la fila).
            self._missing_ep_season_widgets[season_key] = {
                "episodes_fr": episodes_fr, "toggle_btn": toggle_btn, "n_eps": len(lines),
                "lines": lines, "r": r, "built": False,
            }
            if expanded:
                self._build_missing_ep_season_episode_rows(season_key)
        return detail_fr

    def _build_missing_ep_season_episode_rows(self, season_key):
        """Construye las filas de episodio de una temporada dentro de su
        episodes_fr ya existente -- separado de
        _build_missing_ep_detail_frame para poder llamarse tarde (al
        expandir la temporada) en vez de siempre al expandir la serie."""
        widgets = self._missing_ep_season_widgets.get(season_key)
        if widgets is None or widgets["built"]:
            return
        widgets["built"] = True
        tmdb_id, season = season_key
        episodes_fr = widgets["episodes_fr"]
        r = widgets["r"]
        detail_font = self._missing_ep_detail_font
        episode_links = self.config_data.get("custom_links_episode", [])

        ep_row = 0
        for _season, ep, title, name in widgets["lines"]:
            ctk.CTkLabel(episodes_fr, text=name, font=detail_font, text_color=PENDING_COLOR,
                         anchor="w").grid(row=ep_row, column=0, sticky="w", pady=1)
            btn_col = 1
            ctk.CTkButton(episodes_fr, text="📋", width=28, height=22, fg_color="transparent",
                          border_width=1, command=lambda n=name: self._copy_to_clipboard(n)).grid(
                row=ep_row, column=btn_col, padx=(6, 0), pady=1)
            variables = {"serie": r["name"], "tmdb_id": tmdb_id, "temporada": season,
                        "episodio": ep, "titulo": title, "nombre_archivo": name}
            for link in episode_links:
                template = link.get("url_template", "")
                if not template:
                    continue
                btn_col += 1
                short_label = (link.get("name", "").split() or ["🔗"])[-1]
                ctk.CTkButton(episodes_fr, text=short_label, width=70, height=22,
                              fg_color="transparent", border_width=1,
                              command=lambda t=template, v=variables, bg=link.get("background", False):
                              self._resolve_missing_ep_ruta_and_open(t, v, r, bg)).grid(
                    row=ep_row, column=btn_col, padx=(4, 0), pady=1)
            ep_row += 1

    def _toggle_missing_ep_season_expand(self, tmdb_id, season):
        season_key = (tmdb_id, season)
        if season_key in self._missing_ep_expanded_seasons:
            self._missing_ep_expanded_seasons.discard(season_key)
            expand_now = False
        else:
            self._missing_ep_expanded_seasons.add(season_key)
            expand_now = True

        widgets = self._missing_ep_season_widgets.get(season_key)
        if widgets is None:
            return   # la temporada no está visible ahora mismo (serie colapsada) -- nada que actualizar
        arrow = "v" if expand_now else ">"
        widgets["toggle_btn"].configure(text=f"{arrow} Temporada {season} ({widgets['n_eps']} episodios)")
        if expand_now:
            self._build_missing_ep_season_episode_rows(season_key)
            widgets["episodes_fr"].grid()
        else:
            widgets["episodes_fr"].grid_remove()

    def _copy_to_clipboard(self, text: str):
        self.clipboard_clear()
        self.clipboard_append(text)
        self._set_status(f"Copiado: {text}", SUCCESS_COLOR)

    def _toggle_missing_ep_expand(self, tmdb_id):
        if tmdb_id in self._missing_ep_expanded:
            self._missing_ep_expanded.discard(tmdb_id)
            expand_now = False
        else:
            self._missing_ep_expanded.add(tmdb_id)
            expand_now = True

        widgets = self._missing_ep_row_widgets.get(tmdb_id)
        if widgets is None:
            return   # la fila no está visible ahora mismo (filtrada) -- nada que actualizar
        widgets["toggle_btn"].configure(text="v" if expand_now else ">")
        if expand_now:
            if widgets["detail_fr"] is None:
                widgets["detail_fr"] = self._build_missing_ep_detail_frame(widgets["row_fr"], widgets["r"])
            widgets["detail_fr"].pack(fill="x", padx=(36, 12), pady=(0, 8))
        elif widgets["detail_fr"] is not None:
            widgets["detail_fr"].pack_forget()

    def _on_missing_ep_name_click(self, tmdb_id, r: dict):
        """Pulsar el nombre de una serie despliega sus episodios (igual que
        antes) Y además carga su ficha de TMDB en el panel lateral, igual
        que el buscador de Archivos."""
        self._toggle_missing_ep_expand(tmdb_id)
        self._show_missing_ep_poster(r)

    def _show_missing_ep_poster(self, r: dict):
        self._missing_ep_current_row = r
        self._missing_ep_selected_tmdb_id = r["tmdb_id"]
        for tid, widgets in self._missing_ep_row_widgets.items():
            widgets["row_fr"].configure(
                fg_color=SELECTED_ROW_COLOR if tid == r["tmdb_id"] else ("gray95", "gray17"))
        self._update_status_bar()
        self._set_textbox_text(self._missing_ep_detail_title, r["name"])
        self._set_textbox_text(self._missing_ep_detail_overview, "Cargando...")
        self._missing_ep_poster_label.configure(image=None, text="…")
        self._update_missing_ep_ai_verdict_label(r)
        self._render_missing_ep_show_links(r)
        # El botón se activa aunque la ficha aún esté cargando -- preguntar
        # a la IA solo necesita los recuentos por temporada, ya calculados
        # de antemano en el propio escaneo, no depende del póster/sinopsis.
        self._missing_ep_ai_ask_btn.configure(state="normal")
        token = object()
        self._missing_ep_poster_token = token
        self._update_missing_ep_path_label(r, token)

        def worker():
            try:
                details = self.tmdb.get_tv_details(r["tmdb_id"])
            except Exception:
                details = {}
            overview = details.get("overview", "") or ""
            poster_path = details.get("poster_path")
            poster_url = f"{TMDB_IMAGE}{poster_path}" if poster_path else None
            self.after(0, lambda: self._apply_missing_ep_detail(token, overview, poster_url))
        threading.Thread(target=worker, daemon=True).start()

    def _apply_missing_ep_detail(self, token, overview: str, poster_url):
        if self._missing_ep_poster_token is not token:
            return   # el usuario ya pulso otra serie mientras esta cargaba
        self._set_textbox_text(self._missing_ep_detail_overview, overview or "Sin sinopsis disponible")
        if poster_url:
            threading.Thread(target=self._load_missing_ep_poster,
                             args=(poster_url, token), daemon=True).start()
        else:
            self._missing_ep_poster_label.configure(image=None, text="Sin póster")

    def _update_missing_ep_ai_verdict_label(self, r: dict):
        """Muestra debajo de la sinopsis el veredicto de la IA para esta
        serie, si ya se conoce (de un escaneo con el fallback de IA
        activado, o de haber pulsado antes "Preguntar a la IA" para ella).
        Vacío si no hay ningún veredicto todavía. Si se preguntó por el
        doblaje castellano (check_spanish_dub, ver
        _ask_ai_about_current_missing_ep_show), también se muestra ese
        resultado -- antes solo se veía el veredicto de hueco_real/
        numeracion_distinta y el recorte de doblaje quedaba invisible
        (aplicado en silencio a la tabla, sin que el usuario supiera qué
        había decidido la IA al respecto)."""
        verdict = r.get("ai_verdict")
        if not verdict:
            self._missing_ep_ai_verdict_lbl.configure(text="")
            return
        dismissed = verdict.get("veredicto") == "numeracion_distinta"
        veredicto_txt = "probablemente NO falta nada real" if dismissed else "probablemente sí falta de verdad"
        motivo = verdict.get("motivo", "")
        text = f"🤖 Según la IA, {veredicto_txt}" + (f": {motivo}" if motivo else "")
        # El "motivo" es texto libre de la IA -- puede generalizar de más
        # ("en todas las temporadas") aunque r["missing"] (la misma fuente
        # que ya construye bien la lista/tabla) solo tenga huecos de
        # verdad en alguna temporada concreta. Se añade aquí, calculado
        # por la app y no por la IA, para que el mensaje nunca pueda
        # contradecir lo que la lista ya muestra correctamente.
        if not dismissed:
            affected = sorted(r.get("missing", {}).keys())
            if affected:
                etiqueta = "temporada" if len(affected) == 1 else "temporadas"
                text += f"\n📋 Huecos reales en la lista: {etiqueta} {', '.join(str(s) for s in affected)}"
        dub_cutoff = verdict.get("doblaje_castellano")
        if dub_cutoff is not None:
            if dub_cutoff:
                partes = ", ".join(f"T{s} hasta el {s}x{ep:02d}" for s, ep in sorted(dub_cutoff.items()))
                text += f"\n🎙 Doblaje castellano: {partes}"
            else:
                text += "\n🎙 Doblaje castellano: sin recorte encontrado (parece completo, o sin datos fiables)"
        self._missing_ep_ai_verdict_lbl.configure(text=text)

    def _missing_ep_series_path(self, r: dict, use_cache_only: bool = True, ftp_conn=None) -> str:
        """Ruta de la carpeta de la serie en el FTP, si ya existe (p.ej.
        "/datos2/series/La nena") -- para el {ruta} de los enlaces
        personalizables, así se sabe dónde está de verdad en el servidor
        sin tener que buscarla a mano. use_cache_only=True (por defecto,
        mismo patrón que la columna "Destino" de Archivos) no bloquea la
        interfaz -- solo mira la caché ya existente, y sale vacío si esa
        carpeta aún no se ha listado nunca. use_cache_only=False sí conecta
        y lista de verdad si hace falta -- lo usan, en un hilo aparte,
        tanto _update_missing_ep_path_label (al pulsar una serie, para
        mostrar la ruta directamente) como _resolve_missing_ep_ruta_and_open
        (al pulsar un botón personalizable que use {ruta}) cuando la caché
        todavía no tiene nada que ofrecer -- ambos pasan su propia conexión
        FTP dedicada (ftp_conn), NUNCA self.ftp, porque ftplib no es
        seguro entre hilos y self.ftp lo usan a la vez otras partes de la
        app (refresco periódico de espacio, subidas...); reutilizarlo aquí
        corrompía la conexión y hacía fallar la búsqueda en TODAS las
        series, no solo en la que se estaba mirando."""
        import types
        if not use_cache_only:
            # _find_category_with_existing_folder solo lista una raíz si
            # NO está ya en self._ftp_dir_cache -- si quedó una entrada
            # vacía de antes (p.ej. un corte de red al listar, visto ya
            # una vez con el cruce del FTP del escaneo), "ya está en
            # caché" y nunca se vuelve a intentar, aunque use_cache_only
            # diga que sí se puede conectar de verdad. Se limpian aquí las
            # raíces vacías para forzar un listado fresco de verdad.
            cats = self.config_data.get("ftp_categories", {"tv": [], "movie": []}).get("tv", [])
            for cat in cats:
                root = cat.get("root", "")
                if root and not self._ftp_dir_cache.get(root):
                    self._ftp_dir_cache.pop(root, None)

        # SimpleNamespace, no MediaInfo -- _find_category_with_existing_folder
        # solo necesita title/media_type y, si se conoce, folder_name (ver
        # get_jellyfin_series::folder_name, el nombre REAL de carpeta que ya
        # nos dio el propio servidor de medios). MediaInfo es un dataclass
        # de campos fijos sin folder_name -- construirlo aquí ignoraba en
        # silencio el nombre real ya conocido y volvía a depender solo del
        # parecido de nombres, el mismo fallo que "Acusado" (Jellyfin, en
        # español) vs carpeta real "Accused" ya tuvo en el botón de borrar.
        info = types.SimpleNamespace(title=r["name"], media_type="tv", folder_name=r.get("folder_name"))
        category, folder_name = self._find_category_with_existing_folder(
            ftp_conn or self.ftp, info, use_cache_only=use_cache_only)
        if not category or not folder_name:
            return ""
        return f"{category.get('root', '').rstrip('/')}/{folder_name}"

    def _update_missing_ep_path_label(self, r: dict, token):
        """Muestra directamente la ruta de la serie en el FTP, sin pulsar
        ningún botón -- independiente del sistema de enlaces personaliza-
        bles. Si ya está en caché se ve al instante; si no, conecta de
        verdad en un hilo aparte (con su PROPIA conexión FTP, no self.ftp
        -- ver _missing_ep_series_path) y se actualiza sola en cuanto la
        encuentra (o dice que no la encontró). *token* evita pisar la
        etiqueta si el usuario ya pulsó otra serie mientras tanto (mismo
        patrón que _missing_ep_poster_token para el póster)."""
        ruta = self._missing_ep_series_path(r, use_cache_only=True)
        if ruta:
            self._missing_ep_path_lbl.configure(text=f"📁 {ruta}", text_color=PENDING_COLOR)
            return

        self._missing_ep_path_lbl.configure(text="📁 Buscando la carpeta en el FTP...", text_color=PENDING_COLOR)

        def worker():
            from core.ftp_client import FTPClient
            own_ftp = FTPClient()
            try:
                own_ftp.connect(
                    self.config_data.get("ftp_host", ""),
                    int(self.config_data.get("ftp_port", 21)),
                    self.config_data.get("ftp_user", ""),
                    self.config_data.get("ftp_password", ""),
                    self.config_data.get("ftp_use_tls", False))
                found_ruta = self._missing_ep_series_path(r, use_cache_only=False, ftp_conn=own_ftp)
            except Exception:
                found_ruta = ""
            finally:
                own_ftp.disconnect()
            self.after(0, lambda: self._apply_missing_ep_path_label(token, found_ruta))
        threading.Thread(target=worker, daemon=True).start()

    def _apply_missing_ep_path_label(self, token, ruta: str):
        if self._missing_ep_poster_token is not token:
            return   # el usuario ya pulsó otra serie mientras se buscaba -- no pisar su etiqueta
        if ruta:
            self._missing_ep_path_lbl.configure(text=f"📁 {ruta}", text_color=PENDING_COLOR)
        else:
            self._missing_ep_path_lbl.configure(text="📁 No se encontró la carpeta en el FTP",
                                                text_color=WARNING_COLOR)

    def _render_missing_ep_show_links(self, r: dict):
        """Botones de los enlaces personalizables (Ajustes) a nivel serie
        -- sin episodio concreto, así que {temporada}/{episodio}/{titulo}
        quedan vacíos si la plantilla los menciona (ver
        core.custom_links.build_link_url)."""
        for w in self._missing_ep_links_frame.winfo_children():
            w.destroy()
        links = self.config_data.get("custom_links_show", [])
        if not links:
            return
        variables = {"serie": r["name"], "tmdb_id": r["tmdb_id"]}
        for link in links:
            template = link.get("url_template", "")
            if not template:
                continue
            ctk.CTkButton(
                self._missing_ep_links_frame, text=link.get("name", "Enlace"),
                fg_color="transparent", border_width=1,
                command=lambda t=template, v=variables, bg=link.get("background", False):
                self._resolve_missing_ep_ruta_and_open(t, v, r, bg)
            ).pack(fill="x", pady=2)

    def _resolve_missing_ep_ruta_and_open(self, template: str, base_variables: dict, r: dict,
                                          background: bool = False):
        """Abre (o dispara en segundo plano, ver _open_custom_link) un
        enlace personalizable que puede usar {ruta} -- si la plantilla no
        la menciona, se abre al momento sin tocar el FTP para nada. Si la
        menciona, primero prueba la caché ya existente (rápido); si esa
        carpeta todavía no está en caché, conecta de verdad al FTP en un
        hilo aparte -- pulsar un botón sí justifica esperar un poco a una
        conexión real, a diferencia de solo pintar la tabla (por eso
        _missing_ep_series_path usa caché-solo ahí)."""
        if "{ruta}" not in template:
            self._open_custom_link(template, base_variables, background)
            return

        ruta = self._missing_ep_series_path(r, use_cache_only=True)
        if ruta:
            self._open_custom_link(template, dict(base_variables, ruta=ruta), background)
            return

        self._set_status("Buscando la carpeta de la serie en el FTP...", PENDING_COLOR)

        def worker():
            from core.ftp_client import FTPClient
            own_ftp = FTPClient()
            try:
                own_ftp.connect(
                    self.config_data.get("ftp_host", ""),
                    int(self.config_data.get("ftp_port", 21)),
                    self.config_data.get("ftp_user", ""),
                    self.config_data.get("ftp_password", ""),
                    self.config_data.get("ftp_use_tls", False))
                found_ruta = self._missing_ep_series_path(r, use_cache_only=False, ftp_conn=own_ftp)
            except Exception:
                found_ruta = ""
            finally:
                own_ftp.disconnect()
            self.after(0, lambda: self._finish_missing_ep_ruta_open(
                template, base_variables, found_ruta, background))
        threading.Thread(target=worker, daemon=True).start()

    def _finish_missing_ep_ruta_open(self, template: str, base_variables: dict, ruta: str,
                                     background: bool = False):
        if not ruta:
            self._set_status("No se encontró la carpeta de la serie en el FTP", WARNING_COLOR)
        else:
            self._set_status("Carpeta encontrada", SUCCESS_COLOR)
        self._open_custom_link(template, dict(base_variables, ruta=ruta), background)

    def _open_custom_link(self, template: str, variables: dict, background: bool = False):
        """Por defecto abre la URL en el navegador. Si el botón está
        marcado como "en segundo plano" (Ajustes), en vez de abrir una
        pestaña se hace una petición GET silenciosa en un hilo aparte --
        útil para botones que disparan un webhook o una búsqueda en otra
        herramienta (Sonarr/Radarr, etc.) sin necesitar ver nada, sin
        interrumpir lo que se esté haciendo con una ventana nueva."""
        from core.custom_links import build_link_url
        url = build_link_url(template, variables)
        if not url:
            return
        if background:
            self._set_status(f"Ejecutando en segundo plano: {url}", PENDING_COLOR)
            threading.Thread(target=self._fire_background_link, args=(url,), daemon=True).start()
            return
        import webbrowser
        webbrowser.open(url)

    def _fire_background_link(self, url: str):
        try:
            resp = requests.get(url, timeout=10)
            ok = resp.status_code < 400
        except Exception as e:
            # Python borra "e" al salir del except -- sin capturarlo en un
            # argumento por defecto, la lambda diferida (self.after) lo
            # referenciaría ya borrado y lanzaría NameError al dispararse,
            # en vez de mostrar el error real.
            msg = str(e)
            self.after(0, lambda m=msg: self._set_status(f"Enlace en segundo plano falló: {m}", ERROR_COLOR))
            return
        if ok:
            self.after(0, lambda: self._set_status("✓ Enlace en segundo plano completado", SUCCESS_COLOR))
        else:
            self.after(0, lambda: self._set_status(
                f"Enlace en segundo plano devolvió {resp.status_code}", WARNING_COLOR))

    def _ask_ai_about_current_missing_ep_show(self):
        """Botón por serie (no uno general): pregunta a Groq solo por la
        serie que se está viendo ahora mismo en el panel lateral. Si
        "Ocultar sin doblaje ES" está activo, esta consulta manual TAMBIÉN
        le pregunta por el doblaje castellano (check_spanish_dub) -- es el
        ÚNICO sitio de toda la app donde se le pregunta esto a la IA (nunca
        automáticamente, ver _ask_ai_for_missing_ep_verdicts); su veredicto
        sustituye al chequeo de TMDB para esta serie en
        _visible_missing_ep_row.

        A diferencia de la consulta por lotes (_ask_ai_for_missing_ep_verdicts,
        que solo manda recuentos de episodios para mantener el coste bajo con
        muchas series a la vez), aquí se pide primero get_tv_details() --
        una sola llamada extra, aceptable porque esto es un botón manual
        para UNA serie -- para mandarle también el año de emisión y el
        título original: sin esto, la IA solo tenía el nombre en español
        para identificar la serie y su época, que es justo el tipo de dato
        que más ayuda a acertar con el doblaje castellano (series antiguas
        o con doblaje interrumpido).

        Si check_spanish_dub, también se busca la serie en eldoblaje.com
        (ver core/eldoblaje.py) y se manda su texto real como
        "info_doblaje_eldoblaje" -- probado en vivo: sin este texto, hasta
        el modelo más fiable puede quedarse sin datos reales para
        responder; con él, extrae el corte correcto de verdad (confirmado
        con Bleach). Si la búsqueda no encuentra nada, sigue funcionando
        igual que antes -- nunca bloquea la consulta a la IA.

        También se manda "ftp_filenames" -- los nombres de archivo REALES
        de esta serie en el FTP, no solo los números de missing/present ya
        calculados -- para que la IA pueda ver de primera mano cosas que un
        simple recuento no distingue, como un episodio doble empaquetado
        en un mismo archivo ("7x21-7x22"). detect_episode() ya reconoce
        estos casos y los añade a "presentes" durante el propio escaneo
        (ver _build_ftp_episode_index/_cross_check_results_with_ftp), así
        que esto es un refuerzo/red de seguridad para lo que ese regex no
        cubra, no la única vía. Con una conexión FTP propia (nunca
        self.ftp, mismo motivo que _missing_ep_series_path); si falla o no
        hay servidor configurado, sigue funcionando igual que antes."""
        r = self._missing_ep_current_row
        api_key = self.config_data.get("ai_api_key", "")
        if not r or not api_key:
            return
        self._missing_ep_ai_verdict_lbl.configure(text="🤖 Preguntando a la IA...")
        check_spanish_dub = self._missing_ep_hide_no_dub_var.get()
        tmdb_id = r["tmdb_id"]

        def worker():
            try:
                details = self.tmdb.get_tv_details(tmdb_id)
            except Exception:
                details = {}
            # present_episodes = expected - missing, calculado aquí sin
            # ninguna llamada extra (ambas listas ya están en la fila desde
            # el escaneo) -- con missing_episodes solo se ve el hueco;
            # con esto también se ve la forma de lo que SÍ hay (p.ej. si lo
            # presente empieza en el episodio 51 en vez de en el 1, eso es
            # justo la pista de un desajuste de numeración con TMDB que un
            # simple recuento no deja ver).
            present_episodes = {}
            for season, expected_eps in r.get("expected_episodes", {}).items():
                missing_eps = set(r.get("missing", {}).get(season, []))
                present = [ep for ep in expected_eps if ep not in missing_eps]
                if present:
                    present_episodes[season] = present

            info_doblaje_eldoblaje = ""
            if check_spanish_dub:
                from core.eldoblaje import search_series, get_dub_summary
                # Nombre en español primero (eldoblaje.com es un sitio
                # para audiencia española, indexa por el título comercial
                # con el que se estrenó aquí) -- si no hay resultado, se
                # prueba con el original como respaldo. Se queda con el
                # primer resultado marcado como serie -- el sitio no da
                # más señales para desambiguar que el propio orden.
                candidates = search_series(r["name"]) or search_series(details.get("original_name") or "")
                if candidates:
                    info_doblaje_eldoblaje = get_dub_summary(candidates[0]["id"])

            ftp_filenames = []
            if self.config_data.get("ftp_host", ""):
                from core.ftp_client import FTPClient as _FTPClient
                own_ftp = _FTPClient()
                try:
                    own_ftp.connect(
                        self.config_data.get("ftp_host", ""), int(self.config_data.get("ftp_port", 21)),
                        self.config_data.get("ftp_user", ""), self.config_data.get("ftp_password", ""),
                        self.config_data.get("ftp_use_tls", False))
                    if own_ftp.is_connected():
                        path = self._missing_ep_series_path(r, use_cache_only=False, ftp_conn=own_ftp)
                        if path:
                            ftp_filenames = own_ftp.list_files_recursive(path, max_depth=2)
                except Exception:
                    pass
                finally:
                    own_ftp.disconnect()

            show_payload = [{
                "tmdb_id": tmdb_id, "name": r["name"],
                "original_name": details.get("original_name") or "",
                "first_air_date": details.get("first_air_date") or "",
                "origin_country": details.get("origin_country") or [],
                "genres": [g.get("name") for g in details.get("genres", []) if g.get("name")],
                "tmdb_seasons": {str(k): v for k, v in r.get("tmdb_season_counts", {}).items()},
                "server_seasons": {str(k): v for k, v in r.get("server_season_counts", {}).items()},
                # Números concretos de episodios que faltan/que hay (no solo
                # el recuento) -- deja ver patrones que un simple recuento
                # no distingue (p.ej. "faltan los últimos 20" vs "faltan 20
                # sueltos por en medio"), útil tanto para el veredicto de
                # hueco_real/numeracion_distinta como para acotar mejor el
                # corte de doblaje.
                "missing_episodes": {str(k): v for k, v in r.get("missing", {}).items()},
                "present_episodes": {str(k): v for k, v in present_episodes.items()},
                "info_doblaje_eldoblaje": info_doblaje_eldoblaje,
                "ftp_filenames": ftp_filenames,
            }]
            from core.missing_episodes_ai import analyze_missing_episodes
            verdicts = analyze_missing_episodes(show_payload, api_key, check_spanish_dub=check_spanish_dub)
            self.after(0, lambda: self._apply_single_missing_ep_ai_verdict(r, verdicts.get(tmdb_id)))
        threading.Thread(target=worker, daemon=True).start()

    def _apply_single_missing_ep_ai_verdict(self, r: dict, verdict):
        if verdict:
            r["ai_verdict"] = verdict
            self._persist_ai_verdicts({r["tmdb_id"]: verdict})
            # Compartir con el resto de clientes del mismo servidor -- solo
            # en esta consulta manual real, nunca desde el chequeo
            # automático por lotes (ver _push_shared_dub_verdict_to_ftp).
            self._push_shared_dub_verdict_to_ftp(r["tmdb_id"], verdict)
        elif not r.get("ai_verdict"):
            self._missing_ep_ai_verdict_lbl.configure(
                text="🤖 No se pudo obtener un veredicto (revisa la API Key de Groq o la conexión).")
        if self._missing_ep_current_row is r:
            self._update_missing_ep_ai_verdict_label(r)
        self._render_missing_episodes_table(reset_page=False)

    def _load_missing_ep_poster(self, url: str, token):
        try:
            resp = requests.get(url, timeout=8)
            img = Image.open(BytesIO(resp.content)).resize((180, 260), Image.LANCZOS)
            ctk_img = ctk.CTkImage(img, size=(180, 260))
            def _apply():
                if self._missing_ep_poster_token is token:
                    self._missing_ep_poster_label.configure(image=ctk_img, text="")
                    self._missing_ep_current_poster = ctk_img
            self.after(0, _apply)
        except Exception:
            pass

    def _missing_episode_lines(self, r: dict) -> list:
        """Una entrada (temporada, episodio, título, nombre_archivo) por
        episodio que falta -- el nombre de archivo sigue la plantilla de TV
        configurada por el usuario, así se reconocen igual que verían el
        archivo ya renombrado en disco, en vez del formato interno "T1E05".
        Si no se conoce el título del episodio (huecos venidos de una
        caché antigua, o de un fallo puntual de TMDB), se deja "{titulo}"
        vacío en vez de omitirlo."""
        template = self.config_data.get("tv_template")
        episode_titles = r.get("episode_titles") or {}
        lines = []
        for season in sorted(r["missing"]):
            for ep in r["missing"][season]:
                title = episode_titles.get(season, {}).get(ep, "")
                info = MediaInfo(tmdb_id=r["tmdb_id"], media_type="tv", title=r["name"],
                                 original_title=r["name"], year="", season=season,
                                 episode=ep, episode_title=title)
                try:
                    name = build_new_name(info, template, ext=".mkv")
                except ValueError:
                    name = f"T{season}E{ep:02d}"
                else:
                    name = name.rsplit(".", 1)[0]   # quitar la extension ficticia -- no es un archivo real
                lines.append((season, ep, title, name))
        return lines

    def _toggle_missing_ep_ignore(self, tmdb_id, ignored: bool):
        self._set_missing_episode_ignored(tmdb_id, ignored)
        for r in self._missing_ep_results:
            if r["tmdb_id"] == tmdb_id:
                r["ignored"] = ignored
        self._update_missing_ep_status_text()
        self._render_missing_episodes_table(reset_page=False)

    # ── Borrar serie desde Episodios que faltan -- mismo diálogo de
    # confirmación que Liberar espacio (_ConfirmDeleteDialog), la acción
    # más peligrosa de la app. A diferencia de Liberar espacio, aquí no se
    # conoce de antemano la ruta real en el FTP (esta pantalla nunca la
    # necesitó hasta ahora) -- hay que resolverla primero (mismo
    # emparejamiento por nombre que ya usa la subida, ver
    # _find_category_with_existing_folder) antes de poder mostrar el
    # diálogo con una ruta de verdad.

    def _rescan_single_missing_ep_series(self, r: dict):
        """Reescanea una sola serie contra TMDB/servidor -- para cuando el
        usuario quiere forzar una recomprobación puntual (p.ej. tras
        rellenar un hueco a mano en el FTP) sin lanzar un reescaneo
        completo de todo el catálogo.

        A propósito NO usa _scan_missing_episodes: su bucle principal se
        puede limitar a una sola serie, pero antes de llegar a él esa
        función siempre pide la lista completa de series Y las
        estadísticas de uso (play_count/last_played, para la puntuación de
        "Tendencia") de TODO el catálogo de Jellyfin/Plex -- visto de
        verdad: get_jellyfin_usage_stats tardó 66s y get_plex_usage_stats
        26s con la biblioteca real del usuario, sobre datos que ni
        siquiera hacían falta para el hueco de UNA serie. Con
        r["source"]/r["server_id"] (ya conocidos de cuando se generó esta
        fila) se puede ir directo a por esa única serie sin ninguna de las
        dos llamadas -- el resto (r["play_count"]/r["last_played_ts"]) se
        conserva tal cual de la fila anterior en vez de re-consultarlo."""
        tmdb_id = r["tmdb_id"]
        name = r["name"]
        if self._missing_ep_scanning:
            self._set_status("Espera a que termine el escaneo en curso", WARNING_COLOR)
            return
        if self._upload_running:
            self._set_status("Espera a que termine la subida en curso antes de comprobar huecos", WARNING_COLOR)
            return
        widgets = self._missing_ep_row_widgets.get(tmdb_id)
        if widgets and widgets.get("rescan_btn"):
            widgets["rescan_btn"].configure(state="disabled", text="…")
        self._set_status(f"Reescaneando \"{name}\"...", PENDING_COLOR)

        def worker():
            try:
                results = self._rescan_single_series_worker(r)
            except Exception:
                _log.exception("Reescaneo de '%s' (tmdb_id=%s): fallo inesperado", name, tmdb_id)
                self.after(0, lambda: self._set_status(
                    f"Error al reescanear \"{name}\" -- revisa app.log", ERROR_COLOR))
                self.after(0, lambda: self._finish_single_missing_ep_rescan(tmdb_id, name, None))
                return
            self.after(0, lambda: self._finish_single_missing_ep_rescan(tmdb_id, name, results))
        threading.Thread(target=worker, daemon=True).start()

    def _rescan_single_series_worker(self, r: dict) -> list:
        """Hilo de fondo de _rescan_single_missing_ep_series -- misma
        lógica que el bucle principal de _scan_missing_episodes para UNA
        serie (recomprobación siempre forzada, como un hueco visto antes),
        más el cruce con el FTP acotado a su propia carpeta (ver
        _cross_check_single_result_with_ftp). Devuelve una lista con 0 o 1
        filas, mismo contrato que espera _finish_single_missing_ep_rescan."""
        from core.media_server_refresh import (get_jellyfin_episodes, get_plex_episodes,
                                               get_jellyfin_series_item, get_plex_series_item)
        from core.missing_episodes import (find_missing_episodes, format_missing_summary,
                                           apply_season_split_filter, find_unknown_seasons,
                                           looks_like_absolute_numbering, remap_absolute_episodes)
        from core.missing_episodes_cache import load_cache, save_cache

        old_tmdb_id = r["tmdb_id"]
        tmdb_id = old_tmdb_id
        name = r["name"]
        source = r.get("source")
        server_id = r.get("server_id")
        folder_name = r.get("folder_name")
        if not source or not server_id:
            # Fila de antes de que estos campos se guardaran en caché --
            # sin ellos no hay atajo posible, hace falta un reescaneo
            # completo (que sí vuelve a traer server_id) para poder
            # reescanear esta serie sola la próxima vez.
            self.after(0, lambda: self._set_status(
                f"\"{name}\" necesita un reescaneo completo antes de poder reescanearse sola", WARNING_COLOR))
            return [r]

        # Releer la ficha ACTUAL de esta serie en el servidor (no solo sus
        # episodios) -- si el usuario corrigió a mano la identificación en
        # Jellyfin/Plex (cambió a qué ficha de TMDB apunta), la fila
        # todavía tenía el tmdb_id VIEJO guardado en caché de la última
        # vez que se pidió la biblioteca entera, y "reescanear esta serie"
        # seguiría comparando huecos contra la ficha equivocada aunque el
        # servidor ya estuviera corregido.
        if source == "jellyfin":
            item = get_jellyfin_series_item(self.config_data.get("jellyfin_host", ""),
                                            self.config_data.get("jellyfin_api_key", ""), server_id)
        else:
            item = get_plex_series_item(self.config_data.get("plex_host", ""),
                                        self.config_data.get("plex_token", ""), server_id)
        if item and item.get("tmdb_id"):
            if item["tmdb_id"] != old_tmdb_id:
                _log.info("Reescaneo individual: '%s' cambió de tmdb_id %s -> %s (identificación "
                          "corregida en el servidor)", item.get("name") or name, old_tmdb_id, item["tmdb_id"])
            tmdb_id = item["tmdb_id"]
            name = item.get("name") or name
            folder_name = item.get("folder_name", folder_name)   # Plex no lo trae, se conserva el de antes

        details = self.tmdb.get_tv_details(tmdb_id)
        if source == "jellyfin":
            present = get_jellyfin_episodes(self.config_data.get("jellyfin_host", ""),
                                            self.config_data.get("jellyfin_api_key", ""), server_id)
        else:
            present = get_plex_episodes(self.config_data.get("plex_host", ""),
                                        self.config_data.get("plex_token", ""), server_id)
        if present is None:
            return [r]   # sin dato fiable ahora mismo -- se deja la fila tal cual

        expected = {}
        episode_titles = {}
        for season in details.get("seasons", []):
            n = season.get("season_number", 0)
            if n <= 0:   # temporada 0 = especiales, no cuenta como hueco
                continue
            try:
                eps = self.tmdb.get_season_episodes(tmdb_id, n)
            except Exception:
                continue
            nums = [e["episode_number"] for e in eps if e.get("episode_number")]
            if nums:
                expected[n] = nums
                episode_titles[n] = {e["episode_number"]: e.get("name", "") for e in eps
                                     if e.get("episode_number")}

        season_episode_counts = {s: len(eps) for s, eps in expected.items()}
        absolute_numbering = looks_like_absolute_numbering(season_episode_counts, present)
        present_for_compare = remap_absolute_episodes(season_episode_counts, present) \
            if absolute_numbering else present

        missing = find_missing_episodes(expected, present_for_compare)
        unknown_seasons = find_unknown_seasons(present_for_compare, expected.keys())
        present_season_counts = {}
        for season, _ep in present_for_compare:
            present_season_counts[season] = present_season_counts.get(season, 0) + 1

        cache = dict(load_cache())
        if tmdb_id != old_tmdb_id:
            cache.pop(str(old_tmdb_id), None)   # identificación corregida -- no dejar la entrada vieja huérfana
        cache_entry = {
            "name": name, "source": source, "server_id": server_id,
            "folder_name": folder_name,
            "last_episode_id": (details.get("last_episode_to_air") or {}).get("id"),
            "expected": {str(k): v for k, v in expected.items()},
            "episode_titles": {str(s): {str(e): t for e, t in eps.items()}
                               for s, eps in episode_titles.items()},
            "missing": {str(k): v for k, v in missing.items()},
            "unknown_seasons": sorted(unknown_seasons),
            "present_season_counts": {str(k): v for k, v in present_season_counts.items()},
            "absolute_numbering": absolute_numbering,
            "ignored": r.get("ignored", False),
            "play_count": r.get("play_count", 0),
            "last_played_ts": r.get("last_played_ts"),
            # Se conserva el veredicto de la IA que ya tenía esta fila --
            # salvo que la identificación se acabara de corregir (tmdb_id
            # distinto), en cuyo caso el veredicto viejo era sobre OTRA
            # ficha y no debe arrastrarse.
            "ai_verdict": r.get("ai_verdict") if tmdb_id == old_tmdb_id else None,
        }
        cache[str(tmdb_id)] = cache_entry

        if not (missing or unknown_seasons):
            save_cache(cache)
            return []   # ya no le falta nada -- se quita de la lista

        # missing aquí es SOLO para mostrar -- cache_entry["missing"] se
        # corrige más abajo, tras el cruce con el FTP, antes de guardar.
        missing, split_seasons = apply_season_split_filter(missing, expected)
        new_row = {
            "tmdb_id": tmdb_id, "name": name, "source": source, "server_id": server_id,
            "missing": missing, "summary": format_missing_summary(name, missing),
            "ignored": r.get("ignored", False), "episode_titles": episode_titles,
            "split_seasons": split_seasons,
            "unknown_seasons": unknown_seasons,
            "play_count": r.get("play_count", 0), "last_played_ts": r.get("last_played_ts"),
            "expected_episodes": {s: sorted(eps) for s, eps in expected.items()},
            "tmdb_season_counts": {s: len(eps) for s, eps in expected.items()},
            "server_season_counts": present_season_counts,
            "ai_verdict": r.get("ai_verdict") if tmdb_id == old_tmdb_id else None,
            "absolute_numbering": absolute_numbering,
            "folder_name": folder_name,
        }
        new_row = self._cross_check_single_result_with_ftp(new_row)
        # Propagar la corrección del cruce FTP a la caché de disco -- antes
        # se guardaba (arriba) con el hueco de ANTES del cruce, así que una
        # corrección real (ej. Dragon Ball GT: 3 episodios que sí estaban
        # en el servidor) solo vivía en esta sesión y volvía a aparecer tal
        # cual en el próximo arranque, mismo bug que en _scan_missing_episodes.
        cache_entry["missing"] = {str(k): v for k, v in new_row["missing"].items()}
        cache_entry["unknown_seasons"] = sorted(new_row.get("unknown_seasons") or [])
        cache_entry["present_season_counts"] = {
            str(k): v for k, v in (new_row.get("server_season_counts") or {}).items()}
        save_cache(cache)

        if not (new_row["missing"] or new_row["unknown_seasons"]):
            return []
        return [new_row]

    def _finish_single_missing_ep_rescan(self, tmdb_id: int, name: str, results):
        """results es None si el reescaneo falló (se deja la fila tal cual
        estaba); si no, 0 o 1 filas -- se sustituye la fila vieja de esta
        serie (si había) por el resultado nuevo, o se quita de la lista si
        ya no le falta nada. El tmdb_id del resultado puede ser DISTINTO
        al que se pasó aquí -- ver _rescan_single_series_worker, releer
        la ficha corrige una identificación equivocada -- así que también
        se quita cualquier fila que ya hubiera con el tmdb_id nuevo, para
        no acabar con dos filas de la misma serie."""
        if results is not None:
            new_ids = {row["tmdb_id"] for row in results} | {tmdb_id}
            self._missing_ep_results = [r for r in self._missing_ep_results if r["tmdb_id"] not in new_ids]
            self._missing_ep_results.extend(results)
            if results:
                self._set_status(f"\"{name}\" actualizada", SUCCESS_COLOR)
            else:
                self._set_status(f"\"{name}\" ya no tiene huecos -- se quitó de la lista", SUCCESS_COLOR)
            self._render_missing_episodes_table(reset_page=False)
        else:
            widgets = self._missing_ep_row_widgets.get(tmdb_id)
            if widgets and widgets.get("rescan_btn"):
                widgets["rescan_btn"].configure(state="normal", text="🔄")

    def _confirm_delete_missing_ep_series(self, r: dict):
        if not self.config_data.get("ftp_host", ""):
            self._set_status("Configura la conexión FTP en Ajustes para poder borrar series", WARNING_COLOR)
            return
        self._set_status(f"Buscando \"{r['name']}\" en el servidor...", PENDING_COLOR)
        threading.Thread(target=self._resolve_missing_ep_series_path, args=(r,), daemon=True).start()

    def _resolve_missing_ep_series_path(self, r: dict):
        import types
        from core.ftp_client import FTPClient
        own_ftp = FTPClient()
        ok, msg = own_ftp.connect(
            self.config_data.get("ftp_host", ""), int(self.config_data.get("ftp_port", 21)),
            self.config_data.get("ftp_user", ""), self.config_data.get("ftp_password", ""),
            self.config_data.get("ftp_use_tls", False))
        if not ok:
            self.after(0, lambda: self._set_status(f"No se pudo conectar al FTP: {msg}", ERROR_COLOR))
            return
        try:
            known_folder = r.get("folder_name")
            info = types.SimpleNamespace(title=r["name"], media_type="tv", folder_name=known_folder)
            cat, folder_name = self._find_category_with_existing_folder(own_ftp, info, force_refresh=True)
            if not folder_name:
                # Diagnóstico para la próxima vez que esto falle: sin esto,
                # "no encontrado" no dice si es que r["folder_name"] estaba
                # vacío (Jellyfin no lo dio, o la fila viene de Plex, que
                # todavía no lo trae), o si estando presente el listado del
                # FTP no lo encontró de todas formas -- dos causas muy
                # distintas que un mismo mensaje en pantalla no distingue.
                cats_checked = [c.get("root", "") for c in
                               self.config_data.get("ftp_categories", {"tv": [], "movie": []}).get("tv", [])]
                _log.warning(
                    "Borrar serie: '%s' (tmdb_id=%s, source=%s) no encontrada -- "
                    "folder_name conocido=%r, categorías comprobadas=%s",
                    r["name"], r.get("tmdb_id"), r.get("source"), known_folder, cats_checked)
                self.after(0, lambda: self._set_status(
                    f"No se encontró \"{r['name']}\" en el servidor -- no se puede borrar", ERROR_COLOR))
                return
            # .rstrip("/"): algunas categorías guardan la raíz con barra
            # final (p.ej. "/datos2/series/") -- sin quitarla, la ruta
            # quedaba con doble barra ("/datos2/series//Accused"). La
            # mayoría de servidores FTP la toleran igual, pero mejor no
            # confiar en eso para una ruta que se va a usar para borrar.
            ftp_path = f"{cat.get('root', '').rstrip('/')}/{folder_name}"
            size_bytes = own_ftp.get_folder_size(ftp_path)
        finally:
            own_ftp.disconnect()
        self.after(0, lambda: self._show_delete_missing_ep_series_dialog(r, ftp_path, size_bytes))

    def _show_delete_missing_ep_series_dialog(self, r: dict, ftp_path: str, size_bytes: int):
        self._set_status("", PENDING_COLOR)
        dlg = _ConfirmDeleteDialog(self, r["name"], ftp_path, size_bytes,
                                    "Borrado manual desde Episodios que faltan")
        if not dlg.result:
            return
        threading.Thread(target=self._delete_missing_ep_series_worker,
                         args=(r, ftp_path, size_bytes), daemon=True).start()

    def _delete_missing_ep_series_worker(self, r: dict, ftp_path: str, size_bytes: int):
        from core.ftp_client import FTPClient
        own_ftp = FTPClient()
        ok, msg = own_ftp.connect(
            self.config_data.get("ftp_host", ""), int(self.config_data.get("ftp_port", 21)),
            self.config_data.get("ftp_user", ""), self.config_data.get("ftp_password", ""),
            self.config_data.get("ftp_use_tls", False))
        if ok:
            ok, msg = own_ftp.delete_folder_recursive(ftp_path)
            own_ftp.disconnect()
        self._save_deletion_history_entry(
            name=r["name"], ftp_path=ftp_path, size_bytes=size_bytes,
            reason="Borrado manual desde Episodios que faltan",
            status="ok" if ok else "error", error_msg="" if ok else msg)
        self.after(0, lambda: self._finish_delete_missing_ep_series(r, ok, msg))

    def _finish_delete_missing_ep_series(self, r: dict, ok: bool, msg: str):
        if ok:
            self._set_status(f"Eliminado: {r['name']}", SUCCESS_COLOR)
            self._refresh_ftp_space()   # el borrado cambia el espacio libre real
            self._remove_series_from_missing_episodes(r["tmdb_id"])
        else:
            self._set_status(f"No se pudo eliminar {r['name']}: {msg}", ERROR_COLOR)

    def _scan_missing_episodes(self, progress_cb=None, cancel_event=None, force_full=False,
                               on_result_cb=None) -> list:
        """Recorre las series de Plex/Jellyfin que tengan activados, y para
        cada una compara la lista completa de episodios de TMDB con lo que
        de verdad hay -- devuelve una lista de dicts (uno por serie con
        algún hueco): {"tmdb_id", "name", "source", "missing", "summary",
        "ignored"}, pensada para alimentar la tabla de _MissingEpisodesTab.

        Incremental por caché (core/missing_episodes_cache.py): el primer
        escaneo de una serie es caro (una llamada a TMDB por temporada), así
        que en los siguientes solo se repite ese trabajo si TMDB indica que
        ha salido un episodio nuevo desde la última vez (comparando
        last_episode_to_air) -- o si la última vez le faltaban episodios,
        por si mientras tanto se rellenó el hueco a mano; sin ninguna de las
        dos cosas, la serie se salta entera. Las series que ya no existen
        en el servidor se quitan de la caché (por si se borraron).
        force_full=True ignora la caché y repite el trabajo completo para
        todas -- pensado para el botón "Reescaneo completo".
        on_result_cb(row), si se pasa, se llama cada vez que se añade una
        fila a resultados (según se van encontrando, no al final) -- lo
        usa "Reescaneo completo" para que la tabla se vaya rellenando en
        vivo en vez de esperar a tener todo el resultado.

        Para reescanear UNA sola serie (botón por fila) esta función NO se
        usa -- ver _rescan_single_series_worker: incluso limitando el
        bucle principal a una sola serie, esta función sigue pidiendo
        ANTES la lista completa de shows y las estadísticas de uso de todo
        el catálogo (get_jellyfin_usage_stats/get_plex_usage_stats), que
        con una biblioteca real tardaron 66s/26s por sí solas -- inútil
        para el hueco de una sola serie."""
        from core.media_server_refresh import (get_jellyfin_series, get_jellyfin_episodes,
                                                get_plex_series, get_plex_episodes,
                                                get_jellyfin_usage_stats, get_plex_usage_stats,
                                                parse_media_date)
        from core.missing_episodes import (find_missing_episodes, format_missing_summary,
                                           apply_season_split_filter, find_unknown_seasons,
                                           looks_like_absolute_numbering, remap_absolute_episodes)
        from core.missing_episodes_cache import load_cache, save_cache
        from core.cleanup_candidates import merge_usage_entries

        cache = dict(load_cache())

        # Si la misma serie está en Jellyfin Y en Plex (bibliotecas
        # espejadas, algo común), aparecía en las dos listas y se procesaba
        # dos veces -- una fila duplicada (o triplicada, si además pasaba
        # algo raro con la caché) por serie. Cada tmdb_id se queda con la
        # primera fuente en la que aparece; el orden Jellyfin->Plex es
        # arbitrario pero determinista.
        shows = []
        seen_tmdb_ids = set()

        def _add_shows(source, shows_list):
            for s in shows_list or []:
                tmdb_id = s.get("tmdb_id")
                if tmdb_id and tmdb_id in seen_tmdb_ids:
                    continue
                if tmdb_id:
                    seen_tmdb_ids.add(tmdb_id)
                shows.append((source, s))

        if self.config_data.get("jellyfin_enabled"):
            _add_shows("jellyfin", get_jellyfin_series(
                self.config_data.get("jellyfin_host", ""), self.config_data.get("jellyfin_api_key", "")))
        if self.config_data.get("plex_enabled"):
            _add_shows("plex", get_plex_series(
                self.config_data.get("plex_host", ""), self.config_data.get("plex_token", "")))

        # Datos de visionado (veces reproducida, última vez) para la
        # puntuación de tendencia -- una sola llamada por servidor (igual
        # que "Liberar espacio"), correlacionado por tmdb_id: a diferencia
        # de Liberar espacio (que solo tiene nombres de carpeta FTP y
        # necesita reconciliar por nombre), esta lista de series ya trae
        # el tmdb_id emparejado, así que no hace falta esa indirección.
        usage_by_tmdb_id = {}

        def _merge_usage_by_tmdb(entry):
            tid = entry.get("tmdb_id")
            if not tid:
                return
            existing = usage_by_tmdb_id.get(tid)
            usage_by_tmdb_id[tid] = merge_usage_entries(existing, entry) if existing else entry

        if self.config_data.get("jellyfin_enabled"):
            for entry in (get_jellyfin_usage_stats(
                    self.config_data.get("jellyfin_host", ""), self.config_data.get("jellyfin_api_key", ""),
                    username=self.config_data.get("jellyfin_username", "")) or {}).values():
                _merge_usage_by_tmdb(entry)
        if self.config_data.get("plex_enabled"):
            for entry in (get_plex_usage_stats(
                    self.config_data.get("plex_host", ""), self.config_data.get("plex_token", "")) or {}).values():
                _merge_usage_by_tmdb(entry)

        # Quitar de la caché las series que ya no están en el servidor
        # (se borraron, o se desactivó esa fuente) -- si no, un hueco viejo
        # de una serie eliminada seguiría apareciendo para siempre.
        current_keys = {str(s.get("tmdb_id")) for _, s in shows if s.get("tmdb_id")}
        for stale_key in [k for k in cache if k not in current_keys and k != "_meta"]:
            del cache[stale_key]

        def _row(tmdb_id, name, source, missing, ignored, expected=None, unknown_seasons=None,
                 present_season_counts=None, server_id=None, folder_name=None, ai_verdict=None):
            # missing aquí es SOLO para mostrar -- la caché en disco (más
            # abajo, cache[key] = {...}) guarda el hueco SIN filtrar, ver
            # apply_season_split_filter.
            missing, split_seasons = apply_season_split_filter(missing, expected or {})
            # Para la puntuación de tendencia (ver core/trending.py) --
            # parse_media_date() ya es idempotente tanto si la entrada viene
            # de una sola fuente (fecha cruda, string ISO8601 o epoch de
            # Plex) como fusionada de las dos (ya convertida a epoch por
            # merge_usage_entries), así que se puede llamar siempre igual.
            usage = usage_by_tmdb_id.get(tmdb_id) or {}
            return {
                "tmdb_id": tmdb_id, "name": name, "source": source, "server_id": server_id,
                "missing": missing, "summary": format_missing_summary(name, missing),
                "ignored": ignored, "episode_titles": {}, "split_seasons": split_seasons,
                "unknown_seasons": unknown_seasons or set(),
                "play_count": usage.get("play_count", 0),
                "last_played_ts": parse_media_date(usage.get("last_played")),
                # Lista real de episodios que debería tener cada temporada
                # según TMDB -- la usa _cross_check_results_with_ftp para
                # recalcular el hueco tras cruzar con el FTP, sin tener que
                # asumir que los números son consecutivos desde el 1.
                "expected_episodes": {s: sorted(eps) for s, eps in (expected or {}).items()},
                # Recuentos por temporada (no las listas de episodios enteras)
                # -- lo que se manda a Groq para el veredicto por lotes, ver
                # core/missing_episodes_ai.py.
                "tmdb_season_counts": {s: len(eps) for s, eps in (expected or {}).items()},
                "server_season_counts": dict(present_season_counts or {}),
                "ai_verdict": ai_verdict,   # {"veredicto", "motivo"} tras preguntar a la IA (persistido, ver _persist_ai_verdicts)
                "absolute_numbering": False,   # se sobreescribe fuera si se detecta (ver looks_like_absolute_numbering)
                # Nombre REAL de la carpeta en el servidor de medios (ver
                # get_jellyfin_series) -- puede no parecerse al nombre
                # mostrado (traducido). Solo lo trae Jellyfin por ahora
                # (Plex necesitaría una llamada aparte por serie); None si
                # no se pudo determinar. Usado para encontrar la carpeta
                # de verdad al borrar (ver _resolve_missing_ep_series_path)
                # sin depender solo del parecido difuso de nombres.
                "folder_name": folder_name,
            }

        results = []
        total = len(shows)
        for i, (source, show) in enumerate(shows):
            if cancel_event and cancel_event.is_set():
                break
            if progress_cb:
                progress_cb(i + 1, total, show.get("name", ""))
            tmdb_id = show.get("tmdb_id")
            if not tmdb_id:
                continue   # esa serie no tiene el ID de TMDB emparejado -- no hay con que comparar
            key = str(tmdb_id)
            cached = cache.get(key)
            ignored = bool(cached and cached.get("ignored", False))

            try:
                details = self.tmdb.get_tv_details(tmdb_id)
            except Exception:
                # Sin conexión con TMDB ahora mismo -- si había un hueco
                # conocido de un escaneo anterior, se sigue mostrando en
                # vez de perderlo por un fallo puntual de red.
                if cached and cached.get("missing"):
                    cached_expected = {int(k): v for k, v in cached.get("expected", {}).items()}
                    cached_counts = {int(k): v for k, v in cached.get("present_season_counts", {}).items()}
                    row = _row(tmdb_id, show.get("name", ""), source,
                              {int(k): v for k, v in cached["missing"].items()}, ignored,
                              expected=cached_expected,
                              unknown_seasons=set(cached.get("unknown_seasons", [])),
                              present_season_counts=cached_counts,
                              folder_name=show.get("folder_name"),
                              ai_verdict=self._ai_verdict_from_cache_entry(cached))
                    row["episode_titles"] = {int(s): {int(e): t for e, t in eps.items()}
                                             for s, eps in cached.get("episode_titles", {}).items()}
                    row["absolute_numbering"] = cached.get("absolute_numbering", False)
                    results.append(row)
                    if on_result_cb:
                        on_result_cb(row)
                continue

            last_episode_id = (details.get("last_episode_to_air") or {}).get("id")
            had_gaps_before = bool(cached and cached.get("missing"))
            needs_full_recheck = (force_full or not cached
                                  or cached.get("last_episode_id") != last_episode_id)

            if not needs_full_recheck and not had_gaps_before:
                continue   # sin episodios nuevos en TMDB y ya estaba completa -- nada que comprobar

            # Presencia real: se re-consulta si hay novedades en TMDB o si
            # la última vez le faltaban episodios (por si se rellenó a mano).
            if source == "jellyfin":
                present = get_jellyfin_episodes(self.config_data.get("jellyfin_host", ""),
                                                 self.config_data.get("jellyfin_api_key", ""), show["id"])
            else:
                present = get_plex_episodes(self.config_data.get("plex_host", ""),
                                             self.config_data.get("plex_token", ""), show["rating_key"])
            # None = falló la consulta (sin red, servidor caído...) -- eso sí
            # se salta, no hay dato fiable. Un set() VACÍO es una respuesta
            # válida ("Jellyfin/Plex no tiene indexado nada de esta serie")
            # y tiene que seguir adelante -- si no, un fallo de indexado
            # como el de Desencanto (Jellyfin decía "0 episodios" con la
            # carpeta llena en el FTP) nunca llegaba ni a compararse con
            # TMDB, y mucho menos a cruzarse con el FTP después.
            if present is None:
                continue

            if needs_full_recheck:
                expected = {}
                episode_titles = {}
                for season in details.get("seasons", []):
                    n = season.get("season_number", 0)
                    if n <= 0:   # temporada 0 = especiales, no cuenta como hueco
                        continue
                    try:
                        eps = self.tmdb.get_season_episodes(tmdb_id, n)
                    except Exception:
                        continue
                    nums = [e["episode_number"] for e in eps if e.get("episode_number")]
                    if nums:
                        expected[n] = nums
                        episode_titles[n] = {e["episode_number"]: e.get("name", "") for e in eps
                                             if e.get("episode_number")}
            else:
                expected = {int(k): v for k, v in cached["expected"].items()}
                episode_titles = {int(s): {int(e): t for e, t in eps.items()}
                                  for s, eps in cached.get("episode_titles", {}).items()}

            # Anime de muchos episodios (Naruto Shippuden y similares) suele
            # organizarse con numeración absoluta (episodio 262 de corrido)
            # en vez de reiniciar por temporada -- comparado tal cual contra
            # TMDB (que sí numera por temporada) da un falso "falta todo".
            # Si se detecta el patrón, se convierte antes de comparar.
            season_episode_counts = {s: len(eps) for s, eps in expected.items()}
            absolute_numbering = looks_like_absolute_numbering(season_episode_counts, present)
            present_for_compare = remap_absolute_episodes(season_episode_counts, present) \
                if absolute_numbering else present

            missing = find_missing_episodes(expected, present_for_compare)
            unknown_seasons = find_unknown_seasons(present_for_compare, expected.keys())
            present_season_counts = {}
            for season, _ep in present_for_compare:
                present_season_counts[season] = present_season_counts.get(season, 0) + 1
            usage = usage_by_tmdb_id.get(tmdb_id) or {}
            cache[key] = {
                "name": show.get("name", ""),
                "source": source,
                "server_id": show.get("id") or show.get("rating_key"),
                "folder_name": show.get("folder_name"),
                "last_episode_id": last_episode_id,
                "expected": {str(k): v for k, v in expected.items()},
                "episode_titles": {str(s): {str(e): t for e, t in eps.items()}
                                   for s, eps in episode_titles.items()},
                "missing": {str(k): v for k, v in missing.items()},
                "unknown_seasons": sorted(unknown_seasons),
                "present_season_counts": {str(k): v for k, v in present_season_counts.items()},
                "absolute_numbering": absolute_numbering,
                "ignored": ignored,
                "play_count": usage.get("play_count", 0),
                "last_played_ts": parse_media_date(usage.get("last_played")),
                # Se conserva el veredicto de la IA de la entrada anterior
                # (si había) -- series con un hueco permanente (p.ej.
                # Bleach, sin doblaje castellano más allá del 1x109) pasan
                # needs_full_recheck=False pero had_gaps_before=True en
                # CASI todos los escaneos normales, así que sin esto el
                # veredicto se perdía en el primer "Comprobar" después de
                # preguntarle a la IA, no solo en un "Reescaneo completo"
                # -- justo lo contrario de "tiene que ser persistente".
                "ai_verdict": (cached or {}).get("ai_verdict"),
            }
            # Se muestra la fila si faltan episodios O si hay temporadas en
            # el servidor que TMDB no conoce -- esto último puede pasar
            # aunque TMDB no reporte ningún hueco (si sus datos para esta
            # serie están incompletos, "falta" lo mismo no significa nada).
            if missing or unknown_seasons:
                row = _row(tmdb_id, show.get("name", ""), source, missing, ignored,
                          expected=expected, unknown_seasons=unknown_seasons,
                          present_season_counts=present_season_counts,
                          server_id=show.get("id") or show.get("rating_key"),
                          folder_name=show.get("folder_name"),
                          ai_verdict=self._ai_verdict_from_cache_entry(cached) if cached else None)
                row["episode_titles"] = episode_titles
                row["absolute_numbering"] = absolute_numbering
                results.append(row)
                if on_result_cb:
                    on_result_cb(row)

        if results and (cancel_event is None or not cancel_event.is_set()):
            # _cross_check_results_with_ftp muta cada fila IN PLACE
            # (r["missing"] = ...) y solo al final filtra las que se
            # quedaron sin ningún hueco real de su valor de retorno -- por
            # eso se guarda la lista de ANTES del cruce (pre_cross_check,
            # mismos objetos dict, no una copia) para poder propagar la
            # corrección a `cache` también para esas, no solo a las que
            # siguen apareciendo en pantalla.
            pre_cross_check = results
            results = self._cross_check_results_with_ftp(pre_cross_check, cancel_event=cancel_event,
                                                          progress_cb=progress_cb)
            # Sin esto, la corrección del cruce FTP (p.ej. Dragon Ball GT:
            # 3 episodios que sí estaban en el servidor pero Jellyfin no
            # los tenía bien indexados) solo vivía en esta sesión --
            # `cache[key]["missing"]` seguía con el hueco de ANTES del
            # cruce (se escribió más arriba, en el bucle principal, antes
            # de cruzar con el FTP), así que al guardar se persistía el
            # dato viejo y el hueco falso volvía a aparecer tal cual en el
            # siguiente arranque, aunque ya se hubiera visto corregido.
            for r in pre_cross_check:
                key = str(r["tmdb_id"])
                if key in cache:
                    cache[key]["missing"] = {str(k): v for k, v in r["missing"].items()}
                    cache[key]["unknown_seasons"] = sorted(r.get("unknown_seasons") or [])
                    cache[key]["present_season_counts"] = {
                        str(k): v for k, v in (r.get("server_season_counts") or {}).items()}

        import time as _time
        cache["_meta"] = {"last_scan_ts": _time.time()}
        save_cache(cache)
        return results

    def _build_ftp_episode_index(self, ftp_conn, cancel_event=None, progress_cb=None) -> dict:
        """Listado COMPLETO (de una sola vez, no serie por serie) de todas
        las categorías de TV configuradas en el FTP -- se llama solo si el
        escaneo por Jellyfin/Plex ya encontró algún hueco, para no pagar
        este coste cuando no hace falta. Devuelve {root: {nombre_carpeta:
        {(temporada, episodio), ...}}}, sacando temporada/episodio del
        propio nombre de archivo (detect_episode), no de cómo se llamen
        las subcarpetas de temporada.
        cancel_event: se comprueba entre carpeta y carpeta durante el
        listado de respaldo (ver más abajo) -- es la fase más lenta de
        todo el escaneo (con un servidor cuyo LIST -R falla, decenas de
        listados individuales seguidos), y antes de este parámetro
        "Cancelar" no tenía forma de interrumpirla: el hilo tenía que
        terminar las ~360 carpetas sí o sí antes de que el botón
        surtiera efecto.
        progress_cb(current, total, nombre): mismo contrato que en
        _scan_missing_episodes, reutilizado aquí para ESTA fase (el
        respaldo carpeta-a-carpeta) -- sin esto, la barra de progreso se
        quedaba clavada al 100% (el recuento de series ya había terminado)
        mientras esta fase, la más lenta con diferencia con una raíz
        grande, seguía trabajando en silencio varios minutos más -- el
        usuario veía el escaneo "parado" y no tenía forma de saber que en
        realidad seguía cruzando datos con el FTP, ni que la lista que
        estaba mirando todavía podía cambiar cuando terminara de verdad."""
        cats = self.config_data.get("ftp_categories", {"tv": [], "movie": []}).get("tv", [])
        index = {}
        for cat in cats:
            if cancel_event and cancel_event.is_set():
                break
            root = cat.get("root", "")
            if not root or root in index:
                continue
            # Se pide SIEMPRE en fresco (sin usar self._ftp_dir_cache) --
            # visto de verdad: un corte de red puntual al listar puede
            # devolver 0 carpetas para una raíz que normalmente tiene
            # cientos, y si eso se cachea, el cruce entero se queda
            # confiando en ese vacío para siempre en esa sesión, "confir-
            # mando" que ninguna serie está en el FTP. Aquí la fiabilidad
            # importa más que ahorrarse una consulta (esta función solo se
            # llama una vez por escaneo).
            # Dos listados, no uno -- mismo motivo que
            # _find_category_with_existing_folder (un único NLST a esta
            # raíz a veces vuelve incompleto sin dar ningún error, visto de
            # verdad con "(Des)encanto"): un escaneo completo real dejó
            # fuera "Los Vengadores: Los Súper Héroes más poderosos de la
            # Tierra" de show_folders con un solo intento, así que su
            # carpeta nunca llegaba a procesarse aunque LIST -R sí trajera
            # sus archivos -- _match_ftp_present no encontraba ninguna
            # carpeta con esa confianza y el cruce se saltaba en silencio
            # para esa serie entera, dejando el hueco falso que Jellyfin
            # había reportado. "Reescanear esta serie" no tenía este fallo
            # porque ya usa la unión de dos intentos para su propia serie.
            first_folders = set(ftp_conn.list_dirs(root))
            second_folders = set(ftp_conn.list_dirs(root))
            if first_folders != second_folders:
                _log.warning("Cruce FTP: listado de '%s' inconsistente entre dos intentos seguidos "
                            "(%d vs %d carpetas) -- usando la unión de ambos",
                            root, len(first_folders), len(second_folders))
            show_folders = list(first_folders | second_folders)
            if show_folders:
                self._ftp_dir_cache[root] = show_folders   # sí se aprovecha para otras partes de la app
                _log.info("Cruce FTP: raíz '%s' -> %d carpeta(s) de serie: %s",
                          root, len(show_folders), show_folders)
            else:
                _log.warning("Cruce FTP: raíz '%s' devolvió 0 carpetas -- sospechoso "
                             "(¿corte de red al listar?), no se usa para este cruce", root)
            index[root] = {}

            # "LIST -R" trae TODOS los archivos de la categoría en una
            # sola petición (si el servidor lo soporta, ver
            # FTPClient.list_tree_recursive) -- evita un
            # list_files_recursive por cada serie, que es lo que hacía
            # lento este cruce en categorías con muchas series.
            # Igual que show_folders arriba: dos peticiones, no una, y
            # unión de los archivos por carpeta -- un escaneo completo real
            # dio "falta Bleach 1x08" con el archivo presente de verdad en
            # el servidor: la carpeta de Bleach SÍ se resolvía por LIST -R
            # (así que nunca caía en el respaldo carpeta-a-carpeta de más
            # abajo, que sí es fiable), pero esa respuesta concreta venía
            # incompleta DENTRO de esa carpeta -- el aviso de "respuesta
            # cortada" de más abajo solo detecta carpetas enteras que
            # faltan, no archivos sueltos que faltan dentro de una carpeta
            # que sí se resolvió. Un segundo LIST -R independiente rara vez
            # se corta exactamente en el mismo punto, así que la unión
            # recupera el archivo que faltaba en cualquiera de los dos.
            files_by_folder = None
            tree1 = ftp_conn.list_tree_recursive(root)
            tree2 = ftp_conn.list_tree_recursive(root)
            if tree1 is not None or tree2 is not None:
                by_folder_1 = files_by_top_level_folder(tree1, root) if tree1 is not None else {}
                by_folder_2 = files_by_top_level_folder(tree2, root) if tree2 is not None else {}
                files_by_folder = {}
                for folder in set(by_folder_1) | set(by_folder_2):
                    set_1 = set(by_folder_1.get(folder, []))
                    set_2 = set(by_folder_2.get(folder, []))
                    files_by_folder[folder] = sorted(set_1 | set_2)
                    # Comparado como conjuntos, no como listas -- el orden en
                    # que el servidor devuelve los archivos puede variar entre
                    # dos peticiones sin que eso sea una respuesta cortada de
                    # verdad, y comparar listas habría avisado de un "hueco"
                    # falso en cada carpeta solo por el orden.
                    if folder in by_folder_1 and folder in by_folder_2 and set_1 != set_2:
                        _log.warning("Cruce FTP: '%s/%s' -- LIST -R inconsistente entre dos intentos "
                                    "seguidos (%d vs %d archivo(s)) -- usando la unión de ambos",
                                    root, folder, len(set_1), len(set_2))
                _log.info("Cruce FTP: LIST -R soportado en '%s' -- %d carpeta(s) resueltas de una vez",
                          root, len(files_by_folder))
                if show_folders and len(files_by_folder) < len(show_folders) * 0.5:
                    # Visto de verdad con la categoría "series/" (368+ carpetas):
                    # el servidor a veces corta la respuesta de "LIST -R" muy
                    # pronto (llegó a resolver 1 sola carpeta de 368) sin dar
                    # ningún error -- una respuesta tan incompleta no es de
                    # fiar, aunque no haya forma de saber cuál es la carpeta
                    # que de verdad se cortó hasta comprobarlas una a una abajo.
                    _log.warning("Cruce FTP: '%s' -- solo %d/%d carpeta(s) en la respuesta de LIST -R, "
                                 "sospechoso de respuesta cortada", root, len(files_by_folder), len(show_folders))

            def _process_folder(folder, files):
                """Analiza y registra UNA carpeta -- separado para poder
                llamarse tan pronto como se tenga el listado de cada una
                (ver más abajo), en vez de esperar a tener las ~360 antes
                de escribir la primera línea en el log. Antes de
                paralelizar el respaldo (ver más abajo) esto se hacía en
                un único bucle, así que el log siempre iba avisando serie
                a serie según se procesaban -- separarlo en dos pasadas
                (listar todo, LUEGO analizar/loguear todo) dejó al log
                completamente mudo durante todo el respaldo, dando la
                sensación de que el escaneo se había colgado cuando en
                realidad seguía vivo, solo que en silencio."""
                present = set()
                unparsed = []
                for fname in files:
                    det = detect_episode(fname)
                    if det.get("season") is not None and det.get("episode") is not None:
                        present.add((det["season"], det["episode"]))
                        # Episodio doble empaquetado en el mismo archivo
                        # (p.ej. "7x21-7x22") -- ver detect_episode.
                        for extra_ep in det.get("extra_episodes", []):
                            present.add((det["season"], extra_ep))
                    else:
                        unparsed.append(fname)
                index[root][folder] = present
                _log.info("Cruce FTP: '%s/%s' -> %d archivo(s), %d episodio(s) reconocidos%s",
                          root, folder, len(files), len(present),
                          f", {len(unparsed)} sin temporada/episodio reconocible: {unparsed[:5]}"
                          if unparsed else "")

            for folder, files in (files_by_folder or {}).items():
                if folder in show_folders:
                    _process_folder(folder, files)

            # Carpetas que "LIST -R" no resolvió (ausentes de files_by_folder,
            # o directamente sin soporte -- files_by_folder es None): NO se
            # interpretan como "carpeta vacía" (daría huecos falsos que el
            # propio cruce FTP debería evitar, ver docstring de esta función),
            # se comprueban una a una, más lento pero fiable. Con una sola
            # conexión y un servidor cuyo LIST -R se corta sistemáticamente
            # (visto de verdad con "series/", 360 carpetas: SIEMPRE solo 1/360
            # resuelta, ni una vez de casualidad) esto podía tardar decenas de
            # minutos -- "Reescaneo completo" daba la sensación de no terminar
            # nunca. Mismo número de conexiones en paralelo que ya usa la
            # subida (ftp_parallel, 1-5): cada una con su propia conexión FTP,
            # nunca comparten ftp_conn (igual que las subidas paralelas).
            fallback_folders = [f for f in show_folders
                                if files_by_folder is None or f not in files_by_folder]
            if fallback_folders:
                parallel = max(1, min(5, int(self.config_data.get("ftp_parallel", 1))))
                if parallel > 1 and len(fallback_folders) > 1:
                    from concurrent.futures import ThreadPoolExecutor, as_completed
                    from core.ftp_client import FTPClient as _FallbackFTPClient

                    def _list_folder_files(folder):
                        worker_ftp = _FallbackFTPClient()
                        ok, _ = worker_ftp.connect(
                            self.config_data.get("ftp_host", ""), int(self.config_data.get("ftp_port", 21)),
                            self.config_data.get("ftp_user", ""), self.config_data.get("ftp_password", ""),
                            self.config_data.get("ftp_use_tls", False))
                        if not ok:
                            return folder, []
                        try:
                            # Dos listados, no uno, misma unión que el resto
                            # de esta función -- una raíz real (368+
                            # carpetas) resultó SIEMPRE sin soporte de
                            # verdad para LIST -R (atascada en 1/368 en 33
                            # escaneos seguidos, nunca de casualidad, ver el
                            # aviso de "respuesta cortada" más arriba), así
                            # que TODAS sus carpetas caen aquí en cada
                            # escaneo -- este respaldo no es un caso raro
                            # para esa categoría, es el camino normal, y un
                            # listado cortado aquí (mismo NLST/LIST que ya
                            # falla en otros sitios de este mismo servidor)
                            # dejaba huecos falsos (Bleach 1x08, presente de
                            # verdad en el servidor).
                            path = f"{root.rstrip('/')}/{folder}"
                            files = list(set(worker_ftp.list_files_recursive(path, max_depth=2))
                                        | set(worker_ftp.list_files_recursive(path, max_depth=2)))
                            return folder, files
                        finally:
                            worker_ftp.disconnect()

                    _log.info("Cruce FTP: '%s' -- %d carpeta(s) sin resolver por LIST -R, "
                              "listando en paralelo (%d conexiones)",
                              root, len(fallback_folders), parallel)
                    with ThreadPoolExecutor(max_workers=parallel) as executor:
                        # as_completed (no executor.map): loguea cada carpeta
                        # en cuanto termina SU listado, no en el orden en que
                        # se enviaron -- con map(), una sola carpeta lenta
                        # bloquea el aviso de las demás que ya habían
                        # terminado, dejando el mismo silencio que se quiere
                        # evitar aquí.
                        futures = [executor.submit(_list_folder_files, f) for f in fallback_folders]
                        done = 0
                        for future in as_completed(futures):
                            folder, files = future.result()
                            _process_folder(folder, files)
                            done += 1
                            if progress_cb:
                                progress_cb(done, len(fallback_folders), f"cruzando con el FTP: {folder}")
                            if cancel_event and cancel_event.is_set():
                                # No se puede matar a media petición un
                                # listado FTP ya en marcha en otro hilo, pero
                                # sí evitar que los que aún no habían
                                # arrancado se pongan a la cola -- deja de
                                # esperar más resultados en cuanto se pide
                                # cancelar, en vez de esperar a que
                                # terminen los ~360.
                                for f in futures:
                                    f.cancel()
                                break
                else:
                    for i, folder in enumerate(fallback_folders, 1):
                        if cancel_event and cancel_event.is_set():
                            break
                        path = f"{root.rstrip('/')}/{folder}"
                        files = list(set(ftp_conn.list_files_recursive(path, max_depth=2))
                                    | set(ftp_conn.list_files_recursive(path, max_depth=2)))
                        _process_folder(folder, files)
                        if progress_cb:
                            progress_cb(i, len(fallback_folders), f"cruzando con el FTP: {folder}")
        return index

    def _cross_check_results_with_ftp(self, results: list, cancel_event=None, progress_cb=None) -> list:
        """Para las series que YA salieron con algún hueco (según Jellyfin/
        Plex), comprueba también el listado real del FTP -- por si el
        propio servidor de medios falló al indexar algo que sí está físi-
        camente en el servidor (visto con Desencanto: Jellyfin decía "0
        episodios" con la carpeta llena). Si de verdad no hay conexión FTP
        configurada o falla, se devuelven los resultados tal cual, sin
        romper el escaneo por esto. cancel_event/progress_cb: ver
        _build_ftp_episode_index, que es donde de verdad se comprueba (esta
        fase es la más lenta de todo el escaneo, así que es la que más
        falta hacía tanto que "Cancelar" pudiera interrumpirla como que la
        barra de progreso no se quedara clavada al 100% mientras seguía
        trabajando en silencio)."""
        from core.missing_episodes import find_missing_episodes, find_unknown_seasons, format_missing_summary

        if not self.config_data.get("ftp_host", ""):
            _log.info("Cruce FTP: omitido, sin servidor FTP configurado")
            return results
        # Conexión propia, NUNCA self.ftp -- este cruce puede tardar bastante
        # (listado recursivo de varias raíces) y self.ftp lo usan a la vez
        # AutoWatcher y otras partes de la GUI; compartirla aquí bloquearía
        # esas subidas durante todo el escaneo, o peor, cruzaría respuestas
        # entre hilos (ver el candado _ftp_cmd_lock en las demás llamadas).
        from core.ftp_client import FTPClient as _FTPClient
        own_ftp = _FTPClient()
        try:
            own_ftp.connect(
                self.config_data.get("ftp_host", ""),
                int(self.config_data.get("ftp_port", 21)),
                self.config_data.get("ftp_user", ""),
                self.config_data.get("ftp_password", ""),
                self.config_data.get("ftp_use_tls", False))
            if not own_ftp.is_connected():
                _log.warning("Cruce FTP: omitido, no se pudo conectar al servidor")
                return results
            ftp_index = self._build_ftp_episode_index(own_ftp, cancel_event=cancel_event, progress_cb=progress_cb)
        except Exception as e:
            _log.warning("Cruce FTP: fallo inesperado, se deja el resultado tal cual: %s", e)
            return results   # sin FTP no se puede cruzar -- se deja tal cual
        finally:
            own_ftp.disconnect()

        if ftp_index and not any(ftp_index.values()):
            # Todas las raíces devolvieron 0 carpetas -- casi seguro un
            # fallo de listado puntual, no que el FTP esté genuinamente
            # vacío. Seguir adelante reportaría "no encontrado en el FTP"
            # para TODAS las series por igual, dando una falsa sensación
            # de confianza. Mejor dejar el resultado de Jellyfin/Plex tal
            # cual que "confirmar" con datos que no son de fiar.
            _log.warning("Cruce FTP: todas las raíces devolvieron 0 carpetas, se descarta este cruce "
                         "por sospechoso y se deja el resultado tal cual")
            return results

        for r in results:
            new_expected = r.get("expected_episodes")
            if not new_expected:
                _log.info("Cruce FTP: '%s' sin lista completa de episodios en caché, no se recalcula", r["name"])
                continue   # sin la lista completa de episodios de TMDB no se puede recalcular el hueco
            ftp_present = self._match_ftp_present(ftp_index, r["name"], r.get("folder_name"))
            if ftp_present is None:
                _log.info("Cruce FTP: '%s' no se encontró en ninguna carpeta del FTP con confianza suficiente",
                          r["name"])
                continue   # esta serie no se encontró en ninguna carpeta del FTP
            _log.info("Cruce FTP: '%s' -> %d episodio(s) encontrados en el FTP (hueco antes: %s)",
                      r["name"], len(ftp_present), r["missing"])

            # Unión: lo que ya decía Jellyfin/Plex (reconstruido a partir de
            # expected - missing) + lo que hay de verdad en el FTP.
            already_present = {(season, ep) for season, eps in new_expected.items()
                               for ep in eps if ep not in set(r["missing"].get(season, []))}
            combined_present = already_present | ftp_present

            new_missing = find_missing_episodes(new_expected, combined_present)
            r["missing"] = new_missing
            r["summary"] = format_missing_summary(r["name"], new_missing)
            r["unknown_seasons"] = find_unknown_seasons(combined_present, new_expected.keys())
            present_counts = {}
            for season, _ep in combined_present:
                present_counts[season] = present_counts.get(season, 0) + 1
            r["server_season_counts"] = present_counts
        return [r for r in results if r["missing"] or r["unknown_seasons"]]

    def _cross_check_single_result_with_ftp(self, result: dict) -> dict:
        """Igual que _cross_check_results_with_ftp, pero para UNA sola
        serie -- usado por el reescaneo individual (ver
        _rescan_single_missing_ep_series). _build_ftp_episode_index lista
        TODAS las carpetas de TODAS las categorías configuradas (necesario
        para un reescaneo completo, pero es la fase más lenta de todo el
        escaneo, y pagarla entera por una sola serie la hacía tardar igual
        que un reescaneo completo). Aquí se localiza directamente la
        carpeta de ESTA serie (_find_category_with_existing_folder, mismo
        método que ya usa el botón de borrar) y solo se lista esa."""
        import types
        from core.missing_episodes import find_missing_episodes, find_unknown_seasons, format_missing_summary

        if not self.config_data.get("ftp_host", ""):
            return result
        new_expected = result.get("expected_episodes")
        if not new_expected:
            return result

        from core.ftp_client import FTPClient as _FTPClient
        own_ftp = _FTPClient()
        try:
            own_ftp.connect(
                self.config_data.get("ftp_host", ""), int(self.config_data.get("ftp_port", 21)),
                self.config_data.get("ftp_user", ""), self.config_data.get("ftp_password", ""),
                self.config_data.get("ftp_use_tls", False))
            if not own_ftp.is_connected():
                _log.warning("Cruce FTP (individual): omitido, no se pudo conectar al servidor")
                return result
            info = types.SimpleNamespace(title=result["name"], media_type="tv",
                                         folder_name=result.get("folder_name"))
            category, folder_name = self._find_category_with_existing_folder(own_ftp, info, force_refresh=True)
            if not folder_name:
                _log.info("Cruce FTP (individual): '%s' no se encontró en ninguna categoría del FTP",
                          result["name"])
                return result
            root = category.get("root", "")
            # Dos listados, no uno, misma unión que el cruce completo (ver
            # _build_ftp_episode_index) -- barato aquí porque es UNA sola
            # carpeta, no cientos, así que vale la pena pagarlo siempre en
            # vez de arriesgarse a un listado cortado que deje fuera un
            # episodio suelto.
            path = f"{root.rstrip('/')}/{folder_name}"
            files = list(set(own_ftp.list_files_recursive(path, max_depth=2))
                        | set(own_ftp.list_files_recursive(path, max_depth=2)))
        except Exception as e:
            _log.warning("Cruce FTP (individual): fallo inesperado para '%s': %s", result["name"], e)
            return result
        finally:
            own_ftp.disconnect()

        ftp_present = set()
        for fname in files:
            det = detect_episode(fname)
            if det.get("season") is not None and det.get("episode") is not None:
                ftp_present.add((det["season"], det["episode"]))
                # Episodio doble empaquetado en el mismo archivo (p.ej.
                # "7x21-7x22") -- ver detect_episode.
                for extra_ep in det.get("extra_episodes", []):
                    ftp_present.add((det["season"], extra_ep))
        _log.info("Cruce FTP (individual): '%s' -> %d episodio(s) encontrados en '%s/%s' (hueco antes: %s)",
                  result["name"], len(ftp_present), root, folder_name, result["missing"])

        already_present = {(season, ep) for season, eps in new_expected.items()
                           for ep in eps if ep not in set(result["missing"].get(season, []))}
        combined_present = already_present | ftp_present
        new_missing = find_missing_episodes(new_expected, combined_present)
        result["missing"] = new_missing
        result["summary"] = format_missing_summary(result["name"], new_missing)
        result["unknown_seasons"] = find_unknown_seasons(combined_present, new_expected.keys())
        present_counts = {}
        for season, _ep in combined_present:
            present_counts[season] = present_counts.get(season, 0) + 1
        result["server_season_counts"] = present_counts
        return result

    @staticmethod
    def _match_ftp_present(ftp_index: dict, show_name: str, known_folder_name: str = None):
        """Busca *show_name* entre todas las carpetas de todas las
        categorías del índice FTP (misma confianza que
        _find_category_with_existing_folder: nombre real ya conocido si
        se pasa known_folder_name -- ver get_jellyfin_series::folder_name,
        para series cuyo nombre mostrado está traducido y no se parece en
        nada al de su carpeta real ("Acusado" vs "Accused") --, si no,
        nombre exacto tras sanear, o ratio >= 0.90). Devuelve el set de
        episodios encontrados en esa carpeta, o None si no hay ninguna
        coincidencia de esa confianza."""
        sanitized_desired = _ftp_safe(show_name)
        best_candidate, best_ratio = None, 0.0
        for folders in ftp_index.values():
            if known_folder_name:
                folders_lower = {f.lower(): f for f in folders}
                real = folders_lower.get(known_folder_name.lower())
                if real:
                    return folders[real]
            if sanitized_desired in folders:
                return folders[sanitized_desired]
            candidate, ratio = best_match(show_name, list(folders.keys()), min_ratio=0.55)
            if candidate and ratio >= 0.90:
                return folders[candidate]
            if candidate and ratio > best_ratio:
                best_candidate, best_ratio = candidate, ratio
        if best_candidate:
            _log.info("Cruce FTP: '%s' -- candidato más parecido '%s' con ratio %.2f "
                      "(hace falta >= 0.90 para reutilizarlo en silencio)",
                      show_name, best_candidate, best_ratio)
        return None

    def _set_missing_episode_ignored(self, tmdb_id, ignored: bool) -> None:
        """Marca (o desmarca) una serie como ignorada en la caché del
        detector de huecos -- persiste entre escaneos hasta que el usuario
        la desmarque a mano."""
        from core.missing_episodes_cache import load_cache, save_cache
        cache = dict(load_cache())
        key = str(tmdb_id)
        if key in cache:
            cache[key]["ignored"] = ignored
            save_cache(cache)

    # ── Exportar / Importar configuración ──

    def _build_config_transfer_section(self, tab):
        scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        scroll.pack(fill="both", expand=True)
        fr = ctk.CTkFrame(scroll)
        fr.pack(fill="both", expand=True, padx=0, pady=(0, 8))
        fr.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(fr, text="Copia de seguridad de la configuración",
                     font=self._cfg_font_title).grid(row=0, column=0, pady=(12, 4))
        ctk.CTkLabel(fr, text="Exporta tu configuración de CLIENTE (conexión FTP, carpeta vigilada, "
                              "preferencias de este equipo...) a un archivo, o impórtala en otra "
                              "instalación. No incluye la configuración de servidor -- categorías, "
                              "plantillas, Plex/Jellyfin... esa se sincroniza/publica aparte, ver "
                              "Servidor. La contraseña FTP nunca se exporta en texto plano — tendrás "
                              "que volver a introducirla tras importar.",
                     font=self._cfg_font_desc, text_color=PENDING_COLOR, wraplength=900).grid(
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
        from core.server_config import SHARED_CONFIG_KEYS
        # Solo configuración de CLIENTE -- la de servidor (incluidos los
        # términos aprendidos del fallback de IA, que tampoco viven en
        # config.json) tiene su propio mecanismo de sincronizar/publicar
        # (ver _sync_server_config_from_ftp/_publish_server_config);
        # meterla también aquí sería redundante y confuso, dos caminos
        # distintos para cambiar lo mismo.
        data = {k: v for k, v in self.config_data.to_dict().items() if k not in SHARED_CONFIG_KEYS}
        # Metadatos de versión: permiten avisar al importar si el archivo
        # viene de una versión de aRenombrar más nueva que podría usar un
        # formato que esta versión no entiende del todo.
        data["_export_schema_version"] = CONFIG_EXPORT_SCHEMA_VERSION
        data["_app_version"] = __version__
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
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

        # Archivos exportados antes de este campo (sin _export_schema_version)
        # se tratan como versión 1 -- es la única que ha existido hasta ahora.
        file_schema  = data.pop("_export_schema_version", 1)
        file_app_ver = data.pop("_app_version", None)
        if file_schema > CONFIG_EXPORT_SCHEMA_VERSION:
            if not messagebox.askyesno(
                    "Archivo de una versión más nueva",
                    f"Este archivo se exportó con una versión de aRenombrar más "
                    f"reciente que esta{f' (v{file_app_ver})' if file_app_ver else ''} "
                    f"y puede usar un formato que esta versión no entiende del todo. "
                    f"¿Importar de todas formas?"):
                return

        if not messagebox.askyesno(
                "Importar configuración",
                "Esto sobrescribirá tu configuración de CLIENTE actual (conexión FTP, carpeta "
                "vigilada, preferencias de este equipo... excepto la contraseña FTP, que deberás "
                "volver a introducir). No toca la configuración de servidor. ¿Continuar?"):
            return
        data.pop("ftp_password", None)   # nunca importar contraseñas en texto plano
        # Solo configuración de CLIENTE -- un archivo exportado con una
        # versión anterior de la app (antes de este cambio) podía traer
        # claves de servidor mezcladas; se descartan aquí igual que si
        # nunca hubieran estado en el archivo (ver _export_config).
        from core.server_config import SHARED_CONFIG_KEYS
        data.pop("learned_junk_terms", None)
        data = {k: v for k, v in data.items() if k not in SHARED_CONFIG_KEYS}
        # Mismo paso de validación que al guardar Ajustes a mano (ver
        # _save_all_settings) -- sin esto, importar una configuración con
        # un "Tu nombre" distinto cambiaba de identidad en silencio, sin
        # comprobar que el nombre no estuviera ya en uso ni preguntar qué
        # hacer con las reservas del nombre anterior.
        if "app_user_name" in data:
            data["app_user_name"] = self._resolve_app_user_name_change(data["app_user_name"])
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
        _set_entry(self._reservation_quota_entry, self.config_data.get("reservation_quota_gb", 100))

        _set_entry(self._api_key_entry, self.config_data.get("tmdb_api_key", ""))
        self._lang_combo.set(self.config_data.get("language", "es-ES"))
        self.tmdb.set_api_key(self.config_data.get("tmdb_api_key", ""))
        self.tmdb.set_language(self.config_data.get("language", "es-ES"))

        if self.config_data.get("ai_fallback_enabled"):
            self._ai_fallback_switch.select()
        else:
            self._ai_fallback_switch.deselect()
        _set_entry(self._ai_key_entry, self.config_data.get("ai_api_key", ""))

        if self.config_data.get("plex_enabled"):
            self._plex_switch.select()
        else:
            self._plex_switch.deselect()
        _set_entry(self._plex_host_entry, self.config_data.get("plex_host", ""))
        _set_entry(self._plex_token_entry, self.config_data.get("plex_token", ""))
        if self.config_data.get("jellyfin_enabled"):
            self._jellyfin_switch.select()
        else:
            self._jellyfin_switch.deselect()
        _set_entry(self._jellyfin_host_entry, self.config_data.get("jellyfin_host", ""))
        _set_entry(self._jellyfin_key_entry, self.config_data.get("jellyfin_api_key", ""))
        _set_entry(self._jellyfin_username_entry, self.config_data.get("jellyfin_username", ""))

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

    def _build_ftp_categories_section(self, tab):
        saved = self.config_data.get("ftp_categories", {"tv": [], "movie": []})
        self._tv_categories    = [dict(c) for c in saved.get("tv", [])]
        self._movie_categories = [dict(c) for c in saved.get("movie", [])]
        self._genres_cache     = {"tv": [], "movie": []}

        scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        scroll.pack(fill="both", expand=True)
        cats_fr = ctk.CTkFrame(scroll)
        cats_fr.pack(fill="both", expand=True, padx=0, pady=(0, 8))
        cats_fr.grid_columnconfigure(0, weight=1)
        cats_fr.grid_columnconfigure(1, weight=1)

        hdr_cats = ctk.CTkFrame(cats_fr, fg_color="transparent")
        hdr_cats.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(12, 4))
        hdr_cats.grid_columnconfigure(0, weight=1)
        hdr_cats.grid_columnconfigure(2, weight=1)
        ctk.CTkLabel(hdr_cats, text="Categorías FTP",
                     font=self._cfg_font_title).grid(row=0, column=1)
        ctk.CTkButton(hdr_cats, text="🔄 Recargar géneros", width=160, height=26,
                      command=self._load_genres_async).grid(row=0, column=2, sticky="e", padx=12)
        ctk.CTkLabel(cats_fr,
                     text="Cada categoría busca y sube contenido en su propia ruta del servidor. "
                          "La app elige la categoría sola según el género de TMDB — el orden importa "
                          "(la primera que coincida gana); una categoría sin géneros marcados actúa "
                          "como categoría por defecto para lo que no encaje en ninguna otra.",
                     font=self._cfg_font_desc, text_color=PENDING_COLOR, wraplength=900).grid(
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
                     font=self._cfg_font_small_bold).pack(anchor="w", pady=(0, 4))
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
                     font=self._cfg_font_desc, text_color=PENDING_COLOR).grid(
            row=1, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 2))
        genre_frame = ctk.CTkFrame(card, fg_color="transparent")
        genre_frame.grid(row=2, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 6))
        cat["_genre_vars"] = {}
        options = self._genre_options_for(media_type, cat)
        if not options:
            ctk.CTkLabel(genre_frame, text="(géneros no cargados aún — pulsa 'Recargar géneros')",
                         font=self._cfg_font_desc, text_color=PENDING_COLOR).pack(anchor="w")
        else:
            cur_genre_ids = set(cat.get("genre_ids") or [])
            cols = 3
            # Fuente compartida -- un CTkFont nuevo (llamada a Tcl) por
            # cada género de cada categoría se notaba al construir esta
            # sección con varias categorías y géneros ya cargados.
            genre_font = self._cfg_font_desc
            for i, (gid, gname) in enumerate(options):
                var = tk.BooleanVar(value=gid in cur_genre_ids)
                ctk.CTkCheckBox(genre_frame, text=gname, variable=var, width=140,
                                font=genre_font).grid(
                    row=i // cols, column=i % cols, sticky="w", padx=4, pady=2)
                cat["_genre_vars"][gid] = var

        ctk.CTkLabel(card, text="Ruta en el servidor:",
                     font=self._cfg_font_desc, text_color=PENDING_COLOR).grid(
            row=3, column=0, sticky="w", padx=8, pady=(0, 2))
        root_entry = ctk.CTkEntry(card, placeholder_text="/datos2/series")
        root_entry.insert(0, cat.get("root", ""))
        root_entry.grid(row=3, column=1, sticky="ew", padx=8, pady=(0, 2))
        cat["_root_entry"] = root_entry

        ctk.CTkLabel(card, text="Plantilla (relativa a la ruta):",
                     font=self._cfg_font_desc, text_color=PENDING_COLOR).grid(
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
        En macOS, arrastrar desde Finder puede entregar URIs "file:///..."
        en vez de una ruta plana según la versión del backend tkdnd — se
        despoja el prefijo y se decodifica el %-escaping si aparece.
        """
        import re
        from urllib.parse import unquote, urlparse
        result = []
        for match in re.finditer(r'\{([^}]+)\}|(\S+)', data):
            path = match.group(1) or match.group(2)
            if path.startswith("file://"):
                parsed = urlparse(path)
                path = unquote(parsed.path)
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
        # self._upload_running solo indica una subida MANUAL en marcha -- el
        # modo automático nunca lo toca, aunque añade sus propias filas a
        # esta misma lista (self.files, vía _on_auto_file_event) con
        # estados "en_cola"/"subiendo" igual que la manual. Sin
        # self._watcher aquí, "Limpiar" con el modo automático subiendo
        # borraba esas filas sin preguntar nada -- el diálogo nunca llegaba
        # a aparecer aunque hubiera subidas de verdad en curso.
        if self._upload_running or self._watcher is not None:
            pendientes = [e for e in self.files if e.status != "subido"]
            if pendientes:
                dlg = _ClearDialog(self, len(pendientes))
                if dlg.result == "solo_subidos":
                    self.files = [e for e in self.files if e.status != "subido"]
                elif dlg.result == "todo":
                    if self._upload_running:
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

    def _files_change_page(self, delta: int):
        n_pages = max(1, -(-len(self.files) // self._file_table.page_size))
        new_page = max(0, min(n_pages - 1, self._files_page + delta))
        if new_page == self._files_page:
            return
        self._files_page = new_page
        self._refresh_table()

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
            self._files_page_lbl.configure(text="")
            self._files_prev_btn.configure(state="disabled")
            self._files_next_btn.configure(state="disabled")
            self._update_status_bar()
            return
        self._drop_zone.pack_forget()

        total = len(self.files)
        page_size = self._file_table.page_size
        n_pages = max(1, -(-total // page_size))
        # No se resetea a la página 0 en cada refresco (a diferencia de
        # Episodios en un reescaneo completo): aquí _refresh_table() se
        # llama constantemente por cambios normales de la cola (añadir/
        # quitar un archivo, AutoWatcher detectando uno nuevo...), y
        # devolver siempre a la primera página interrumpiría a quien esté
        # mirando otra -- solo se corrige (clamp) si la página actual ya no
        # existe (p.ej. tras borrar los últimos archivos de la última página).
        self._files_page = max(0, min(n_pages - 1, self._files_page))
        start = self._files_page * page_size
        page_files = self.files[start:start + page_size]

        self._files_page_lbl.configure(text=f"Página {self._files_page + 1} de {n_pages}")
        self._files_prev_btn.configure(state="normal" if self._files_page > 0 else "disabled")
        self._files_next_btn.configure(state="normal" if self._files_page < n_pages - 1 else "disabled")
        # Subir el scroll interno de la tabla -- si no, tras cambiar de
        # página con el scroll bajado quedaba viendo un hueco en blanco
        # (mismo arreglo que Episodios, ver [[project_pagination_user_object_limit]]).
        self._file_list_frame._parent_canvas.yview_moveto(0)

        cw = self._col_widths
        for i, entry in enumerate(page_files):
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

            _BF = self._font_btn

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

            # Destino: doble clic para fijarlo a mano (ver _edit_remote_dir)
            # -- en naranja/acento si el usuario ya lo fijó, para
            # distinguirlo del calculado automáticamente por categoría.
            dest_lbl = ctk.CTkLabel(
                rf, text=_fit_text(self._preview_remote_path(entry), cw["dest"], self._font_det),
                anchor="w", font=self._font_det, width=cw["dest"], cursor="hand2",
                text_color=ACCENT if entry.remote_dir_override else PENDING_COLOR)
            dest_lbl.pack(side="left", padx=(4, 0), pady=2)   # mirror sash nn|dest
            dest_lbl.bind("<Double-Button-1>", lambda ev, e=entry: self._edit_remote_dir(e))

            st_lbl = ctk.CTkLabel(rf, text=_status_label(entry.status), width=cw["stat"],
                                   anchor="w", font=self._font_small,
                                   text_color=sc.get(entry.status, PENDING_COLOR))
            st_lbl.pack(side="left", padx=(4, 0), pady=2)    # mirror sash nn|stat

            ftp_bar = ctk.CTkProgressBar(rf, height=8, width=cw["bar"], corner_radius=0)
            ftp_bar.set(entry.ftp_progress)
            ftp_bar.pack(side="left", padx=(4, 0), pady=2)   # mirror sash stat|bar

            spd_text = _fmt_speed(entry.ftp_speed) if entry.ftp_speed > 0 else ""
            ftp_speed = ctk.CTkLabel(rf, text=spd_text, width=cw["spd"],
                                      font=self._font_small, text_color=PENDING_COLOR)
            ftp_speed.pack(side="left", padx=(4, 0), pady=2) # mirror sash bar|spd

            size_lbl = ctk.CTkLabel(rf, text=self._file_size_text(entry), width=cw["size"],
                                     anchor="w", font=self._font_small, text_color=PENDING_COLOR)
            size_lbl.pack(side="left", padx=(4, 0), pady=2)  # mirror sash spd|size

            fav_btn = ctk.CTkButton(
                rf, text=self._fav_symbol(entry), width=cw["btn"], height=26,
                font=_BF, fg_color="transparent", border_width=0,
                text_color=ACCENT if self._entry_is_favorite(entry) else PENDING_COLOR,
                hover_color=("gray85", "#2b2b2b"),
                state="normal" if entry.media_info else "disabled",
                command=lambda e=entry: self._toggle_entry_favorite(e))
            fav_btn.pack(side="left", padx=(4, 0), pady=2)

            # Reservar (ver core/reservations.py) solo tiene sentido una vez
            # el archivo está de verdad en el servidor -- antes de subir no
            # hay nada que proteger todavía, así que se deshabilita hasta
            # que entry.status == "subido" (igual que favorito, deshabilitado
            # sin media_info).
            lock_btn = ctk.CTkButton(
                rf, text=self._lock_symbol(entry), width=cw["btn"], height=26,
                font=_BF, fg_color="transparent", border_width=0,
                text_color=ACCENT if self._entry_is_reserved(entry) else PENDING_COLOR,
                hover_color=("gray85", "#2b2b2b"),
                state="normal" if (entry.media_info and entry.status == "subido") else "disabled",
                command=lambda e=entry: self._toggle_entry_reservation(e))
            lock_btn.pack(side="left", padx=(4, 0), pady=2)

            ftp_up = ctk.CTkButton(rf, text="▲", width=cw["btn"], height=26,
                                    font=_BF, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                    command=lambda e=entry: self._upload_one(e))
            ftp_up.pack(side="left", padx=(4, 0), pady=2)    # mirror sash size|btns

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

            for w in (rf, name_lbl, det_lbl, nn_lbl, dest_lbl, st_lbl):
                w.bind("<Button-1>", lambda ev, e=entry: self._select_entry(e))
                w.bind("<Button-3>", lambda ev, e=entry: self._show_row_menu(ev, e))

            self._file_rows.append({
                "frame": rf, "name": name_lbl, "detected": det_lbl,
                "new_name": nn_lbl, "dest": dest_lbl, "status": st_lbl,
                "ftp_bar": ftp_bar, "ftp_speed": ftp_speed, "size": size_lbl,
                "ftp_up": ftp_up, "play_btn": play_btn, "fav": fav_btn, "lock": lock_btn,
                "entry": entry,
                "_raw_name": entry.name,
                "_raw_det":  det_text,
            })

        self._file_table.note_rows_rendered(len(page_files))
        self._update_status_bar()

    def _file_size_text(self, entry) -> str:
        """Peso del archivo en disco (columna "Peso", junto a "Vel.") --
        leído del filesystem, no de un dato guardado al identificar el
        archivo. El modo automático puede mover el archivo a "procesados/"
        o borrarlo justo después de subirlo (según "Acción tras subir" en
        Ajustes) sin avisar a esta fila -- entry.path se queda apuntando a
        una ruta que ya no existe, y sin este caché la columna se quedaba
        en blanco justo cuando la subida terminaba bien, como si algo
        hubiera fallado. Se guarda el último tamaño leído con éxito en la
        propia entrada y se reutiliza mientras el archivo no esté
        disponible -- solo vacío si nunca se pudo leer ni una vez."""
        try:
            size = Path(entry.path).stat().st_size
            entry._last_known_size_text = _fmt_size(size)
            entry._last_known_size_bytes = size
        except OSError:
            pass
        return entry._last_known_size_text

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
                row["dest"].configure(
                    text=_fit_text(self._preview_remote_path(entry), self._col_widths["dest"], self._font_det),
                    text_color=ACCENT if entry.remote_dir_override else PENDING_COLOR)
                row["status"].configure(text=_status_label(entry.status),
                                         text_color=sc.get(entry.status, PENDING_COLOR))
                row["size"].configure(text=self._file_size_text(entry))
                row["fav"].configure(
                    text=self._fav_symbol(entry),
                    text_color=ACCENT if self._entry_is_favorite(entry) else PENDING_COLOR,
                    state="normal" if entry.media_info else "disabled")
                row["lock"].configure(
                    text=self._lock_symbol(entry),
                    text_color=ACCENT if self._entry_is_reserved(entry) else PENDING_COLOR,
                    state="normal" if (entry.media_info and entry.status == "subido") else "disabled")
                break
        self._update_status_bar()

    def _entry_is_favorite(self, entry) -> bool:
        if not entry.media_info:
            return False
        return self._is_favorite(entry.media_info.media_type, entry.media_info.tmdb_id)

    def _fav_symbol(self, entry) -> str:
        return "★" if self._entry_is_favorite(entry) else "☆"

    def _toggle_entry_favorite(self, entry):
        if not entry.media_info:
            return
        mi = entry.media_info
        self._toggle_favorite(mi.media_type, mi.tmdb_id, mi.title,
                               on_done=lambda e=entry: self._update_row(e))

    def _entry_is_reserved(self, entry) -> bool:
        if not entry.media_info:
            return False
        return self._is_reserved(entry.media_info.media_type, entry.media_info.tmdb_id)

    def _lock_symbol(self, entry) -> str:
        return "🔒" if self._entry_is_reserved(entry) else "🔓"

    def _toggle_entry_reservation(self, entry):
        # Deliberadamente restringido a archivos ya subidos (ver el botón
        # en _refresh_table): antes de eso no hay nada en el servidor que
        # proteger. Reservar un episodio protege la serie ENTERA en
        # Liberar espacio (misma clave media_type+tmdb_id) -- así que la
        # cuota debe cargarse con el tamaño real de la serie/película
        # completa, no el de este único archivo, o un usuario podría
        # protegerse una serie de 80GB pagando solo el 1.2GB de un
        # episodio (ver _best_known_size_bytes).
        if not entry.media_info or entry.status != "subido":
            return
        mi = entry.media_info
        size_bytes = self._best_known_size_bytes(mi.media_type, mi.tmdb_id, self._file_size_bytes(entry))
        self._toggle_reservation(mi.media_type, mi.tmdb_id, mi.title, size_bytes,
                                  on_done=lambda e=entry: self._update_row(e))

    def _best_known_size_bytes(self, media_type: str, tmdb_id: int, fallback_bytes: int) -> int:
        """Mejor estimación del tamaño REAL en el servidor de una serie/
        película completa, para cargar la cuota de reservas con precisión
        al reservar desde Archivos (un solo archivo) -- si "Liberar
        espacio" ya la tiene en su caché de último análisis
        (self._cleanup_raw_items, cargada al arrancar desde
        cleanup_candidates_cache.json aunque no se haya visitado esa
        pestaña en esta sesión), se usa ESE tamaño de carpeta completa en
        vez de fallback_bytes (el de un único archivo)."""
        for it in getattr(self, "_cleanup_raw_items", []):
            if it.media_type == media_type and it.tmdb_id == tmdb_id:
                return it.size_bytes
        return fallback_bytes

    def _file_size_bytes(self, entry) -> int:
        try:
            return Path(entry.path).stat().st_size
        except OSError:
            return 0

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

    def _try_ai_fallback(self, entry, tmdb):
        """Último recurso cuando TMDB no encuentra nada con el título
        limpiado localmente: si el usuario activó el fallback de IA en
        Ajustes, le pide a la IA que identifique qué es ruido en el nombre
        de archivo. Si el título que resulta (usando nuestra propia
        detección de temporada/episodio + esos términos nuevos) SÍ encuentra
        resultado en TMDB, se aprenden esos términos para que la próxima vez
        no haga falta la IA. Si no ayuda, no se aprende nada -- evita que un
        despiste de la IA contamine la lista para futuros archivos.
        Devuelve (results, query, det) o None."""
        if not self.config_data.get("ai_fallback_enabled"):
            return None
        api_key = self.config_data.get("ai_api_key", "")
        if not api_key:
            return None
        from core.ai_title_fallback import guess_title_via_ai
        stem = entry.name[:-len(entry.ext)] if entry.ext else entry.name
        ai_result = guess_title_via_ai(stem, api_key)
        if not ai_result:
            return None
        retry_det = detect_episode(entry.name, extra_junk_terms=ai_result["junk_tokens"])
        retry_query = retry_det.get("title", "")
        if not retry_query:
            return None
        retry_results = tmdb.search_multi(retry_query)
        if not retry_results:
            return None
        from core.learned_terms import add_learned_terms
        add_learned_terms(ai_result["junk_tokens"])
        return retry_results, retry_query, retry_det

    def _search_entry(self, entry, tmdb=None):
        if tmdb is None:
            tmdb = self.tmdb
        det   = entry.detected
        query = det.get("title", "")
        if not query:
            entry.status    = "error"
            entry.error_msg = "No se pudo detectar el nombre"
            _log.warning("Busqueda: no se pudo detectar nombre para %r", entry.name)
            return
        results = tmdb.search_multi(query)
        if not results:
            fallback = self._try_ai_fallback(entry, tmdb)
            if fallback:
                results, query, det = fallback
                _log.info("Busqueda: '%s' sin resultados, la IA encontro '%s' para %r",
                          det.get("title", query), query, entry.name)
            else:
                entry.status    = "error"
                entry.error_msg = "Sin resultados en TMDB"
                _log.warning("Busqueda: sin resultados en TMDB para '%s' (%r)", query, entry.name)
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
        _log.info("Busqueda: %r -> '%s' (confianza %d%%)", entry.name, entry.new_name, entry.confidence)
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

    _SEARCH_DEBOUNCE_MS = 500   # espera tras la última tecla antes de buscar sola
    _SEARCH_MIN_CHARS   = 2     # no buscar con una sola letra

    def _on_search_key_release(self, event=None):
        """Programa una búsqueda automática tras una pausa al escribir.
        Cada tecla cancela la búsqueda programada anterior y agenda una
        nueva, así solo se llama a TMDB cuando el usuario deja de teclear."""
        # Teclas de navegación/edición que no cambian el texto no deben
        # reiniciar el temporizador ni disparar una búsqueda.
        if event is not None and event.keysym in (
                "Up", "Down", "Left", "Right", "Shift_L", "Shift_R",
                "Control_L", "Control_R", "Alt_L", "Alt_R", "Tab", "Return"):
            return
        if self._search_debounce_id is not None:
            self.after_cancel(self._search_debounce_id)
        self._search_debounce_id = self.after(self._SEARCH_DEBOUNCE_MS, self._manual_search)

    def _manual_search(self, use_ai_fallback=False):
        self._search_debounce_id = None
        query = self._result_combo.get().strip()
        # Campo vacío pero hay un archivo seleccionado: usar el título ya
        # detectado localmente para ese archivo en vez de no hacer nada --
        # solo tiene sentido para una búsqueda explícita (botón/Enter), no
        # mientras se escribe, así que _on_search_key_release nunca llega
        # aquí con el campo vacío por su cuenta.
        if not query and self._selected_entry:
            query = self._selected_entry.detected.get("title", "")
            if query:
                self._result_combo.set(query)
        if len(query) < self._SEARCH_MIN_CHARS or not self._selected_entry:
            return
        self._set_status("Buscando...", WARNING_COLOR)
        threading.Thread(target=self._manual_search_worker, args=(query, use_ai_fallback), daemon=True).start()

    def _manual_search_worker(self, query, use_ai_fallback=False):
        try:
            results = self.tmdb.search_multi(query)
            # Si mientras tanto el usuario ya escribió algo distinto, esta
            # respuesta ha quedado obsoleta -- no pisar lo que hay ahora.
            if self._result_combo.get().strip() != query:
                return
            if not results and use_ai_fallback and self._selected_entry:
                fallback = self._try_ai_fallback(self._selected_entry, self.tmdb)
                if fallback:
                    results, ai_query, _det = fallback
                    query = ai_query
                    self.after(0, lambda q=ai_query: self._result_combo.set(q))
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
            self.after(0, lambda: self._apply_search_results(labels))
        except Exception as e:
            # Ver _fire_background_link: "e" se borra al salir del except,
            # hay que capturarlo en un argumento por defecto antes de que
            # la lambda diferida lo referencie ya inexistente.
            msg = str(e)
            self.after(0, lambda m=msg: self._set_status(f"Error: {m}", ERROR_COLOR))

    def _apply_search_results(self, labels):
        # Recortar las etiquetas al ancho real del combobox: el desplegable
        # es un menú nativo del SO que se autoajusta a la etiqueta más larga,
        # así que sin esto se sale del ancho del cuadro de texto con títulos
        # largos ("El Nombre Larguísimo De La Serie (2024) [tv]").
        px_width = max(self._result_combo.winfo_width() - 34, 80)
        shown = _truncate_dropdown_labels(labels, px_width, self._search_dropdown_font)
        self._result_combo.configure(values=shown)
        # El texto que el usuario escribió se deja tal cual (no se
        # sobrescribe con el resultado) para poder seguir afinando la
        # búsqueda sin que el campo se lo trague cada vez.
        if labels:
            self._preview_result(0)
            self._set_status(f"{len(labels)} resultado(s) — pulsa ▾ para elegir uno", SUCCESS_COLOR)
            # Nota: se probó abrir el desplegable automáticamente aquí (para
            # no necesitar el clic en la flechita), pero el menú nativo que
            # usa CTkComboBox es modal/bloqueante al abrirse por código
            # fuera de un clic real del usuario — se descartó por el riesgo
            # de colgar la app. Los resultados ya están listos en cuanto se
            # despliega a mano.
        else:
            self._set_status("Sin resultados", WARNING_COLOR)

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
        App._autosize_textbox(widget)
        # El recuento de "displaylines" depende del ancho YA asentado del
        # widget -- justo tras insertar texto (p.ej. la sinopsis, que puede
        # llegar mientras el panel todavía está resolviendo su layout, o
        # justo después de cambiar de panel) el ancho real puede no
        # coincidir todavía con el que se usará de verdad, dando un
        # recuento corto y cortando el texto. Repetirlo una vez más al
        # poco (cuando el layout ya se asentó del todo) corrige ese caso
        # sin coste perceptible en el caso normal.
        widget.after(60, lambda w=widget: App._autosize_textbox(w))

    @staticmethod
    def _autosize_textbox(widget, min_lines: int = 1):
        """Ajusta la altura de un CTkTextbox de solo lectura al número de
        líneas visuales que ocupa su contenido ya envuelto (wrap="word"),
        para que no necesite scrollbar propio — el contenedor que lo rodea
        (el "Zona inferior: detalles con scroll" del panel de TMDB) es el
        único que debe scrollear si hace falta, no cada caja de texto
        suelta por dentro."""
        widget.update_idletasks()
        try:
            counted = widget._textbox.count("1.0", "end", "displaylines")
            n_lines = counted[0] if counted else 1
        except Exception:
            n_lines = 1
        n_lines = max(n_lines, min_lines)
        try:
            line_h = widget.cget("font").metrics("linespace")
        except Exception:
            line_h = 16
        # Margen generoso a propósito: el recuento de "displaylines" venía
        # quedándose corto justo en la última línea (se veía solo la parte
        # de arriba de las letras) -- mejor sobrar espacio de vez en cuando
        # que volver a cortar la sinopsis.
        widget.configure(height=n_lines * line_h + line_h + 16)

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
        # "Renombrar archivos en origen" en Ajustes -- si está desactivado,
        # el botón "Renombrar" no debe tocar el disco para nada (igual que
        # ya hace AutoWatcher): el nombre calculado se queda listo para la
        # subida (con nombre limpio, si "Renombrar en destino" está
        # activado), pero el archivo original no se mueve ni se renombra.
        if not self.config_data.get("rename_local", True):
            for entry in entries:
                self.after(0, lambda e=entry: self._update_row(e))
            self.after(0, lambda: self._set_status(
                "Renombrado en origen desactivado en Ajustes -- se mantiene el nombre original en disco",
                WARNING_COLOR))
            return

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
        if not self.config_data.get("rename_local", True):
            # Mismo criterio que _rename_worker -- no tocar el disco si
            # está desactivado en Ajustes, el nombre calculado se queda
            # listo igualmente para la subida.
            self._set_status(
                "Renombrado en origen desactivado en Ajustes -- se mantiene el nombre original en disco",
                WARNING_COLOR)
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

    def _test_ftp(self):
        host = self._ftp_entries["ftp_host"].get().strip()
        port = int(self._ftp_entries["ftp_port"].get() or 21)
        user = self._ftp_entries["ftp_user"].get().strip()
        pwd  = self._ftp_entries["ftp_password"].get()
        tls  = self._tls_switch.get() in (True, "1", 1)
        self._ftp_status.configure(text="Conectando...", text_color=WARNING_COLOR)
        def worker():
            # Conexión propia y de usar y tirar -- solo para validar que
            # estas credenciales funcionan, NUNCA self.ftp (ver
            # _refresh_ftp_space): no hace falta dejarla abierta para nadie.
            from core.ftp_client import FTPClient as _FTPClient
            own_ftp = _FTPClient()
            ok, msg = own_ftp.connect(host, port, user, pwd, tls)
            own_ftp.disconnect()
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

    def _validate_ai_key(self):
        """Valida la API Key de Groq (fallback de IA) -- independiente del
        botón de arriba, que solo valida la de TMDB."""
        key = self._ai_key_entry.get().strip()
        if not key:
            self._ai_key_status.configure(text="Ingresa una API Key", text_color=ERROR_COLOR)
            return
        self._ai_key_status.configure(text="Validando...", text_color=WARNING_COLOR)
        def worker():
            from core.ai_title_fallback import validate_api_key
            ok  = validate_api_key(key)
            msg = "API Key válida" if ok else "API Key inválida"
            self.after(0, lambda: self._ai_key_status.configure(
                text=msg, text_color=SUCCESS_COLOR if ok else ERROR_COLOR))
        threading.Thread(target=worker, daemon=True).start()

    def _open_learned_terms_dialog(self):
        """Ver/añadir/quitar a mano los términos que el fallback de IA ha
        ido aprendiendo (core/learned_terms.py) -- por si alguno resultó ser
        un error y hace falta corregirlo sin tocar el JSON a mano.

        Se construye una sola vez y se reutiliza (self._learned_terms_win)
        en vez de crear un CTkToplevel/CTkScrollableFrame nuevo cada
        apertura, por la misma fuga de bind_all(<MouseWheel>/...) de
        customtkinter documentada en _DubHiddenDialog -- en el reabierto
        solo se recarga la lista de términos (self._learned_terms_refresh)."""
        if self._learned_terms_win is not None and self._learned_terms_win.winfo_exists():
            self._learned_terms_win.grab_set()
            self._learned_terms_win.lift()
            self._learned_terms_win.focus_force()
            self._learned_terms_refresh()
            return

        win = ctk.CTkToplevel(self)
        self._apply_icon(win)
        win.title("Términos aprendidos")
        win.resizable(False, False)
        win.grab_set()
        win.lift()
        win.focus_force()

        ctk.CTkLabel(win, text="Términos que la IA identificó como ruido técnico y que\n"
                               "ya se reconocen solos, sin volver a consultarla.",
                     font=ctk.CTkFont(size=12), text_color=PENDING_COLOR,
                     justify="left").pack(padx=16, pady=(16, 8), anchor="w")

        add_fr = ctk.CTkFrame(win, fg_color="transparent")
        add_fr.pack(fill="x", padx=16, pady=(0, 8))
        add_entry = ctk.CTkEntry(add_fr, width=280, placeholder_text="Añadir término manualmente...")
        add_entry.pack(side="left")

        list_fr = ctk.CTkScrollableFrame(win, label_text="", width=380, height=240)
        list_fr.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        def _refresh():
            for w in list_fr.winfo_children():
                w.destroy()
            from core.learned_terms import load_learned_terms
            terms = load_learned_terms()
            if not terms:
                ctk.CTkLabel(list_fr, text="Ningún término aprendido todavía.",
                             text_color=PENDING_COLOR).pack(pady=12)
                return
            for term in terms:
                row = ctk.CTkFrame(list_fr, fg_color="transparent")
                row.pack(fill="x", pady=2)
                ctk.CTkLabel(row, text=term, anchor="w").pack(side="left", fill="x", expand=True, padx=(4, 0))
                ctk.CTkButton(row, text="✕", width=28, fg_color="transparent", border_width=1,
                              text_color=ERROR_COLOR,
                              command=lambda t=term: _remove(t)).pack(side="right", padx=4)

        def _remove(term):
            from core.learned_terms import remove_learned_term
            remove_learned_term(term)
            _refresh()

        def _add():
            term = add_entry.get().strip()
            if not term:
                return
            from core.learned_terms import add_learned_terms
            add_learned_terms([term])
            add_entry.delete(0, "end")
            _refresh()

        add_entry.bind("<Return>", lambda _: _add())
        ctk.CTkButton(add_fr, text="+ Añadir", width=80, command=_add).pack(side="left", padx=(6, 0))

        _refresh()

        def _hide():
            win.grab_release()
            win.withdraw()
        ctk.CTkButton(win, text="Cerrar", command=_hide).pack(pady=(0, 16))
        win.protocol("WM_DELETE_WINDOW", _hide)

        # Centrar en la ventana padre, ya con el tamaño real del contenido
        win.update_idletasks()
        dw = win.winfo_reqwidth()
        dh = win.winfo_reqheight()
        px = self.winfo_rootx() + self.winfo_width() // 2
        py = self.winfo_rooty() + self.winfo_height() // 2
        win.geometry(f"{dw}x{dh}+{px - dw//2}+{py - dh//2}")

        self._learned_terms_win = win
        self._learned_terms_refresh = _refresh

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
        try:
            quota_gb = max(1, int(self._reservation_quota_entry.get().strip() or 100))
        except ValueError:
            quota_gb = 100

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
            "shared_data_ftp_path": self._shared_data_ftp_path_entry.get().strip(),
            "app_user_name":        self._app_user_name_entry.get().strip(),

            "tmdb_api_key": self._api_key_entry.get().strip(),
            "language":     self._lang_combo.get(),

            "ai_fallback_enabled": self._ai_fallback_switch.get() in (True, "1", 1),
            "ai_api_key":          self._ai_key_entry.get().strip(),

            "plex_enabled": self._plex_switch.get() in (True, "1", 1),
            "plex_host":    self._plex_host_entry.get().strip(),
            "plex_token":   self._plex_token_entry.get().strip(),
            "jellyfin_enabled":  self._jellyfin_switch.get() in (True, "1", 1),
            "jellyfin_host":     self._jellyfin_host_entry.get().strip(),
            "jellyfin_api_key":  self._jellyfin_key_entry.get().strip(),
            "jellyfin_username": self._jellyfin_username_entry.get().strip(),

            "tv_template":    self._tpl_entries["tv_template"].get().strip(),
            "movie_template": self._tpl_entries["movie_template"].get().strip(),
            "anime_template": self._tpl_entries["anime_template"].get().strip(),

            "custom_links_show": [
                {"name": w["name"].get().strip(), "url_template": w["url"].get().strip(),
                 "background": w["background"].get()}
                for w in self._custom_links_widgets["show"]
                if w["name"].get().strip() or w["url"].get().strip()
            ],
            "custom_links_season": [
                {"name": w["name"].get().strip(), "url_template": w["url"].get().strip(),
                 "background": w["background"].get()}
                for w in self._custom_links_widgets["season"]
                if w["name"].get().strip() or w["url"].get().strip()
            ],
            "custom_links_episode": [
                {"name": w["name"].get().strip(), "url_template": w["url"].get().strip(),
                 "background": w["background"].get()}
                for w in self._custom_links_widgets["episode"]
                if w["name"].get().strip() or w["url"].get().strip()
            ],

            "reservation_quota_gb": quota_gb,

            "ftp_categories": {
                "tv":    [self._category_to_plain_dict(c) for c in self._tv_categories],
                "movie": [self._category_to_plain_dict(c) for c in self._movie_categories],
            },
        }

    def _settings_dirty(self) -> bool:
        """True si algún campo de Ajustes difiere de lo último guardado."""
        current = self._collect_settings()
        return any(self.config_data.get(key) != value for key, value in current.items())

    def _resolve_app_user_name_change(self, new_name: str) -> str:
        """Valida el cambio de "Tu nombre" ANTES de guardar nada -- llamado
        desde _save_all_settings. Puede devolver un nombre distinto al
        pedido (revertido al anterior) si el usuario cancela alguno de los
        pasos, para que _save_all_settings no lo persista.

        Dos comprobaciones, en este orden:
        1. Unicidad: ¿ya hay reservas de otra persona con ese mismo
           nombre? Bloquea tanto crear el nombre por primera vez como
           cambiarlo -- compartir nombre mezclaría cuotas y podría
           impedir liberar una reserva ajena por confundirla con propia.
        2. Si había un nombre anterior CON reservas propias, preguntar qué
           hacer con ellas: traspasarlas al nombre nuevo o desprotegerlas
           todas (_RenameReservationsDialog). Cancelar aquí revierte el
           nombre entero, no solo esta comprobación."""
        old_name = self.config_data.get("app_user_name", "").strip()
        if new_name == old_name:
            return new_name

        from core.reservations import is_name_taken, used_bytes
        if new_name and is_name_taken(self._reservations, new_name, exclude=old_name or None):
            messagebox.showerror(
                "Nombre en uso",
                f"Ya hay reservas hechas por alguien llamado \"{new_name}\" -- elige un nombre "
                "distinto para no mezclar tu cuota de reservas con la suya.")
            return old_name

        if old_name and used_bytes(self._reservations, old_name) > 0:
            dlg = _RenameReservationsDialog(self, old_name, new_name)
            if dlg.result == "transfer":
                self._transfer_reservations(old_name, new_name)
            elif dlg.result == "unprotect":
                self._unprotect_all_reservations(old_name)
            else:
                return old_name

        return new_name

    def _save_all_settings(self):
        data = self._collect_settings()
        resolved_name = self._resolve_app_user_name_change(data["app_user_name"])
        if resolved_name != data["app_user_name"]:
            data["app_user_name"] = resolved_name
            self._app_user_name_entry.delete(0, "end")
            self._app_user_name_entry.insert(0, resolved_name)
        self.config_data.set_many(data)
        self.config_data.save()
        self._set_autostart(data["start_with_windows"])
        self.tmdb.set_api_key(data["tmdb_api_key"])
        self.tmdb.set_language(data["language"])
        if self._watcher and self._watcher.running:
            self._watcher.poll_interval = data["poll_interval"]
        self._invalidate_missing_ep_detail_frames()
        self._set_status("✓ Configuración guardada", SUCCESS_COLOR)

    def _invalidate_missing_ep_detail_frames(self):
        """Descarta los frames de detalle (temporada/episodio) ya
        construidos en Episodios -- el botón de un enlace personalizable
        guarda su plantilla en el propio comando al construirse (closure
        de Python), y ese frame se cachea por serie para no reconstruir
        la tabla entera al expandir/colapsar (ver
        _render_missing_episodes_table). Sin esto, editar una plantilla
        en Ajustes no se reflejaba hasta reiniciar la app: las series que
        estén desplegadas ahora mismo se reconstruyen al momento; el
        resto, la próxima vez que se despliegen."""
        for tmdb_id, widgets in getattr(self, "_missing_ep_row_widgets", {}).items():
            detail_fr = widgets.get("detail_fr")
            if detail_fr is None:
                continue
            detail_fr.destroy()
            widgets["detail_fr"] = None
            if tmdb_id in self._missing_ep_expanded:
                widgets["detail_fr"] = self._build_missing_ep_detail_frame(widgets["row_fr"], widgets["r"])
                widgets["detail_fr"].pack(fill="x", padx=(36, 12), pady=(0, 8))
        self._missing_ep_season_widgets = {}
        # Si hay una ficha de serie abierta en el panel lateral ahora
        # mismo, sus botones a nivel serie también hay que refrescarlos
        # (esos no se cachean por frame, pero sí conviene repintarlos
        # para que usen la plantilla nueva sin tener que volver a pulsar
        # la serie).
        if getattr(self, "_missing_ep_current_row", None) is not None:
            self._render_missing_ep_show_links(self._missing_ep_current_row)

    def _set_status(self, msg, color=None):
        self._status_lbl.configure(text=msg, text_color=color or ACCENT)

    # ──────────────────────────────────────── Bandeja del sistema ──

    def _get_tray_image(self):
        try:
            # self._icon_path (.ico) solo existe en Windows; en macOS/Linux
            # el icono real está en self._icon_png_path (.png) — sin este
            # segundo caso, la bandeja siempre caía al círculo azul
            # genérico de más abajo fuera de Windows.
            source = self._icon_path or self._icon_png_path
            if source:
                return _PILImage.open(source).convert("RGBA")
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
            if is_macos():
                # pystray.Icon.run() "must be called from the main thread"
                # (su propia documentación) — en macOS esto es estricto
                # porque AppKit exige que el runloop se gestione desde el
                # hilo principal, y lanzarlo en un hilo secundario (como se
                # hace abajo para Windows/Linux) puede colgar o crashear la
                # app. El backend darwin de pystray está pensado para
                # integrarse con el mainloop de otra librería ya activo en
                # el hilo principal (aquí, el de Tk) vía run_detached(), que
                # no bloquea: solo marca el icono como listo y reutiliza el
                # runloop de Cocoa que Tk ya está bombeando.
                self._tray.run_detached()
            else:
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
        """Añade o elimina el arranque automático al iniciar sesión —
        registro de Windows o LaunchAgent de macOS según la plataforma."""
        if is_windows():
            self._set_autostart_windows(enabled)
        elif is_macos():
            self._set_autostart_macos(enabled)
        # Linux: sin una convención única estándar (systemd user unit vs.
        # .desktop en autostart/) — no implementado, el switch no hace nada.

    def _set_autostart_windows(self, enabled: bool):
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

    _MACOS_LAUNCH_AGENT_LABEL = "com.arenombrar.app"

    def _macos_launch_agent_path(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{self._MACOS_LAUNCH_AGENT_LABEL}.plist"

    def _set_autostart_macos(self, enabled: bool):
        """Equivalente macOS del registro de Windows: un LaunchAgent de
        usuario (~/Library/LaunchAgents/*.plist) con RunAtLoad, cargado con
        "launchctl load -w" para que también tenga efecto inmediato, no solo
        en el próximo inicio de sesión."""
        import plistlib
        import subprocess
        plist_path = self._macos_launch_agent_path()
        try:
            if enabled:
                if getattr(sys, "frozen", False):
                    args = [sys.executable, "--minimized"]
                else:
                    main_py = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "main.py"))
                    args = [sys.executable, main_py, "--minimized"]
                plist = {
                    "Label": self._MACOS_LAUNCH_AGENT_LABEL,
                    "ProgramArguments": args,
                    "RunAtLoad": True,
                }
                plist_path.parent.mkdir(parents=True, exist_ok=True)
                with open(plist_path, "wb") as f:
                    plistlib.dump(plist, f)
                subprocess.run(["launchctl", "load", "-w", str(plist_path)],
                                capture_output=True)
            elif plist_path.exists():
                subprocess.run(["launchctl", "unload", "-w", str(plist_path)],
                                capture_output=True)
                plist_path.unlink()
        except Exception as e:
            self._set_status(f"Error LaunchAgent: {e}", ERROR_COLOR)

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
        # Sin esto, cerrar estando minimizado a bandeja (o Cmd+Q en macOS,
        # que llega aquí directo sin pasar por _tray_quit) dejaba el icono de
        # pystray corriendo mientras el proceso terminaba -- en macOS en
        # concreto, el backend darwin reutiliza el runloop de Cocoa de Tk
        # (ver _minimize_to_tray), así que destruir la ventana sin parar antes
        # el icono deja su NSStatusItem/observer a medio desregistrar.
        if self._tray and self._tray_running:
            self._tray_running = False
            self._tray.stop()
        if self._watcher:
            self._watcher.stop()
        with self._ftp_cmd_lock:
            self.ftp.disconnect()
        self.config_data.save()
        self._save_session()
        self.destroy()


    # ──────────────────────── Menú contextual de fila (reordenar / resetear) ──

    def _edit_remote_dir(self, entry):
        """Diálogo para fijar a mano la carpeta remota de destino de
        *entry* -- doble clic en la columna "Destino" de la tabla. Mientras
        esté fijada, prevalece sobre la categoría/género calculados
        automáticamente (ver _preview_remote_path y _upload_entry_with)."""
        auto_dir = self._preview_remote_path(entry).rsplit("/", 1)[0]
        current_dir = entry.remote_dir_override or auto_dir

        dlg = ctk.CTkToplevel(self)
        self._apply_icon(dlg)
        dlg.title("Carpeta de destino")
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.lift()

        ctk.CTkLabel(dlg, text=f"Carpeta remota para:\n{entry.name}",
                     font=ctk.CTkFont(size=12, weight="bold"), justify="left",
                     wraplength=380).pack(padx=20, pady=(20, 8))
        path_entry = ctk.CTkEntry(dlg, width=380)
        path_entry.insert(0, current_dir)
        path_entry.pack(padx=20, pady=(0, 4))
        if entry.remote_dir_override:
            ctk.CTkLabel(dlg, text=f"Automático (por categoría): {auto_dir or '—'}",
                         font=ctk.CTkFont(size=11), text_color=PENDING_COLOR,
                         wraplength=380, justify="left").pack(padx=20, pady=(0, 8))

        def _save():
            new_dir = path_entry.get().strip()
            entry.remote_dir_override = new_dir or None
            self._update_row(entry)
            dlg.destroy()

        def _use_auto():
            entry.remote_dir_override = None
            self._update_row(entry)
            dlg.destroy()

        bf = ctk.CTkFrame(dlg, fg_color="transparent")
        bf.pack(padx=20, pady=(12, 20))
        ctk.CTkButton(bf, text="Guardar", command=_save, width=100,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER).pack(side="left", padx=4)
        ctk.CTkButton(bf, text="Usar automático", command=_use_auto, width=140,
                      fg_color="transparent", border_width=1).pack(side="left", padx=4)
        ctk.CTkButton(bf, text="Cancelar", command=dlg.destroy, width=90,
                      fg_color="transparent", border_width=1).pack(side="left", padx=4)

        dlg.update_idletasks()
        pw = self.winfo_rootx() + self.winfo_width() // 2
        ph = self.winfo_rooty() + self.winfo_height() // 2
        dlg.geometry(f"+{pw - dlg.winfo_reqwidth()//2}+{ph - dlg.winfo_reqheight()//2}")

    def _show_row_menu(self, event, entry):
        # Seleccionar la fila al abrir el menú -- si no, es fácil pulsar
        # con el botón derecho sobre una fila distinta a la que se tenía
        # seleccionada y acabar aplicando la acción (o viendo el panel de
        # detalles) sobre la fila equivocada.
        self._select_entry(entry)

        menu = tk.Menu(self, tearoff=0)
        try:
            idx = self.files.index(entry)
        except ValueError:
            return
        n = len(self.files)

        # -- Identificación / subida --
        if entry.status != "subido":
            menu.add_command(label="🔍 Buscar de nuevo en TMDB",
                             command=lambda: self._search_new_entries([entry]))
        if entry.media_info and entry.status in ("listo", "renombrado", "error", "omitido"):
            menu.add_command(label="▲  Subir este archivo", command=lambda: self._upload_one(entry))
        menu.add_command(label="✎  Editar carpeta de destino…",
                         command=lambda: self._edit_remote_dir(entry))
        menu.add_separator()

        # -- Archivo --
        menu.add_command(label="▶  Reproducir", command=lambda: self._play_file(entry))
        menu.add_command(label="📂 Abrir carpeta contenedora",
                         command=lambda: self._open_containing_folder(entry))
        menu.add_command(label="📋 Copiar nombre original",
                         command=lambda: self._copy_to_clipboard(entry.name))
        if entry.new_name:
            menu.add_command(label="📋 Copiar nuevo nombre",
                             command=lambda: self._copy_to_clipboard(entry.new_name))
        menu.add_separator()

        # -- Orden en la lista --
        if idx > 0:
            menu.add_command(label="▲  Mover arriba",     command=lambda: self._move_entry(entry, -1))
            menu.add_command(label="⏫ Ir al principio",  command=lambda: self._move_entry_to(entry, 0))
        if idx < n - 1:
            menu.add_command(label="▼  Mover abajo",      command=lambda: self._move_entry(entry, 1))
            menu.add_command(label="⏬ Ir al final",       command=lambda: self._move_entry_to(entry, n - 1))

        # -- Estado / quitar --
        if entry.status in ("subido", "renombrado", "error"):
            menu.add_separator()
            menu.add_command(label="↺  Restablecer (volver a pendiente)",
                             command=lambda: self._reset_entry(entry))
        menu.add_separator()
        menu.add_command(label="✕  Quitar de la lista", command=lambda: self._remove_entry(entry))

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
        if is_windows():
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
        # Fallback: Centro de Notificaciones vía osascript (macOS) — no
        # requiere ninguna dependencia extra, "osascript" viene con el
        # sistema. Comillas dobles escapadas a mano porque el AppleScript en
        # sí va entre comillas dobles en el argumento de -e.
        elif is_macos():
            try:
                safe_title = title.replace('"', "'")[:100]
                safe_msg   = msg.replace('"', "'")[:200]
                script = f'display notification "{safe_msg}" with title "{safe_title}"'
                subprocess.Popen(["osascript", "-e", script])
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

    def _save_history_entry(self, filename: str, remote: str, status: str, size: int, error_msg: str = "",
                             local_path: str = ""):
        entry = {
            "ts":        _time.time(),
            "filename":  filename,
            "remote":    remote,
            "status":    status,
            "size":      size,
            "error_msg": error_msg,
            # Ruta local en el momento de la subida -- solo para poder
            # reintentar directamente desde Historial sin tener que
            # volver a Archivos (ver _retry_history_upload). Vacía en
            # registros de antes de este campo; el botón "Reintentar"
            # se deshabilita en ese caso.
            "local_path": local_path,
            # Quién lo subió -- para el historial de actividad compartido
            # entre clientes (ver _push_activity_entry_to_ftp), y también
            # útil ya en local si en el futuro hace falta distinguir.
            "person":    self.config_data.get("app_user_name", ""),
        }
        with self._history_lock:
            history = self._load_history()
            history.append(entry)
            # Mantener solo los últimos 500 registros
            if len(history) > 500:
                history = history[-500:]
            try:
                self._history_path().write_text(
                    json.dumps(history, ensure_ascii=False, indent=2),
                    encoding="utf-8")
            except Exception:
                pass
            self._history_dirty = True
        self._push_activity_entry_to_ftp(entry, "subida")

    # ── Historial de borrados ("Liberar espacio") -- mismo patrón que el
    # historial de subidas de arriba, en un archivo aparte para no
    # mezclar "lo que se subió" con "lo que se borró", que son acciones
    # de naturaleza muy distinta (una añade, la otra quita para siempre).

    def _deletion_history_path(self) -> Path:
        return _appdata_dir() / "deletion_history.json"

    def _load_deletion_history(self) -> list:
        try:
            p = self._deletion_history_path()
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
        return []

    def _save_deletion_history_entry(self, name: str, ftp_path: str, size_bytes: int,
                                     reason: str, status: str, error_msg: str = ""):
        """reason: descripción legible de qué filtros la marcaron como
        candidata (p.ej. "vista, sin repetir en 12 meses") -- para poder
        entender después POR QUÉ se borró algo, no solo QUÉ."""
        entry = {
            "ts":        _time.time(),
            "name":      name,
            "ftp_path":  ftp_path,
            "size":      size_bytes,
            "reason":    reason,
            "status":    status,
            "error_msg": error_msg,
            # Quién lo borró -- ver _save_history_entry.
            "person":    self.config_data.get("app_user_name", ""),
        }
        with self._history_lock:
            history = self._load_deletion_history()
            history.append(entry)
            if len(history) > 500:
                history = history[-500:]
            try:
                self._deletion_history_path().write_text(
                    json.dumps(history, ensure_ascii=False, indent=2),
                    encoding="utf-8")
            except Exception:
                pass
        self._push_activity_entry_to_ftp(entry, "borrado")

    def _build_history_tab(self, parent):
        """Vista de historial integrada (igual que Episodios/Configuración,
        ver _show_view) -- sustituye a la antigua ventana emergente para
        que se navegue igual que el resto de la app."""
        parent.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(parent, fg_color=("gray90", "gray20"), corner_radius=8)
        header.grid(row=0, column=0, sticky="ew", pady=(0, CONTAINER_GAP))
        self._history_title_lbl = ctk.CTkLabel(
            header, text="Historial de subidas", font=ctk.CTkFont(size=14, weight="bold"))
        self._history_title_lbl.pack(side="left", padx=12, pady=8)
        # "Ver todo el servidor" -- mismo componente y mismo sitio
        # conceptual que ya usa Protegidos (self._protected_show_all_var):
        # apagado (por defecto) muestra solo lo que este cliente subió,
        # igual que siempre; encendido muestra el historial de actividad
        # compartido (subidas Y borrados de todos los clientes del mismo
        # servidor, ver _push_activity_entry_to_ftp/_shared_activity_history).
        self._history_show_all_var = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(header, text="Ver todo el servidor", variable=self._history_show_all_var,
                      command=self._on_history_show_all_toggled).pack(side="left", padx=(0, 12), pady=8)
        ctk.CTkButton(header, text="🗑 Limpiar historial", width=150,
                      fg_color="transparent", border_width=1,
                      border_color=ERROR_COLOR, text_color=ERROR_COLOR,
                      hover_color=("gray85", "#3d1010"),
                      command=self._clear_history).pack(side="right", padx=(4, 12), pady=8)
        ctk.CTkButton(header, text="📥 Descargar log completo", width=180,
                      fg_color="transparent", border_width=1,
                      command=self._export_log).pack(side="right", padx=4, pady=8)
        ctk.CTkButton(header, text="📥 Descargar historial", width=170,
                      fg_color="transparent", border_width=1,
                      command=self._export_history).pack(side="right", padx=4, pady=8)

        table_fr = ctk.CTkFrame(parent, fg_color="transparent")
        table_fr.grid(row=1, column=0, sticky="nsew")
        table_fr.grid_columnconfigure(0, weight=1)
        table_fr.grid_rowconfigure(0, weight=1)

        # Paginado (ver TableView.page_size, calculado dinámicamente): con hasta 500 registros y 5
        # widgets por fila, dibujar el historial entero de golpe llega a
        # sumar miles de ventanas nativas de Tk -- Windows limita a 10000
        # objetos GUI por proceso, y superarlo rompe el pintado de TODA
        # la ventana en silencio (no solo de esta tabla). Ver el mismo
        # paginado en Episodios que faltan y Liberar espacio. Debajo de
        # la tabla (no encima) y centrada -- sin sticky="ew", para que el
        # frame ocupe solo su tamaño natural y quede centrado en la
        # columna (que sí tiene weight=1).
        nav_fr = ctk.CTkFrame(parent, fg_color="transparent")
        nav_fr.grid(row=2, column=0, pady=(6, 0))
        self._history_prev_btn = ctk.CTkButton(
            nav_fr, text="< Anterior", width=100, fg_color="transparent", border_width=1,
            command=lambda: self._history_change_page(-1))
        self._history_prev_btn.pack(side="left")
        self._history_page_lbl = ctk.CTkLabel(nav_fr, text="", text_color=PENDING_COLOR)
        self._history_page_lbl.pack(side="left", padx=12)
        self._history_next_btn = ctk.CTkButton(
            nav_fr, text="Siguiente >", width=100, fg_color="transparent", border_width=1,
            command=lambda: self._history_change_page(1))
        self._history_next_btn.pack(side="left")

        self._history_col_order = ["fecha", "archivo", "tipo", "cliente", "destino", "tamano", "estado"]
        self._history_font = ctk.CTkFont(size=11)
        self._history_rows = []
        self._history_empty_msg = None   # label "sin subidas...", ver _refresh_history_view
        self._history_all = []   # historial completo (más reciente primero), ver _refresh_history_view
        self._history_page = 0
        self._history_dirty = True   # ver _refresh_history_view: solo se relee/redibuja si hubo cambios reales

        # TableView: cabecera fija (no scrollea con las filas) + cuerpo
        # con scroll -- mismo componente que usan Archivos/Episodios/
        # Liberar espacio, ver gui/table_view.py. Los anchos y el propio
        # arrastre de separadores los gestiona el componente; las filas
        # solo necesitan leer table.col_width(key) para pintarse.
        sw0 = self._saved_col_widths("historial")
        self._history_table = TableView(table_fr, columns=[
            ColumnSpec("fecha", "Fecha", width=sw0.get("fecha", 130), min_width=50, resizable=True),
            ColumnSpec("archivo", "Archivo", width=sw0.get("archivo", 280), min_width=50, resizable=True),
            # tipo/cliente: solo tienen contenido variado con "Ver todo el
            # servidor" activo (en local, "archivo" siempre es "Subida" y
            # "cliente" siempre este mismo usuario) -- se muestran siempre
            # de todas formas, mismo criterio que la columna "Persona" ya
            # existente en Sincronizar visionado, para no reconstruir la
            # tabla entera solo por encender/apagar el interruptor.
            ColumnSpec("tipo", "Tipo", width=sw0.get("tipo", 70), min_width=50, resizable=True),
            ColumnSpec("cliente", "Cliente", width=sw0.get("cliente", 110), min_width=50, resizable=True),
            ColumnSpec("destino", "Destino FTP / motivo", width=sw0.get("destino", 200), min_width=50, resizable=True),
            ColumnSpec("tamano", "Tamaño", width=sw0.get("tamano", 70), min_width=50, resizable=True),
            ColumnSpec("estado", "Estado", width=sw0.get("estado", 80), min_width=50),
            ColumnSpec("accion", "", width=110),
        ])
        self._history_table.grid(row=0, column=0, sticky="nsew")
        # Al arrastrar un separador, redibujar también las filas ya
        # pintadas con los anchos nuevos -- TableView solo se encarga de
        # su propia cabecera.
        self._history_table.on_column_resize = lambda: self._history_render_page()
        self._history_table.on_widths_changed = lambda w: self._save_table_col_widths("historial", w)
        self._history_table.enable_dynamic_page_size(self._on_history_page_size_changed)

        # Diferido -- ver el mismo motivo en _build_missing_episodes_tab:
        # esta pestaña se construye al arrancar aunque esté oculta, y el
        # historial puede tener hasta 500 registros (un widget por cada
        # uno). _show_view() ya llama a _refresh_history_view() de
        # verdad cada vez que se entra en esta vista, así que esto solo
        # afecta al primer dibujado al arrancar.
        self.after(120, self._refresh_history_view)

    def _refresh_history_view(self):
        """Recarga el historial de disco y redibuja la página actual --
        se llama al construir la vista y cada vez que se entra en ella
        (_show_view), por si hubo subidas nuevas desde la última vez que
        se miró. Si no hay subidas/borrados nuevos desde la última vez
        (self._history_dirty), no hace nada -- releer el JSON y reconstruir
        la tabla en cada cambio de pestaña era trabajo desperdiciado la
        mayoría de las veces. Como es una recarga desde disco (el
        orden/contenido puede haber cambiado), vuelve siempre a la primera
        página."""
        _t0 = _time.perf_counter()
        skipped = not self._history_dirty
        try:
            if skipped:
                return
            if self._history_show_all_var.get():
                # Ya sincronizado en memoria (ver _sync_activity_history_from_ftp,
                # llamado al entrar en esta pestaña) -- no hace falta releer
                # nada de disco para esta rama.
                self._history_all = list(reversed(self._shared_activity_history))
                self._history_title_lbl.configure(
                    text=f"Historial de subidas  ({len(self._history_all)} registros, todo el servidor)")
            else:
                history = self._load_history()
                self._history_all = list(reversed(history))   # más reciente primero
                self._history_title_lbl.configure(text=f"Historial de subidas  ({len(history)} registros)")
            self._history_page = 0
            self._history_render_page()
            self._history_dirty = False
        finally:
            _log.info("Vista: _refresh_history_view %6.0f ms%s", (_time.perf_counter() - _t0) * 1000,
                       " (sin cambios, omitido)" if skipped else "")

    def _on_history_show_all_toggled(self):
        self._history_dirty = True
        self._refresh_history_view()

    def _history_change_page(self, delta: int):
        n_pages = max(1, -(-len(self._history_all) // self._history_table.page_size))
        new_page = max(0, min(n_pages - 1, self._history_page + delta))
        if new_page == self._history_page:
            return
        self._history_page = new_page
        self._history_render_page()

    def _on_history_page_size_changed(self, new_size: int):
        """Callback de TableView.enable_dynamic_page_size -- a diferencia
        de Archivos/Episodios/Liberar espacio, _history_render_page_impl
        NO reclampa self._history_page por su cuenta (solo lo hace
        _history_change_page), así que un cambio de tamaño de página por
        redimensionado necesita reclamparlo aquí antes de redibujar, o el
        usuario podría quedar "aparcado" en una página que ya no existe."""
        n_pages = max(1, -(-len(self._history_all) // new_size))
        self._history_page = max(0, min(n_pages - 1, self._history_page))
        self._history_render_page()

    def _history_render_page(self):
        _t0 = _time.perf_counter()
        try:
            self._history_render_page_impl()
        finally:
            _log.info("Vista: _history_render_page %6.0f ms", (_time.perf_counter() - _t0) * 1000)

    def _history_render_page_impl(self):
        """Dibuja solo la página actual (self._history_page) de
        self._history_all -- ver TableView.page_size. Separado de
        _refresh_history_view para que cambiar de página no implique
        releer el historial de disco."""
        import datetime

        self._history_table.clear_rows()
        self._history_rows = []
        self._history_empty_msg = None

        total = len(self._history_all)
        page_size = self._history_table.page_size
        n_pages = max(1, -(-total // page_size))
        start = self._history_page * page_size
        page_items = self._history_all[start:start + page_size]

        self._history_page_lbl.configure(
            text=f"Página {self._history_page + 1} de {n_pages}" if total else "")
        self._history_prev_btn.configure(state="normal" if self._history_page > 0 else "disabled")
        self._history_next_btn.configure(state="normal" if self._history_page < n_pages - 1 else "disabled")

        # Volver arriba del todo -- si no, al cambiar de página con el
        # scroll bajado, las filas nuevas se dibujan pero el hueco (ahora
        # vacío) por el que se había bajado se queda visible.
        self._history_table.scroll_to_top()

        if not page_items:
            self._history_empty_msg = ctk.CTkLabel(
                self._history_table.body, text="Sin subidas registradas todavía.", text_color=PENDING_COLOR)
            self._history_empty_msg.pack(pady=30)
            return

        cw = {key: self._history_table.col_width(key) for key in self._history_col_order}
        font = self._history_font
        sc = {"ok": SUCCESS_COLOR, "error": ERROR_COLOR}
        for entry in page_items:
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
            # kind: solo lo llevan las entradas del historial compartido
            # (ver _push_activity_entry_to_ftp) -- las locales son siempre
            # subidas, así que a falta de esta clave se asume "subida".
            kind = entry.get("kind", "subida")
            is_deletion = kind == "borrado"
            # Si falló, en esta columna es más útil el motivo que la ruta
            # remota -- en un borrado, "reason" (por qué se marcó como
            # candidata) ocupa el mismo hueco conceptual.
            error_msg = entry.get("error_msg", "")
            showing_error = st == "error" and bool(error_msg)
            if is_deletion:
                destino_text = entry.get("reason", "")
                archivo_text = entry.get("name", "")
            else:
                destino_text = error_msg if showing_error else entry.get("remote", "")
                archivo_text = entry.get("filename", "")
            raw = {
                "fecha": ts, "archivo": archivo_text,
                "tipo": "Borrado" if is_deletion else "Subida",
                "cliente": entry.get("person", ""),
                "destino": destino_text,
                "tamano": sz_str, "estado": st.capitalize(),
            }
            extra_by_col = {
                "destino": {"text_color": ERROR_COLOR} if showing_error else {},
                "estado": {"text_color": sc.get(st, PENDING_COLOR)},
            }
            # Sin fg_color propio -- mismo fondo por defecto que las
            # filas de Archivos/Episodios, no transparente (para que la
            # tabla se vea igual, no "sin color" de fondo).
            row_fr = ctk.CTkFrame(self._history_table.body)
            row_fr.pack(fill="x", pady=1, padx=2)
            row_labels = {"_raw": raw}
            for idx, key in enumerate(self._history_col_order):
                lbl = ctk.CTkLabel(row_fr, text=_fit_text(raw[key], cw[key], font), width=cw[key],
                                   anchor="w", font=font, **extra_by_col.get(key, {}))
                # padx=(4, 0) espeja el separador de 4px de la cabecera en
                # todas menos la primera columna, igual que en Archivos.
                lbl.pack(side="left", padx=(4, 0) if idx > 0 else (0, 0), pady=2)
                row_labels[key] = lbl

            # Solo tiene sentido reintentar subidas que fallaron -- y solo
            # si se guardó la ruta local en su momento (registros de antes
            # de este campo, o si la ruta ya no existe, se avisa al pulsar
            # en vez de deshabilitarlo aquí en silencio, ver
            # _retry_history_upload). Un borrado no tiene "reintentar" en
            # este mismo sentido -- deshabilitado siempre para esas filas.
            retry_btn = ctk.CTkButton(
                row_fr, text="🔄 Reintentar", width=self._history_table.col_width("accion"),
                fg_color="transparent", border_width=1,
                state="normal" if (st == "error" and not is_deletion) else "disabled",
                command=lambda e=entry: self._retry_history_upload(e))
            retry_btn.pack(side="left", padx=(4, 4), pady=2)
            row_labels["accion"] = retry_btn

            self._history_rows.append(row_labels)

        self._history_table.note_rows_rendered(len(page_items))

    def _retry_history_upload(self, entry: dict):
        """Reintenta una subida fallida directamente desde Historial, sin
        tener que volver a Archivos y añadir el archivo otra vez a mano.
        No pasa por la detección/categoría de TMDB -- ya sabemos exacta-
        mente adónde tenía que ir (entry["remote"]), así que solo repite
        la transferencia en sí (con reanudación si quedó un archivo
        parcial del intento anterior)."""
        local_path = entry.get("local_path", "")
        if not local_path or not os.path.exists(local_path):
            messagebox.showwarning(
                "No se puede reintentar",
                "El archivo original ya no está en esa ubicación -- súbelo de nuevo desde Archivos.")
            return
        remote = entry.get("remote", "")
        if not remote or "/" not in remote:
            messagebox.showwarning("No se puede reintentar", "Falta la ruta de destino de este registro.")
            return
        remote_dir, remote_filename = remote.rsplit("/", 1)
        remote_dir += "/"
        self._set_status(f"Reintentando: {Path(local_path).name}", PENDING_COLOR)
        threading.Thread(target=self._retry_history_upload_worker,
                         args=(local_path, remote_dir, remote_filename), daemon=True).start()

    def _retry_history_upload_worker(self, local_path: str, remote_dir: str, remote_filename: str):
        # Mismo cupo de "Subidas simultáneas" que una subida normal (ver
        # core/upload_slots.py) -- sin esto, un reintento desde Historial
        # podría abrir una conexión FTP extra por encima del límite que el
        # usuario configuró, a la vez que una subida manual o automática
        # en curso.
        if not self._upload_slots.acquire(cancel_event=self._upload_cancel):
            self.after(0, lambda: self._set_status("Reintento cancelado", WARNING_COLOR))
            return
        try:
            from core.ftp_client import FTPClient
            own_ftp = FTPClient()
            ok, msg = own_ftp.connect(
                self.config_data.get("ftp_host", ""), int(self.config_data.get("ftp_port", 21)),
                self.config_data.get("ftp_user", ""), self.config_data.get("ftp_password", ""),
                self.config_data.get("ftp_use_tls", False))
            if not ok:
                self.after(0, lambda: self._set_status(f"No se pudo conectar: {msg}", ERROR_COLOR))
                return
            try:
                speed_kbs = float(self.config_data.get("ftp_speed_limit", 0)) * 1024
                up_ok, up_msg = own_ftp.upload_file(
                    local_path, remote_dir, speed_limit_kbs=speed_kbs,
                    try_resume=True, remote_filename=remote_filename)
            finally:
                own_ftp.disconnect()
        finally:
            self._upload_slots.release()

        try:
            size = Path(local_path).stat().st_size
        except OSError:
            size = 0
        status = "ok" if up_ok else "error"
        self._save_history_entry(
            Path(local_path).name, f"{remote_dir.rstrip('/')}/{remote_filename}",
            status, size, error_msg="" if up_ok else up_msg, local_path=local_path)

        if up_ok:
            self.after(0, lambda: self._set_status(f"Reintento OK: {remote_filename}", SUCCESS_COLOR))
        else:
            self.after(0, lambda: self._set_status(f"Reintento fallido: {up_msg}", ERROR_COLOR))
        self.after(0, self._refresh_history_view)

    def _clear_history(self):
        try:
            self._history_path().write_text("[]", encoding="utf-8")
        except Exception:
            pass
        self._history_dirty = True   # escribe el JSON directamente, no pasa por _save_history_entry
        self._refresh_history_view()
        self._set_status("Historial borrado", WARNING_COLOR)

    def _export_history(self):
        """Guarda el historial de subidas tal cual (JSON) donde el usuario
        elija -- para poder compartirlo o revisarlo fuera de la app."""
        dest = filedialog.asksaveasfilename(
            title="Descargar historial", defaultextension=".json",
            initialfile="historial_subidas.json",
            filetypes=[("JSON", "*.json"), ("Todos los archivos", "*.*")])
        if not dest:
            return
        try:
            Path(dest).write_text(
                json.dumps(self._load_history(), ensure_ascii=False, indent=2), encoding="utf-8")
            self._set_status(f"Historial guardado en {dest}", SUCCESS_COLOR)
        except Exception as e:
            self._set_status(f"No se pudo guardar el historial: {e}", ERROR_COLOR)

    def _export_log(self):
        """Empaqueta en un .zip todos los archivos de log de la app
        (app.log, media_refresh.log, auto_watcher.log, ai_fallback.log --
        los que existan) donde el usuario elija, para compartirlos o
        revisarlos fuera de la app sin tener que ir a buscarlos a mano en
        la carpeta de datos."""
        import zipfile
        dest = filedialog.asksaveasfilename(
            title="Descargar log completo", defaultextension=".zip",
            initialfile="logs_arenombrar.zip",
            filetypes=[("Archivo ZIP", "*.zip"), ("Todos los archivos", "*.*")])
        if not dest:
            return
        log_names = ("app.log", "media_refresh.log", "auto_watcher.log", "ai_fallback.log")
        data_dir = _appdata_dir()
        found = 0
        try:
            with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
                for name in log_names:
                    p = data_dir / name
                    if p.exists():
                        zf.write(p, arcname=name)
                        found += 1
            if found:
                self._set_status(f"{found} archivo(s) de log guardados en {dest}", SUCCESS_COLOR)
            else:
                self._set_status("No se encontró ningún archivo de log todavía", WARNING_COLOR)
        except Exception as e:
            self._set_status(f"No se pudo guardar el log: {e}", ERROR_COLOR)

    # ─────────────────────────────────────────── Liberar espacio ──
    # La vista más delicada de toda la app: el primer paso hacia un
    # borrado real e irreversible en el servidor. Principios que rigen
    # todo este bloque (ver conversación):
    #   - Nunca hay un criterio "por defecto" que decida solo -- con
    #     todos los filtros desactivados se ve la lista COMPLETA, no una
    #     preselección de "candidatas seguras".
    #   - Nunca se borra nada sin un clic explícito + confirmación
    #     individual (_ConfirmDeleteDialog) -- no existe "seleccionar
    #     todo y borrar".
    #   - Todo borrado queda registrado en deletion_history.json, éxito o
    #     fallo, con el motivo por el que apareció como candidata.

    def _build_cleanup_tab(self, parent):
        parent.grid_rowconfigure(2, weight=1)

        # Grid a 3 columnas con las dos exteriores en el mismo grupo
        # "uniform" -- mismo patrón que Archivos/Episodios/Historial para
        # que el título quede centrado de verdad pase lo que pese cada
        # lado (con pack normal, el texto explicativo a la izquierda
        # descentraba el título hacia la derecha).
        header = ctk.CTkFrame(parent, fg_color=("gray90", "gray20"), corner_radius=8)
        header.grid(row=0, column=0, sticky="ew", pady=(0, CONTAINER_GAP))
        header.grid_columnconfigure(0, weight=1, uniform="cleanup_header_sides")
        header.grid_columnconfigure(1, weight=0)
        header.grid_columnconfigure(2, weight=1, uniform="cleanup_header_sides")

        left_fr = ctk.CTkFrame(header, fg_color="transparent")
        left_fr.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(left_fr, text=("Solo un informe -- nunca borra nada por su cuenta. "
                                    "Cada eliminación exige tu confirmación."),
                     font=ctk.CTkFont(size=11), text_color=PENDING_COLOR).pack(side="left", padx=12, pady=8)

        ctk.CTkLabel(header, text="Liberar espacio",
                     font=ctk.CTkFont(size=14, weight="bold")).grid(row=0, column=1, padx=16, pady=8)

        right_fr = ctk.CTkFrame(header, fg_color="transparent")
        right_fr.grid(row=0, column=2, sticky="e")
        self._cleanup_scan_btn = ctk.CTkButton(right_fr, text="🔍 Analizar servidor", width=170,
                                               command=self._start_cleanup_scan)
        self._cleanup_scan_btn.pack(side="right", padx=(4, 12), pady=8)

        self._cleanup_status_lbl = ctk.CTkLabel(
            parent, text="Sin analizar todavía -- pulsa \"Analizar servidor\"", text_color=PENDING_COLOR,
            font=ctk.CTkFont(size=12), anchor="w")
        self._cleanup_status_lbl.grid(row=1, column=0, sticky="w", pady=(0, 6))
        self._cleanup_progress = ctk.CTkProgressBar(parent)
        self._cleanup_progress.set(0)
        # (empaquetado solo mientras analiza, ver _start_cleanup_scan)

        body = ctk.CTkFrame(parent, fg_color="transparent")
        body.grid(row=2, column=0, sticky="nsew")
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        # -- Panel de filtros, todos combinables entre sí (Y lógico) --
        filters_fr = ctk.CTkScrollableFrame(body, width=270, label_text="Filtros")
        filters_fr.grid(row=0, column=0, sticky="ns", padx=(0, CONTAINER_GAP))
        ctk.CTkLabel(filters_fr, text="Los filtros activos se combinan entre sí -- "
                                      "sin ninguno activo, se ve la lista completa.",
                     font=ctk.CTkFont(size=10), text_color=PENDING_COLOR,
                     wraplength=230, justify="left").pack(anchor="w", pady=(0, 10))

        self._cleanup_search_entry = ctk.CTkEntry(filters_fr, placeholder_text="Buscar por nombre...")
        self._cleanup_search_entry.pack(anchor="w", fill="x", pady=(0, 12))

        self._cleanup_age_var = ctk.BooleanVar(value=False)
        age_row = ctk.CTkFrame(filters_fr, fg_color="transparent")
        age_row.pack(anchor="w", pady=(4, 12))
        ctk.CTkCheckBox(age_row, text="Añadida +", variable=self._cleanup_age_var, width=0
                        ).pack(side="left")
        self._cleanup_age_entry = ctk.CTkEntry(age_row, width=40)
        self._cleanup_age_entry.insert(0, "12")
        self._cleanup_age_entry.pack(side="left", padx=(4, 4))
        ctk.CTkLabel(age_row, text="meses").pack(side="left")

        ctk.CTkLabel(filters_fr, text="Visionado",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", pady=(0, 4))
        self._cleanup_watched_combo = ctk.CTkComboBox(
            filters_fr, values=["(sin filtro)", "Nunca vista", "Vista, sin repetir en", "Pocas reproducciones",
                               "Sin datos de visionado"],
            width=240, command=self._on_cleanup_watched_mode_changed)
        self._cleanup_watched_combo.set("(sin filtro)")
        self._cleanup_watched_combo.pack(anchor="w", pady=(0, 4))

        # Solo uno de estos dos umbrales tiene sentido según el modo
        # elegido arriba -- se muestra/oculta en _on_cleanup_watched_mode_changed,
        # en vez de dejar ambos siempre visibles aunque no se apliquen.
        # Ambos van DENTRO de un contenedor fijo que se empaqueta una sola
        # vez aquí y nunca se mueve -- si se hiciera pack_forget()/pack()
        # directamente sobre las filas dentro de filters_fr, Tk las
        # reinserta al FINAL del panel (después de "Aplicar filtros"), no
        # en su sitio original, y cada cambio de modo hacía que la fila
        # "saltara" al fondo del todo.
        # height=1 -- sin esto, CTkFrame usa 200px de alto por defecto
        # incluso vacía (sin ninguna fila dentro empaquetada), dejando un
        # hueco enorme cuando el modo es "(sin filtro)"/"Nunca vista" (que
        # no muestran ninguna de las dos filas). Con un hijo empaquetado
        # dentro, pack_propagate (activo por defecto) hace que el
        # contenedor crezca para ajustarse a él -- este 1px solo es el
        # tamaño de referencia cuando no hay nada dentro.
        watched_threshold_frame = ctk.CTkFrame(filters_fr, fg_color="transparent", height=1)
        watched_threshold_frame.pack(anchor="w", fill="x")

        self._cleanup_not_rewatched_row = ctk.CTkFrame(watched_threshold_frame, fg_color="transparent")
        self._cleanup_not_rewatched_entry = ctk.CTkEntry(self._cleanup_not_rewatched_row, width=45)
        self._cleanup_not_rewatched_entry.insert(0, "12")
        self._cleanup_not_rewatched_entry.pack(side="left")
        ctk.CTkLabel(self._cleanup_not_rewatched_row, text="meses sin repetir",
                     font=ctk.CTkFont(size=10)).pack(side="left", padx=(4, 0))

        self._cleanup_max_playcount_row = ctk.CTkFrame(watched_threshold_frame, fg_color="transparent")
        self._cleanup_max_playcount_entry = ctk.CTkEntry(self._cleanup_max_playcount_row, width=45)
        self._cleanup_max_playcount_entry.insert(0, "1")
        self._cleanup_max_playcount_entry.pack(side="left")
        ctk.CTkLabel(self._cleanup_max_playcount_row, text="máx. veces vista",
                     font=ctk.CTkFont(size=10)).pack(side="left", padx=(4, 0))

        self._on_cleanup_watched_mode_changed(self._cleanup_watched_combo.get())

        self._cleanup_size_var = ctk.BooleanVar(value=False)
        size_row = ctk.CTkFrame(filters_fr, fg_color="transparent")
        size_row.pack(anchor="w", pady=(4, 12))
        ctk.CTkCheckBox(size_row, text="Más de", variable=self._cleanup_size_var, width=0
                        ).pack(side="left")
        self._cleanup_size_entry = ctk.CTkEntry(size_row, width=40)
        self._cleanup_size_entry.insert(0, "5")
        self._cleanup_size_entry.pack(side="left", padx=(4, 4))
        ctk.CTkLabel(size_row, text="GB").pack(side="left")

        ctk.CTkLabel(filters_fr, text="Tipo",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", pady=(0, 4))
        self._cleanup_type_tv_var = ctk.BooleanVar(value=True)
        self._cleanup_type_movie_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(filters_fr, text="Series", variable=self._cleanup_type_tv_var
                        ).pack(anchor="w")
        ctk.CTkCheckBox(filters_fr, text="Películas", variable=self._cleanup_type_movie_var
                        ).pack(anchor="w", pady=(0, 12))

        self._cleanup_duplicates_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(filters_fr, text="Solo duplicados", variable=self._cleanup_duplicates_var
                        ).pack(anchor="w")
        ctk.CTkLabel(filters_fr, text="El mismo contenido identificado en 2 o más sitios "
                                      "(p.ej. la misma película en dos categorías).",
                     font=ctk.CTkFont(size=10), text_color=PENDING_COLOR,
                     wraplength=230, justify="left").pack(anchor="w", pady=(0, 12))

        ctk.CTkLabel(filters_fr, text="Protegidas",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", pady=(0, 4))
        # Favoritos y reservados nunca se pueden borrar desde aquí (ver
        # _build_cleanup_result_row) -- por defecto se ocultan de la lista
        # igual que siempre, estos dos checkboxes solo sirven para
        # REVISARLOS (auditar qué protege espacio, liberar una reserva
        # antigua para hacer sitio a otra) sin salir de esta pestaña.
        self._cleanup_show_favorites_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(filters_fr, text="Mostrar favoritos", variable=self._cleanup_show_favorites_var
                        ).pack(anchor="w")
        self._cleanup_show_reserved_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(filters_fr, text="Mostrar reservados", variable=self._cleanup_show_reserved_var
                        ).pack(anchor="w", pady=(0, 4))
        self._cleanup_quota_lbl = ctk.CTkLabel(
            filters_fr, text="", font=ctk.CTkFont(size=10), text_color=PENDING_COLOR,
            wraplength=230, justify="left")
        self._cleanup_quota_lbl.pack(anchor="w", pady=(0, 12))

        ctk.CTkLabel(filters_fr, text="Categoría",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", pady=(0, 4))
        self._cleanup_category_frame = ctk.CTkFrame(filters_fr, fg_color="transparent")
        self._cleanup_category_frame.pack(anchor="w", fill="x", pady=(0, 4))
        self._cleanup_category_vars = {}   # nombre de categoría -> BooleanVar
        ctk.CTkLabel(filters_fr, text="(se rellena tras analizar)",
                     font=ctk.CTkFont(size=10), text_color=PENDING_COLOR).pack(anchor="w", pady=(0, 12))

        ctk.CTkButton(filters_fr, text="Aplicar filtros",
                      command=self._apply_cleanup_filters).pack(anchor="w", fill="x", pady=(4, 0))

        # -- Resultados en el centro --
        results_wrap = ctk.CTkFrame(body, fg_color="transparent")
        results_wrap.grid(row=0, column=1, sticky="nsew", padx=(0, CONTAINER_GAP))
        results_wrap.grid_columnconfigure(0, weight=1)
        results_wrap.grid_rowconfigure(0, weight=1)

        # TableView: mismo componente que Archivos/Episodios/Historial
        # (ver gui/table_view.py) -- cabecera fija con los mismos anchos
        # que leen las filas, así nunca se desalinean entre sí. La fila
        # sigue en dos líneas (nombre arriba, tamaño/motivo debajo); la
        # columna "candidata" solo etiqueta ese bloque, no una línea.
        self._cleanup_table = TableView(results_wrap, columns=[
            ColumnSpec("icon", "", width=28),
            ColumnSpec("candidata", "Candidata", expand=True),
            ColumnSpec("tendencia", "Tendencia", width=70),
            ColumnSpec("fav", "", width=32),
            ColumnSpec("reserve", "", width=32),
            ColumnSpec("del", "", width=110),
        ])
        self._cleanup_table.grid(row=0, column=0, sticky="nsew")
        self._cleanup_table.enable_dynamic_page_size(lambda _size: self._render_cleanup_page())

        # Paginado (ver TableView.page_size, calculado dinámicamente) -- mismo motivo que en
        # Episodios que faltan/Historial: con un servidor grande, cientos
        # de candidatas de golpe llegan a agotar el límite de objetos GUI
        # de Windows (10000 por proceso) y rompen el pintado de la
        # ventana. El contador de candidatas vive en esta misma barra
        # (izquierda), no encima de la lista -- deja ese hueco libre para
        # la cabecera de columnas. Columnas laterales "uniform" (mismo
        # truco que las cabeceras con título centrado) para que
        # Anterior/Página/Siguiente después del contador, no centrados
        # sobre el ancho completo -- con el texto del contador variando
        # bastante de longitud ("1444 candidata(s) -- 10.8 TB
        # liberables"), forzar columnas simétricas para centrar la
        # paginación lo dejaba cortado en listas grandes. El contador
        # tiene todo el espacio libre que necesite (weight=1); la
        # paginación ocupa su ancho natural justo a continuación.
        bottom_bar = ctk.CTkFrame(results_wrap, fg_color="transparent")
        bottom_bar.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        bottom_bar.grid_columnconfigure(0, weight=1)

        self._cleanup_results_lbl = ctk.CTkLabel(
            bottom_bar, text="", font=ctk.CTkFont(size=12), text_color=PENDING_COLOR, anchor="w")
        self._cleanup_results_lbl.grid(row=0, column=0, sticky="w")

        cleanup_nav_fr = ctk.CTkFrame(bottom_bar, fg_color="transparent")
        cleanup_nav_fr.grid(row=0, column=1)
        self._cleanup_prev_btn = ctk.CTkButton(
            cleanup_nav_fr, text="< Anterior", width=100, fg_color="transparent", border_width=1,
            command=lambda: self._cleanup_change_page(-1))
        self._cleanup_prev_btn.pack(side="left")
        self._cleanup_page_lbl = ctk.CTkLabel(cleanup_nav_fr, text="", text_color=PENDING_COLOR)
        self._cleanup_page_lbl.pack(side="left", padx=12)
        self._cleanup_next_btn = ctk.CTkButton(
            cleanup_nav_fr, text="Siguiente >", width=100, fg_color="transparent", border_width=1,
            command=lambda: self._cleanup_change_page(1))
        self._cleanup_next_btn.pack(side="left")

        # -- Ficha de TMDB de la candidata pulsada, a la derecha --
        self._cleanup_side_panel = self._build_cleanup_side_panel(body)
        self._cleanup_side_panel.grid(row=0, column=2, sticky="ns")

        self._update_cleanup_quota_label()   # estado inicial, antes de cualquier "Aplicar filtros"

        self._cleanup_raw_items = []
        self._cleanup_filtered_items = []
        self._cleanup_duplicate_siblings = {}
        self._cleanup_scanning = False
        self._cleanup_poster_token = None
        self._cleanup_current_poster = None
        self._cleanup_selected_item = None   # candidata pulsada, ver _show_cleanup_poster
        self._cleanup_row_widgets = []   # [(item, row_frame), ...] -- para resaltar la fila seleccionada
        self._cleanup_page = 0
        # Compartidas entre todas las filas -- mismo motivo que en
        # _build_files_tab/_build_missing_episodes_tab: no crear un
        # CTkFont nuevo (llamada a Tcl) por cada candidata de la lista.
        self._cleanup_name_font = ctk.CTkFont(size=13, weight="bold")
        self._cleanup_reason_font = ctk.CTkFont(size=11)

        # Cargar el último análisis guardado (si lo hay) para no obligar
        # a repetir el escaneo completo -- que puede tardar más de un
        # minuto en un servidor grande -- cada vez que se abre la app.
        from core.cleanup_candidates_cache import load_cache
        cached = load_cache()
        if cached.get("items"):
            self._cleanup_raw_items = cached["items"]
            self._cleanup_last_scan_ts = cached.get("last_scan_ts")
            self._populate_cleanup_category_filters()
            # Diferido -- mismo motivo que en _build_missing_episodes_tab
            # y _build_history_tab: dibuja hasta 100 filas (paginado) de
            # la última lista de candidatas guardada, y esta pestaña se
            # construye al arrancar aunque esté oculta.
            self.after(200, self._apply_cleanup_filters)
            age = _time.time() - self._cleanup_last_scan_ts if self._cleanup_last_scan_ts else None
            when_txt = self._fmt_cleanup_scan_age(age)
            self._cleanup_status_lbl.configure(
                text=f"Último análisis: {when_txt} -- pulsa \"Analizar servidor\" para actualizar")

    @staticmethod
    def _fmt_cleanup_scan_age(age_seconds) -> str:
        if age_seconds is None:
            return "hace un tiempo"
        if age_seconds < 3600:
            return f"hace {int(age_seconds // 60)} min"
        if age_seconds < 86400:
            return f"hace {int(age_seconds // 3600)} h"
        return f"hace {int(age_seconds // 86400)} día(s)"

    def _start_cleanup_scan(self):
        if self._cleanup_scanning:
            return
        if not self.config_data.get("ftp_host", ""):
            self._set_status("Configura el servidor FTP en Ajustes primero", WARNING_COLOR)
            return
        self._cleanup_scanning = True
        self._cleanup_scan_btn.configure(state="disabled")
        self._cleanup_status_lbl.grid_remove()
        self._cleanup_progress.set(0)
        self._cleanup_progress.grid(row=1, column=0, sticky="ew", pady=(0, 6))

        def worker():
            # Sin este try/except, cualquier fallo dentro de
            # _scan_cleanup_candidates (un hueco de red, un dato inesperado
            # de Jellyfin/Plex/FTP...) mataba el hilo en silencio antes de
            # llegar a _finish_cleanup_scan -- el botón se quedaba
            # deshabilitado y la barra de progreso llena para siempre.
            try:
                items = self._scan_cleanup_candidates(
                    progress_cb=lambda c, t, n: self.after(
                        0, lambda c=c, t=t, n=n: self._update_cleanup_progress(c, t, n)))
            except Exception:
                _log.exception("Liberar espacio: fallo inesperado durante el análisis")
                self.after(0, lambda: self._set_status(
                    "El análisis terminó con un error -- revisa app.log", ERROR_COLOR))
                self.after(0, lambda: self._finish_cleanup_scan(self._cleanup_raw_items))
                return
            self.after(0, lambda: self._finish_cleanup_scan(items))
        threading.Thread(target=worker, daemon=True).start()

    def _update_cleanup_progress(self, current: int, total: int, name: str):
        if total > 0:
            self._cleanup_progress.set(current / total)
            self._cleanup_status_lbl.configure(text=f"Analizando ({current}/{total}): {name}")
        else:
            # Fase previa al recorrido de carpetas (consultar Jellyfin/Plex,
            # listar el FTP) -- sin cantidad total conocida todavía, solo
            # se muestra el mensaje de qué fase es, para que no parezca
            # que la app se ha quedado colgada mientras tarda.
            self._cleanup_progress.set(0)
            self._cleanup_status_lbl.configure(text=name)

    def _finish_cleanup_scan(self, items: list):
        self._cleanup_scanning = False
        self._cleanup_scan_btn.configure(state="normal")
        self._cleanup_progress.grid_remove()
        self._cleanup_status_lbl.grid()
        self._cleanup_raw_items = items
        self._cleanup_last_scan_ts = _time.time()
        total_size = sum(it.size_bytes for it in items)
        ftp_count = getattr(self, "_cleanup_last_ftp_size_count", 0)
        extra = f" ({ftp_count} calculados por FTP, más lentos)" if ftp_count else ""
        self._cleanup_status_lbl.configure(
            text=f"Análisis completo: {len(items)} elemento(s), {_fmt_size(total_size)} en total{extra}")
        self._populate_cleanup_category_filters()
        self._apply_cleanup_filters()

        from core.cleanup_candidates_cache import save_cache
        try:
            save_cache(items, self._cleanup_last_scan_ts)
        except Exception:
            _log.warning("Liberar espacio: no se pudo guardar el caché del análisis", exc_info=True)

    def _scan_cleanup_candidates(self, progress_cb=None) -> list:
        """Combina Jellyfin/Plex (visionado) y FTP (tamaño, categoría) en
        una lista de CleanupItem -- la fuente de datos cruda que luego se
        filtra con core.cleanup_candidates.filter_candidates() según lo
        que el usuario tenga marcado. Conexión FTP propia (no self.ftp),
        para no competir con otro uso simultáneo de la conexión
        compartida (subidas, refresco de espacio...)."""
        from core.cleanup_candidates import CleanupItem, merge_usage_entries
        from core.media_server_refresh import get_jellyfin_usage_stats, get_plex_usage_stats, parse_media_date
        from core.ftp_client import FTPClient

        t0 = _time.monotonic()
        # Claves (nombre, tipo) en vez de solo nombre -- una serie y una
        # película pueden compartir el mismo título EXACTO (p.ej.
        # "Fargo", serie y película sin relación entre sí), y antes se
        # fusionaban en una sola entrada, haciendo que las dos carpetas
        # del FTP acabaran con el mismo tmdb_id/visionado aunque fueran
        # contenidos distintos.
        usage_by_key = {}

        def _usage_bucket(media_type):
            return "tv" if media_type == "tv" else "movie"

        def _merge_usage(name, media_type, entry):
            key = (name, _usage_bucket(media_type))
            existing = usage_by_key.get(key)
            usage_by_key[key] = merge_usage_entries(existing, entry) if existing else entry

        if self.config_data.get("jellyfin_enabled"):
            if progress_cb:
                progress_cb(0, 0, "Consultando Jellyfin (visionado y tamaños)...")
            t_jf = _time.monotonic()
            stats = get_jellyfin_usage_stats(
                self.config_data.get("jellyfin_host", ""), self.config_data.get("jellyfin_api_key", ""),
                username=self.config_data.get("jellyfin_username", "")) or {}
            for entry in stats.values():
                _merge_usage(entry["name"], entry.get("media_type"), entry)
            _log.info("Liberar espacio: Jellyfin -> %d elemento(s) en %.1fs",
                      len(stats), _time.monotonic() - t_jf)
        if self.config_data.get("plex_enabled"):
            if progress_cb:
                progress_cb(0, 0, "Consultando Plex (visionado y tamaños)...")
            t_px = _time.monotonic()
            stats = get_plex_usage_stats(
                self.config_data.get("plex_host", ""), self.config_data.get("plex_token", "")) or {}
            for entry in stats.values():
                _merge_usage(entry["name"], entry.get("media_type"), entry)
            _log.info("Liberar espacio: Plex -> %d elemento(s) en %.1fs",
                      len(stats), _time.monotonic() - t_px)

        own_ftp = FTPClient()
        ok, _msg = own_ftp.connect(
            self.config_data.get("ftp_host", ""), int(self.config_data.get("ftp_port", 21)),
            self.config_data.get("ftp_user", ""), self.config_data.get("ftp_password", ""),
            self.config_data.get("ftp_use_tls", False))
        if not ok:
            return []

        try:
            if progress_cb:
                progress_cb(0, 0, "Listando carpetas del FTP...")
            from core.cleanup_candidates import group_loose_files_by_name

            cats = self.config_data.get("ftp_categories", {"tv": [], "movie": []})
            folder_list = []       # [(media_type, categoria, root, carpeta), ...]
            loose_groups_list = []  # [(media_type, categoria, root, nombre_base, {"size_bytes","file_names"}), ...]
            ftp_tree_sizes = {}    # (root, carpeta) -> tamaño, solo si "LIST -R" funcionó para ese root
            dash_r_roots = 0
            for media_type, cat_list in cats.items():
                for cat in cat_list:
                    root = cat.get("root", "").rstrip("/")
                    if not root:
                        continue
                    # "LIST -R" trae el árbol entero (con tamaños) de esta
                    # categoría en UNA sola petición -- si el servidor lo
                    # soporta (vsftpd con ls_recurse_enable=YES en
                    # vsftpd.conf), evita tener que pedir el tamaño de
                    # cada serie/película una por una. Si no lo soporta,
                    # se sigue como antes (recorrido por carpeta).
                    tree = own_ftp.list_tree_recursive(root)
                    if tree is not None:
                        dash_r_roots += 1
                        for name, total_bytes in sizes_by_top_level_folder(tree, root).items():
                            ftp_tree_sizes[(root, name)] = total_bytes
                        root_files = tree.get(root, [])
                    else:
                        root_files = own_ftp.list_files_with_sizes(root)
                    for folder in own_ftp.list_dirs(root):
                        folder_list.append((media_type, cat, root, folder))
                    # Series/películas guardadas como archivos sueltos
                    # directamente en la raíz (sin carpeta propia -- visto
                    # de verdad: una biblioteca de películas con
                    # vídeo+póster+backdrop+.nfo sueltos, sin una carpeta
                    # por película) -- sin esto, "Liberar espacio" solo
                    # veía las carpetas y se saltaba casi todo el
                    # contenido guardado así.
                    for base_name, group in group_loose_files_by_name(root_files).items():
                        loose_groups_list.append((media_type, cat, root, base_name, group))

            if dash_r_roots:
                _log.info("Liberar espacio: LIST -R soportado en %d/%d categoría(s)",
                          dash_r_roots, sum(len(v) for v in cats.values()))
            if loose_groups_list:
                _log.info("Liberar espacio: %d elemento(s) detectados como archivos sueltos (sin carpeta propia)",
                          len(loose_groups_list))

            # Emparejamiento EXCLUSIVO, separado por tipo de medio: antes,
            # cada carpeta buscaba su mejor coincidencia por separado con
            # best_match(), lo que permitía que dos carpetas de nombre
            # parecido (una serie y su remake, "X" y "X: la película"...)
            # se llevaran el MISMO título de Jellyfin/Plex -- dejando a
            # una de las dos con datos de visionado que en realidad eran
            # de la otra. Ahora se calcula de una vez, con el parecido
            # más fuerte ganando prioridad, y ningún título se asigna dos
            # veces -- y SOLO dentro del mismo tipo (serie con serie,
            # película con película): una serie y una película pueden
            # compartir título EXACTO sin ser lo mismo (p.ej. "Fargo",
            # serie y película sin relación), y comparándolas todas
            # juntas se fusionaban por error.
            candidate_names_by_bucket = {"tv": [], "movie": []}
            for _mt, _cat, _root, folder in folder_list:
                candidate_names_by_bucket[_usage_bucket(_mt)].append(folder)
            for _mt, _cat, _root, base_name, _grp in loose_groups_list:
                candidate_names_by_bucket[_usage_bucket(_mt)].append(base_name)
            usage_names_by_bucket = {"tv": [], "movie": []}
            for _name, _bucket in usage_by_key.keys():
                usage_names_by_bucket[_bucket].append(_name)
            name_to_usage_name_by_bucket = {}
            for _bucket in ("tv", "movie"):
                if usage_names_by_bucket[_bucket] and candidate_names_by_bucket[_bucket]:
                    name_to_usage_name_by_bucket[_bucket] = match_names_exclusively(
                        candidate_names_by_bucket[_bucket], usage_names_by_bucket[_bucket], min_ratio=0.75)
                else:
                    name_to_usage_name_by_bucket[_bucket] = {}

            items = []
            total = len(folder_list) + len(loose_groups_list)
            matched_count = 0
            ftp_size_count = 0
            t_walk = _time.monotonic()
            for i, (media_type, cat, root, folder) in enumerate(folder_list):
                if progress_cb:
                    progress_cb(i + 1, total, folder)
                path = f"{root.rstrip('/')}/{folder}"
                bucket = _usage_bucket(media_type)
                usage = None
                matched_name = name_to_usage_name_by_bucket[bucket].get(folder)
                if matched_name:
                    usage = usage_by_key.get((matched_name, bucket))
                    matched_count += 1
                # Orden de preferencia para el tamaño: 1) LIST -R (ya
                # calculado para TODA la categoría en una sola petición,
                # el más barato), 2) Jellyfin/Plex si hay coincidencia por
                # nombre, 3) cálculo por FTP carpeta por carpeta (el más
                # lento, solo como último recurso).
                size = ftp_tree_sizes.get((root, folder)) or (usage or {}).get("size_bytes") or 0
                if not size:
                    ftp_size_count += 1
                    size = own_ftp.get_folder_size(path)
                items.append(CleanupItem(
                    tmdb_id=(usage or {}).get("tmdb_id"),
                    name=folder,
                    media_type="tv" if media_type == "tv" else "movie",
                    ftp_path=path,
                    category_name=cat.get("name", ""),
                    size_bytes=size,
                    fully_watched=bool((usage or {}).get("fully_watched")),
                    play_count=(usage or {}).get("play_count", 0) or 0,
                    last_played_ts=parse_media_date((usage or {}).get("last_played")),
                    date_added_ts=parse_media_date((usage or {}).get("date_added")),
                ))
            for j, (media_type, cat, root, base_name, group) in enumerate(loose_groups_list):
                if progress_cb:
                    progress_cb(len(folder_list) + j + 1, total, base_name)
                bucket = _usage_bucket(media_type)
                usage = None
                matched_name = name_to_usage_name_by_bucket[bucket].get(base_name)
                if matched_name:
                    usage = usage_by_key.get((matched_name, bucket))
                    matched_count += 1
                # El tamaño ya se conoce con exactitud (se sumó al listar
                # la raíz), no hace falta ningún cálculo extra por FTP.
                size = group["size_bytes"] or (usage or {}).get("size_bytes") or 0
                items.append(CleanupItem(
                    tmdb_id=(usage or {}).get("tmdb_id"),
                    name=base_name,
                    media_type="tv" if media_type == "tv" else "movie",
                    ftp_path=f"{root}/{base_name}",
                    category_name=cat.get("name", ""),
                    size_bytes=size,
                    fully_watched=bool((usage or {}).get("fully_watched")),
                    play_count=(usage or {}).get("play_count", 0) or 0,
                    last_played_ts=parse_media_date((usage or {}).get("last_played")),
                    date_added_ts=parse_media_date((usage or {}).get("date_added")),
                    loose_file_paths=[f"{root}/{fn}" for fn in group["file_names"]],
                ))
            self._cleanup_last_ftp_size_count = ftp_size_count
            _log.info(
                "Liberar espacio: %d carpeta(s) + %d archivo(s) suelto(s), %d emparejadas por nombre, "
                "%d con tamaño calculado por FTP (lento), recorrido en %.1fs, total %.1fs",
                len(folder_list), len(loose_groups_list), matched_count, ftp_size_count,
                _time.monotonic() - t_walk, _time.monotonic() - t0)
            return items
        finally:
            own_ftp.disconnect()

    def _populate_cleanup_category_filters(self):
        for w in self._cleanup_category_frame.winfo_children():
            w.destroy()
        self._cleanup_category_vars = {}
        names = sorted({it.category_name for it in self._cleanup_raw_items if it.category_name})
        for name in names:
            var = ctk.BooleanVar(value=True)
            ctk.CTkCheckBox(self._cleanup_category_frame, text=name, variable=var
                            ).pack(anchor="w")
            self._cleanup_category_vars[name] = var

    def _on_cleanup_watched_mode_changed(self, choice: str):
        """Muestra solo el umbral que tiene sentido para el modo de
        visionado elegido -- "meses sin repetir" no pinta nada si el modo
        es "Nunca vista", y viceversa con "máx. veces vista". Solo cambia
        qué campo se ve; no aplica el filtro -- eso solo lo hace el botón
        "Aplicar filtros" (ver _apply_cleanup_filters)."""
        self._cleanup_not_rewatched_row.pack_forget()
        self._cleanup_max_playcount_row.pack_forget()
        if choice == "Vista, sin repetir en":
            self._cleanup_not_rewatched_row.pack(anchor="w", pady=(0, 12))
        elif choice == "Pocas reproducciones":
            self._cleanup_max_playcount_row.pack(anchor="w", pady=(0, 12))

    def _apply_cleanup_filters(self):
        """Vuelve a filtrar la lista YA analizada (self._cleanup_raw_items)
        según los filtros marcados ahora mismo -- no repite el análisis
        de red, solo recalcula sobre los datos ya obtenidos."""
        from core.cleanup_candidates import (
            CleanupFilters, filter_candidates,
            WATCHED_NEVER, WATCHED_NOT_REWATCHED, WATCHED_LOW_PLAYCOUNT, WATCHED_NO_DATA)

        def _int_or(entry, default=None):
            try:
                return int(entry.get().strip())
            except (ValueError, AttributeError):
                return default

        min_age = _int_or(self._cleanup_age_entry) if self._cleanup_age_var.get() else None

        watched_choice = self._cleanup_watched_combo.get()
        watched_mode = not_rewatched_months = max_play_count = None
        if watched_choice == "Nunca vista":
            watched_mode = WATCHED_NEVER
        elif watched_choice == "Vista, sin repetir en":
            watched_mode = WATCHED_NOT_REWATCHED
            not_rewatched_months = _int_or(self._cleanup_not_rewatched_entry, 12)
        elif watched_choice == "Pocas reproducciones":
            watched_mode = WATCHED_LOW_PLAYCOUNT
            max_play_count = _int_or(self._cleanup_max_playcount_entry, 1)
        elif watched_choice == "Sin datos de visionado":
            watched_mode = WATCHED_NO_DATA

        min_size_gb = None
        if self._cleanup_size_var.get():
            try:
                min_size_gb = float(self._cleanup_size_entry.get().strip().replace(",", "."))
            except ValueError:
                min_size_gb = None

        media_types = set()
        if self._cleanup_type_tv_var.get():
            media_types.add("tv")
        if self._cleanup_type_movie_var.get():
            media_types.add("movie")
        if media_types == {"tv", "movie"}:
            media_types = None   # los dos marcados = sin filtro (se ven ambos tipos)
        # Si NINGUNO está marcado, media_types se queda como conjunto
        # vacío a propósito -- matches_filters() lo trata como "no
        # coincide nada", no como "sin filtro". Antes se convertía
        # también a None aquí, y desmarcar los dos tipos mostraba todo
        # en vez de nada.

        category_names = {name for name, var in self._cleanup_category_vars.items() if var.get()}
        if not self._cleanup_category_vars or category_names == set(self._cleanup_category_vars.keys()):
            category_names = None   # todas marcadas = sin filtro

        filters = CleanupFilters(
            min_age_months=min_age, watched_mode=watched_mode,
            not_rewatched_months=not_rewatched_months, max_play_count=max_play_count,
            min_size_gb=min_size_gb, media_types=media_types, category_names=category_names,
            name_query=self._cleanup_search_entry.get().strip(),
            only_duplicates=self._cleanup_duplicates_var.get())
        self._cleanup_filtered_items = filter_candidates(self._cleanup_raw_items, filters)
        # Favoritos y reservados nunca son candidatas BORRABLES (el botón
        # "Eliminar" sale deshabilitado para ellas, ver
        # _build_cleanup_result_row) -- por defecto tampoco aparecen en la
        # lista, igual que siempre, pero "Mostrar favoritos"/"Mostrar
        # reservados" permite revisarlas sin salir de esta pestaña.
        if not self._cleanup_show_favorites_var.get():
            self._cleanup_filtered_items = [
                it for it in self._cleanup_filtered_items
                if not self._is_favorite(it.media_type, it.tmdb_id)]
        if not self._cleanup_show_reserved_var.get():
            self._cleanup_filtered_items = [
                it for it in self._cleanup_filtered_items
                if not self._is_reserved(it.media_type, it.tmdb_id)]
        self._update_cleanup_quota_label()
        # Alfabético, no el orden en que las devolvió el escaneo del
        # servidor (que no sigue ningún criterio reconocible para quien
        # mira la lista).
        self._cleanup_filtered_items.sort(key=lambda it: it.name.lower())

        # Con "Solo duplicados" activo, cada fila necesita saber en qué
        # OTRAS rutas existe el mismo contenido (mismo tmdb_id Y mismo
        # media_type -- TMDB numera series y películas por separado, así
        # que el mismo número puede corresponder a dos cosas sin relación
        # alguna, ver find_duplicate_tmdb_ids), para poder mostrarlo en la
        # descripción -- así se ve de un vistazo cuál de las copias
        # conservar antes de pulsar "Eliminar" en una de ellas (el botón
        # de cada fila ya borra solo esa copia, deja las demás intactas).
        self._cleanup_duplicate_siblings = {}
        if filters.only_duplicates:
            from core.cleanup_candidates import find_duplicate_tmdb_ids
            dup_keys = find_duplicate_tmdb_ids(self._cleanup_raw_items)
            by_key = {}
            for it in self._cleanup_raw_items:
                key = (it.tmdb_id, it.media_type)
                if key in dup_keys:
                    by_key.setdefault(key, []).append(it)
            for it in self._cleanup_raw_items:
                key = (it.tmdb_id, it.media_type)
                if key in dup_keys:
                    self._cleanup_duplicate_siblings[id(it)] = [
                        s for s in by_key[key] if s is not it]

        self._render_cleanup_results()

    def _cleanup_item_reason_text(self, item) -> str:
        """Resumen legible de por qué este elemento aparece en la lista --
        se muestra en la fila y se guarda tal cual en el historial de
        borrados si se elimina, para saber después el motivo, no solo el qué."""
        parts = []
        siblings = getattr(self, "_cleanup_duplicate_siblings", {}).get(id(item))
        if siblings:
            paths = "; ".join(s.ftp_path for s in siblings)
            parts.append(f"⚠ también en: {paths}")
        if item.loose_file_paths:
            n = len(item.loose_file_paths)
            parts.append(f"{n} archivo{'s' if n != 1 else ''} suelto{'s' if n != 1 else ''} (sin carpeta propia)")
        if item.date_added_ts:
            months = int((_time.time() - item.date_added_ts) / (30 * 24 * 3600))
            parts.append(f"añadida hace {months} mes(es)")
        if item.fully_watched:
            if item.last_played_ts:
                months = int((_time.time() - item.last_played_ts) / (30 * 24 * 3600))
                parts.append(f"vista, sin repetir hace {months} mes(es)")
            else:
                parts.append("vista por completo")
        elif item.play_count:
            parts.append(f"reproducida {item.play_count} vez/veces")
        else:
            parts.append("nunca vista")
        return ", ".join(parts) if parts else "sin datos de visionado"

    def _render_cleanup_results(self):
        """Punto de entrada tras aplicar filtros o terminar un análisis --
        el conjunto de candidatas cambió, así que vuelve a la primera
        página. Para redibujar la página actual sin resetearla (p.ej.
        tras borrar una sola candidata) usar _render_cleanup_page()
        directamente -- ver _finish_delete_cleanup_item."""
        items = self._cleanup_filtered_items
        if items:
            total_size = sum(it.size_bytes for it in items)
            self._cleanup_results_lbl.configure(
                text=f"{len(items)} candidata(s) -- {_fmt_size(total_size)} liberables")
        else:
            self._cleanup_results_lbl.configure(text="")
        self._cleanup_page = 0
        self._render_cleanup_page()

    def _cleanup_change_page(self, delta: int):
        n_pages = max(1, -(-len(self._cleanup_filtered_items) // self._cleanup_table.page_size))
        new_page = max(0, min(n_pages - 1, self._cleanup_page + delta))
        if new_page == self._cleanup_page:
            return
        self._cleanup_page = new_page
        self._render_cleanup_page()

    def _render_cleanup_page(self):
        """Dibuja solo self._cleanup_page de self._cleanup_filtered_items
        -- ver TableView.page_size. Página acotada (no "Mostrar más"
        acumulativo): con cientos de candidatas (habitual desde que
        también se detectan archivos sueltos), crear TODAS las filas de
        golpe -- o ir acumulando cada vez más sin soltar las anteriores --
        llega a agotar el límite de objetos GUI de Windows (10000 por
        proceso) y rompe el pintado de TODA la ventana, no solo de esta
        lista. Con página fija, el número de widgets vivos a la vez tiene
        un techo pase lo que pase con el tamaño real de la lista."""
        self._cleanup_table.clear_rows()
        self._cleanup_row_widgets = []

        items = self._cleanup_filtered_items
        if not items:
            msg = "Sin candidatas con estos filtros." if self._cleanup_raw_items else \
                  "Pulsa \"🔍 Analizar servidor\" para ver candidatas."
            ctk.CTkLabel(self._cleanup_table.body, text=msg, text_color=PENDING_COLOR).pack(pady=30)
            self._cleanup_page_lbl.configure(text="")
            self._cleanup_prev_btn.configure(state="disabled")
            self._cleanup_next_btn.configure(state="disabled")
            return

        total = len(items)
        page_size = self._cleanup_table.page_size
        n_pages = max(1, -(-total // page_size))
        self._cleanup_page = max(0, min(n_pages - 1, self._cleanup_page))
        start = self._cleanup_page * page_size
        page_items = items[start:start + page_size]

        self._cleanup_page_lbl.configure(text=f"Página {self._cleanup_page + 1} de {n_pages}")
        self._cleanup_prev_btn.configure(state="normal" if self._cleanup_page > 0 else "disabled")
        self._cleanup_next_btn.configure(state="normal" if self._cleanup_page < n_pages - 1 else "disabled")

        # Volver arriba del todo -- si no, al cambiar de página/filtro con
        # el scroll bajado, las filas nuevas se dibujan pero el hueco
        # (ahora vacío) por el que se había bajado se queda visible, dando
        # la sensación de "no hay resultados" aunque sí los haya.
        self._cleanup_table.scroll_to_top()

        self._cleanup_render_token = getattr(self, "_cleanup_render_token", 0) + 1
        self._render_cleanup_rows_batch(page_items, 0, len(page_items), self._cleanup_render_token)

    def _render_cleanup_rows_batch(self, items: list, start: int, end: int, token: int, batch_size: int = 20):
        if token != self._cleanup_render_token:
            return   # se cambió de página/filtro mientras tanto -- abandonar este lote
        batch_end = min(start + batch_size, end)
        for item in items[start:batch_end]:
            self._build_cleanup_result_row(item)
        if batch_end < end:
            self.after(1, lambda: self._render_cleanup_rows_batch(items, batch_end, end, token, batch_size))
        else:
            # Solo al terminar el ÚLTIMO lote -- medir con la página a
            # medio construir daría un alto medio por fila incorrecto
            # (ver TableView.note_rows_rendered).
            self._cleanup_table.note_rows_rendered(len(items))

    def _build_cleanup_result_row(self, item):
        # pack (side="left", una columna con fill="x"+expand=True) --
        # mismo patrón que _build_missing_ep_row/_refresh_table, ver
        # gui/table_view.py. Los anchos vienen de self._cleanup_table
        # (col_width), la misma fuente que usa la cabecera, así nunca se
        # desalinean entre sí.
        cw = self._cleanup_table.col_width
        is_selected = item is self._cleanup_selected_item
        row = ctk.CTkFrame(self._cleanup_table.body,
                           fg_color=SELECTED_ROW_COLOR if is_selected else ("gray95", "gray17"))
        row.pack(fill="x", pady=3, padx=2)
        row.pack(fill="x", pady=3, padx=2)
        self._cleanup_row_widgets.append((item, row))

        icon = "📺" if item.media_type == "tv" else "🎬"
        ctk.CTkLabel(row, text=icon, width=cw("icon")).pack(side="left", padx=(8, 4), pady=8)

        info_fr = ctk.CTkFrame(row, fg_color="transparent")
        info_fr.pack(side="left", fill="x", expand=True, pady=6)
        name_lbl = ctk.CTkLabel(info_fr, text=item.name, font=self._cleanup_name_font,
                                anchor="w", cursor="hand2")
        name_lbl.pack(fill="x")
        name_lbl.bind("<Button-1>", lambda e, it=item: self._show_cleanup_poster(it))
        reason = self._cleanup_item_reason_text(item)
        reason_lbl = ctk.CTkLabel(info_fr, text=f"{_fmt_size(item.size_bytes)} -- {reason}",
                                  font=self._cleanup_reason_font, text_color=PENDING_COLOR,
                                  anchor="w", cursor="hand2")
        reason_lbl.pack(fill="x")
        reason_lbl.bind("<Button-1>", lambda e, it=item: self._show_cleanup_poster(it))

        score = trending_score(item.play_count, item.last_played_ts, _time.time())
        ctk.CTkLabel(row, text=format_trending_score(score), width=cw("tendencia"),
                     font=self._cleanup_reason_font, text_color=PENDING_COLOR).pack(
            side="left", padx=(4, 4), pady=6)

        is_fav = self._is_favorite(item.media_type, item.tmdb_id)
        is_res = self._is_reserved(item.media_type, item.tmdb_id)

        # Marcar favorito aquí también quita la fila de la lista al
        # instante (ver _apply_cleanup_filters): un favorito nunca se
        # muestra como candidata a borrar por defecto -- salvo que el
        # filtro "Mostrar favoritos" esté activo, ver más abajo.
        ctk.CTkButton(row, text="★" if is_fav else "☆", width=cw("fav"), fg_color="transparent", border_width=0,
                      text_color=ACCENT if is_fav else PENDING_COLOR, hover_color=("gray85", "#2b2b2b"),
                      command=lambda it=item: self._toggle_cleanup_item_favorite(it)).pack(
            side="left", padx=(0, 4), pady=6)

        # Igual que favoritos pero con cuota configurable por usuario (ver
        # core/reservations.py) -- "🔒" reservado (por cualquiera, no solo
        # el usuario actual), "🔓" libre.
        ctk.CTkButton(row, text="🔒" if is_res else "🔓", width=cw("reserve"), fg_color="transparent",
                      border_width=0, text_color=ACCENT if is_res else PENDING_COLOR,
                      hover_color=("gray85", "#2b2b2b"),
                      command=lambda it=item: self._toggle_cleanup_item_reservation(it)).pack(
            side="left", padx=(0, 4), pady=6)

        # Protegido (favorito o reservado) nunca se borra desde aquí, ni
        # aunque el filtro correspondiente lo esté mostrando -- deshabilitar
        # en vez de ocultar, para que quede claro POR QUÉ no se puede.
        is_protected = is_fav or is_res
        del_btn = ctk.CTkButton(
            row, text="🗑 Eliminar" if not is_protected else "🔒 Protegida",
            width=cw("del"), fg_color="transparent", border_width=1,
            border_color=ERROR_COLOR if not is_protected else PENDING_COLOR,
            text_color=ERROR_COLOR if not is_protected else PENDING_COLOR,
            hover_color=("gray85", "#3d1010") if not is_protected else ("gray90", "gray20"),
            state="disabled" if is_protected else "normal",
            command=lambda it=item: self._confirm_and_delete_cleanup_item(it))
        del_btn.pack(side="left", padx=8, pady=6)

    def _build_cleanup_side_panel(self, parent):
        """Ficha de TMDB de la candidata pulsada -- póster + sinopsis,
        mismo patrón que _build_missing_ep_side_panel (que a su vez
        copiaba el panel "Buscar en TMDB" de Archivos). Solo lectura."""
        panel = ctk.CTkFrame(parent, width=240)
        panel.grid_propagate(False)
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(0, weight=1)

        scroll = ctk.CTkScrollableFrame(panel, fg_color="transparent", label_text="")
        scroll.grid(row=0, column=0, sticky="nsew", padx=4, pady=6)
        scroll.columnconfigure(0, weight=1)

        self._cleanup_poster_label = ctk.CTkLabel(
            scroll, text="Pulsa una candidata\npara ver su ficha",
            width=180, height=220, text_color=PENDING_COLOR)
        self._cleanup_poster_label.pack(pady=(4, 2))
        self._cleanup_detail_title = ctk.CTkTextbox(
            scroll, width=200, height=1, wrap="word",
            font=ctk.CTkFont(size=13, weight="bold"), fg_color="transparent",
            activate_scrollbars=False)
        self._cleanup_detail_title.configure(state="disabled")
        self._cleanup_detail_title.pack(pady=(4, 0), fill="x")
        self._cleanup_detail_overview = ctk.CTkTextbox(
            scroll, width=200, height=1, wrap="word",
            font=ctk.CTkFont(size=11), fg_color="transparent",
            activate_scrollbars=False)
        self._cleanup_detail_overview.configure(state="disabled")
        self._cleanup_detail_overview.pack(pady=4, fill="x")

        self._cleanup_detail_path_lbl = ctk.CTkLabel(
            scroll, text="", font=ctk.CTkFont(size=11), text_color=PENDING_COLOR,
            wraplength=200, justify="left", anchor="w")
        self._cleanup_detail_path_lbl.pack(pady=(0, 4), fill="x")
        return panel

    def _show_cleanup_poster(self, item):
        """Al pulsar el nombre o el motivo de una fila, carga su ficha de
        TMDB en el panel lateral -- mismo patrón que
        _show_missing_ep_poster en Episodios."""
        self._cleanup_selected_item = item
        for it, row in self._cleanup_row_widgets:
            row.configure(fg_color=SELECTED_ROW_COLOR if it is item else ("gray95", "gray17"))
        self._update_status_bar()
        self._set_textbox_text(self._cleanup_detail_title, item.name)
        self._cleanup_detail_path_lbl.configure(text=f"📁 {item.ftp_path}")
        token = object()
        self._cleanup_poster_token = token

        if not item.tmdb_id:
            self._set_textbox_text(self._cleanup_detail_overview,
                                    "Sin coincidencia en Jellyfin/Plex -- no hay ficha de TMDB disponible.")
            self._cleanup_poster_label.configure(image=None, text="Sin póster")
            return

        self._set_textbox_text(self._cleanup_detail_overview, "Cargando...")
        self._cleanup_poster_label.configure(image=None, text="…")

        def worker():
            try:
                if item.media_type == "tv":
                    details = self.tmdb.get_tv_details(item.tmdb_id)
                else:
                    details = self.tmdb.get_movie_details(item.tmdb_id)
            except Exception:
                details = {}
            overview = details.get("overview", "") or ""
            poster_path = details.get("poster_path")
            poster_url = f"{TMDB_IMAGE}{poster_path}" if poster_path else None
            self.after(0, lambda: self._apply_cleanup_detail(token, overview, poster_url))
        threading.Thread(target=worker, daemon=True).start()

    def _apply_cleanup_detail(self, token, overview: str, poster_url):
        if self._cleanup_poster_token is not token:
            return   # el usuario ya pulsó otra candidata mientras esta cargaba
        self._set_textbox_text(self._cleanup_detail_overview, overview or "Sin sinopsis disponible")
        if poster_url:
            threading.Thread(target=self._load_cleanup_poster, args=(poster_url, token), daemon=True).start()
        else:
            self._cleanup_poster_label.configure(image=None, text="Sin póster")

    def _load_cleanup_poster(self, url: str, token):
        try:
            resp = requests.get(url, timeout=8)
            img = Image.open(BytesIO(resp.content)).resize((180, 260), Image.LANCZOS)
            ctk_img = ctk.CTkImage(img, size=(180, 260))
            def _apply():
                if self._cleanup_poster_token is token:
                    self._cleanup_poster_label.configure(image=ctk_img, text="")
                    self._cleanup_current_poster = ctk_img
            self.after(0, _apply)
        except Exception:
            pass

    def _toggle_cleanup_item_favorite(self, item):
        # _apply_cleanup_filters() ya llama a _render_cleanup_results() al
        # final -- reaplicar el filtro es lo que de verdad hace falta,
        # porque marcar favorito aquí saca la fila de la lista (ver arriba,
        # salvo que "Mostrar favoritos" esté activo).
        self._toggle_favorite(item.media_type, item.tmdb_id, item.name,
                               on_done=self._apply_cleanup_filters)

    def _toggle_cleanup_item_reservation(self, item):
        self._toggle_reservation(item.media_type, item.tmdb_id, item.name, item.size_bytes,
                                  on_done=self._apply_cleanup_filters)

    def _quota_status(self, user: str) -> tuple:
        """(used_gb, quota_gb, color, aviso) para una etiqueta de cuota de
        reservas -- aviso PROACTIVO en cuanto se supera el 90% del límite
        configurado (ver _reservation_quota_bytes), no solo al fallar un
        intento de reservar algo que ya no cabe (ver _toggle_reservation).
        Calculado en un único sitio para que Liberar espacio y Protegidos
        coincidan siempre en cuándo avisar."""
        from core.reservations import used_bytes
        quota_bytes = self._reservation_quota_bytes()
        used = used_bytes(self._reservations, user)
        quota_gb = quota_bytes / (1024 ** 3)
        ratio = used / quota_bytes if quota_bytes else 0
        if ratio >= 1.0:
            color, aviso = ERROR_COLOR, " -- cuota agotada"
        elif ratio >= 0.9:
            color, aviso = WARNING_COLOR, " ⚠ cerca del límite"
        else:
            color, aviso = PENDING_COLOR, ""
        return used / (1024 ** 3), quota_gb, color, aviso

    def _update_cleanup_quota_label(self):
        user = self.config_data.get("app_user_name", "").strip()
        if not user:
            self._cleanup_quota_lbl.configure(
                text="Configura \"Tu nombre\" en Ajustes → Conexión FTP para poder reservar espacio.",
                text_color=PENDING_COLOR)
            return
        used_gb, quota_gb, color, aviso = self._quota_status(user)
        self._cleanup_quota_lbl.configure(
            text=f"Reservado por {user}: {used_gb:.1f} de {quota_gb:.0f}GB{aviso}", text_color=color)

    def _confirm_and_delete_cleanup_item(self, item):
        """Un solo elemento, con confirmación explícita -- nunca hay un
        "seleccionar todo y borrar" en esta vista. Ver _ConfirmDeleteDialog.

        El botón "Eliminar" ya sale deshabilitado para favoritos/reservados
        (ver _build_cleanup_result_row), pero esta comprobación se repite
        aquí como defensa en profundidad: si otro cliente marcó el mismo
        ítem como favorito/reservado mientras esta fila ya estaba pintada
        (sin refrescar), el estado en memoria de este botón podría estar
        desfasado -- y un borrado accidental de algo protegido no tiene
        vuelta atrás."""
        if self._is_favorite(item.media_type, item.tmdb_id) or self._is_reserved(item.media_type, item.tmdb_id):
            messagebox.showwarning(
                "Protegida", f"\"{item.name}\" está marcada como favorita o reservada -- no se puede borrar.")
            return
        reason = self._cleanup_item_reason_text(item)
        dlg = _ConfirmDeleteDialog(self, item.name, item.ftp_path, item.size_bytes, reason)
        if not dlg.result:
            return
        threading.Thread(target=self._delete_cleanup_item_worker, args=(item, reason), daemon=True).start()

    def _delete_cleanup_item_worker(self, item, reason: str):
        from core.ftp_client import FTPClient
        own_ftp = FTPClient()
        ok, msg = own_ftp.connect(
            self.config_data.get("ftp_host", ""), int(self.config_data.get("ftp_port", 21)),
            self.config_data.get("ftp_user", ""), self.config_data.get("ftp_password", ""),
            self.config_data.get("ftp_use_tls", False))
        if ok:
            if item.loose_file_paths:
                # Archivos sueltos (sin carpeta propia) -- se borra cada
                # archivo del grupo (vídeo + póster/backdrop/.../nfo) uno
                # a uno, no una carpeta entera. Se detiene en el primer
                # fallo, igual que delete_folder_recursive.
                ok, msg = True, "Archivos eliminados"
                for file_path in item.loose_file_paths:
                    ok, msg = own_ftp.delete_file(file_path)
                    if not ok:
                        break
            else:
                ok, msg = own_ftp.delete_folder_recursive(item.ftp_path)
            own_ftp.disconnect()
        self._save_deletion_history_entry(
            name=item.name, ftp_path=item.ftp_path, size_bytes=item.size_bytes,
            reason=reason, status="ok" if ok else "error", error_msg="" if ok else msg)
        self.after(0, lambda: self._finish_delete_cleanup_item(item, ok, msg))

    def _finish_delete_cleanup_item(self, item, ok: bool, msg: str):
        if ok:
            self._cleanup_raw_items = [it for it in self._cleanup_raw_items if it is not item]
            self._cleanup_filtered_items = [it for it in self._cleanup_filtered_items if it is not item]
            items = self._cleanup_filtered_items
            if items:
                total_size = sum(it.size_bytes for it in items)
                self._cleanup_results_lbl.configure(
                    text=f"{len(items)} candidata(s) -- {_fmt_size(total_size)} liberables")
            else:
                self._cleanup_results_lbl.configure(text="")
            self._render_cleanup_page()   # preserva la página actual (clamped), no la resetea a la 1ª
            self._set_status(f"Eliminado: {item.name} ({_fmt_size(item.size_bytes)} liberados)", SUCCESS_COLOR)
            self._refresh_ftp_space()   # el borrado cambia el espacio libre real -- refrescar el indicador
            # Actualizar también el caché en disco -- si no, el elemento
            # borrado volvería a aparecer al reabrir la app (cargaría el
            # análisis guardado de antes del borrado).
            from core.cleanup_candidates_cache import save_cache
            try:
                save_cache(self._cleanup_raw_items, getattr(self, "_cleanup_last_scan_ts", None) or _time.time())
            except Exception:
                _log.warning("Liberar espacio: no se pudo actualizar el caché tras borrar", exc_info=True)
            # Si la serie borrada también aparecía en "Episodios que
            # faltan" (con SOLO algunos episodios), esa fila ahora está
            # obsoleta -- ya no queda nada de ella en el servidor, así que
            # no tiene sentido seguir mostrándola como "pendiente". Mismo
            # mecanismo que al subir un episodio (ver
            # _remove_uploaded_from_missing_episodes); en películas no hay
            # "episodios que faltan" que actualizar.
            if item.media_type == "tv" and item.tmdb_id is not None:
                self._remove_series_from_missing_episodes(item.tmdb_id)
        else:
            self._set_status(f"No se pudo eliminar {item.name}: {msg}", ERROR_COLOR)

    # -------------------------------------------------------- Protegidos tab

    def _build_protected_tab(self, parent):
        """Gestión de TODO lo que el usuario actual tenga reservado (ver
        core/reservations.py), venga de Archivos o de Liberar espacio --
        cuota usada y un botón para liberar cada uno sin tener que ir a
        buscarlo en la pestaña donde se marcó."""
        parent.grid_rowconfigure(2, weight=1)

        header = ctk.CTkFrame(parent, fg_color=("gray90", "gray20"), corner_radius=8)
        header.grid(row=0, column=0, sticky="ew", pady=(0, CONTAINER_GAP))
        header.grid_columnconfigure(0, weight=1, uniform="protected_header_sides")
        header.grid_columnconfigure(1, weight=0)
        header.grid_columnconfigure(2, weight=1, uniform="protected_header_sides")

        left_fr = ctk.CTkFrame(header, fg_color="transparent")
        left_fr.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(left_fr, text="Todo lo que hayas reservado desde Archivos o Liberar espacio.",
                     font=ctk.CTkFont(size=11), text_color=PENDING_COLOR).pack(side="left", padx=12, pady=8)

        ctk.CTkLabel(header, text="Protegidos",
                     font=ctk.CTkFont(size=14, weight="bold")).grid(row=0, column=1, padx=16, pady=8)

        # Columna derecha deliberadamente sin ningún widget dentro -- basta
        # con declarar el column/weight/uniform para que el título quede
        # centrado de verdad (ver _build_cleanup_tab); un CTkFrame() vacío
        # aquí usaría su alto por defecto (~200px, ver el mismo comentario
        # en _build_cleanup_tab sobre watched_threshold_frame) e inflaba
        # todo el contenedor de la cabecera.

        top_bar = ctk.CTkFrame(parent, fg_color="transparent")
        top_bar.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        self._protected_quota_lbl = ctk.CTkLabel(
            top_bar, text="", font=ctk.CTkFont(size=12), text_color=PENDING_COLOR, anchor="w")
        self._protected_quota_lbl.pack(side="left")
        self._protected_show_all_var = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(top_bar, text="Ver todo el servidor", variable=self._protected_show_all_var,
                      command=self._render_protected_table).pack(side="right")

        body = ctk.CTkFrame(parent, fg_color="transparent")
        body.grid(row=2, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)

        sw0 = self._saved_col_widths("protected")
        self._protected_table = TableView(body, columns=[
            ColumnSpec("icon", "", width=28),
            ColumnSpec("name", "Reservado", expand=True),
            ColumnSpec("size", "Tamaño", width=sw0.get("size", 90), min_width=60),
            ColumnSpec("owner", "Reservado por", width=sw0.get("owner", 120), min_width=60),
            ColumnSpec("release", "", width=110),
        ])
        self._protected_table.grid(row=0, column=0, sticky="nsew")
        self._protected_table.on_widths_changed = lambda w: self._save_table_col_widths("protected", w)
        self._protected_table.enable_dynamic_page_size(self._on_protected_page_size_changed)

        self._protected_name_font = ctk.CTkFont(size=12)

        # Paginado (ver TableView.page_size, calculado dinámicamente) -- esta era la única tabla de
        # listado de toda la app sin límite: con muchas reservas acumuladas
        # se reconstruía entera (sin límite de filas) cada vez que se
        # entraba en la pestaña. Mismo patrón que Historial/Liberar espacio.
        nav_fr = ctk.CTkFrame(parent, fg_color="transparent")
        nav_fr.grid(row=3, column=0, pady=(6, 0))
        self._protected_prev_btn = ctk.CTkButton(
            nav_fr, text="< Anterior", width=100, fg_color="transparent", border_width=1,
            command=lambda: self._protected_change_page(-1))
        self._protected_prev_btn.pack(side="left")
        self._protected_page_lbl = ctk.CTkLabel(nav_fr, text="", text_color=PENDING_COLOR)
        self._protected_page_lbl.pack(side="left", padx=12)
        self._protected_next_btn = ctk.CTkButton(
            nav_fr, text="Siguiente >", width=100, fg_color="transparent", border_width=1,
            command=lambda: self._protected_change_page(1))
        self._protected_next_btn.pack(side="left")

        self._protected_items = []   # página actual, ver _render_protected_table
        self._protected_page = 0

    def _render_protected_table(self):
        # Envoltorio fino solo para medir tiempos (ver app.log) -- mismo
        # patrón que _show_view/_show_view_impl.
        _t0 = _time.perf_counter()
        try:
            self._render_protected_table_impl()
        finally:
            _log.info("Vista: _render_protected_table %6.0f ms", (_time.perf_counter() - _t0) * 1000)

    def _render_protected_table_impl(self):
        """Recalcula qué mostrar a partir de self._reservations ahora mismo
        -- se llama al abrir la pestaña, tras liberar una fila, tras
        alternar "Ver todo el servidor" y tras cualquier sincronización con
        el FTP mientras esta pestaña esté activa (ver
        _apply_synced_reservations). Vuelve siempre a la primera página;
        separado de _render_protected_page para que cambiar de página no
        implique recalcular el filtro."""
        user = self.config_data.get("app_user_name", "").strip()
        show_all = self._protected_show_all_var.get()

        if not user:
            self._protected_quota_lbl.configure(
                text="Configura \"Tu nombre\" en Ajustes → Conexión FTP para poder reservar espacio.",
                text_color=PENDING_COLOR)
        else:
            used_gb, quota_gb, color, aviso = self._quota_status(user)
            self._protected_quota_lbl.configure(
                text=f"Tu cuota ({user}): {used_gb:.1f} de {quota_gb:.0f}GB reservados{aviso}",
                text_color=color)

        if show_all:
            shown = list(self._reservations.items())
        elif user:
            shown = [(key, entry) for key, entry in self._reservations.items()
                     if entry.get("reserved_by") == user]
        else:
            shown = []
        shown.sort(key=lambda kv: kv[1].get("name", "").lower())

        self._protected_items = shown
        self._protected_page = 0
        self._render_protected_page()

    def _protected_change_page(self, delta: int):
        n_pages = max(1, -(-len(self._protected_items) // self._protected_table.page_size))
        new_page = max(0, min(n_pages - 1, self._protected_page + delta))
        if new_page == self._protected_page:
            return
        self._protected_page = new_page
        self._render_protected_page()

    def _on_protected_page_size_changed(self, new_size: int):
        """Callback de TableView.enable_dynamic_page_size -- mismo motivo
        que _on_history_page_size_changed: _render_protected_page_impl no
        reclampa self._protected_page por su cuenta, así que un cambio de
        tamaño de página por redimensionado necesita reclamparlo aquí
        antes de redibujar."""
        n_pages = max(1, -(-len(self._protected_items) // new_size))
        self._protected_page = max(0, min(n_pages - 1, self._protected_page))
        self._render_protected_page()

    def _render_protected_page(self):
        _t0 = _time.perf_counter()
        try:
            self._render_protected_page_impl()
        finally:
            _log.info("Vista: _render_protected_page %6.0f ms", (_time.perf_counter() - _t0) * 1000)

    def _render_protected_page_impl(self):
        """Dibuja solo la página actual (self._protected_page) de
        self._protected_items -- ver TableView.page_size."""
        self._protected_table.clear_rows()

        user = self.config_data.get("app_user_name", "").strip()
        show_all = self._protected_show_all_var.get()
        total = len(self._protected_items)
        page_size = self._protected_table.page_size
        n_pages = max(1, -(-total // page_size))
        start = self._protected_page * page_size
        page_items = self._protected_items[start:start + page_size]

        self._protected_page_lbl.configure(
            text=f"Página {self._protected_page + 1} de {n_pages}" if total else "")
        self._protected_prev_btn.configure(state="normal" if self._protected_page > 0 else "disabled")
        self._protected_next_btn.configure(state="normal" if self._protected_page < n_pages - 1 else "disabled")

        if not page_items:
            if show_all:
                msg = "Nadie ha reservado nada en este servidor todavía."
            elif user:
                msg = ("Nada reservado todavía -- usa el candado 🔒 en Archivos o Liberar espacio "
                       "para proteger algo de borrado.")
            else:
                msg = "Configura tu nombre en Ajustes para empezar a reservar espacio."
            ctk.CTkLabel(self._protected_table.body, text=msg, text_color=PENDING_COLOR).pack(pady=30)
            return

        for key, entry in page_items:
            self._build_protected_row(key, entry, user)
        self._protected_table.scroll_to_top()
        self._protected_table.note_rows_rendered(len(page_items))

    def _build_protected_row(self, key: str, entry: dict, user: str):
        cw = self._protected_table.col_width
        row = ctk.CTkFrame(self._protected_table.body, fg_color=("gray95", "gray17"))
        row.pack(fill="x", pady=3, padx=2)

        icon = "📺" if entry.get("media_type") == "tv" else "🎬"
        ctk.CTkLabel(row, text=icon, width=cw("icon")).pack(side="left", padx=(8, 4), pady=8)

        ctk.CTkLabel(row, text=entry.get("name", ""), font=self._protected_name_font,
                     anchor="w").pack(side="left", fill="x", expand=True, padx=(4, 4), pady=8)

        ctk.CTkLabel(row, text=_fmt_size(entry.get("size_bytes", 0)), width=cw("size"),
                     text_color=PENDING_COLOR).pack(side="left", padx=(0, 4), pady=8)

        owner = entry.get("reserved_by", "")
        ctk.CTkLabel(row, text=owner, width=cw("owner"),
                     text_color=ACCENT if owner == user else PENDING_COLOR).pack(
            side="left", padx=(0, 4), pady=8)

        # Solo el dueño puede liberarla de verdad -- _toggle_reservation ya
        # lo comprueba y avisa, pero deshabilitar el botón aquí (en vez de
        # dejar que el aviso aparezca cada vez) es más claro en la vista
        # "Ver todo el servidor", donde la mayoría de filas serán ajenas.
        is_mine = owner == user
        ctk.CTkButton(row, text="🔓 Liberar" if is_mine else "🔒 De otra persona",
                      width=cw("release"), fg_color="transparent", border_width=1,
                      state="normal" if is_mine else "disabled",
                      command=lambda k=key, e=entry: self._release_protected_row(k, e)).pack(
            side="left", padx=8, pady=6)

    def _release_protected_row(self, key: str, entry: dict):
        # Reutiliza _toggle_reservation tal cual: la fila solo existe
        # aquí si ya está reservada, así que siempre toma la rama de
        # liberar (comprueba dueño, sincroniza con FTP, etc. -- mismo
        # camino que el candado de Archivos/Liberar espacio).
        self._toggle_reservation(entry["media_type"], entry["tmdb_id"], entry.get("name", ""),
                                  entry.get("size_bytes", 0), on_done=self._render_protected_table)

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
