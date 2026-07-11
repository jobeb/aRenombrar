"""
TableView: cabecera de columnas + cuerpo con scroll, compartido por las
cuatro pestañas de lista (Archivos, Episodios que faltan, Liberar espacio,
Historial). Existe para arreglar de raíz un bug que se repitió varias
veces al mantener cada tabla por separado: la cabecera y las filas leían
anchos de columna de sitios distintos (o calculados con fórmulas
distintas), así que cualquier cambio en una fila -- un botón nuevo, un
icono nuevo -- descuadraba la cabecera sin que fuera obvio por qué.

Con TableView, cabecera y filas SIEMPRE leen el mismo ancho para la misma
columna (self.col_width(key)), y la cabecera nunca se puede salir de su
columna: si el texto de la cabecera no cabe en el ancho pedido, la
columna crece para acomodarlo (antes, el CTkLabel de la cabecera crecía
él solo más allá del ancho configurado y desalineaba todo lo de después).
"""

import tkinter as tk
import customtkinter as ctk


class ColumnSpec:
    """Una columna de TableView.

    key: identificador (el que se usa luego en table.col_width(key)).
    header: texto de la cabecera ("" para una columna sin etiqueta, p.ej.
        la de un botón de acción).
    width: ancho base en px (ignorado si expand=True).
    min_width: ancho mínimo al arrastrar un separador (ver resizable).
    resizable: si hay un separador arrastrable a la DERECHA de esta
        columna, que redistribuye ancho entre esta columna y la
        siguiente (igual que _sash_press/_sash_motion de Archivos).
    expand: esta columna ocupa el espacio sobrante (fill="x",
        expand=True) -- como máximo UNA columna de la tabla debería
        marcarse así.
    """

    __slots__ = ("key", "header", "width", "min_width", "resizable", "expand", "anchor")

    def __init__(self, key, header="", width=80, min_width=40,
                 resizable=False, expand=False, anchor="w"):
        self.key = key
        self.header = header
        self.width = width
        self.min_width = min_width
        self.resizable = resizable
        self.expand = expand
        self.anchor = anchor


class TableView(ctk.CTkFrame):
    """Cabecera fija (no scrollea) + CTkScrollableFrame para las filas.

    Uso:
        table = TableView(parent, columns=[
            ColumnSpec("nombre", "Nombre", width=200, resizable=True),
            ColumnSpec("detalle", "Detalle", expand=True),
        ])
        table.grid(...)
        # construir una fila:
        row = ctk.CTkFrame(table.body)
        row.pack(fill="x", pady=3, padx=2)
        ctk.CTkLabel(row, width=table.col_width("nombre"), ...).pack(side="left")
        ...

    Las columnas con resizable=True/expand=True se recalculan solas al
    arrastrar su separador; las demás mantienen su `width` tal cual
    (súbelo a mano con set_width() si hace falta adaptarlo, p.ej. al
    ancho de la ventana -- ver _on_table_resize en Archivos)."""

    _PADX_BETWEEN = 4   # separación horizontal entre columnas consecutivas
    _HEADER_TEXT_PADDING = 16   # margen que se añade al medir el texto de cabecera

    def __init__(self, master, columns, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self.columns = columns
        self._widths = {}
        self._header_min = {}   # ancho mínimo para que quepa el texto de cabecera -- ver _init_widths
        self._sash_state = None
        self._header_labels = {}
        self._bold_font = ctk.CTkFont(weight="bold")

        self.header_frame = ctk.CTkFrame(self, corner_radius=0)
        self.header_frame.grid(row=0, column=0, sticky="ew")

        self.body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.body.grid(row=1, column=0, sticky="nsew", pady=(2, 0))

        self._init_widths()
        self._build_header()

    # ------------------------------------------------------------ anchos --

    def _min_width_for(self, key: str) -> int:
        """Ancho mínimo real de una columna: el mayor entre min_width (el
        que se pidió para arrastrar) y lo que necesita su propio texto de
        cabecera -- si no, arrastrar un separador puede volver a dejar la
        cabecera más ancha que su columna (el bug que motivó TableView)."""
        col = next(c for c in self.columns if c.key == key)
        return max(col.min_width, self._header_min.get(key, 0))

    def _init_widths(self):
        for col in self.columns:
            width = col.width
            if col.header and not col.expand:
                # La cabecera nunca se sale de su columna: si el texto no
                # cabe en el ancho pedido, la columna crece para acomodarlo
                # (esto es justo lo que fallaba antes -- el CTkLabel de la
                # cabecera crecía él solo y desalineaba lo de después).
                needed = self._bold_font.measure(col.header) + self._HEADER_TEXT_PADDING
                self._header_min[col.key] = needed
                width = max(width, needed)
            self._widths[col.key] = width

    def col_width(self, key: str) -> int:
        return self._widths[key]

    def set_width(self, key: str, width: int, refresh: bool = True):
        """Cambia el ancho base de una columna (p.ej. al redimensionar la
        ventana en Archivos, ver _on_table_resize) -- no toca columnas
        expand=True, que siempre ocupan el espacio sobrante. Nunca deja la
        columna más estrecha de lo que necesita su propio texto de
        cabecera (ver _min_width_for)."""
        self._widths[key] = max(width, self._min_width_for(key))
        if refresh:
            self.refresh_header(keys=[key])

    # ------------------------------------------------------------ cabecera --

    def _next_resizable_pair(self, key: str):
        keys = [c.key for c in self.columns]
        i = keys.index(key)
        return keys[i + 1] if i + 1 < len(keys) else None

    def _build_header(self):
        for w in self.header_frame.winfo_children():
            w.destroy()
        self._header_labels = {}

        for i, col in enumerate(self.columns):
            lbl = ctk.CTkLabel(
                self.header_frame, text=col.header, font=self._bold_font, anchor=col.anchor,
                width=0 if col.expand else self._widths[col.key])
            padx = (self._PADX_BETWEEN, 0) if i > 0 else (0, 0)
            if col.expand:
                lbl.pack(side="left", padx=padx, pady=4, fill="x", expand=True)
            else:
                lbl.pack(side="left", padx=padx, pady=4)
            self._header_labels[col.key] = lbl

            if col.resizable:
                right_key = self._next_resizable_pair(col.key)
                if right_key is not None:
                    sash = tk.Frame(self.header_frame, width=4, bg="#505060",
                                     cursor="sb_h_double_arrow")
                    sash.pack(side="left", fill="y", pady=6)
                    sash.bind("<ButtonPress-1>", lambda e, l=col.key, r=right_key: self._sash_press(e, l, r))
                    sash.bind("<B1-Motion>", self._sash_motion)
                    sash.bind("<ButtonRelease-1>", self._sash_release)

    def refresh_header(self, keys=None):
        """Actualiza el ancho de las etiquetas ya construidas -- NUNCA
        destruye/recrea los widgets de la cabecera (a diferencia de
        _build_header, que solo se llama una vez, al construir la
        tabla). Si esto reconstruyera en cada arrastre de separador,
        el propio separador que está recibiendo los eventos de ratón
        (<B1-Motion>) se destruiría a mitad de gesto, cortando el
        arrastre en cuanto el cursor se moviera -- justo el bug que
        reportó el usuario ("se suelta al salir del separador").

        keys: si se da, solo reconfigura esas columnas (p.ej. las dos
        que cambian en un arrastre de separador) -- reconfigurar TODAS
        las etiquetas en cada píxel de arrastre, aunque no cambien de
        ancho, generaba más trabajo de redibujado del necesario."""
        for col in self.columns:
            if col.expand or (keys is not None and col.key not in keys):
                continue
            lbl = self._header_labels.get(col.key)
            if lbl is not None:
                lbl.configure(width=self._widths[col.key])

    def header_label(self, key: str):
        return self._header_labels.get(key)

    # --------------------------------------------------- arrastre de sash --

    def _sash_press(self, event, left_key, right_key):
        self._sash_state = {
            "left": left_key, "right": right_key, "x0": event.x_root,
            "left_w": self._widths[left_key], "right_w": self._widths[right_key],
        }

    def _sash_motion(self, event):
        if not self._sash_state:
            return
        delta = event.x_root - self._sash_state["x0"]
        new_l = max(self._min_width_for(self._sash_state["left"]), self._sash_state["left_w"] + delta)
        new_r = max(self._min_width_for(self._sash_state["right"]), self._sash_state["right_w"] - delta)
        self._widths[self._sash_state["left"]] = new_l
        self._widths[self._sash_state["right"]] = new_r
        self.refresh_header(keys=[self._sash_state["left"], self._sash_state["right"]])
        if self.on_column_resize:
            self.on_column_resize()

    def _sash_release(self, event):
        self._sash_state = None
        if self.on_widths_changed:
            self.on_widths_changed(self.all_widths())

    def all_widths(self) -> dict:
        """Anchos actuales de todas las columnas no-expand -- para guardar
        en disco tras un arrastre (ver on_widths_changed). No se incluye
        la columna expand porque su ancho no es un valor propio, es "lo
        que sobra" tras restar las demás."""
        return {c.key: self._widths[c.key] for c in self.columns if not c.expand}

    # ------------------------------------------------------------- cuerpo --

    on_column_resize = None   # callback opcional: redibujar filas tras arrastrar un sash
    on_widths_changed = None  # callback opcional: persistir anchos tras soltar un separador

    def clear_rows(self):
        """Vacía el cuerpo de la tabla (todas las filas) -- la cabecera
        vive en un frame aparte, así que esto nunca se la lleva por
        delante."""
        for w in self.body.winfo_children():
            w.destroy()

    def scroll_to_top(self):
        self.body._parent_canvas.yview_moveto(0)
