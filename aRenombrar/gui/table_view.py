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

    # Tamaño de página dinámico (ver enable_dynamic_page_size/note_rows_rendered
    # más abajo) -- en vez de un número fijo de filas por pestaña, se calcula
    # cuántas caben de verdad en el alto real del canvas, para no depender del
    # scroll interno en uso normal.
    MIN_PAGE_ROWS = 10       # suelo pedido por el usuario, aunque la ventana sea muy pequeña
    DEFAULT_PAGE_ROWS = 25   # valor de arranque, antes de medir ninguna fila real
    _RESIZE_DEBOUNCE_MS = 150   # mismo valor que _DETAIL_RESIZE_DEBOUNCE_MS en gui/app.py

    def __init__(self, master, columns, scrollable: bool = True, **kwargs):
        """scrollable=False: self.body es un CTkFrame normal (crece con
        cada fila añadida, sin barra de scroll propia) en vez de un
        CTkScrollableFrame -- para listas pequeñas/ya acotadas (p.ej. ya
        paginadas) que deben apoyarse en el scroll de la pantalla que las
        contiene, no tener una segunda barra de scroll anidada e inútil
        (ver "Sincronizar visionado"). Por defecto True: mismo
        comportamiento de siempre, el que necesitan Archivos/Episodios/
        Liberar espacio/Historial con listas sin acotar."""
        # border_width en el propio TableView (aunque fg_color siga
        # "transparent"): sin esto, el "contenedor" de la tabla no tiene
        # ningún límite visible propio -- depende por completo de que el
        # color de fondo heredado del padre contraste con el de las filas,
        # lo cual varía según dónde se monte la tabla (CTkTabview resuelve
        # su propio fg_color de forma distinta según el color del SUYO,
        # ver ctk_tabview.py) y llevó a que la tabla de "Usuarios
        # emparejados" (Sincronizar visionado) se fundiera con el fondo
        # pese a que las filas ya tenían su color de la convención
        # establecida. Un borde explícito garantiza el límite sin
        # depender de esa cadena de colores heredados.
        super().__init__(master, fg_color="transparent",
                          border_width=1, border_color=("gray70", "gray30"), **kwargs)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self.columns = columns
        self.scrollable = scrollable
        self._widths = {}
        self._header_min = {}   # ancho mínimo para que quepa el texto de cabecera -- ver _init_widths
        self._sash_state = None
        self._header_labels = {}
        self._bold_font = ctk.CTkFont(weight="bold")

        self.page_size = self.DEFAULT_PAGE_ROWS
        self._row_stride_px = None   # alto medio medido por fila, ver note_rows_rendered
        self._resize_after_id = None
        self.on_page_size_changed = None   # callback opcional, ver enable_dynamic_page_size

        # fg_color explícito: sin esto la cabecera usa el gris por defecto
        # de CTkFrame (gray17 en modo oscuro), que es EXACTAMENTE el mismo
        # gris que las filas (ver fg_color=("gray95","gray17") en las
        # filas de cada pestaña) -- cabecera y filas se fundían en un solo
        # bloque sin ninguna línea de separación entre ambas.
        self.header_frame = ctk.CTkFrame(self, corner_radius=0, fg_color=("gray80", "gray23"))
        self.header_frame.grid(row=0, column=0, sticky="ew")

        if scrollable:
            # fg_color explícito, mismo motivo que en la rama scrollable=False
            # de abajo: CTkScrollableFrame, con fg_color="transparent", delega
            # su color real de fondo a un CTkFrame interno (self._parent_frame)
            # que a su vez recorre la cadena de padres "transparent" hacia
            # arriba (customtkinter._detect_color_of_master) para adivinarlo.
            # Cuantos más niveles de frames "transparent" haya de por medio
            # (Archivos/Episodios/etc. anidan varios: pestaña → body → este
            # TableView → CTkScrollableFrame), más falla esa adivinanza --
            # se notó como un recuadro mal coloreado justo en las esquinas
            # redondeadas de la primera fila, pegado al triángulo de
            # expandir/colapsar de Episodios que faltan (que vive ahí
            # pegado al borde izquierdo). Un color fijo aquí corta esa
            # cadena de raíz, igual que ya se hizo abajo para scrollable=False.
            self.body = ctk.CTkScrollableFrame(self, fg_color=("gray92", "gray14"))
        else:
            # fg_color explícito (NO "transparent"): con scrollable=False,
            # self.body es un CTkFrame normal colgado de una cadena de
            # frames "transparent" (mapping_fr → fr → outer CTkScrollableFrame
            # → tab de CTkTabview) -- customtkinter recorre esa cadena para
            # calcular su color real de fondo, y con esa cadena tan larga
            # (y un CTkTabview de por medio, que decide su propio fg_color
            # de forma dinámica según el de SU padre) el cálculo salía
            # inconsistente entre pasadas de dibujado: las filas (encima
            # de este frame) se fundían con el fondo, y además el redondeado
            # de esquinas de la primera fila usaba un color de esquina
            # distinto arriba que abajo (el propio bg_color detectado
            # había cambiado entre un redibujado y otro). Un color propio y
            # fijo aquí corta esa cadena: cada fila detecta SIEMPRE el
            # mismo padre real, así que su redondeado es consistente y
            # contrasta de forma predecible con fg_color=("gray95","gray17")
            # de las filas.
            self.body = ctk.CTkFrame(self, fg_color=("gray92", "gray14"))
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
        """No-op si scrollable=False -- self.body es un CTkFrame normal,
        sin canvas de scroll que mover (el scroll, si hace falta, lo da
        la pantalla que contiene esta tabla)."""
        if self.scrollable:
            self.body._parent_canvas.yview_moveto(0)

    # ------------------------------------------------- tamaño de página --

    def enable_dynamic_page_size(self, on_change):
        """A partir de ahora, page_size se recalcula solo cuando el
        contenedor cambia de alto (redimensionar la ventana), en vez de
        quedarse fijo en DEFAULT_PAGE_ROWS -- on_change(nuevo_tamaño) se
        llama solo cuando el número de filas que caben cambia de verdad,
        nunca en cada píxel de un arrastre (ver _on_body_configure).
        No-op si scrollable=False: sin CTkScrollableFrame no hay canvas
        cuyo alto real represente "lo que cabe sin scroll" (ver
        Sincronizar visionado, cuyas tablas viven dentro del scroll de
        toda la pestaña, no de un hueco acotado propio)."""
        if not self.scrollable:
            return
        self.on_page_size_changed = on_change
        # add="+": este mismo canvas puede tener ya otro binding de
        # <Configure> propio de la pestaña (p.ej. _on_table_resize en
        # Archivos, que solo reajusta anchos de columna) -- sin add="+"
        # este binding lo sustituiría en vez de convivir con él.
        self.body._parent_canvas.bind("<Configure>", self._on_body_configure, add="+")

    def _on_body_configure(self, event):
        if self._resize_after_id is not None:
            self.after_cancel(self._resize_after_id)
        self._resize_after_id = self.after(
            self._RESIZE_DEBOUNCE_MS, lambda h=event.height: self._recompute_page_size(h))

    def _recompute_page_size(self, avail_height):
        self._resize_after_id = None
        if self._row_stride_px is None or avail_height <= 1:
            # Todavía no se ha medido ninguna fila real, o el canvas aún
            # no tiene geometría de verdad (ventana sin mapear) -- se
            # autocorrige en el siguiente note_rows_rendered().
            return
        fitting = max(self.MIN_PAGE_ROWS, avail_height // self._row_stride_px)
        if fitting != self.page_size:
            self.page_size = fitting
            if self.on_page_size_changed:
                self.on_page_size_changed(fitting)

    def note_rows_rendered(self, n_rows: int):
        """Las funciones _render_*_page deben llamar a esto justo al
        terminar de construir su página -- mide el alto medio real por
        fila (self._row_stride_px) y lo usa para saber cuántas filas
        caben la próxima vez que cambie el alto disponible.

        Se mide el promedio de TODA la página (winfo_reqheight() / n_rows),
        no el alto de una sola fila, para que absorba sin más el pady/padx
        propio de cada tabla (Archivos usa pady=1; las tablas "de tarjeta"
        como Episodios/Protegidos usan más espaciado) sin necesitar una
        constante de alto distinta por pestaña. winfo_reqheight() (no
        winfo_height()) porque ya está disponible nada más construir los
        widgets, incluso antes de que la ventana esté mapeada en
        pantalla -- así se puede medir en el primerísimo dibujado, no
        solo tras un redimensionado real.

        Nota: si la página incluye una fila "expandida" (ver
        self._missing_ep_expanded en gui/app.py, el estado de expandir/
        colapsar persiste entre redibujados), el alto medido sale un poco
        mayor de lo estrictamente necesario y el tamaño de página calculado
        es ligeramente conservador -- seguro (el canvas absorbe cualquier
        desajuste) y se autocorrige en cuanto el usuario colapsa filas.

        IMPORTANTE -- por qué la autocorrección solo dispara UNA vez: la
        primera versión de esto volvía a llamar a on_page_size_changed
        cada vez que la medida cambiaba (con el cuidado de solo remedir en
        páginas "completas"), pero eso no bastaba -- con una lista ya
        corta (menos elementos que page_size), TODA página es "completa"
        por definición (page_items nunca puede pasar del total), así que
        cada redibujado (p.ej. al ir terminando subidas una a una, o justo
        al arrancar con pocos resultados) volvía a remedir, y cualquier
        variación de un solo píxel entre medidas (redondeo de división
        entera, una fila con distinto contenido) disparaba otra
        autocorrección más -- un bucle de redibujado sin fin, reproducido
        dos veces en sesiones reales. Ahora la autocorrección SOLO ocurre
        una vez, la primerísima vez que hay una medida real (para
        corregir el valor de arranque, DEFAULT_PAGE_ROWS, por el real).
        A partir de ahí, page_size únicamente vuelve a cambiar por una
        redimensión de ventana genuina (ver _on_body_configure, que solo
        reacciona a que el propio CANVAS cambie de tamaño, no a que
        cambien las filas dentro) -- nunca porque el contenido cambie y
        se redibuje, así que un redibujado ya no puede encadenar otro."""
        if n_rows <= 0 or not self.scrollable:
            return
        first_measurement = self._row_stride_px is None
        if not first_measurement and n_rows < self.page_size:
            return   # página corta -- no fiable para refinar la medida (ver más arriba)
        self.update_idletasks()
        self._row_stride_px = max(1, self.body.winfo_reqheight() // n_rows)
        if first_measurement and self.on_page_size_changed:
            self.after(0, lambda: self._recompute_page_size(self.body._parent_canvas.winfo_height()))
