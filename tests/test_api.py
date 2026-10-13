import json
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import db, main

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SEARCHES_DIR", str(tmp_path / "searches"))
    monkeypatch.setenv("GRAPH_DB", str(tmp_path / "nao-existe.db"))
    monkeypatch.setenv("OPENALEX_MAILTO", "test@example.org")
    app = main.create_app()
    with TestClient(app) as c:
        yield c


def open_search(client, seed_id="W1", title="Seminal"):
    """Cria e abre uma busca vazia, sem passar pelo crawl."""
    store = client.app.state.searches
    sid = store.create({"id": seed_id, "title": title, "year": 2015})
    return sid, store.conn(sid)


def seed_graph(client):
    """Popula o banco do app diretamente, sem passar pela API."""
    _, conn = open_search(client)
    db.upsert_paper(
        conn,
        {
            "id": "W1",
            "doi": "10.1/seed",
            "title": "Seminal",
            "year": 2015,
            "cited_by_count": 900,
            "abstract": "O abstract do seminal.",
            "venue": "Nature",
            "landing_url": "https://example.org/seed",
            "oa_url": None,
            "authors": [{"name": "A. Autor", "affiliation": "Univ X"}],
            "topics": ["Deep Learning"],
            "depth": 0,
        },
    )
    db.upsert_paper(
        conn,
        {
            "id": "W2",
            "doi": None,
            "title": "Citante sem abstract",
            "year": 2021,
            "cited_by_count": 10,
            "abstract": "",
            "venue": None,
            "landing_url": None,
            "oa_url": None,
            "authors": [],
            "topics": ["Graph Theory"],
            "depth": 1,
        },
    )
    db.add_edge(conn, citing_id="W2", cited_id="W1")
    db.record_expansion(conn, "W1", top_n=20, min_year=None, total_citers=1240)
    return conn


# -------------------------------------------------------------------- /resolve


@respx.mock
def test_resolve_an_id_returns_the_paper(client):
    respx.get(url__startswith="https://api.openalex.org/works/W2741809807").mock(
        return_value=httpx.Response(200, json=fixture("work_seed.json"))
    )

    r = client.post("/api/resolve", json={"input": "W2741809807"})

    assert r.status_code == 200
    assert r.json()["paper"]["id"] == "W2741809807"
    assert r.json()["candidates"] == []


@respx.mock
def test_resolve_free_text_returns_candidates(client):
    respx.get(url__startswith="https://api.openalex.org/works?").mock(
        return_value=httpx.Response(200, json=fixture("search_results.json"))
    )

    r = client.post("/api/resolve", json={"input": "attention is all you need"})

    assert r.status_code == 200
    assert r.json()["paper"] is None
    assert len(r.json()["candidates"]) == 5


@respx.mock
def test_resolve_an_unknown_paper_is_a_404_with_a_readable_message(client):
    respx.get(url__startswith="https://api.openalex.org/works/").mock(
        return_value=httpx.Response(404, json={})
    )

    r = client.post("/api/resolve", json={"input": "10.9999/nope"})

    assert r.status_code == 404
    assert "detail" in r.json()


def test_resolve_rejects_empty_input(client):
    assert client.post("/api/resolve", json={"input": "   "}).status_code == 422


@respx.mock
def test_an_openalex_outage_becomes_a_502_not_a_crash(client):
    respx.get(url__startswith="https://api.openalex.org/works/").mock(
        return_value=httpx.Response(500, json={})
    )

    r = client.post("/api/resolve", json={"input": "W123"})

    assert r.status_code == 502


# ---------------------------------------------------------------------- /crawl


@respx.mock
def test_crawl_streams_progress_and_persists_the_graph(client):
    respx.get(url__startswith="https://api.openalex.org/works/W2741809807").mock(
        return_value=httpx.Response(200, json=fixture("work_seed.json"))
    )
    respx.get(url__startswith="https://api.openalex.org/works?").mock(
        return_value=httpx.Response(200, json=fixture("citers_page.json"))
    )

    with client.stream(
        "POST", "/api/crawl", json={"seed_id": "W2741809807", "depth": 1, "top_n": 5}
    ) as r:
        assert r.status_code == 200
        events = [
            json.loads(line[len("data: "):])
            for line in r.iter_lines()
            if line.startswith("data: ")
        ]

    assert events[-1]["done"] is True
    assert events[-1]["nodes"] == 6  # seminal + 5 citantes

    graph = client.get("/api/graph").json()
    assert len(graph["nodes"]) == 6
    assert {"citing": "W3137875885", "cited": "W2741809807"} in graph["edges"]


@respx.mock
def test_crawl_reports_truncation_when_it_hits_the_node_budget(client):
    respx.get(url__startswith="https://api.openalex.org/works/W2741809807").mock(
        return_value=httpx.Response(200, json=fixture("work_seed.json"))
    )
    respx.get(url__startswith="https://api.openalex.org/works?").mock(
        return_value=httpx.Response(200, json=fixture("citers_page.json"))
    )

    with client.stream(
        "POST",
        "/api/crawl",
        json={"seed_id": "W2741809807", "depth": 3, "top_n": 5, "max_nodes": 3},
    ) as r:
        events = [
            json.loads(line[len("data: "):])
            for line in r.iter_lines()
            if line.startswith("data: ")
        ]

    assert events[-1]["truncated"] is True


def test_crawl_validates_its_parameters(client):
    bad = [
        {"seed_id": "W1", "depth": 0},
        {"seed_id": "W1", "depth": 99},
        {"seed_id": "W1", "top_n": 0},
        {"seed_id": "W1", "max_nodes": 100000},
    ]
    for payload in bad:
        assert client.post("/api/crawl", json=payload).status_code == 422, payload


# ---------------------------------------------------------------------- /graph


def test_graph_is_empty_before_any_crawl(client):
    assert client.get("/api/graph").json() == {"nodes": [], "edges": []}


def test_graph_exposes_what_the_canvas_needs(client):
    seed_graph(client)

    graph = client.get("/api/graph").json()

    seed = next(n for n in graph["nodes"] if n["id"] == "W1")
    assert seed["frontier"] is False
    assert seed["total_citers"] == 1240
    assert seed["mark"] == "unread"
    assert seed["cited_by_count"] == 900
    citer = next(n for n in graph["nodes"] if n["id"] == "W2")
    assert citer["frontier"] is True
    assert citer["depth"] == 1


# ---------------------------------------------------------------------- /paper


def test_paper_detail_carries_authors_links_abstract_and_topics(client):
    seed_graph(client)

    body = client.get("/api/paper/W1").json()

    assert body["authors"] == [{"name": "A. Autor", "affiliation": "Univ X"}]
    assert body["abstract"] == "O abstract do seminal."
    assert body["topics"] == ["Deep Learning"]
    assert body["landing_url"] == "https://example.org/seed"
    assert body["openalex_url"] == "https://openalex.org/W1"
    assert body["doi_url"] == "https://doi.org/10.1/seed"


def test_paper_detail_404s_for_a_node_not_in_the_graph(client):
    open_search(client)
    assert client.get("/api/paper/W404").status_code == 404


@respx.mock
def test_a_missing_abstract_is_fetched_from_semantic_scholar_on_first_open(client):
    conn = seed_graph(client)
    db.upsert_paper(conn, {**db.get_paper(conn, "W2"), "doi": "10.1/citer"})
    route = respx.get(url__startswith="https://api.semanticscholar.org/").mock(
        return_value=httpx.Response(200, json={"abstract": "Resgatado do S2."})
    )

    assert client.get("/api/paper/W2").json()["abstract"] == "Resgatado do S2."

    # Segunda abertura sai do cache: nenhuma requisicao nova.
    assert client.get("/api/paper/W2").json()["abstract"] == "Resgatado do S2."
    assert route.call_count == 1


def test_a_paper_without_a_doi_reports_the_abstract_as_unavailable(client):
    seed_graph(client)  # W2 nao tem DOI

    body = client.get("/api/paper/W2").json()

    assert body["abstract"] == ""
    assert body["abstract_available"] is False


# ----------------------------------------------------------------------- /mark


def test_marking_a_paper_persists_and_shows_up_in_the_graph(client):
    seed_graph(client)

    r = client.put("/api/paper/W2/mark", json={"status": "interested", "note": "tema?"})

    assert r.status_code == 200
    node = next(n for n in client.get("/api/graph").json()["nodes"] if n["id"] == "W2")
    assert node["mark"] == "interested"
    assert client.get("/api/paper/W2").json()["note"] == "tema?"


def test_an_invalid_mark_is_rejected(client):
    seed_graph(client)
    assert client.put("/api/paper/W2/mark", json={"status": "amei"}).status_code == 422


def test_marking_a_paper_outside_the_graph_is_a_404(client):
    open_search(client)
    assert client.put("/api/paper/W404/mark", json={"status": "read"}).status_code == 404


# --------------------------------------------------------------------- /expand


@respx.mock
def test_expand_grows_a_frontier_node(client):
    seed_graph(client)
    respx.get(url__startswith="https://api.openalex.org/works?").mock(
        return_value=httpx.Response(200, json=fixture("citers_page.json"))
    )

    r = client.post("/api/expand", json={"paper_id": "W2", "top_n": 5})

    assert r.status_code == 200
    assert r.json()["new_nodes"] == 5
    assert r.json()["total_citers"] == 1240
    node = next(n for n in client.get("/api/graph").json()["nodes"] if n["id"] == "W2")
    assert node["frontier"] is False


def test_expanding_a_node_outside_the_graph_is_a_404(client):
    open_search(client)
    assert client.post("/api/expand", json={"paper_id": "W404"}).status_code == 404


# -------------------------------------------------------------------- buscas


def crawl(client, seed_id="W2741809807"):
    with client.stream(
        "POST", "/api/crawl", json={"seed_id": seed_id, "depth": 1, "top_n": 5}
    ) as r:
        assert r.status_code == 200
        sid = r.headers["X-Search-Id"]
        list(r.iter_lines())
    return sid


def mock_openalex():
    respx.get(url__startswith="https://api.openalex.org/works/W2741809807").mock(
        return_value=httpx.Response(200, json=fixture("work_seed.json"))
    )
    respx.get(url__startswith="https://api.openalex.org/works?").mock(
        return_value=httpx.Response(200, json=fixture("citers_page.json"))
    )


def test_there_are_no_searches_at_first(client):
    assert client.get("/api/searches").json() == {"active": None, "searches": []}


@respx.mock
def test_each_crawl_becomes_a_new_search_and_the_list_shows_it(client):
    mock_openalex()

    first = crawl(client)
    second = crawl(client)

    listing = client.get("/api/searches").json()
    assert first != second
    assert listing["active"] == second
    assert [s["id"] for s in listing["searches"]] == [second, first]  # mais nova primeiro
    newest = listing["searches"][0]
    assert newest["seed_id"] == "W2741809807"
    assert newest["seed_title"]
    assert newest["nodes"] == 6
    assert newest["params"]["top_n"] == 5
    assert newest["crawling"] is False


@respx.mock
def test_searches_are_isolated_graphs(client):
    mock_openalex()
    old = crawl(client)
    client.put("/api/paper/W3137875885/mark", json={"status": "interested"})

    open_search(client, seed_id="W9", title="Outro seminal")

    assert client.get("/api/graph").json() == {"nodes": [], "edges": []}
    client.put("/api/searches/active", json={"id": old})
    graph = client.get("/api/graph").json()
    assert len(graph["nodes"]) == 6
    assert next(n for n in graph["nodes"] if n["id"] == "W3137875885")["mark"] == "interested"
    assert client.get("/api/searches").json()["searches"][1]["interested"] == 1


def test_closing_the_active_search_empties_the_canvas_without_deleting_it(client):
    seed_graph(client)

    r = client.put("/api/searches/active", json={"id": None})

    assert r.json()["active"] is None
    assert len(r.json()["searches"]) == 1
    assert client.get("/api/graph").json() == {"nodes": [], "edges": []}
    assert client.get("/api/paper/W1").status_code == 409


def test_opening_an_unknown_search_is_a_404(client):
    assert client.put("/api/searches/active", json={"id": "nao-existe"}).status_code == 404
    assert client.put("/api/searches/active", json={"id": "../../etc"}).status_code == 404


def test_the_open_search_survives_a_restart(tmp_path, monkeypatch, client):
    sid, _ = open_search(client)

    with TestClient(main.create_app()) as again:
        assert again.get("/api/searches").json()["active"] == sid


def test_deleting_a_search_removes_its_file(client, tmp_path):
    sid, _ = open_search(client)
    keep, _ = open_search(client, seed_id="W2", title="Fica")

    r = client.delete(f"/api/searches/{sid}")

    assert r.status_code == 200
    assert [s["id"] for s in r.json()["searches"]] == [keep]
    assert not (tmp_path / "searches" / f"{sid}.db").exists()
    assert client.delete(f"/api/searches/{sid}").status_code == 404


def test_deleting_the_active_search_leaves_none_open(client):
    sid, _ = open_search(client)

    assert client.delete(f"/api/searches/{sid}").json()["active"] is None


def test_a_search_being_crawled_cannot_be_deleted(client):
    sid, _ = open_search(client)
    client.app.state.searches.busy.add(sid)

    assert client.delete(f"/api/searches/{sid}").status_code == 409


def test_the_old_single_graph_db_is_imported_once_as_a_search(tmp_path, monkeypatch):
    legacy = tmp_path / "graph.db"
    conn = db.connect(legacy)
    db.upsert_paper(conn, {"id": "W1", "title": "Seminal antigo", "year": 2016, "depth": 0})
    db.upsert_paper(conn, {"id": "W2", "title": "Citante", "year": 2020, "depth": 1})
    db.set_mark(conn, "W2", "interested")
    conn.close()
    monkeypatch.setenv("SEARCHES_DIR", str(tmp_path / "searches"))
    monkeypatch.setenv("GRAPH_DB", str(legacy))

    with TestClient(main.create_app()) as c:
        listing = c.get("/api/searches").json()
        (only,) = listing["searches"]
        assert listing["active"] == only["id"]
        assert only["seed_title"] == "Seminal antigo"
        assert only["nodes"] == 2 and only["interested"] == 1
        c.delete(f"/api/searches/{only['id']}")

    assert legacy.exists()  # copiado, nunca movido
    with TestClient(main.create_app()) as c:
        assert c.get("/api/searches").json()["searches"] == []  # nao reimporta


# ------------------------------------------------------------------- interface


def test_the_root_url_serves_the_page(client):
    r = client.get("/")

    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "cytoscape" in r.text.lower()


def test_an_old_graph_with_several_seeds_is_labelled_as_mixed(tmp_path, monkeypatch):
    legacy = tmp_path / "graph.db"
    conn = db.connect(legacy)
    db.upsert_paper(conn, {"id": "W1", "title": "Muito citado", "cited_by_count": 900, "depth": 0})
    db.upsert_paper(conn, {"id": "W2", "title": "Outro seminal", "cited_by_count": 10, "depth": 0})
    conn.close()
    monkeypatch.setenv("SEARCHES_DIR", str(tmp_path / "searches"))
    monkeypatch.setenv("GRAPH_DB", str(legacy))

    with TestClient(main.create_app()) as c:
        (only,) = c.get("/api/searches").json()["searches"]

    assert only["seed_title"] == "Grafo antigo: Muito citado + 1 outro"
