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
    monkeypatch.setenv("GRAPH_DB", str(tmp_path / "api.db"))
    monkeypatch.setenv("OPENALEX_MAILTO", "test@example.org")
    app = main.create_app()
    with TestClient(app) as c:
        yield c


def seed_graph(client, name="busca 1"):
    """Popula o banco do app diretamente, sem passar pela API. Devolve o id da
    investigacao criada."""
    conn = client.app.state.conn
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
            "field": "Computer Science",
            "subfield": "Artificial Intelligence",
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
            "field": "Mathematics",
            "subfield": "Discrete Mathematics",
        },
    )
    inv = db.create_investigation(conn, name=name, seed_id="W1", top_n=20)
    db.add_member(conn, inv, "W1", depth=0)
    db.add_member(conn, inv, "W2", depth=1)
    db.add_edge(conn, citing_id="W2", cited_id="W1")
    db.record_expansion(conn, inv, "W1", top_n=20, min_year=None, total_citers=1240)
    return inv


# ------------------------------------------------------------- /investigations


def test_there_are_no_investigations_before_the_first_search(client):
    assert client.get("/api/investigations").json() == []


def test_an_investigation_is_listed_with_its_seed_and_size(client):
    seed_graph(client, name="aprendizado federado")

    body = client.get("/api/investigations").json()

    assert len(body) == 1
    assert body[0]["name"] == "aprendizado federado"
    assert body[0]["seed_title"] == "Seminal"
    assert body[0]["nodes"] == 2


def test_an_investigation_can_be_renamed(client):
    inv = seed_graph(client)

    r = client.patch(f"/api/investigations/{inv}", json={"name": "nome melhor"})

    assert r.status_code == 200
    assert client.get("/api/investigations").json()[0]["name"] == "nome melhor"


def test_renaming_rejects_an_empty_name(client):
    inv = seed_graph(client)
    assert client.patch(f"/api/investigations/{inv}", json={"name": "   "}).status_code == 422


def test_renaming_an_unknown_investigation_is_a_404(client):
    assert client.patch("/api/investigations/999", json={"name": "x"}).status_code == 404


def test_deleting_an_investigation_removes_it_from_the_list(client):
    inv = seed_graph(client)

    assert client.delete(f"/api/investigations/{inv}").status_code == 200
    assert client.get("/api/investigations").json() == []


def test_deleting_an_investigation_keeps_the_paper_cache(client):
    """Os artigos custaram requisicoes: apagar a investigacao nao pode joga-los fora."""
    inv = seed_graph(client)
    client.delete(f"/api/investigations/{inv}")

    conn = client.app.state.conn
    assert db.get_paper(conn, "W1") is not None
    assert db.get_paper(conn, "W2") is not None


def test_deleting_an_unknown_investigation_is_a_404(client):
    assert client.delete("/api/investigations/999").status_code == 404


def test_two_investigations_are_listed_newest_first(client):
    a = seed_graph(client, name="primeira")
    conn = client.app.state.conn
    b = db.create_investigation(conn, name="segunda", seed_id="W1")

    assert [i["id"] for i in client.get("/api/investigations").json()] == [b, a]


# ---------------------------------------------------------------------- /graph


def test_graph_is_empty_before_any_crawl(client):
    assert client.get("/api/graph").json() == {"nodes": [], "edges": []}


def test_graph_without_a_parameter_serves_the_most_recent_investigation(client):
    """Compatibilidade: sem o parametro, a rota devolve a investigacao mais recente,
    de modo que a interface antiga continua funcionando."""
    seed_graph(client, name="antiga")
    conn = client.app.state.conn
    recent = db.create_investigation(conn, name="recente", seed_id="W2")
    db.add_member(conn, recent, "W2", depth=0)

    body = client.get("/api/graph").json()

    assert [n["id"] for n in body["nodes"]] == ["W2"]
    assert body == client.get(f"/api/graph?investigation={recent}").json()


def test_graph_can_be_asked_for_a_specific_investigation(client):
    inv = seed_graph(client)

    body = client.get(f"/api/graph?investigation={inv}").json()

    assert {n["id"] for n in body["nodes"]} == {"W1", "W2"}
    assert body["edges"] == [{"citing": "W2", "cited": "W1"}]


def test_graph_exposes_what_the_canvas_needs(client):
    inv = seed_graph(client)

    graph = client.get(f"/api/graph?investigation={inv}").json()

    seed = next(n for n in graph["nodes"] if n["id"] == "W1")
    assert seed["frontier"] is False
    assert seed["total_citers"] == 1240
    assert seed["mark"] == "unread"
    assert seed["cited_by_count"] == 900
    assert seed["subfield"] == "Artificial Intelligence"
    citer = next(n for n in graph["nodes"] if n["id"] == "W2")
    assert citer["frontier"] is True
    assert citer["depth"] == 1


def test_graph_of_an_unknown_investigation_is_a_404(client):
    assert client.get("/api/graph?investigation=999").status_code == 404


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

    assert client.post("/api/resolve", json={"input": "W123"}).status_code == 502


# ---------------------------------------------------------------------- /crawl


def crawl_events(client, payload):
    with client.stream("POST", "/api/crawl", json=payload) as r:
        assert r.status_code == 200, r.read()
        return [
            json.loads(line[len("data: "):])
            for line in r.iter_lines()
            if line.startswith("data: ")
        ]


def mock_crawl_api():
    respx.get(url__startswith="https://api.openalex.org/works/W2741809807").mock(
        return_value=httpx.Response(200, json=fixture("work_seed.json"))
    )
    respx.get(url__startswith="https://api.openalex.org/works?").mock(
        return_value=httpx.Response(200, json=fixture("citers_page.json"))
    )


@respx.mock
def test_a_crawl_without_an_investigation_creates_one(client):
    mock_crawl_api()

    events = crawl_events(client, {"seed_id": "W2741809807", "depth": 1, "top_n": 5})

    inv = events[-1]["investigation_id"]
    assert inv, "o evento final deveria dizer em qual investigacao o grafo foi montado"
    listed = client.get("/api/investigations").json()
    assert len(listed) == 1
    assert listed[0]["id"] == inv
    # O nome sai do titulo do seminal, para a usuaria reconhecer a busca depois.
    assert "state of OA" in listed[0]["name"]


@respx.mock
def test_crawl_streams_progress_and_persists_the_graph(client):
    mock_crawl_api()

    events = crawl_events(client, {"seed_id": "W2741809807", "depth": 1, "top_n": 5})

    assert events[-1]["done"] is True
    assert events[-1]["nodes"] == 6  # seminal + 5 citantes

    graph = client.get(f"/api/graph?investigation={events[-1]['investigation_id']}").json()
    assert len(graph["nodes"]) == 6
    assert {"citing": "W3137875885", "cited": "W2741809807"} in graph["edges"]


@respx.mock
def test_a_second_crawl_of_the_same_seed_builds_a_separate_investigation(client):
    """E o que sustenta comparar "sem filtro" com "desde 2024" lado a lado."""
    mock_crawl_api()

    first = crawl_events(client, {"seed_id": "W2741809807", "depth": 1, "top_n": 5})[-1]
    second = crawl_events(
        client, {"seed_id": "W2741809807", "depth": 1, "top_n": 5, "min_year": 2024}
    )[-1]

    assert first["investigation_id"] != second["investigation_id"]
    assert len(client.get("/api/investigations").json()) == 2


@respx.mock
def test_a_crawl_into_an_existing_investigation_does_not_create_another(client):
    mock_crawl_api()
    inv = seed_graph(client)

    events = crawl_events(
        client, {"seed_id": "W2741809807", "depth": 1, "top_n": 5, "investigation_id": inv}
    )

    assert events[-1]["investigation_id"] == inv
    assert len(client.get("/api/investigations").json()) == 1


@respx.mock
def test_crawling_into_an_unknown_investigation_is_a_404(client):
    mock_crawl_api()
    r = client.post("/api/crawl", json={"seed_id": "W2741809807", "investigation_id": 999})
    assert r.status_code == 404


@respx.mock
def test_crawl_reports_truncation_when_it_hits_the_node_budget(client):
    mock_crawl_api()

    events = crawl_events(
        client, {"seed_id": "W2741809807", "depth": 3, "top_n": 5, "max_nodes": 3}
    )

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


# ---------------------------------------------------------------------- /paper


def test_paper_detail_carries_authors_links_abstract_and_topics(client):
    inv = seed_graph(client)

    body = client.get(f"/api/paper/W1?investigation={inv}").json()

    assert body["authors"] == [{"name": "A. Autor", "affiliation": "Univ X"}]
    assert body["abstract"] == "O abstract do seminal."
    assert body["topics"] == ["Deep Learning"]
    assert body["landing_url"] == "https://example.org/seed"
    assert body["openalex_url"] == "https://openalex.org/W1"
    assert body["doi_url"] == "https://doi.org/10.1/seed"
    assert body["depth"] == 0
    assert body["total_citers"] == 1240


def test_paper_detail_404s_for_a_node_not_in_the_graph(client):
    seed_graph(client)
    assert client.get("/api/paper/W404").status_code == 404


def test_paper_detail_404s_for_a_paper_outside_the_asked_investigation(client):
    """Esta no cache global, mas nao neste recorte."""
    inv = seed_graph(client)
    conn = client.app.state.conn
    other = db.create_investigation(conn, name="outra", seed_id="W1")
    db.add_member(conn, other, "W1", depth=0)

    assert client.get(f"/api/paper/W2?investigation={other}").status_code == 404
    assert client.get(f"/api/paper/W2?investigation={inv}").status_code == 200


@respx.mock
def test_a_missing_abstract_is_fetched_from_semantic_scholar_on_first_open(client):
    inv = seed_graph(client)
    conn = client.app.state.conn
    db.upsert_paper(conn, {**db.get_paper(conn, "W2"), "doi": "10.1/citer"})
    route = respx.get(url__startswith="https://api.semanticscholar.org/").mock(
        return_value=httpx.Response(200, json={"abstract": "Resgatado do S2."})
    )

    assert client.get(f"/api/paper/W2?investigation={inv}").json()["abstract"] == "Resgatado do S2."

    # Segunda abertura sai do cache: nenhuma requisicao nova.
    assert client.get(f"/api/paper/W2?investigation={inv}").json()["abstract"] == "Resgatado do S2."
    assert route.call_count == 1


def test_a_paper_without_a_doi_reports_the_abstract_as_unavailable(client):
    inv = seed_graph(client)  # W2 nao tem DOI

    body = client.get(f"/api/paper/W2?investigation={inv}").json()

    assert body["abstract"] == ""
    assert body["abstract_available"] is False


# ----------------------------------------------------------------------- /mark


def test_marking_a_paper_persists_and_shows_up_in_the_graph(client):
    inv = seed_graph(client)

    r = client.put("/api/paper/W2/mark", json={"status": "interested", "note": "tema?"})

    assert r.status_code == 200
    node = next(
        n for n in client.get(f"/api/graph?investigation={inv}").json()["nodes"] if n["id"] == "W2"
    )
    assert node["mark"] == "interested"
    assert client.get(f"/api/paper/W2?investigation={inv}").json()["note"] == "tema?"


def test_a_mark_is_visible_from_every_investigation(client):
    """Marcacao segue o artigo: reencontra-lo marcado em outra busca e o sinal de
    sobreposicao entre as duas."""
    seed_graph(client)
    conn = client.app.state.conn
    other = db.create_investigation(conn, name="outra", seed_id="W2")
    db.add_member(conn, other, "W2", depth=0)

    client.put("/api/paper/W2/mark", json={"status": "interested"})

    assert client.get(f"/api/graph?investigation={other}").json()["nodes"][0]["mark"] == "interested"


def test_an_invalid_mark_is_rejected(client):
    seed_graph(client)
    assert client.put("/api/paper/W2/mark", json={"status": "amei"}).status_code == 422


def test_marking_a_paper_outside_the_graph_is_a_404(client):
    assert client.put("/api/paper/W404/mark", json={"status": "read"}).status_code == 404


# --------------------------------------------------------------------- /expand


@respx.mock
def test_expand_grows_a_frontier_node(client):
    inv = seed_graph(client)
    respx.get(url__startswith="https://api.openalex.org/works?").mock(
        return_value=httpx.Response(200, json=fixture("citers_page.json"))
    )

    r = client.post("/api/expand", json={"paper_id": "W2", "top_n": 5, "investigation_id": inv})

    assert r.status_code == 200
    assert r.json()["new_nodes"] == 5
    assert r.json()["total_citers"] == 1240
    node = next(
        n for n in client.get(f"/api/graph?investigation={inv}").json()["nodes"] if n["id"] == "W2"
    )
    assert node["frontier"] is False


@respx.mock
def test_expand_without_an_investigation_uses_the_most_recent(client):
    inv = seed_graph(client)
    respx.get(url__startswith="https://api.openalex.org/works?").mock(
        return_value=httpx.Response(200, json=fixture("citers_page.json"))
    )

    assert client.post("/api/expand", json={"paper_id": "W2", "top_n": 5}).status_code == 200
    assert len(client.get(f"/api/graph?investigation={inv}").json()["nodes"]) == 7


def test_expanding_a_node_outside_the_graph_is_a_404(client):
    seed_graph(client)
    assert client.post("/api/expand", json={"paper_id": "W404"}).status_code == 404


def test_expanding_in_an_unknown_investigation_is_a_404(client):
    seed_graph(client)
    r = client.post("/api/expand", json={"paper_id": "W2", "investigation_id": 999})
    assert r.status_code == 404


# ------------------------------------------------------------------- interface


def test_the_root_url_serves_the_page(client):
    r = client.get("/")

    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "cytoscape" in r.text.lower()
