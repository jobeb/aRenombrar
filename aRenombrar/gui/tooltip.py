"""
Tooltip genérico para cualquier widget de customtkinter/tkinter -- aparece
tras una pequeña pausa al pasar el ratón por encima, se cierra al salir o al
hacer clic. customtkinter no trae ningún tooltip propio, y este es el primer
sitio del proyecto que necesita uno (ver core/trending.py::
explain_trending_score, usado en "Episodios que faltan"/"Liberar espacio").
"""

import tkinter as tk


class Tooltip:
    _DELAY_MS = 500

    def __init__(self, widget, text_fn):
        """text_fn: callable sin argumentos que devuelve el texto a
        mostrar en el momento de pasar el ratón (no una cadena fija) --
        así el tooltip siempre refleja el dato real de esa fila/celda en
        ese instante, aunque la fila se haya reconstruido desde que se
        creó el tooltip. Si text_fn() devuelve una cadena vacía/None, no
        se muestra nada (p.ej. una celda sin dato todavía)."""
        self.widget = widget
        self.text_fn = text_fn
        self._after_id = None
        self._tip = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<Button-1>", self._hide, add="+")

    def _schedule(self, event=None):
        self._cancel_pending()
        self._after_id = self.widget.after(self._DELAY_MS, self._show)

    def _cancel_pending(self):
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _show(self):
        self._after_id = None
        if self._tip is not None:
            return
        text = self.text_fn()
        if not text:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{x}+{y}")
        try:
            tip.attributes("-topmost", True)
        except Exception:
            pass
        tk.Label(tip, text=text, justify="left", background="#2b2b2b", foreground="#f0f0f0",
                  relief="solid", borderwidth=1, padx=8, pady=5,
                  font=("Segoe UI", 10)).pack()
        self._tip = tip

    def _hide(self, event=None):
        self._cancel_pending()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None


def attach_tooltip(widget, text_fn) -> Tooltip:
    """Punto de entrada simple -- ver Tooltip. Devuelve la instancia por si
    el llamador necesita guardarla (no hace falta para que funcione, los
    binds ya quedan puestos en *widget*, pero evita que un futuro GC la
    recoja antes de tiempo en algún caso límite)."""
    return Tooltip(widget, text_fn)
