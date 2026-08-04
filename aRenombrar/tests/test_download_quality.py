from core.download_quality import score_download, best_result, is_adult_content
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


# ---- Porno / contenido adulto ----

def test_is_adult_content_detects_porn_markers():
    assert is_adult_content("Serie S01E01 XXX 1080p.mkv")
    assert is_adult_content("Serie S01E01 Porn 720p.avi")
    assert is_adult_content("Hentai Collection 4K.mkv")
    assert is_adult_content("Serie XXX Parody 1080p.mp4")
    assert not is_adult_content("Serie S01E01 1080p.mkv")
    assert not is_adult_content("Sex Education S01E01 1080p.mkv")  # título legítimo
    assert not is_adult_content("Adult Swim 2x04 720p.mkv")


def test_best_result_never_picks_porn_even_with_best_quality():
    """Un XXX 4K con muchas fuentes NO debe ganar jamás a un capítulo normal."""
    results = [
        _r(1, "Los Simpsons 2x04 XXX 2160p 4K spa", sources=50, complete=True),
        _r(2, "Los Simpsons 2x04 720p", sources=3, complete=False),
    ]
    best = best_result(results, "Los Simpsons 2x04")
    assert best is not None
    assert best.number == 2


def test_best_result_returns_none_when_only_porn():
    results = [
        _r(1, "XXX Porn 1080p.mkv", sources=99, complete=True),
        _r(2, "Hentai 4K.mkv", sources=10, complete=True),
    ]
    assert best_result(results, "Algo") is None


def test_porn_never_scores_above_threshold():
    assert score_download(_r(1, "XXX 4K 100 fuentes", sources=99, complete=True)) < 15.0
    assert score_download(_r(1, "Serie 720p", sources=3, complete=True)) >= 15.0


# ---- Precisión extra ----

def test_best_result_rejects_other_series_with_same_episode():
    """Buscando "Los Simpsons 2x04", un "Padre de Familia 2x04 1080p" (otra
    serie, misma numeración) NO debe ganar al capítulo de la serie pedida."""
    results = [
        _r(1, "Padre de Familia 2x04 1080p spa HDTV", sources=9, complete=True),
        _r(2, "Los Simpsons 2x04 720p", sources=3, complete=False),
    ]
    best = best_result(results, "Los Simpsons 2x04")
    assert best.number == 2


def test_best_result_prefers_real_episode_over_sample():
    """Un "sample" / "trailer" del capítulo NO debe ganar al capítulo entero."""
    results = [
        _r(1, "Serie S01E01 sample 1080p.mkv", sources=5, complete=True),
        _r(2, "Serie S01E01 720p.mkv", sources=3, complete=True),
    ]
    best = best_result(results, "Serie S01E01")
    assert best.number == 2


def test_non_video_extension_never_wins():
    """Un .emulecollection / .srt no es un capítulo descargable."""
    results = [
        _r(1, "Serie S01E01 1080p.emulecollection", sources=9, complete=True),
        _r(2, "Serie S01E01 720p.mkv", sources=3, complete=True),
    ]
    best = best_result(results, "Serie S01E01")
    assert best.number == 2


def test_cam_rip_penalized_over_web():
    """Una captura de cine (cam/screener) no debe ganar a una copia web."""
    low = score_download(_r(1, "Serie S01E01 CAM 1080p", sources=9, complete=True))
    good = score_download(_r(1, "Serie S01E01 WEB-DL 1080p", sources=3, complete=False))
    assert good > low


def test_latino_audio_counts_as_spanish():
    plain = score_download(_r(1, "Serie S01E01 1080p"))
    lat = score_download(_r(1, "Serie S01E01 1080p latino"))
    assert lat > plain