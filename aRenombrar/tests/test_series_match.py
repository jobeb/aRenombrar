from core.series_match import (
    normalize_series_name,
    series_similarity,
    best_match,
    match_names_exclusively,
)


def test_normalize_strips_accents_case_and_article():
    assert normalize_series_name("¡The Boys!") == "boys"
    assert normalize_series_name("El Chavo del Ocho") == "chavo del ocho"


def test_similarity_identical_after_normalization_is_one():
    assert series_similarity("Breaking Bad", "breaking bad") == 1.0


def test_similarity_short_name_contained_in_long_name_is_high():
    # nombre corto de carpeta vs. nombre largo que da TMDB
    assert series_similarity("Peaky Blinders", "Peaky Blinders UK") >= 0.90


def test_similarity_unrelated_titles_is_low():
    assert series_similarity("Breaking Bad", "The Wire") < 0.55


def test_best_match_picks_highest_ratio_above_threshold():
    candidates = ["The Wire", "Breaking Bad USA", "Better Call Saul"]
    name, ratio = best_match("Breaking Bad", candidates, min_ratio=0.55)
    assert name == "Breaking Bad USA"
    assert ratio >= 0.55


def test_best_match_returns_none_when_nothing_close_enough():
    candidates = ["The Wire", "The Sopranos"]
    name, ratio = best_match("Breaking Bad", candidates, min_ratio=0.55)
    assert name is None
    assert ratio == 0.0


def test_similarity_short_normalized_title_does_not_false_positive_on_substring():
    # "El 47" se normaliza a "47" al quitarle el artículo -- una cadena de
    # 2 caracteres que aparecería como subcadena de cualquier título con un
    # "47" en cualquier parte (un año, un número de carpeta...), dando un
    # falso positivo de "es la misma película" si no se protege.
    assert series_similarity("El 47", "Otra Pelicula (1947)") < 0.55
    assert series_similarity("El 47", "0047-Titulo Cualquiera") < 0.55


def test_similarity_still_boosts_meaningful_short_titles():
    # 4+ caracteres normalizados sigue considerándose una coincidencia
    # fuerte -- solo se protege contra fragmentos casi vacíos como "47".
    assert series_similarity("Coco", "Coco (2017)") >= 0.90


# ── Emparejamiento exclusivo (sin robar el mismo target dos veces) ─────────

def test_match_names_exclusively_basic_case():
    result = match_names_exclusively(["Breaking Bad"], ["Breaking Bad", "Better Call Saul"])
    assert result == {"Breaking Bad": "Breaking Bad"}


def test_match_names_exclusively_prevents_two_candidates_stealing_same_target():
    # "Breaking Bad" y "Breaking Bad El Camino" son parecidos entre sí --
    # con best_match() independiente, AMBOS podrían intentar quedarse con
    # el único título real "Breaking Bad" de Jellyfin/Plex. Aquí solo uno
    # de los dos (el de mayor parecido) debe llevarselo.
    candidates = ["Breaking Bad", "Breaking Bad El Camino"]
    targets = ["Breaking Bad"]
    result = match_names_exclusively(candidates, targets, min_ratio=0.55)
    assert len(result) == 1
    # El de mayor parecido (coincidencia exacta) se lleva el target real.
    assert result.get("Breaking Bad") == "Breaking Bad"
    assert "Breaking Bad El Camino" not in result


def test_match_names_exclusively_assigns_different_targets_to_different_candidates():
    candidates = ["Breaking Bad", "Better Call Saul"]
    targets = ["Breaking Bad", "Better Call Saul", "Otra Serie"]
    result = match_names_exclusively(candidates, targets, min_ratio=0.55)
    assert result == {"Breaking Bad": "Breaking Bad", "Better Call Saul": "Better Call Saul"}


def test_match_names_exclusively_no_match_below_min_ratio():
    result = match_names_exclusively(["Serie Completamente Distinta"], ["Breaking Bad"], min_ratio=0.55)
    assert result == {}


def test_similarity_different_years_never_match_even_if_text_is_close():
    # Mismo nombre, año distinto = pelicula distinta -- ni en modo normal
    # ni en modo estricto debe considerarse un parecido alto, por muy
    # pocos caracteres que cambien entre "1954" y "2014".
    assert series_similarity("Godzilla (1954)", "Godzilla (2014)") == 0.0
    assert series_similarity("Godzilla (1954)", "Godzilla (2014)", strict=True) == 0.0


def test_similarity_only_one_side_has_year_does_not_trigger_year_check():
    # Si solo uno de los dos trae año identificable, la comprobación de
    # "años distintos" no debe aplicarse (deja pasar al resto de la
    # lógica con normalidad).
    assert series_similarity("Coco", "Coco (2017)") >= 0.90


def test_similarity_strict_mode_blocks_unrelated_titles_sharing_a_prefix_word():
    # "Animal" y "Animal Crackers" son peliculas DISTINTAS, no la misma
    # con o sin subtitulo -- en modo estricto (usado para liberar
    # espacio, donde un falso positivo puede llevar a mostrar datos de
    # visionado equivocados) no debe dispararse el impulso de subcadena.
    assert series_similarity("Animal", "Animal Crackers", strict=True) < 0.75
    # En modo normal (por defecto, usado para reutilizar carpetas o
    # detectar duplicados de subida con otro nombre de release) SÍ sigue
    # dandose el impulso -- ese comportamiento existente no debe romperse.
    assert series_similarity("Animal", "Animal Crackers") >= 0.90


def test_similarity_strict_mode_still_allows_same_year_release_name_variants():
    # Mismo año en ambos = señal suficiente de que es el mismo título,
    # incluso en modo estricto, aunque sobren palabras de la release.
    assert series_similarity("Pelicula (2024)", "Pelicula 2024 OtraVersion WEB-DL",
                             strict=True) >= 0.90


def test_match_names_exclusively_is_strict_by_default():
    # Por defecto (a diferencia de series_similarity), match_names_exclusively
    # usa strict=True -- su único uso real es liberar espacio, donde
    # "Animal"/"Animal Crackers" no deben confundirse.
    result = match_names_exclusively(["Animal", "Animal Crackers"], ["Animal Crackers"], min_ratio=0.75)
    assert result == {"Animal Crackers": "Animal Crackers"}
    assert "Animal" not in result


def test_match_names_exclusively_more_candidates_than_targets():
    # 3 candidatos parecidos compitiendo por un unico target real -- solo
    # el de mayor parecido se lo lleva, los otros dos se quedan sin pareja.
    candidates = ["Serie X Temporada 1", "Serie X", "Serie X Extendida"]
    targets = ["Serie X"]
    result = match_names_exclusively(candidates, targets, min_ratio=0.55)
    assert len(result) == 1
    assert result.get("Serie X") == "Serie X"
