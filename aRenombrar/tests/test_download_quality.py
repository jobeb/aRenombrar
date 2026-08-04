from core.download_quality import score_download, best_result
from core.amule_client import AmuleSearchResult as R


def _r(number=1, name="Serie S01E01", size="500 MB", sources=5, complete=True):
    return R(number=number, name=name, size_human=size, sources=sources,
             complete=complete)


def test_best_result_empty():
    assert best_result([]) is None


def test_best_result_none_below_minimum():
    """Si ningún resultado alcanza el umbral mínimo (nada que coincida de
    verdad con la búsqueda), no se destaca ninguno."""
    results = [
        _r(1, "archivo-aleatorio.bin", size="5 MB", sources=0, complete=False),
        _r(2, "xd", size="10 KB", sources=1, complete=False),
    ]
    assert best_result(results) is None


def test_best_result_picks_higher_quality_and_language():
    results = [
        _r(1, "Serie S01E01 720p", sources=3, complete=False),
        _r(2, "Serie S01E01 1080p spa HDTV x264", sources=7, complete=True),
        _r(3, "Serie S01E01 SD", sources=1, complete=False),
    ]
    assert best_result(results).number == 2


def test_best_result_returns_identity_of_element():
    results = [_r(1, "a 720p"), _r(2, "b 1080p")]
    assert best_result(results) is results[1]


def test_spanish_lang_gives_points():
    en = score_download(_r(1, "Serie S01E01 720p HDTV x264"))
    es = score_download(_r(1, "Serie S01E01 720p HDTV x264 Español"))
    assert es > en


def test_resolution_order():
    la = score_download(_r(1, "Serie 720p"))
    hd = score_download(_r(1, "Serie 1080p"))
    uhd = score_download(_r(1, "Serie 2160p 4K"))
    assert uhd > hd > la


def test_extension_preference():
    mkv = score_download(_r(1, "Serie S01E01.mkv"))
    avi = score_download(_r(1, "Serie S01E01.avi"))
    assert mkv > avi


def test_more_complete_sources_scores_higher():
    few = score_download(_r(1, "Serie S01E01 720p", sources=1, complete=False))
    many = score_download(_r(1, "Serie S01E01 720p", sources=12, complete=True))
    assert many > few


def test_size_wildly_wrong_penalized():
    tiny = score_download(_r(1, "Serie S01E01 1080p", size="5 KB"))
    sane = score_download(_r(1, "Serie S01E01 1080p", size="1200 MB"))
    assert sane > tiny


def test_best_result_prefers_matching_season_episode():
    """Buscando "2x04" NO debe elegirse un "22x04" de otra temporada solo por
    tener mejor calidad (real: el mejor candidato salía "Los Simpsons 22x04"
    en vez del 2x04 pedido)."""
    results = [
        _r(1, "Los Simpsons 2x04 Episode 720p", sources=3, complete=True),
        _r(2, "Los Simpsons 22x04 1080p spa HDTV x264", sources=9, complete=True),
    ]
    assert best_result(results, "Los Simpsons 2x04").number == 1


def test_season_episode_mismatch_outweighs_quality():
    """Con un capítulo de otra temporada que puntúa mucho mejor en calidad,
    la no-coincidencia de temporada/episodio debe mandar sobre la calidad."""
    wrong = score_download(_r(1, "Serie 22x04 2160p 4K spa x265 mkv", sources=12,
                              complete=True), "Serie 2x04")
    right = score_download(_r(2, "Serie 2x04 720p", sources=2, complete=False),
                           "Serie 2x04")
    assert right > wrong


def test_season_match_without_query_still_ranks_by_quality():
    """Sin consulta (query=""), la puntuación sigue siendo la clásica por
    calidad: todo el aparato de coincidencia de temporada queda inactivo."""
    low = score_download(_r(1, "Serie 720p"))
    high = score_download(_r(2, "Serie 1080p"))
    assert high > low