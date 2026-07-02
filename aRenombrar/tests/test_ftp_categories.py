from core.ftp_categories import (
    new_category_id,
    choose_category,
    split_template,
    build_wildcard_category,
)


def test_new_category_id_are_unique_and_prefixed():
    ids = {new_category_id() for _ in range(20)}
    assert len(ids) == 20
    assert all(i.startswith("cat_") for i in ids)


def test_choose_category_specific_match_wins_over_order():
    categories = [
        {"name": "Series",       "genre_ids": []},
        {"name": "Documentales", "genre_ids": [99]},
    ]
    chosen = choose_category([99], categories)
    assert chosen["name"] == "Documentales"


def test_choose_category_first_specific_match_wins_when_two_overlap():
    categories = [
        {"name": "Infantil",  "genre_ids": [16, 10751]},
        {"name": "Animacion", "genre_ids": [16]},
    ]
    # el genero 16 esta en ambas -- el orden de la lista desempata
    chosen = choose_category([16], categories)
    assert chosen["name"] == "Infantil"


def test_choose_category_falls_back_to_wildcard():
    categories = [
        {"name": "Documentales", "genre_ids": [99]},
        {"name": "Series",       "genre_ids": []},
    ]
    chosen = choose_category([18], categories)  # 18 = Drama, no matchea ninguna especifica
    assert chosen["name"] == "Series"


def test_choose_category_empty_genre_ids_falls_back_to_wildcard():
    categories = [
        {"name": "Documentales", "genre_ids": [99]},
        {"name": "Series",       "genre_ids": []},
    ]
    chosen = choose_category([], categories)
    assert chosen["name"] == "Series"


def test_choose_category_returns_none_without_categories():
    assert choose_category([16], []) is None


def test_choose_category_returns_none_without_wildcard_and_no_match():
    categories = [{"name": "Documentales", "genre_ids": [99]}]
    assert choose_category([18], categories) is None


def test_split_template_with_serie_placeholder():
    root, rel = split_template("/datos2/series/{serie}/Temporada {temporada:02d}/")
    assert root == "/datos2/series"
    assert rel == "{serie}/Temporada {temporada:02d}/"


def test_split_template_without_serie_placeholder_is_all_root():
    root, rel = split_template("/peliculas/varias")
    assert root == "/peliculas/varias"
    assert rel == ""


def test_split_template_empty_string():
    assert split_template("") == ("", "")
    assert split_template(None) == ("", "")


def test_build_wildcard_category_shape():
    cat = build_wildcard_category("Series", "/datos2/series/{serie}/Temporada {temporada:02d}/")
    assert cat["name"] == "Series"
    assert cat["genre_ids"] == []
    assert cat["root"] == "/datos2/series"
    assert cat["template"] == "{serie}/Temporada {temporada:02d}/"
    assert cat["id"].startswith("cat_")


def test_build_wildcard_category_blank_template_has_no_root():
    cat = build_wildcard_category("Películas", "")
    assert cat["root"] == ""
