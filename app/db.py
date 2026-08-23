"""Persistencia em SQLite.

O cache e uma consequencia do schema: um artigo que ja esta em `papers` nunca e
rebuscado, e o estado (grafo + triagem) sobrevive a reinicios do servidor.

Direcao das arestas: `citing_id` -> `cited_id`, ou seja, a aresta aponta para tras
no tempo. O crawl caminha ao contrario dela, do seminal para quem o citou.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

MARKS = ("unread", "interested", "read", "discarded")

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    id              TEXT PRIMARY KEY,
    doi             TEXT,
    title           TEXT NOT NULL,
    year            INTEGER,
    cited_by_count  INTEGER NOT NULL DEFAULT 0,
    abstract        TEXT,
    venue           TEXT,
    landing_url     TEXT,
    oa_url          TEXT,
    authors         TEXT NOT NULL DEFAULT '[]',
    topics          TEXT NOT NULL DEFAULT '[]',
    field           TEXT,
    subfield        TEXT,
    depth           INTEGER NOT NULL DEFAULT 0,
    fetched_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS edges (
    citing_id  TEXT NOT NULL,
    cited_id   TEXT NOT NULL,
    PRIMARY KEY (citing_id, cited_id)
);
CREATE INDEX IF NOT EXISTS idx_edges_cited ON edges(cited_id);

-- `min_year` guarda 0 para "sem filtro", nunca NULL: o SQLite considera NULLs
-- distintos entre si em chaves primarias, o que faria ON CONFLICT nunca disparar
-- e acumular linhas duplicadas. Traduzido por _year_key/_year_out nas fronteiras.
CREATE TABLE IF NOT EXISTS expansions (
    paper_id      TEXT NOT NULL,
    top_n         INTEGER NOT NULL,
    min_year      INTEGER NOT NULL DEFAULT 0,
    total_citers  INTEGER NOT NULL DEFAULT 0,
    done_at       TEXT NOT NULL,
    PRIMARY KEY (paper_id, top_n, min_year)
);

CREATE TABLE IF NOT EXISTS marks (
    paper_id    TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _year_key(min_year: int | None) -> int:
    """None ("sem filtro de ano") vira 0 para que a chave primaria funcione."""
    return 0 if min_year is None else int(min_year)


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


# Colunas acrescentadas depois do primeiro schema. `CREATE TABLE IF NOT EXISTS` nao
# altera uma tabela existente, entao um graph.db antigo precisa ganha-las aqui — do
# contrario a usuaria perderia o grafo que ja montou.
_ADDED_COLUMNS = {"papers": [("field", "TEXT"), ("subfield", "TEXT")]}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def upsert_paper(conn: sqlite3.Connection, paper: dict) -> None:
    """Insere ou atualiza. `depth` sempre converge para o menor valor visto:
    o mesmo artigo pode ser alcancado por caminhos de comprimentos diferentes e o
    que importa para o layout e a distancia minima ate o seminal."""
    conn.execute(
        """
        INSERT INTO papers (id, doi, title, year, cited_by_count, abstract, venue,
                            landing_url, oa_url, authors, topics, field, subfield,
                            depth, fetched_at)
        VALUES (:id, :doi, :title, :year, :cited_by_count, :abstract, :venue,
                :landing_url, :oa_url, :authors, :topics, :field, :subfield,
                :depth, :fetched_at)
        ON CONFLICT(id) DO UPDATE SET
            doi            = excluded.doi,
            title          = excluded.title,
            year           = excluded.year,
            cited_by_count = excluded.cited_by_count,
            abstract       = COALESCE(NULLIF(excluded.abstract, ''), papers.abstract),
            venue          = excluded.venue,
            landing_url    = excluded.landing_url,
            oa_url         = excluded.oa_url,
            authors        = excluded.authors,
            topics         = excluded.topics,
            field          = excluded.field,
            subfield       = excluded.subfield,
            depth          = MIN(papers.depth, excluded.depth),
            fetched_at     = excluded.fetched_at
        """,
        {
            "id": paper["id"],
            "doi": paper.get("doi"),
            "title": paper.get("title") or "(sem titulo)",
            "year": paper.get("year"),
            "cited_by_count": paper.get("cited_by_count") or 0,
            "abstract": paper.get("abstract") or "",
            "venue": paper.get("venue"),
            "landing_url": paper.get("landing_url"),
            "oa_url": paper.get("oa_url"),
            "authors": json.dumps(paper.get("authors") or [], ensure_ascii=False),
            "topics": json.dumps(paper.get("topics") or [], ensure_ascii=False),
            "field": paper.get("field"),
            "subfield": paper.get("subfield"),
            "depth": paper.get("depth") or 0,
            "fetched_at": _now(),
        },
    )
    conn.commit()


def add_edge(conn: sqlite3.Connection, citing_id: str, cited_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO edges (citing_id, cited_id) VALUES (?, ?)",
        (citing_id, cited_id),
    )
    conn.commit()


def record_expansion(
    conn: sqlite3.Connection,
    paper_id: str,
    top_n: int,
    min_year: int | None,
    total_citers: int,
) -> None:
    conn.execute(
        """
        INSERT INTO expansions (paper_id, top_n, min_year, total_citers, done_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(paper_id, top_n, min_year) DO UPDATE SET
            total_citers = excluded.total_citers,
            done_at      = excluded.done_at
        """,
        (paper_id, top_n, _year_key(min_year), total_citers, _now()),
    )
    conn.commit()


def is_expanded(
    conn: sqlite3.Connection, paper_id: str, top_n: int, min_year: int | None
) -> bool:
    """Uma expansao anterior conta apenas se cobriu ao menos o mesmo `top_n` com o
    MESMO filtro de ano. Filtros de ano diferentes nao se contem: o top-20 sem filtro
    pode nao conter o top-20 de >=2020."""
    row = conn.execute(
        """
        SELECT 1 FROM expansions
        WHERE paper_id = ? AND top_n >= ? AND min_year = ?
        LIMIT 1
        """,
        (paper_id, top_n, _year_key(min_year)),
    ).fetchone()
    return row is not None


def get_paper(conn: sqlite3.Connection, paper_id: str) -> dict | None:
    row = conn.execute(
        """
        SELECT p.*,
               COALESCE(m.status, 'unread')         AS mark,
               COALESCE(m.note, '')                 AS note,
               (SELECT MAX(total_citers) FROM expansions e
                 WHERE e.paper_id = p.id)           AS total_citers,
               NOT EXISTS (SELECT 1 FROM expansions e
                            WHERE e.paper_id = p.id) AS frontier
          FROM papers p
          LEFT JOIN marks m ON m.paper_id = p.id
         WHERE p.id = ?
        """,
        (paper_id,),
    ).fetchone()
    return _paper_row(row) if row else None


def _paper_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["authors"] = json.loads(d["authors"])
    d["topics"] = json.loads(d["topics"])
    d["frontier"] = bool(d["frontier"])
    return d


def get_graph(conn: sqlite3.Connection) -> dict:
    """Payload leve para o canvas: o suficiente para desenhar e filtrar, sem
    abstracts. O detalhe completo vem de `get_paper` no clique."""
    nodes = [
        {
            "id": r["id"],
            "title": r["title"],
            "year": r["year"],
            "cited_by_count": r["cited_by_count"],
            "depth": r["depth"],
            "topics": json.loads(r["topics"]),
            "field": r["field"],
            "subfield": r["subfield"],
            "mark": r["mark"],
            "frontier": bool(r["frontier"]),
            "total_citers": r["total_citers"],
        }
        for r in conn.execute(
            """
            SELECT p.id, p.title, p.year, p.cited_by_count, p.depth, p.topics,
                   p.field, p.subfield,
                   COALESCE(m.status, 'unread')         AS mark,
                   (SELECT MAX(total_citers) FROM expansions e
                     WHERE e.paper_id = p.id)           AS total_citers,
                   NOT EXISTS (SELECT 1 FROM expansions e
                                WHERE e.paper_id = p.id) AS frontier
              FROM papers p
              LEFT JOIN marks m ON m.paper_id = p.id
             ORDER BY p.depth, p.cited_by_count DESC
            """
        )
    ]
    edges = [
        {"citing": r["citing_id"], "cited": r["cited_id"]}
        for r in conn.execute(
            "SELECT citing_id, cited_id FROM edges ORDER BY cited_id, citing_id"
        )
    ]
    return {"nodes": nodes, "edges": edges}


def set_mark(conn: sqlite3.Connection, paper_id: str, status: str, note: str = "") -> None:
    if status not in MARKS:
        raise ValueError(f"marca invalida: {status!r}; esperado um de {MARKS}")
    conn.execute(
        """
        INSERT INTO marks (paper_id, status, note, updated_at) VALUES (?, ?, ?, ?)
        ON CONFLICT(paper_id) DO UPDATE SET
            status = excluded.status,
            note = excluded.note,
            updated_at = excluded.updated_at
        """,
        (paper_id, status, note, _now()),
    )
    conn.commit()
