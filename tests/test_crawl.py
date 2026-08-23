import pytest

from app import crawl, db


class FakeSource:
    """Fonte de citacoes em memoria com a mesma interface de OpenAlexClient.

    Nao e um mock: implementa de verdade `fetch_citers` sobre um grafo declarado no
    teste, incluindo a ordenacao por citacoes e o filtro de ano. Os testes conferem o
    que o crawl produziu; `calls` existe so para o teste de trafego redundante.
    """

    def __init__(self, graph: dict[str, list[dict]]):
        self.graph = graph
        self.calls: list[str] = []

    async def fetch_citers(self, paper_id, top_n, min_year, depth):
        self.calls.append(paper_id)
        citers = self.graph.get(paper_id, [])
        if min_year:
            citers = [c for c in citers if (c.get("year") or 0) >= min_year]
        total = len(citers)
        ordered = sorted(citers, key=lambda c: c["cited_by_count"], reverse=True)
        return [dict(c, depth=depth) for c in ordered[:top_n]], total


def p(pid, cited=0, year=2020):
    return {
        "id": pid,
        "doi": f"10.1/{pid}",
        "title": f"Paper {pid}",
        "year": year,
        "cited_by_count": cited,
        "abstract": "",
        "venue": "V",
        "landing_url": None,
        "oa_url": None,
        "authors": [],
        "topics": [],
    }


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "crawl.db")
    yield c
    c.close()


def nodes_by_id(conn):
    return {n["id"]: n for n in db.get_graph(conn)["nodes"]}


def edge_set(conn):
    return {(e["citing"], e["cited"]) for e in db.get_graph(conn)["edges"]}


# ------------------------------------------------------------------------ BFS


async def test_depth_zero_stores_the_seed_and_asks_the_api_for_nothing(conn):
    src = FakeSource({"W1": [p("W2")]})

    await crawl.crawl(conn, src, p("W1"), crawl.CrawlParams(depth=0))

    assert list(nodes_by_id(conn)) == ["W1"]
    assert src.calls == []


async def test_depth_one_links_the_seed_to_everyone_who_cited_it(conn):
    src = FakeSource({"W1": [p("W2"), p("W3")]})

    await crawl.crawl(conn, src, p("W1"), crawl.CrawlParams(depth=1))

    assert set(nodes_by_id(conn)) == {"W1", "W2", "W3"}
    # A aresta aponta do citante para o citado.
    assert edge_set(conn) == {("W2", "W1"), ("W3", "W1")}


async def test_depth_two_follows_citations_of_citations(conn):
    src = FakeSource({"W1": [p("W2")], "W2": [p("W3")], "W3": [p("W4")]})

    await crawl.crawl(conn, src, p("W1"), crawl.CrawlParams(depth=2))

    assert set(nodes_by_id(conn)) == {"W1", "W2", "W3"}
    assert edge_set(conn) == {("W2", "W1"), ("W3", "W2")}
    assert "W3" not in src.calls  # W3 esta na profundidade limite, nao expande


async def test_depth_is_the_shortest_path_from_the_seed(conn):
    # W3 cita o seminal diretamente E cita W2, que tambem cita o seminal.
    src = FakeSource({"W1": [p("W2"), p("W3")], "W2": [p("W3")], "W3": []})

    await crawl.crawl(conn, src, p("W1"), crawl.CrawlParams(depth=2))

    depths = {i: n["depth"] for i, n in nodes_by_id(conn).items()}
    assert depths == {"W1": 0, "W2": 1, "W3": 1}


async def test_a_paper_reached_by_two_paths_is_one_node_with_both_edges(conn):
    src = FakeSource({"W1": [p("W2"), p("W3")], "W2": [p("W4")], "W3": [p("W4")], "W4": []})

    await crawl.crawl(conn, src, p("W1"), crawl.CrawlParams(depth=2))

    assert len(nodes_by_id(conn)) == 4
    assert ("W4", "W2") in edge_set(conn)
    assert ("W4", "W3") in edge_set(conn)


async def test_a_citation_cycle_does_not_loop_forever(conn):
    src = FakeSource({"W1": [p("W2")], "W2": [p("W1")]})

    await crawl.crawl(conn, src, p("W1"), crawl.CrawlParams(depth=5))

    assert set(nodes_by_id(conn)) == {"W1", "W2"}
    assert src.calls.count("W1") == 1


# ------------------------------------------------------------------- orcamento


async def test_top_n_keeps_only_the_most_cited_citers(conn):
    src = FakeSource(
        {"W1": [p("Wlow", cited=1), p("Whigh", cited=999), p("Wmid", cited=50)]}
    )

    await crawl.crawl(conn, src, p("W1"), crawl.CrawlParams(depth=1, top_n=2))

    assert set(nodes_by_id(conn)) == {"W1", "Whigh", "Wmid"}


async def test_the_true_citer_count_is_recorded_even_when_top_n_truncates(conn):
    src = FakeSource({"W1": [p(f"W{i}", cited=i) for i in range(10, 40)]})

    await crawl.crawl(conn, src, p("W1"), crawl.CrawlParams(depth=1, top_n=3))

    seed = nodes_by_id(conn)["W1"]
    assert seed["total_citers"] == 30
    assert seed["frontier"] is False


async def test_max_nodes_stops_the_crawl(conn):
    wide = {"W1": [p(f"A{i}", cited=i) for i in range(50)]}
    for i in range(50):
        wide[f"A{i}"] = [p(f"B{i}_{j}", cited=j) for j in range(50)]
    src = FakeSource(wide)

    result = await crawl.crawl(
        conn, src, p("W1"), crawl.CrawlParams(depth=3, top_n=50, max_nodes=100)
    )

    assert len(nodes_by_id(conn)) <= 100
    assert result["truncated"] is True


async def test_a_crawl_within_budget_is_not_reported_as_truncated(conn):
    src = FakeSource({"W1": [p("W2")], "W2": []})

    result = await crawl.crawl(conn, src, p("W1"), crawl.CrawlParams(depth=2, max_nodes=100))

    assert result["truncated"] is False
    assert result["nodes"] == 2


async def test_min_year_excludes_older_citers(conn):
    src = FakeSource({"W1": [p("Wold", year=2010), p("Wnew", year=2024)]})

    await crawl.crawl(conn, src, p("W1"), crawl.CrawlParams(depth=1, min_year=2020))

    assert set(nodes_by_id(conn)) == {"W1", "Wnew"}


# ----------------------------------------------------------------------- cache


async def test_a_node_already_expanded_is_not_fetched_again(conn):
    src = FakeSource({"W1": [p("W2")], "W2": []})
    params = crawl.CrawlParams(depth=1, top_n=20)

    await crawl.crawl(conn, src, p("W1"), params)
    await crawl.crawl(conn, src, p("W1"), params)

    assert src.calls == ["W1"]


async def test_asking_for_a_wider_top_n_does_refetch(conn):
    src = FakeSource({"W1": [p(f"W{i}", cited=i) for i in range(30)]})

    await crawl.crawl(conn, src, p("W1"), crawl.CrawlParams(depth=1, top_n=5))
    await crawl.crawl(conn, src, p("W1"), crawl.CrawlParams(depth=1, top_n=25))

    assert src.calls == ["W1", "W1"]
    assert len(nodes_by_id(conn)) == 26


# -------------------------------------------------------------------- expansao


async def test_expand_grows_one_node_by_exactly_one_level(conn):
    src = FakeSource({"W1": [p("W2")], "W2": [p("W3")], "W3": [p("W4")]})
    await crawl.crawl(conn, src, p("W1"), crawl.CrawlParams(depth=1))
    assert nodes_by_id(conn)["W2"]["frontier"] is True

    result = await crawl.expand(conn, src, "W2", top_n=20, min_year=None)

    assert set(nodes_by_id(conn)) == {"W1", "W2", "W3"}
    assert nodes_by_id(conn)["W2"]["frontier"] is False
    assert nodes_by_id(conn)["W3"]["depth"] == 2
    assert result["new_nodes"] == 1
    assert "W3" not in src.calls  # expandiu W2, nao a vizinhanca inteira


async def test_expanding_a_node_with_no_new_citers_reports_zero(conn):
    src = FakeSource({"W1": [p("W2")], "W2": []})
    await crawl.crawl(conn, src, p("W1"), crawl.CrawlParams(depth=1))

    result = await crawl.expand(conn, src, "W2", top_n=20, min_year=None)

    assert result["new_nodes"] == 0
    assert nodes_by_id(conn)["W2"]["frontier"] is False


async def test_expanding_an_unknown_node_is_an_error(conn):
    src = FakeSource({})
    with pytest.raises(KeyError):
        await crawl.expand(conn, src, "W404", top_n=20, min_year=None)


# ------------------------------------------------------------------- progresso


async def test_progress_is_reported_as_the_crawl_advances(conn):
    src = FakeSource({"W1": [p("W2"), p("W3")], "W2": [p("W4")], "W3": []})
    seen = []

    await crawl.crawl(
        conn, src, p("W1"), crawl.CrawlParams(depth=2), on_progress=seen.append
    )

    assert seen, "o crawl nao reportou progresso nenhum"
    assert seen[-1]["done"] is True
    assert seen[-1]["nodes"] == len(nodes_by_id(conn))
    assert all("expanded" in ev and "nodes" in ev for ev in seen)


async def test_progress_titles_survive_non_ascii(conn):
    # Titulos reais tem acentos, aspas curvas e travessoes; o console do Windows e
    # cp1252. O progresso nao pode quebrar por isso.
    weird = dict(p("W2"), title="Grafos e citações — análise “profunda”")
    src = FakeSource({"W1": [weird]})
    seen = []

    await crawl.crawl(conn, src, p("W1"), crawl.CrawlParams(depth=1), on_progress=seen.append)

    assert any("citações" in (ev.get("title") or "") for ev in seen)
