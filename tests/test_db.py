"""As tabelas GLOBAIS: o cache de artigos, as arestas e a triagem.

O que e por investigacao (composicao, profundidade, expansoes) esta em
test_investigations.py.
"""

import pytest

from app import db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    yield c
    c.close()


def paper(pid="W1", **over):
    base = {
        "id": pid,
        "doi": "10.1/abc",
        "title": "Attention Is All You Need",
        "year": 2017,
        "cited_by_count": 100,
        "abstract": "The dominant sequence transduction models...",
        "venue": "NeurIPS",
        "landing_url": "https://example.org/paper",
        "oa_url": "https://example.org/paper.pdf",
        "authors": [{"name": "A. Vaswani", "affiliation": "Google Brain"}],
        "topics": ["Machine Translation", "Deep Learning"],
        "field": "Computer Science",
        "subfield": "Artificial Intelligence",
    }
    base.update(over)
    return base


# ------------------------------------------------------------- cache de artigos


def test_upsert_then_get_roundtrips_all_fields(conn):
    db.upsert_paper(conn, paper())

    got = db.get_paper(conn, "W1")

    assert got["title"] == "Attention Is All You Need"
    assert got["year"] == 2017
    assert got["doi"] == "10.1/abc"
    assert got["venue"] == "NeurIPS"
    assert got["oa_url"] == "https://example.org/paper.pdf"
    assert got["authors"] == [{"name": "A. Vaswani", "affiliation": "Google Brain"}]
    assert got["topics"] == ["Machine Translation", "Deep Learning"]


def test_get_paper_returns_none_when_absent(conn):
    assert db.get_paper(conn, "W404") is None


def test_upsert_twice_keeps_one_row_and_updates_it(conn):
    db.upsert_paper(conn, paper())
    db.upsert_paper(conn, paper(cited_by_count=250))

    assert db.get_paper(conn, "W1")["cited_by_count"] == 250
    assert conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 1


def test_a_later_fetch_without_an_abstract_does_not_erase_the_one_we_have(conn):
    """O resumo pode ter vindo do Semantic Scholar depois de um custo real; uma
    releitura do OpenAlex sem resumo nao pode apaga-lo."""
    db.upsert_paper(conn, paper(abstract="resumo resgatado com esforco"))

    db.upsert_paper(conn, paper(abstract=""))

    assert db.get_paper(conn, "W1")["abstract"] == "resumo resgatado com esforco"


def test_the_topic_hierarchy_is_stored(conn):
    db.upsert_paper(conn, paper())

    got = db.get_paper(conn, "W1")
    assert got["field"] == "Computer Science"
    assert got["subfield"] == "Artificial Intelligence"


def test_an_unclassified_paper_is_stored_without_a_hierarchy(conn):
    db.upsert_paper(conn, paper(field=None, subfield=None, topics=[]))

    got = db.get_paper(conn, "W1")
    assert got["field"] is None
    assert got["subfield"] is None
    assert got["topics"] == []


def test_a_paper_with_no_title_gets_a_placeholder_instead_of_failing(conn):
    db.upsert_paper(conn, paper(title=None))
    assert db.get_paper(conn, "W1")["title"] == "(sem titulo)"


def test_an_unscoped_paper_has_no_depth_or_coverage(conn):
    """Sem investigacao, profundidade e cobertura nao existem: sao propriedades do
    recorte, nao do artigo."""
    db.upsert_paper(conn, paper())

    got = db.get_paper(conn, "W1")
    assert got["depth"] is None
    assert got["total_citers"] is None


# ---------------------------------------------------------------------- arestas


def test_edges_are_deduplicated(conn):
    db.add_edge(conn, citing_id="W2", cited_id="W1")
    db.add_edge(conn, citing_id="W2", cited_id="W1")

    rows = conn.execute("SELECT citing_id, cited_id FROM edges").fetchall()
    assert [tuple(r) for r in rows] == [("W2", "W1")]


def test_an_edge_is_directional(conn):
    db.add_edge(conn, citing_id="W2", cited_id="W1")
    db.add_edge(conn, citing_id="W1", cited_id="W2")

    assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 2


# -------------------------------------------------------------------- triagem


def test_mark_defaults_to_unread(conn):
    db.upsert_paper(conn, paper())
    assert db.get_paper(conn, "W1")["mark"] == "unread"
    assert db.get_paper(conn, "W1")["note"] == ""


def test_setting_a_mark_stores_the_status_and_the_note(conn):
    db.upsert_paper(conn, paper())

    db.set_mark(conn, "W1", "interested", note="talvez o tema")

    got = db.get_paper(conn, "W1")
    assert got["mark"] == "interested"
    assert got["note"] == "talvez o tema"


def test_mark_is_replaced_not_duplicated(conn):
    db.upsert_paper(conn, paper())
    db.set_mark(conn, "W1", "interested", note="")
    db.set_mark(conn, "W1", "discarded", note="fora do escopo")

    assert db.get_paper(conn, "W1")["mark"] == "discarded"
    assert conn.execute("SELECT COUNT(*) FROM marks").fetchone()[0] == 1


def test_an_invalid_mark_is_refused(conn):
    db.upsert_paper(conn, paper())
    with pytest.raises(ValueError, match="invalida"):
        db.set_mark(conn, "W1", "amei")


# ------------------------------------------------------------------ persistencia


def test_state_survives_reopening_the_database(tmp_path):
    path = tmp_path / "persist.db"
    c1 = db.connect(path)
    db.upsert_paper(c1, paper("W1"))
    db.set_mark(c1, "W1", "read", note="lido")
    inv = db.create_investigation(c1, "uma busca", "W1")
    db.add_member(c1, inv, "W1", depth=0)
    c1.close()

    c2 = db.connect(path)
    assert db.get_paper(c2, "W1")["mark"] == "read"
    assert db.get_graph(c2, inv)["nodes"][0]["title"] == "Attention Is All You Need"
    assert db.list_investigations(c2)[0]["name"] == "uma busca"
    c2.close()
