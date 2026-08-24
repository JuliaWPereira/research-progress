"""Investigacoes: varios grafos no mesmo banco, com cache de artigos compartilhado."""

import sqlite3

import pytest

from app import db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "inv.db")
    yield c
    c.close()


def paper(pid="W1", **over):
    base = {
        "id": pid,
        "doi": f"10.1/{pid}",
        "title": f"Paper {pid}",
        "year": 2020,
        "cited_by_count": 100,
        "abstract": "",
        "venue": "V",
        "landing_url": None,
        "oa_url": None,
        "authors": [],
        "topics": ["T"],
        "field": "Computer Science",
        "subfield": "Artificial Intelligence",
    }
    base.update(over)
    return base


def ids(conn, inv):
    return {n["id"] for n in db.get_graph(conn, inv)["nodes"]}


# ----------------------------------------------------------------- ciclo de vida


def test_a_new_database_has_no_investigations(conn):
    assert db.list_investigations(conn) == []


def test_creating_an_investigation_returns_a_usable_id(conn):
    db.upsert_paper(conn, paper("W1"))
    inv = db.create_investigation(conn, name="Fed. learning", seed_id="W1", top_n=25)

    got = db.get_investigation(conn, inv)
    assert got["name"] == "Fed. learning"
    assert got["seed_id"] == "W1"
    assert got["top_n"] == 25
    assert got["min_year"] is None


def test_an_investigation_remembers_its_year_filter(conn):
    db.upsert_paper(conn, paper("W1"))
    inv = db.create_investigation(conn, "fronteira", "W1", top_n=25, min_year=2024)

    assert db.get_investigation(conn, inv)["min_year"] == 2024


def test_get_investigation_returns_none_for_an_unknown_id(conn):
    assert db.get_investigation(conn, 999) is None


def test_listing_shows_the_seed_title_and_the_node_count(conn):
    db.upsert_paper(conn, paper("W1", title="O seminal"))
    db.upsert_paper(conn, paper("W2"))
    inv = db.create_investigation(conn, "A", "W1")
    db.add_member(conn, inv, "W1", depth=0)
    db.add_member(conn, inv, "W2", depth=1)

    listed = db.list_investigations(conn)

    assert len(listed) == 1
    assert listed[0]["seed_title"] == "O seminal"
    assert listed[0]["nodes"] == 2


def test_renaming_an_investigation(conn):
    db.upsert_paper(conn, paper("W1"))
    inv = db.create_investigation(conn, "nome ruim", "W1")

    db.rename_investigation(conn, inv, "nome bom")

    assert db.get_investigation(conn, inv)["name"] == "nome bom"


def test_the_newest_investigation_is_listed_first(conn):
    db.upsert_paper(conn, paper("W1"))
    first = db.create_investigation(conn, "primeira", "W1")
    second = db.create_investigation(conn, "segunda", "W1")

    assert [i["id"] for i in db.list_investigations(conn)] == [second, first]


# --------------------------------------------------------------- isolamento


def test_two_investigations_hold_different_sets_of_papers(conn):
    for pid in ("W1", "W2", "W3"):
        db.upsert_paper(conn, paper(pid))
    a = db.create_investigation(conn, "A", "W1")
    b = db.create_investigation(conn, "B", "W3")
    db.add_member(conn, a, "W1", 0)
    db.add_member(conn, a, "W2", 1)
    db.add_member(conn, b, "W3", 0)

    assert ids(conn, a) == {"W1", "W2"}
    assert ids(conn, b) == {"W3"}


def test_the_same_paper_can_sit_at_different_depths_in_each_investigation(conn):
    db.upsert_paper(conn, paper("W1"))
    db.upsert_paper(conn, paper("W9"))
    a = db.create_investigation(conn, "A", "W1")
    b = db.create_investigation(conn, "B", "W9")

    db.add_member(conn, a, "W9", depth=2)
    db.add_member(conn, b, "W9", depth=0)

    depth = lambda inv: next(n for n in db.get_graph(conn, inv)["nodes"] if n["id"] == "W9")["depth"]
    assert depth(a) == 2
    assert depth(b) == 0


def test_depth_converges_to_the_shortest_path_within_one_investigation(conn):
    db.upsert_paper(conn, paper("W1"))
    inv = db.create_investigation(conn, "A", "W1")

    db.add_member(conn, inv, "W1", depth=3)
    db.add_member(conn, inv, "W1", depth=1)
    db.add_member(conn, inv, "W1", depth=5)

    assert db.get_graph(conn, inv)["nodes"][0]["depth"] == 1


def test_expanding_in_one_investigation_leaves_the_other_a_frontier(conn):
    db.upsert_paper(conn, paper("W1"))
    a = db.create_investigation(conn, "A", "W1")
    b = db.create_investigation(conn, "B", "W1")
    db.add_member(conn, a, "W1", 0)
    db.add_member(conn, b, "W1", 0)

    db.record_expansion(conn, a, "W1", top_n=20, min_year=None, total_citers=500)

    assert db.is_expanded(conn, a, "W1", top_n=20, min_year=None) is True
    assert db.is_expanded(conn, b, "W1", top_n=20, min_year=None) is False
    assert db.get_graph(conn, a)["nodes"][0]["frontier"] is False
    assert db.get_graph(conn, b)["nodes"][0]["frontier"] is True


def test_only_edges_between_members_show_up_in_a_scoped_graph(conn):
    for pid in ("W1", "W2", "Wout"):
        db.upsert_paper(conn, paper(pid))
    inv = db.create_investigation(conn, "A", "W1")
    db.add_member(conn, inv, "W1", 0)
    db.add_member(conn, inv, "W2", 1)

    db.add_edge(conn, citing_id="W2", cited_id="W1")
    db.add_edge(conn, citing_id="Wout", cited_id="W1")  # Wout nao e membro

    assert db.get_graph(conn, inv)["edges"] == [{"citing": "W2", "cited": "W1"}]


# ------------------------------------------------------------- cache global


def test_a_paper_fetched_once_is_available_to_every_investigation(conn):
    db.upsert_paper(conn, paper("W9", title="Caro de buscar", abstract="resumo longo"))
    a = db.create_investigation(conn, "A", "W9")
    b = db.create_investigation(conn, "B", "W9")
    db.add_member(conn, a, "W9", 0)
    db.add_member(conn, b, "W9", 0)

    for inv in (a, b):
        node = db.get_graph(conn, inv)["nodes"][0]
        assert node["title"] == "Caro de buscar"
    assert db.get_paper(conn, "W9")["abstract"] == "resumo longo"


def test_a_mark_is_global_so_overlap_between_investigations_is_visible(conn):
    """Marcacao segue o ARTIGO, nao a pergunta: reencontrar um artigo ja marcado ao
    abrir outra investigacao e o sinal de sobreposicao entre as duas."""
    db.upsert_paper(conn, paper("W9"))
    a = db.create_investigation(conn, "A", "W9")
    b = db.create_investigation(conn, "B", "W9")
    db.add_member(conn, a, "W9", 0)
    db.add_member(conn, b, "W9", 1)

    db.set_mark(conn, "W9", "interested", note="candidato")

    assert db.get_graph(conn, a)["nodes"][0]["mark"] == "interested"
    assert db.get_graph(conn, b)["nodes"][0]["mark"] == "interested"


def test_total_citers_is_reported_per_investigation(conn):
    db.upsert_paper(conn, paper("W1"))
    a = db.create_investigation(conn, "A", "W1")
    b = db.create_investigation(conn, "B", "W1", min_year=2024)
    db.add_member(conn, a, "W1", 0)
    db.add_member(conn, b, "W1", 0)

    db.record_expansion(conn, a, "W1", top_n=20, min_year=None, total_citers=9000)
    db.record_expansion(conn, b, "W1", top_n=20, min_year=2024, total_citers=300)

    assert db.get_graph(conn, a)["nodes"][0]["total_citers"] == 9000
    assert db.get_graph(conn, b)["nodes"][0]["total_citers"] == 300


def test_paper_detail_can_be_scoped_to_report_depth_and_coverage(conn):
    db.upsert_paper(conn, paper("W1"))
    inv = db.create_investigation(conn, "A", "W1")
    db.add_member(conn, inv, "W1", depth=2)
    db.record_expansion(conn, inv, "W1", top_n=20, min_year=None, total_citers=77)

    scoped = db.get_paper(conn, "W1", investigation_id=inv)

    assert scoped["depth"] == 2
    assert scoped["total_citers"] == 77
    assert scoped["frontier"] is False


# ---------------------------------------------------------------- exclusao


def test_deleting_an_investigation_leaves_its_neighbours_intact(conn):
    for pid in ("W1", "W2"):
        db.upsert_paper(conn, paper(pid))
    a = db.create_investigation(conn, "A", "W1")
    b = db.create_investigation(conn, "B", "W2")
    db.add_member(conn, a, "W1", 0)
    db.add_member(conn, b, "W2", 0)
    db.record_expansion(conn, a, "W1", 20, None, 5)

    db.delete_investigation(conn, a)

    assert db.get_investigation(conn, a) is None
    assert [i["id"] for i in db.list_investigations(conn)] == [b]
    assert ids(conn, b) == {"W2"}


def test_deleting_an_investigation_keeps_the_paper_cache_and_the_marks(conn):
    db.upsert_paper(conn, paper("W1", abstract="caro de buscar"))
    a = db.create_investigation(conn, "A", "W1")
    db.add_member(conn, a, "W1", 0)
    db.set_mark(conn, "W1", "read", note="lido")

    db.delete_investigation(conn, a)

    assert db.get_paper(conn, "W1")["abstract"] == "caro de buscar"
    assert db.get_paper(conn, "W1")["mark"] == "read"


def test_deleting_an_investigation_drops_its_membership_and_expansions(conn):
    db.upsert_paper(conn, paper("W1"))
    a = db.create_investigation(conn, "A", "W1")
    db.add_member(conn, a, "W1", 0)
    db.record_expansion(conn, a, "W1", 20, None, 5)

    db.delete_investigation(conn, a)

    assert conn.execute("SELECT COUNT(*) FROM members").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM expansions").fetchone()[0] == 0


def test_deleting_an_unknown_investigation_is_an_error(conn):
    with pytest.raises(KeyError):
        db.delete_investigation(conn, 999)


# ---------------------------------------------------------------- migracao


LEGACY_SCHEMA = """
CREATE TABLE papers (
    id TEXT PRIMARY KEY, doi TEXT, title TEXT NOT NULL, year INTEGER,
    cited_by_count INTEGER NOT NULL DEFAULT 0, abstract TEXT, venue TEXT,
    landing_url TEXT, oa_url TEXT, authors TEXT NOT NULL DEFAULT '[]',
    topics TEXT NOT NULL DEFAULT '[]', field TEXT, subfield TEXT,
    depth INTEGER NOT NULL DEFAULT 0, fetched_at TEXT NOT NULL);
CREATE TABLE edges (
    citing_id TEXT NOT NULL, cited_id TEXT NOT NULL,
    PRIMARY KEY (citing_id, cited_id));
CREATE TABLE expansions (
    paper_id TEXT NOT NULL, top_n INTEGER NOT NULL,
    min_year INTEGER NOT NULL DEFAULT 0, total_citers INTEGER NOT NULL DEFAULT 0,
    done_at TEXT NOT NULL, PRIMARY KEY (paper_id, top_n, min_year));
CREATE TABLE marks (
    paper_id TEXT PRIMARY KEY, status TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);
"""


def legacy_db(path):
    """Um graph.db no formato anterior as investigacoes: um grafo unico, com a
    profundidade guardada em `papers` e expansoes sem escopo."""
    old = sqlite3.connect(str(path))
    old.executescript(LEGACY_SCHEMA)
    old.executescript(
        """
        INSERT INTO papers (id, title, year, cited_by_count, depth, fetched_at, topics)
        VALUES ('Wseed', 'Deep learning', 2015, 90000, 0, '2026-08-01', '["ML"]'),
               ('Wa',    'Citante A',     2019,   500, 1, '2026-08-01', '["ML"]'),
               ('Wb',    'Citante B',     2021,   120, 2, '2026-08-01', '["ML"]');
        INSERT INTO edges VALUES ('Wa', 'Wseed'), ('Wb', 'Wa');
        INSERT INTO expansions VALUES ('Wseed', 20, 0, 8341, '2026-08-01'),
                                      ('Wa',    20, 0,  212, '2026-08-01');
        INSERT INTO marks VALUES ('Wa', 'interested', 'promissor', '2026-08-01');
        """
    )
    old.commit()
    old.close()


def test_an_existing_graph_becomes_one_investigation(tmp_path):
    path = tmp_path / "legacy.db"
    legacy_db(path)

    conn = db.connect(path)

    listed = db.list_investigations(conn)
    assert len(listed) == 1, "o grafo existente deveria virar exatamente uma investigacao"
    assert listed[0]["seed_id"] == "Wseed"
    assert listed[0]["nodes"] == 3
    # O nome sai do titulo do seminal, para a usuaria reconhecer o que e.
    assert "Deep learning" in listed[0]["name"]
    conn.close()


def test_migration_preserves_depths_edges_expansions_and_marks(tmp_path):
    path = tmp_path / "legacy.db"
    legacy_db(path)

    conn = db.connect(path)
    inv = db.list_investigations(conn)[0]["id"]
    graph = db.get_graph(conn, inv)

    assert {n["id"]: n["depth"] for n in graph["nodes"]} == {"Wseed": 0, "Wa": 1, "Wb": 2}
    assert {(e["citing"], e["cited"]) for e in graph["edges"]} == {("Wa", "Wseed"), ("Wb", "Wa")}

    seed = next(n for n in graph["nodes"] if n["id"] == "Wseed")
    assert seed["total_citers"] == 8341, "a cobertura ja buscada deveria sobreviver"
    assert seed["frontier"] is False
    assert next(n for n in graph["nodes"] if n["id"] == "Wb")["frontier"] is True
    assert next(n for n in graph["nodes"] if n["id"] == "Wa")["mark"] == "interested"
    conn.close()


def test_migration_does_not_refetch_anything(tmp_path):
    """Depois de migrar, expandir o seminal com a mesma cobertura nao deve pedir nada:
    o cache continua valendo."""
    path = tmp_path / "legacy.db"
    legacy_db(path)

    conn = db.connect(path)
    inv = db.list_investigations(conn)[0]["id"]

    assert db.is_expanded(conn, inv, "Wseed", top_n=20, min_year=None) is True
    assert db.is_expanded(conn, inv, "Wb", top_n=20, min_year=None) is False
    conn.close()


def test_reopening_a_migrated_database_does_not_migrate_twice(tmp_path):
    path = tmp_path / "legacy.db"
    legacy_db(path)

    db.connect(path).close()
    conn = db.connect(path)

    assert len(db.list_investigations(conn)) == 1
    assert db.list_investigations(conn)[0]["nodes"] == 3
    conn.close()


def test_a_database_from_before_field_and_subfield_still_migrates(tmp_path):
    """O formato mais antigo de todos: sem campo/subcampo e sem investigacoes."""
    path = tmp_path / "ancient.db"
    old = sqlite3.connect(str(path))
    old.executescript(LEGACY_SCHEMA.replace(" field TEXT, subfield TEXT,", ""))
    old.execute(
        "INSERT INTO papers (id, title, depth, fetched_at) VALUES ('Wx', 'Antigo', 0, '2020-01-01')"
    )
    old.commit()
    old.close()

    conn = db.connect(path)

    node = db.get_graph(conn, db.list_investigations(conn)[0]["id"])["nodes"][0]
    assert node["id"] == "Wx"
    assert node["field"] is None
    conn.close()


def two_seed_legacy_db(path):
    """O caso real: duas buscas feitas antes das investigacoes existirem, fundidas num
    grafo unico com dois artigos na profundidade 0."""
    old = sqlite3.connect(str(path))
    old.executescript(LEGACY_SCHEMA)
    old.executescript(
        """
        INSERT INTO papers (id, title, year, cited_by_count, depth, fetched_at, topics) VALUES
            ('Wfed',   'FedAvg',        2016, 9000, 0, '2026-08-01', '[]'),
            ('Wsurv',  'Survey',        2019, 3000, 0, '2026-08-01', '[]'),
            ('Wa',     'Cita FedAvg',   2018,  500, 1, '2026-08-01', '[]'),
            ('Wb',     'Cita Wa',       2020,  100, 2, '2026-08-01', '[]'),
            ('Wc',     'Cita o survey', 2021,   50, 1, '2026-08-01', '[]'),
            ('Wboth',  'Cita os dois',  2022,   20, 1, '2026-08-01', '[]');
        INSERT INTO edges VALUES
            ('Wa', 'Wfed'), ('Wb', 'Wa'), ('Wc', 'Wsurv'),
            ('Wboth', 'Wfed'), ('Wboth', 'Wsurv');
        INSERT INTO expansions VALUES ('Wfed', 20, 0, 9000, '2026-08-01');
        """
    )
    old.commit()
    old.close()


def test_a_legacy_graph_with_two_seeds_splits_into_two_investigations(tmp_path):
    """Era a queixa real: duas buscas misturadas num grafo so. As arestas ja dizem qual
    artigo veio de qual seminal, entao a migracao reconstroi as duas."""
    path = tmp_path / "dois.db"
    two_seed_legacy_db(path)

    conn = db.connect(path)

    listed = db.list_investigations(conn)
    assert len(listed) == 2
    assert {i["seed_id"] for i in listed} == {"Wfed", "Wsurv"}
    by_seed = {i["seed_id"]: i["id"] for i in listed}

    # O cone do FedAvg desce dois niveis; o do survey, um.
    fed = {n["id"]: n["depth"] for n in db.get_graph(conn, by_seed["Wfed"])["nodes"]}
    surv = {n["id"]: n["depth"] for n in db.get_graph(conn, by_seed["Wsurv"])["nodes"]}
    assert fed == {"Wfed": 0, "Wa": 1, "Wb": 2, "Wboth": 1}
    assert surv == {"Wsurv": 0, "Wc": 1, "Wboth": 1}
    conn.close()


def test_a_paper_cited_from_both_cones_belongs_to_both_investigations(tmp_path):
    """Sobreposicao nao e erro: e o sinal de que as duas perguntas se encontram."""
    path = tmp_path / "dois.db"
    two_seed_legacy_db(path)

    conn = db.connect(path)
    everywhere = [
        i["id"]
        for i in db.list_investigations(conn)
        if "Wboth" in {n["id"] for n in db.get_graph(conn, i["id"])["nodes"]}
    ]

    assert len(everywhere) == 2
    conn.close()


def test_the_split_names_each_investigation_after_its_own_seed(tmp_path):
    path = tmp_path / "dois.db"
    two_seed_legacy_db(path)

    conn = db.connect(path)
    names = {i["seed_id"]: i["name"] for i in db.list_investigations(conn)}

    assert "FedAvg" in names["Wfed"]
    assert "Survey" in names["Wsurv"]
    conn.close()


def test_the_split_keeps_coverage_so_nothing_is_refetched(tmp_path):
    """A expansao de um artigo vale para toda investigacao que o contenha: foi paga uma
    vez e nao precisa ser paga de novo."""
    path = tmp_path / "dois.db"
    two_seed_legacy_db(path)

    conn = db.connect(path)
    fed = next(i["id"] for i in db.list_investigations(conn) if i["seed_id"] == "Wfed")

    assert db.is_expanded(conn, fed, "Wfed", top_n=20, min_year=None) is True
    node = next(n for n in db.get_graph(conn, fed)["nodes"] if n["id"] == "Wfed")
    assert node["total_citers"] == 9000
    conn.close()


def test_the_split_loses_no_paper(tmp_path):
    path = tmp_path / "dois.db"
    two_seed_legacy_db(path)

    conn = db.connect(path)
    seen = set()
    for i in db.list_investigations(conn):
        seen |= {n["id"] for n in db.get_graph(conn, i["id"])["nodes"]}

    assert seen == {"Wfed", "Wsurv", "Wa", "Wb", "Wc", "Wboth"}
    conn.close()


def test_a_paper_reachable_from_no_seed_is_still_kept_somewhere(tmp_path):
    """Grafo estranho o bastante para ter um artigo solto: ele nao pode desaparecer da
    interface so porque a migracao nao soube de onde veio."""
    path = tmp_path / "orfao.db"
    two_seed_legacy_db(path)
    old = sqlite3.connect(str(path))
    old.execute(
        "INSERT INTO papers (id, title, depth, fetched_at) VALUES ('Wsolto', 'Solto', 2, '2026-08-01')"
    )
    old.commit()
    old.close()

    conn = db.connect(path)
    seen = set()
    for i in db.list_investigations(conn):
        seen |= {n["id"] for n in db.get_graph(conn, i["id"])["nodes"]}

    assert "Wsolto" in seen
    conn.close()


def test_an_investigation_that_merged_two_searches_is_repaired_on_open(tmp_path):
    """Uma versao anterior da migracao juntava varias buscas numa investigacao so, o
    que se reconhece por ter mais de um membro na profundidade 0. Abrir o banco
    conserta, para quem passou por aquela versao nao ficar com a bagunca."""
    path = tmp_path / "merged.db"
    conn = db.connect(path)
    for pid, title in [("Wfed", "FedAvg"), ("Wsurv", "Survey"), ("Wa", "A"), ("Wc", "C")]:
        db.upsert_paper(conn, paper(pid, title=title))
    db.add_edge(conn, citing_id="Wa", cited_id="Wfed")
    db.add_edge(conn, citing_id="Wc", cited_id="Wsurv")
    merged = db.create_investigation(conn, "importado", "Wfed")
    for pid, depth in [("Wfed", 0), ("Wsurv", 0), ("Wa", 1), ("Wc", 1)]:
        db.add_member(conn, merged, pid, depth)
    db.record_expansion(conn, merged, "Wfed", 20, None, 9000)
    conn.close()

    conn = db.connect(path)

    listed = db.list_investigations(conn)
    assert len(listed) == 2, "a investigacao fundida deveria ter sido separada"
    assert {i["seed_id"] for i in listed} == {"Wfed", "Wsurv"}
    by_seed = {i["seed_id"]: i["id"] for i in listed}
    assert {n["id"] for n in db.get_graph(conn, by_seed["Wfed"])["nodes"]} == {"Wfed", "Wa"}
    assert {n["id"] for n in db.get_graph(conn, by_seed["Wsurv"])["nodes"]} == {"Wsurv", "Wc"}
    # A cobertura ja paga acompanha o seminal.
    assert db.is_expanded(conn, by_seed["Wfed"], "Wfed", top_n=20, min_year=None) is True
    conn.close()


def test_repairing_a_merged_investigation_happens_only_once(tmp_path):
    path = tmp_path / "merged.db"
    conn = db.connect(path)
    for pid in ("Wfed", "Wsurv"):
        db.upsert_paper(conn, paper(pid))
    merged = db.create_investigation(conn, "importado", "Wfed")
    db.add_member(conn, merged, "Wfed", 0)
    db.add_member(conn, merged, "Wsurv", 0)
    conn.close()

    db.connect(path).close()
    conn = db.connect(path)

    assert len(db.list_investigations(conn)) == 2
    conn.close()


def test_a_healthy_investigation_is_left_alone(tmp_path):
    path = tmp_path / "ok.db"
    conn = db.connect(path)
    db.upsert_paper(conn, paper("W1"))
    db.upsert_paper(conn, paper("W2"))
    inv = db.create_investigation(conn, "saudavel", "W1")
    db.add_member(conn, inv, "W1", 0)
    db.add_member(conn, inv, "W2", 1)
    conn.close()

    conn = db.connect(path)

    listed = db.list_investigations(conn)
    assert [i["id"] for i in listed] == [inv]
    assert listed[0]["name"] == "saudavel"
    conn.close()


def test_an_empty_legacy_database_gets_no_phantom_investigation(tmp_path):
    path = tmp_path / "empty.db"
    old = sqlite3.connect(str(path))
    old.executescript(LEGACY_SCHEMA)
    old.commit()
    old.close()

    conn = db.connect(path)

    assert db.list_investigations(conn) == []
    conn.close()
