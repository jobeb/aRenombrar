from core.series_match import (
    normalize_series_name,
    series_similarity,
    best_match,
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
