import sqlite3

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
        "depth": 0,
    }
    base.update(over)
    return base


def test_upsert_then_get_roundtrips_all_fields(conn):
    db.upsert_paper(conn, paper())

    got = db.get_paper(conn, "W1")

    assert got["title"] == "Attention Is All You Need"
    assert got["year"] == 2017
    assert got["authors"] == [{"name": "A. Vaswani", "affiliation": "Google Brain"}]
    assert got["topics"] == ["Machine Translation", "Deep Learning"]


def test_get_paper_returns_none_when_absent(conn):
    assert db.get_paper(conn, "W404") is None


def test_the_topic_hierarchy_is_stored_and_exposed_to_the_canvas(conn):
    db.upsert_paper(conn, paper())

    assert db.get_paper(conn, "W1")["field"] == "Computer Science"
    assert db.get_paper(conn, "W1")["subfield"] == "Artificial Intelligence"

    node = db.get_graph(conn)["nodes"][0]
    assert node["field"] == "Computer Science"
    assert node["subfield"] == "Artificial Intelligence"


def test_an_unclassified_paper_is_stored_without_a_hierarchy(conn):
    db.upsert_paper(conn, paper(field=None, subfield=None, topics=[]))

    node = db.get_graph(conn)["nodes"][0]
    assert node["field"] is None
    assert node["subfield"] is None
    assert node["topics"] == []


def test_a_database_from_an_older_version_gains_the_new_columns(tmp_path):
    """Um graph.db criado antes de campo/subcampo existirem deve continuar abrindo."""
    path = tmp_path / "legacy.db"
    old = sqlite3.connect(str(path))
    old.executescript(
        """
        CREATE TABLE papers (
            id TEXT PRIMARY KEY, doi TEXT, title TEXT NOT NULL, year INTEGER,
            cited_by_count INTEGER NOT NULL DEFAULT 0, abstract TEXT, venue TEXT,
            landing_url TEXT, oa_url TEXT, authors TEXT NOT NULL DEFAULT '[]',
            topics TEXT NOT NULL DEFAULT '[]', depth INTEGER NOT NULL DEFAULT 0,
            fetched_at TEXT NOT NULL);
        INSERT INTO papers (id, title, fetched_at) VALUES ('Wold', 'Artigo antigo', '2020-01-01');
        """
    )
    old.commit()
    old.close()

    conn = db.connect(path)

    assert db.get_paper(conn, "Wold")["field"] is None
    db.upsert_paper(conn, paper("Wnew"))
    assert db.get_paper(conn, "Wnew")["field"] == "Computer Science"
    conn.close()


def test_upsert_twice_keeps_one_row_and_updates_it(conn):
    db.upsert_paper(conn, paper())
    db.upsert_paper(conn, paper(cited_by_count=250))

    assert db.get_paper(conn, "W1")["cited_by_count"] == 250
    assert len(db.get_graph(conn)["nodes"]) == 1


def test_upsert_keeps_the_shallowest_depth(conn):
    db.upsert_paper(conn, paper(depth=3))
    db.upsert_paper(conn, paper(depth=1))
    assert db.get_paper(conn, "W1")["depth"] == 1

    db.upsert_paper(conn, paper(depth=5))
    assert db.get_paper(conn, "W1")["depth"] == 1


def test_edges_are_deduplicated(conn):
    db.upsert_paper(conn, paper("W1"))
    db.upsert_paper(conn, paper("W2"))

    db.add_edge(conn, citing_id="W2", cited_id="W1")
    db.add_edge(conn, citing_id="W2", cited_id="W1")

    assert db.get_graph(conn)["edges"] == [{"citing": "W2", "cited": "W1"}]


def test_node_is_frontier_until_expansion_is_recorded(conn):
    db.upsert_paper(conn, paper("W1"))
    assert db.get_graph(conn)["nodes"][0]["frontier"] is True

    db.record_expansion(conn, "W1", top_n=20, min_year=None, total_citers=1240)

    node = db.get_graph(conn)["nodes"][0]
    assert node["frontier"] is False
    assert node["total_citers"] == 1240


def test_expansion_is_specific_to_its_parameters(conn):
    db.upsert_paper(conn, paper("W1"))
    db.record_expansion(conn, "W1", top_n=20, min_year=None, total_citers=1240)

    assert db.is_expanded(conn, "W1", top_n=20, min_year=None) is True
    assert db.is_expanded(conn, "W1", top_n=50, min_year=None) is False
    assert db.is_expanded(conn, "W1", top_n=20, min_year=2020) is False


def test_re_recording_an_unfiltered_expansion_updates_instead_of_duplicating(conn):
    db.upsert_paper(conn, paper("W1"))
    db.record_expansion(conn, "W1", top_n=20, min_year=None, total_citers=1240)
    db.record_expansion(conn, "W1", top_n=20, min_year=None, total_citers=1300)

    rows = conn.execute("SELECT total_citers FROM expansions WHERE paper_id='W1'").fetchall()
    assert [r[0] for r in rows] == [1300]


def test_expanding_wider_supersedes_a_narrower_expansion(conn):
    db.upsert_paper(conn, paper("W1"))
    db.record_expansion(conn, "W1", top_n=50, min_year=None, total_citers=1240)

    assert db.is_expanded(conn, "W1", top_n=20, min_year=None) is True


def test_mark_defaults_to_unread_and_survives_being_set(conn):
    db.upsert_paper(conn, paper("W1"))
    assert db.get_graph(conn)["nodes"][0]["mark"] == "unread"

    db.set_mark(conn, "W1", "interested", note="talvez o tema")

    node = db.get_graph(conn)["nodes"][0]
    assert node["mark"] == "interested"
    assert db.get_paper(conn, "W1")["note"] == "talvez o tema"


def test_mark_is_replaced_not_duplicated(conn):
    db.upsert_paper(conn, paper("W1"))
    db.set_mark(conn, "W1", "interested", note="")
    db.set_mark(conn, "W1", "discarded", note="fora do escopo")

    assert db.get_graph(conn)["nodes"][0]["mark"] == "discarded"
    assert len(db.get_graph(conn)["nodes"]) == 1


def test_state_survives_reopening_the_database(tmp_path):
    path = tmp_path / "persist.db"
    c1 = db.connect(path)
    db.upsert_paper(c1, paper("W1"))
    db.set_mark(c1, "W1", "read", note="lido")
    c1.close()

    c2 = db.connect(path)
    assert db.get_graph(c2)["nodes"][0]["mark"] == "read"
    c2.close()
