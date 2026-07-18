import core.eldoblaje as ed


class _FakeResponse:
    def __init__(self, text="", status=200):
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


# HTML real de eldoblaje.com (recortado), capturado en vivo para Bleach --
# ver el comentario de core.eldoblaje._RESULT_RE/_INFO_RE.
_SEARCH_HTML = """
<td><a href="FichaPelicula.asp?id=53398" class="bodyclass">BLEACH</a></td>
<td><a href="FichaPelicula.asp?id=12616" class="bodyclass">BLEACH [serie de animaci&oacute;n]</a></td>
<a href="FichaActorOriginal.asp?id=31729" class="bodyclass">BLEACH, JULIAN</a>
"""

_DETAIL_HTML = """
<font color="#FFFFFF" class="arial18white">M&aacute;s informaci&oacute;n </font></td>
</tr>
<tr>
  <td class="trebuchett">
    <font color="#333333">
    Estrenada originalmente en Jap&oacute;n el 5-10-2004.</P><P>Consta de 366 episodios, de los
    que solo fueron doblados los 109 primeros.</P><P>Traducida por Maite Madinabeitia.
    </font> </td>
</tr>
"""


def test_search_series_without_title_returns_empty():
    assert ed.search_series("") == []


def test_search_series_filters_out_non_series_results(monkeypatch):
    monkeypatch.setattr(ed.requests, "get", lambda url, params, timeout: _FakeResponse(_SEARCH_HTML))
    results = ed.search_series("Bleach")
    assert results == [{"id": 12616, "name": "BLEACH [serie de animación]"}]


def test_search_series_returns_empty_on_network_failure(monkeypatch):
    def _raise(url, params, timeout):
        raise ConnectionError("sin red")
    monkeypatch.setattr(ed.requests, "get", _raise)
    assert ed.search_series("Bleach") == []


def test_search_series_returns_empty_on_no_matches(monkeypatch):
    monkeypatch.setattr(ed.requests, "get", lambda url, params, timeout: _FakeResponse("<html></html>"))
    assert ed.search_series("Serie Que No Existe") == []


def test_get_dub_summary_extracts_real_episode_count(monkeypatch):
    monkeypatch.setattr(ed.requests, "get", lambda url, params, timeout: _FakeResponse(_DETAIL_HTML))
    summary = ed.get_dub_summary(12616)
    assert "366 episodios" in summary
    assert "109 primeros" in summary
    assert "<" not in summary   # sin etiquetas HTML sueltas


def test_get_dub_summary_without_id_returns_empty():
    assert ed.get_dub_summary(0) == ""


def test_get_dub_summary_returns_empty_when_section_missing(monkeypatch):
    monkeypatch.setattr(ed.requests, "get", lambda url, params, timeout: _FakeResponse("<html>sin esa sección</html>"))
    assert ed.get_dub_summary(12616) == ""


def test_get_dub_summary_returns_empty_on_network_failure(monkeypatch):
    def _raise(url, params, timeout):
        raise ConnectionError("sin red")
    monkeypatch.setattr(ed.requests, "get", _raise)
    assert ed.get_dub_summary(12616) == ""
