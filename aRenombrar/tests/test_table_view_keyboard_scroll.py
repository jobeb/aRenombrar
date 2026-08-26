"""Desplazamiento de los listados con el teclado (gui/table_view.py).

Estos tests SÍ construyen widgets de verdad, así que se saltan solos donde no
haya customtkinter/tkinter -- entre otros sitios, el CI (ver
.github/workflows/tests.yml, que a propósito no instala las dependencias de
interfaz). En el equipo de desarrollo se ejecutan y cubren el hueco que deja
pyflakes: dos fallos reales que solo aparecían al ARRANCAR la app.

  1. `TableView.bind_all(...)` -> AttributeError: CTkBaseClass lo prohíbe.
     Tumbaba el arranque entero antes de pintar la primera pestaña.
  2. `self._canvas()` -> TypeError: 'CTkCanvas' object is not callable.
     CTkFrame ya define un atributo `_canvas`, que tapaba el método. El
     binding se disparaba en cada tecla y moría en la primera línea, así que
     "no funcionan las flechas" sin una sola pista en pantalla.
"""

import pytest

ctk = pytest.importorskip("customtkinter")

from gui.table_view import ColumnSpec, TableView


class _FakeKey:
    """Lo justo que mira _on_scroll_key de un evento de tecla."""
    def __init__(self, keysym):
        self.keysym = keysym
        self.x_root = 0
        self.y_root = 0


@pytest.fixture(scope="module")
def root():
    """UN solo root de Tk para todo el módulo: crear y destruir varios en el
    mismo proceso falla al recargar tk.tcl ("this probably means that tk wasn't
    installed properly"), y con un fixture por test eso hacía saltar uno suelto
    de forma aleatoria."""
    try:
        win = ctk.CTk()
    except Exception as e:                      # sin entorno gráfico
        pytest.skip(f"sin display para tkinter: {e}")
    win.geometry("600x200")
    yield win
    win.destroy()


@pytest.fixture
def tabla(root):
    holder = ctk.CTkFrame(root)
    holder.pack(fill="both", expand=True)
    tv = TableView(holder, columns=[ColumnSpec("a", "Columna", width=200)])
    tv.pack(fill="both", expand=True)
    # Filas de sobra para que haya algo que desplazar de verdad.
    for i in range(60):
        ctk.CTkLabel(tv.body, text=f"fila {i}").pack(fill="x")
    root.update()
    # El ratón no está de verdad encima de la tabla en un test: se simula el
    # "¿qué widget hay bajo el puntero?" que usa _on_scroll_key.
    tv.winfo_containing = lambda x, y: tv.body
    yield tv
    holder.destroy()


def test_construir_la_tabla_no_revienta_por_bind_all(tabla):
    # Si _enable_keyboard_scroll usara self.bind_all, la fixture ni llegaría.
    assert tabla._scroll_canvas() is not None


def test_flecha_abajo_desplaza_hacia_abajo(tabla):
    antes = tabla._scroll_canvas().yview()[0]
    tabla._on_scroll_key(_FakeKey("Down"))
    tabla.update()
    assert tabla._scroll_canvas().yview()[0] > antes


def test_flecha_arriba_vuelve_hacia_arriba(tabla):
    tabla._on_scroll_key(_FakeKey("Next"))     # bajar una página primero
    tabla.update()
    medio = tabla._scroll_canvas().yview()[0]
    assert medio > 0
    tabla._on_scroll_key(_FakeKey("Up"))
    tabla.update()
    assert tabla._scroll_canvas().yview()[0] < medio


def test_fin_e_inicio_van_a_los_extremos(tabla):
    tabla._on_scroll_key(_FakeKey("End"))
    tabla.update()
    assert tabla._scroll_canvas().yview()[1] == pytest.approx(1.0, abs=0.01)
    tabla._on_scroll_key(_FakeKey("Home"))
    tabla.update()
    assert tabla._scroll_canvas().yview()[0] == pytest.approx(0.0, abs=0.01)


def test_no_secuestra_las_flechas_mientras_se_escribe(tabla, root):
    entry = ctk.CTkEntry(root)
    entry.pack()
    tabla.update()
    tabla.focus_get = lambda: entry._entry   # el widget real que recibe el foco
    antes = tabla._scroll_canvas().yview()[0]
    assert tabla._on_scroll_key(_FakeKey("Down")) is None
    tabla.update()
    assert tabla._scroll_canvas().yview()[0] == antes


def test_no_hace_nada_si_el_raton_esta_fuera_de_la_tabla(tabla):
    tabla.winfo_containing = lambda x, y: None
    antes = tabla._scroll_canvas().yview()[0]
    assert tabla._on_scroll_key(_FakeKey("Down")) is None
    assert tabla._scroll_canvas().yview()[0] == antes
