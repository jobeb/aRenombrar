from core.missing_episodes import (find_missing_episodes, format_missing_summary,
                                    format_missing_ranges, looks_like_season_split,
                                    find_unknown_seasons, looks_like_absolute_numbering,
                                    remap_absolute_episodes, remove_missing_episode, _season_ranges)


def test_find_missing_episodes_finds_gaps():
    expected = {1: [1, 2, 3, 4, 5]}
    present = {(1, 1), (1, 2), (1, 4)}
    missing = find_missing_episodes(expected, present)
    assert missing == {1: [3, 5]}


def test_find_missing_episodes_no_gaps_returns_empty():
    expected = {1: [1, 2, 3]}
    present = {(1, 1), (1, 2), (1, 3)}
    assert find_missing_episodes(expected, present) == {}


def test_find_missing_episodes_season_fully_missing():
    expected = {1: [1, 2], 2: [1, 2]}
    present = {(1, 1), (1, 2)}
    missing = find_missing_episodes(expected, present)
    assert missing == {2: [1, 2]}


def test_find_missing_episodes_ignores_seasons_without_gaps():
    expected = {1: [1, 2], 2: [1, 2], 3: [1]}
    present = {(1, 1), (1, 2), (2, 1), (2, 2), (3, 1)}
    assert find_missing_episodes(expected, present) == {}


def test_season_ranges_groups_consecutive_episodes():
    assert _season_ranges(1, [1, 2, 3, 7]) == ["T1E01-T1E03", "T1E07"]


def test_season_ranges_single_episode():
    assert _season_ranges(2, [5]) == ["T2E05"]


def test_season_ranges_all_consecutive():
    assert _season_ranges(1, [1, 2, 3, 4]) == ["T1E01-T1E04"]


def test_format_missing_summary_empty_returns_empty_string():
    assert format_missing_summary("Serie X", {}) == ""


def test_format_missing_summary_formats_multiple_seasons():
    missing = {1: [5, 8], 3: [1, 2, 3]}
    summary = format_missing_summary("Serie X", missing)
    assert summary == "Serie X: T1E05, T1E08, T3E01-T3E03"


def test_format_missing_ranges_empty_returns_empty_string():
    assert format_missing_ranges({}) == ""


def test_format_missing_ranges_without_show_name():
    missing = {1: [5, 8], 3: [1, 2, 3]}
    assert format_missing_ranges(missing) == "T1E05, T1E08, T3E01-T3E03"


def test_looks_like_season_split_detects_netflix_style_split():
    # Disenchantment: TMDB cuenta 20 episodios en la temporada 1 (Parte 1 +
    # Parte 2), pero la biblioteca solo tiene los primeros 10 -- exactamente
    # la segunda mitad completa falta.
    season_episodes = list(range(1, 21))
    missing = list(range(11, 21))
    assert looks_like_season_split(season_episodes, missing) is True


def test_looks_like_season_split_false_when_gaps_are_scattered():
    # Huecos sueltos (episodios 5 y 8) no es un patron de "partes" -- son
    # de verdad episodios sueltos que faltan.
    season_episodes = list(range(1, 21))
    missing = [5, 8]
    assert looks_like_season_split(season_episodes, missing) is False


def test_looks_like_season_split_false_for_small_seasons():
    # Una temporada normal de 10 episodios a la que le faltan los ultimos 5
    # no es sospechosa de ser una "parte" de Netflix -- demasiado pequeña.
    season_episodes = list(range(1, 11))
    missing = [6, 7, 8, 9, 10]
    assert looks_like_season_split(season_episodes, missing) is False


def test_looks_like_season_split_false_when_first_half_incomplete():
    # Si tambien falta algo de la primera mitad, no es un split limpio.
    season_episodes = list(range(1, 21))
    missing = [5] + list(range(11, 21))
    assert looks_like_season_split(season_episodes, missing) is False


def test_find_unknown_seasons_detects_seasons_tmdb_does_not_know():
    # Anfibilandia/Amphibia: el servidor tiene temporadas 1, 2 y 3, pero el
    # ID de TMDB emparejado (posiblemente equivocado) solo conoce la 1.
    present = {(1, 1), (1, 2), (2, 1), (3, 1)}
    expected_seasons = [1]
    assert find_unknown_seasons(present, expected_seasons) == {2, 3}


def test_find_unknown_seasons_empty_when_all_seasons_known():
    present = {(1, 1), (1, 2), (2, 1)}
    expected_seasons = [1, 2]
    assert find_unknown_seasons(present, expected_seasons) == set()


def test_find_unknown_seasons_empty_when_present_is_empty():
    assert find_unknown_seasons(set(), [1, 2]) == set()


def test_looks_like_absolute_numbering_detects_naruto_style():
    # Naruto Shippuden: TMDB tiene varias temporadas, pero el servidor
    # reporta todo bajo "temporada 1" con episodios numerados de corrido
    # muy por encima de lo que esa temporada debería tener.
    tmdb_season_counts = {1: 10, 2: 10, 3: 10}
    present = {(1, n) for n in range(1, 26)}
    assert looks_like_absolute_numbering(tmdb_season_counts, present) is True


def test_looks_like_absolute_numbering_false_for_normal_per_season_numbering():
    tmdb_season_counts = {1: 10, 2: 10, 3: 10}
    present = {(1, n) for n in range(1, 11)} | {(2, n) for n in range(1, 11)} | {(3, n) for n in range(1, 6)}
    assert looks_like_absolute_numbering(tmdb_season_counts, present) is False


def test_looks_like_absolute_numbering_false_for_single_season_show():
    tmdb_season_counts = {1: 20}
    present = {(1, n) for n in range(1, 21)}
    assert looks_like_absolute_numbering(tmdb_season_counts, present) is False


def test_looks_like_absolute_numbering_false_when_present_empty():
    assert looks_like_absolute_numbering({1: 10, 2: 10}, set()) is False


def test_remap_absolute_episodes_converts_to_per_season():
    tmdb_season_counts = {1: 10, 2: 10, 3: 10}
    present = {(1, n) for n in range(1, 26)}   # numeracion absoluta 1..25, todo bajo "temporada 1"
    remapped = remap_absolute_episodes(tmdb_season_counts, present)
    expected = ({(1, n) for n in range(1, 11)}
                | {(2, n) for n in range(1, 11)}
                | {(3, n) for n in range(1, 6)})
    assert remapped == expected


def _missing_row(tmdb_id=1, name="Serie X", missing=None, unknown_seasons=None):
    missing = missing if missing is not None else {1: [2, 3]}
    return {"tmdb_id": tmdb_id, "name": name, "missing": missing,
            "summary": format_missing_summary(name, missing),
            "unknown_seasons": unknown_seasons or set()}


def test_remove_missing_episode_removes_just_that_episode():
    row = _missing_row(missing={1: [2, 3]})
    results = [row]
    assert remove_missing_episode(results, 1, 1, 2) is True
    assert row["missing"] == {1: [3]}
    assert row["summary"] == "Serie X: T1E03"


def test_remove_missing_episode_removes_season_when_empty():
    row = _missing_row(missing={1: [2], 2: [5]})
    results = [row]
    assert remove_missing_episode(results, 1, 1, 2) is True
    assert row["missing"] == {2: [5]}


def test_remove_missing_episode_drops_row_when_nothing_left_missing():
    row = _missing_row(missing={1: [2]})
    results = [row]
    assert remove_missing_episode(results, 1, 1, 2) is True
    assert results == []


def test_remove_missing_episode_keeps_row_if_unknown_seasons_remain():
    row = _missing_row(missing={1: [2]}, unknown_seasons={99})
    results = [row]
    assert remove_missing_episode(results, 1, 1, 2) is True
    assert results == [row]
    assert row["missing"] == {}


def test_remove_missing_episode_returns_false_when_not_actually_missing():
    row = _missing_row(missing={1: [2, 3]})
    results = [row]
    assert remove_missing_episode(results, 1, 1, 99) is False
    assert row["missing"] == {1: [2, 3]}   # sin tocar


def test_remove_missing_episode_returns_false_for_unknown_tmdb_id():
    results = [_missing_row(tmdb_id=1)]
    assert remove_missing_episode(results, 999, 1, 2) is False
