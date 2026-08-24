"""Persistencia em SQLite.

O schema separa FATO de RECORTE:

- `papers` e `edges` sao globais. "Este artigo tem este titulo" e "X citou Y" nao
  dependem da pergunta que se esta fazendo, e manter o cache global e o que torna
  instantaneo alternar entre investigacoes ou reaproveitar um artigo caro de buscar.
- `members`, `expansions` e o proprio `investigations` sao por investigacao: a
  profundidade de um artigo e a distancia ate AQUELE seminal, e o que ja foi escavado
  depende da pergunta.
- `marks` e global de proposito: "interessa" quer dizer "interessa para minha tese",
  nao "interessa para esta consulta". Reencontrar um artigo ja marcado ao abrir outra
  investigacao e justamente o sinal de sobreposicao entre as duas.

Direcao das arestas: `citing_id` -> `cited_id`, ou seja, a aresta aponta para tras no
tempo. O crawl caminha ao contrario dela, do seminal para quem o citou.
"""

from __future__ import annotations

import json
import sqlite3
from collections import deque
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
    fetched_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS edges (
    citing_id  TEXT NOT NULL,
    cited_id   TEXT NOT NULL,
    PRIMARY KEY (citing_id, cited_id)
);
CREATE INDEX IF NOT EXISTS idx_edges_cited ON edges(cited_id);

CREATE TABLE IF NOT EXISTS investigations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    seed_id     TEXT NOT NULL,
    top_n       INTEGER NOT NULL DEFAULT 20,
    min_year    INTEGER,
    created_at  TEXT NOT NULL
);

-- Quais artigos pertencem a uma investigacao, e a que distancia do seminal dela.
CREATE TABLE IF NOT EXISTS members (
    investigation_id  INTEGER NOT NULL,
    paper_id          TEXT NOT NULL,
    depth             INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (investigation_id, paper_id)
);
CREATE INDEX IF NOT EXISTS idx_members_inv ON members(investigation_id);

-- `min_year` guarda 0 para "sem filtro", nunca NULL: o SQLite considera NULLs
-- distintos entre si em chaves primarias, o que faria ON CONFLICT nunca disparar e
-- acumular linhas duplicadas. Traduzido por _year_key nas fronteiras.
CREATE TABLE IF NOT EXISTS expansions (
    investigation_id  INTEGER NOT NULL,
    paper_id          TEXT NOT NULL,
    top_n             INTEGER NOT NULL,
    min_year          INTEGER NOT NULL DEFAULT 0,
    total_citers      INTEGER NOT NULL DEFAULT 0,
    done_at           TEXT NOT NULL,
    PRIMARY KEY (investigation_id, paper_id, top_n, min_year)
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


# --------------------------------------------------------------------- migracao

# Colunas acrescentadas depois do primeiro schema. `CREATE TABLE IF NOT EXISTS` nao
# altera uma tabela existente, entao um graph.db antigo precisa ganha-las aqui — do
# contrario a usuaria perderia o grafo que ja montou.
_ADDED_COLUMNS = {"papers": [("field", "TEXT"), ("subfield", "TEXT")]}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        existing = _columns(conn, table)
        for name, decl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    _adopt_legacy_graph(conn)
    _split_merged_imports(conn)


def _adopt_legacy_graph(conn: sqlite3.Connection) -> None:
    """Converte um banco pre-investigacoes num conjunto de investigacoes normais, sem
    perder nada nem baixar nada de novo.

    Um banco antigo podia acumular VARIAS buscas no mesmo grafo — era a queixa que
    motivou as investigacoes. As arestas guardam de qual seminal cada artigo desceu,
    entao aqui as buscas sao reconstruidas: uma investigacao por artigo que estava na
    profundidade 0, com os membros e as profundidades recalculados por alcance.

    Roda no maximo uma vez: depois da conversao `papers.depth` deixa de existir, e a
    guarda abaixo barra."""
    if "depth" not in _columns(conn, "papers"):
        return  # banco ja no formato novo
    if conn.execute("SELECT 1 FROM investigations LIMIT 1").fetchone():
        return  # ja convertido antes
    if not conn.execute("SELECT COUNT(*) AS n FROM papers").fetchone()["n"]:
        # Banco antigo mas vazio: nao inventa uma investigacao fantasma.
        _drop_legacy_depth(conn)
        return

    legacy_depth = {
        r["id"]: r["depth"] for r in conn.execute("SELECT id, depth FROM papers")
    }
    titles = {r["id"]: r["title"] for r in conn.execute("SELECT id, title FROM papers")}

    seeds = [pid for pid, d in legacy_depth.items() if d == 0]
    if not seeds:
        # Grafo sem raiz declarada: o mais citado e a melhor aposta de seminal.
        seeds = [
            conn.execute(
                "SELECT id FROM papers ORDER BY cited_by_count DESC LIMIT 1"
            ).fetchone()["id"]
        ]

    top_n = _legacy_top_n(conn)
    cones = {seed: _cone_from(conn, seed) for seed in sorted(seeds)}

    # Nenhum artigo pode desaparecer da interface por a migracao nao saber de onde veio:
    # o que nenhum cone alcanca entra no primeiro cone, na profundidade que ja tinha.
    reached = set().union(*cones.values()) if cones else set()
    for pid in legacy_depth:
        if pid not in reached:
            cones[sorted(seeds)[0]][pid] = legacy_depth[pid]

    if "investigation_id" not in _columns(conn, "expansions"):
        _rescope_legacy_expansions(conn)

    for seed, members in cones.items():
        inv = create_investigation(
            conn,
            name=f"{titles.get(seed) or seed} (importado)",
            seed_id=seed,
            top_n=top_n,
        )
        conn.executemany(
            "INSERT OR REPLACE INTO members (investigation_id, paper_id, depth) VALUES (?, ?, ?)",
            [(inv, pid, depth) for pid, depth in members.items()],
        )
        # A cobertura ja buscada vale para toda investigacao que contenha o artigo:
        # foi paga uma vez, nao precisa ser paga de novo.
        conn.execute(
            """
            INSERT OR REPLACE INTO expansions
                (investigation_id, paper_id, top_n, min_year, total_citers, done_at)
            SELECT ?, paper_id, top_n, min_year, total_citers, done_at
              FROM legacy_expansions
             WHERE paper_id IN (SELECT paper_id FROM members WHERE investigation_id = ?)
            """,
            (inv, inv),
        )

    conn.execute("DROP TABLE IF EXISTS legacy_expansions")
    _drop_legacy_depth(conn)
    conn.commit()


def _split_merged_imports(conn: sqlite3.Connection) -> None:
    """Separa investigacoes que carregam mais de uma busca.

    Uma investigacao sadia tem exatamente UM membro na profundidade 0 — o crawler so
    marca zero para o seminal. Mais de um significa que varias buscas foram fundidas
    (era o comportamento de uma versao anterior da migracao), e o reparo e o mesmo
    algoritmo de cones. Depois de rodar, cada investigacao tem um seminal so, e a
    condicao deixa de valer: nao repete."""
    merged = [
        r["investigation_id"]
        for r in conn.execute(
            """
            SELECT investigation_id FROM members WHERE depth = 0
             GROUP BY investigation_id HAVING COUNT(*) > 1
            """
        )
    ]
    if not merged:
        return

    titles = {r["id"]: r["title"] for r in conn.execute("SELECT id, title FROM papers")}
    for old in merged:
        info = get_investigation(conn, old) or {}
        members = {
            r["paper_id"]: r["depth"]
            for r in conn.execute(
                "SELECT paper_id, depth FROM members WHERE investigation_id = ?", (old,)
            )
        }
        coverage = [
            dict(r)
            for r in conn.execute("SELECT * FROM expansions WHERE investigation_id = ?", (old,))
        ]
        seeds = sorted(pid for pid, d in members.items() if d == 0)

        # Restringe cada cone aos artigos que ja estavam nesta investigacao: o cache
        # global pode ter artigos de outras buscas, e eles nao pertencem aqui.
        cones = {seed: _cone_from(conn, seed, limit=set(members)) for seed in seeds}
        reached = set().union(*cones.values())
        for pid, depth in members.items():
            if pid not in reached:
                cones[seeds[0]][pid] = depth

        for seed, cone in cones.items():
            inv = create_investigation(
                conn,
                name=f"{titles.get(seed) or seed} (separado)",
                seed_id=seed,
                top_n=info.get("top_n") or 20,
                min_year=info.get("min_year"),
            )
            conn.executemany(
                "INSERT OR REPLACE INTO members (investigation_id, paper_id, depth) "
                "VALUES (?, ?, ?)",
                [(inv, pid, depth) for pid, depth in cone.items()],
            )
            conn.executemany(
                """
                INSERT OR REPLACE INTO expansions
                    (investigation_id, paper_id, top_n, min_year, total_citers, done_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (inv, e["paper_id"], e["top_n"], e["min_year"], e["total_citers"], e["done_at"])
                    for e in coverage
                    if e["paper_id"] in cone
                ],
            )

        conn.execute("DELETE FROM members WHERE investigation_id = ?", (old,))
        conn.execute("DELETE FROM expansions WHERE investigation_id = ?", (old,))
        conn.execute("DELETE FROM investigations WHERE id = ?", (old,))

    conn.commit()


def _cone_from(
    conn: sqlite3.Connection, seed: str, limit: set[str] | None = None
) -> dict[str, int]:
    """Quem desce deste seminal, e a que distancia — BFS pelas arestas ao contrario
    (do citado para quem o citou), que e o sentido em que o crawl caminha.

    `limit`, quando dado, restringe o cone a esse conjunto de artigos."""
    citers: dict[str, list[str]] = {}
    for r in conn.execute("SELECT citing_id, cited_id FROM edges"):
        if limit is not None and r["citing_id"] not in limit:
            continue
        citers.setdefault(r["cited_id"], []).append(r["citing_id"])

    depth = {seed: 0}
    queue = deque([seed])
    while queue:
        node = queue.popleft()
        for citer in citers.get(node, ()):
            if citer not in depth:
                depth[citer] = depth[node] + 1
                queue.append(citer)
    return depth


def _legacy_top_n(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(top_n) AS n FROM expansions").fetchone()
    return int(row["n"]) if row and row["n"] else 20


def _rescope_legacy_expansions(conn: sqlite3.Connection) -> None:
    """`expansions` ganhou uma coluna na chave primaria, e o SQLite nao altera chave
    primaria no lugar: guarda as linhas antigas em `legacy_expansions` e recria a
    tabela no formato novo, para cada investigacao herdar a cobertura que lhe cabe."""
    conn.executescript(
        """
        ALTER TABLE expansions RENAME TO legacy_expansions;
        CREATE TABLE expansions (
            investigation_id  INTEGER NOT NULL,
            paper_id          TEXT NOT NULL,
            top_n             INTEGER NOT NULL,
            min_year          INTEGER NOT NULL DEFAULT 0,
            total_citers      INTEGER NOT NULL DEFAULT 0,
            done_at           TEXT NOT NULL,
            PRIMARY KEY (investigation_id, paper_id, top_n, min_year)
        );
        """
    )


def _drop_legacy_depth(conn: sqlite3.Connection) -> None:
    """`papers.depth` virou `members.depth`. Remover a coluna e o que impede a migracao
    de rodar de novo na proxima abertura."""
    conn.execute("ALTER TABLE papers DROP COLUMN depth")


# ---------------------------------------------------------------- investigacoes


def create_investigation(
    conn: sqlite3.Connection,
    name: str,
    seed_id: str,
    top_n: int = 20,
    min_year: int | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO investigations (name, seed_id, top_n, min_year, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (name.strip() or "sem nome", seed_id, top_n, min_year, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


_INVESTIGATION_SELECT = """
    SELECT i.*,
           COALESCE(p.title, '(seminal fora do cache)') AS seed_title,
           (SELECT COUNT(*) FROM members m
             WHERE m.investigation_id = i.id)           AS nodes
      FROM investigations i
      LEFT JOIN papers p ON p.id = i.seed_id
"""


def list_investigations(conn: sqlite3.Connection) -> list[dict]:
    """Mais recente primeiro: e quase sempre a que a usuaria quer retomar."""
    return [
        dict(r)
        for r in conn.execute(f"{_INVESTIGATION_SELECT} ORDER BY i.created_at DESC, i.id DESC")
    ]


def get_investigation(conn: sqlite3.Connection, investigation_id: int) -> dict | None:
    row = conn.execute(f"{_INVESTIGATION_SELECT} WHERE i.id = ?", (investigation_id,)).fetchone()
    return dict(row) if row else None


def rename_investigation(conn: sqlite3.Connection, investigation_id: int, name: str) -> None:
    cur = conn.execute(
        "UPDATE investigations SET name = ? WHERE id = ?",
        (name.strip() or "sem nome", investigation_id),
    )
    if not cur.rowcount:
        raise KeyError(f"investigacao {investigation_id} nao existe")
    conn.commit()


def delete_investigation(conn: sqlite3.Connection, investigation_id: int) -> None:
    """Remove a investigacao, sua composicao e suas expansoes.

    `papers`, `edges` e `marks` ficam: sao cache e triagem, custaram requisicoes e
    valem para as outras investigacoes."""
    if get_investigation(conn, investigation_id) is None:
        raise KeyError(f"investigacao {investigation_id} nao existe")
    conn.execute("DELETE FROM members WHERE investigation_id = ?", (investigation_id,))
    conn.execute("DELETE FROM expansions WHERE investigation_id = ?", (investigation_id,))
    conn.execute("DELETE FROM investigations WHERE id = ?", (investigation_id,))
    conn.commit()


# ------------------------------------------------------------------- artigos


def upsert_paper(conn: sqlite3.Connection, paper: dict) -> None:
    """Grava o artigo no cache GLOBAL. A profundidade nao entra aqui: ela depende da
    investigacao e mora em `members` (ver `add_member`)."""
    conn.execute(
        """
        INSERT INTO papers (id, doi, title, year, cited_by_count, abstract, venue,
                            landing_url, oa_url, authors, topics, field, subfield,
                            fetched_at)
        VALUES (:id, :doi, :title, :year, :cited_by_count, :abstract, :venue,
                :landing_url, :oa_url, :authors, :topics, :field, :subfield,
                :fetched_at)
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
            "fetched_at": _now(),
        },
    )
    conn.commit()


def add_member(
    conn: sqlite3.Connection, investigation_id: int, paper_id: str, depth: int
) -> None:
    """Inclui o artigo na investigacao. `depth` converge para o MENOR valor visto: o
    mesmo artigo pode ser alcancado por caminhos de comprimentos diferentes, e o que
    importa para o layout e a distancia minima ate o seminal."""
    conn.execute(
        """
        INSERT INTO members (investigation_id, paper_id, depth) VALUES (?, ?, ?)
        ON CONFLICT(investigation_id, paper_id) DO UPDATE SET
            depth = MIN(members.depth, excluded.depth)
        """,
        (investigation_id, paper_id, int(depth)),
    )
    conn.commit()


def add_edge(conn: sqlite3.Connection, citing_id: str, cited_id: str) -> None:
    """Aresta global: "X citou Y" e fato, e vale para qualquer investigacao."""
    conn.execute(
        "INSERT OR IGNORE INTO edges (citing_id, cited_id) VALUES (?, ?)",
        (citing_id, cited_id),
    )
    conn.commit()


# ------------------------------------------------------------------ expansoes


def record_expansion(
    conn: sqlite3.Connection,
    investigation_id: int,
    paper_id: str,
    top_n: int,
    min_year: int | None,
    total_citers: int,
) -> None:
    conn.execute(
        """
        INSERT INTO expansions (investigation_id, paper_id, top_n, min_year,
                                total_citers, done_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(investigation_id, paper_id, top_n, min_year) DO UPDATE SET
            total_citers = excluded.total_citers,
            done_at      = excluded.done_at
        """,
        (investigation_id, paper_id, top_n, _year_key(min_year), total_citers, _now()),
    )
    conn.commit()


def is_expanded(
    conn: sqlite3.Connection,
    investigation_id: int,
    paper_id: str,
    top_n: int,
    min_year: int | None,
) -> bool:
    """Uma expansao anterior conta apenas se foi NESTA investigacao e cobriu ao menos o
    mesmo `top_n` com o MESMO filtro de ano. Filtros de ano diferentes nao se contem: o
    top-20 sem filtro pode nao conter o top-20 de >=2020."""
    row = conn.execute(
        """
        SELECT 1 FROM expansions
        WHERE investigation_id = ? AND paper_id = ? AND top_n >= ? AND min_year = ?
        LIMIT 1
        """,
        (investigation_id, paper_id, top_n, _year_key(min_year)),
    ).fetchone()
    return row is not None


# --------------------------------------------------------------------- leitura

# Profundidade, cobertura e "e fronteira?" dependem da investigacao; quando nenhuma e
# informada, `get_paper` devolve so o que e global (o cache do artigo e a marca).
_SCOPED_FIELDS = """
           mb.depth                                     AS depth,
           (SELECT MAX(total_citers) FROM expansions e
             WHERE e.paper_id = p.id
               AND e.investigation_id = mb.investigation_id) AS total_citers,
           NOT EXISTS (SELECT 1 FROM expansions e
                        WHERE e.paper_id = p.id
                          AND e.investigation_id = mb.investigation_id) AS frontier
"""


def get_paper(
    conn: sqlite3.Connection, paper_id: str, investigation_id: int | None = None
) -> dict | None:
    if investigation_id is None:
        row = conn.execute(
            """
            SELECT p.*,
                   COALESCE(m.status, 'unread') AS mark,
                   COALESCE(m.note, '')         AS note,
                   NULL AS depth, NULL AS total_citers, 1 AS frontier
              FROM papers p
              LEFT JOIN marks m ON m.paper_id = p.id
             WHERE p.id = ?
            """,
            (paper_id,),
        ).fetchone()
    else:
        row = conn.execute(
            f"""
            SELECT p.*,
                   COALESCE(m.status, 'unread') AS mark,
                   COALESCE(m.note, '')         AS note,
                   {_SCOPED_FIELDS}
              FROM papers p
              JOIN members mb ON mb.paper_id = p.id AND mb.investigation_id = ?
              LEFT JOIN marks m ON m.paper_id = p.id
             WHERE p.id = ?
            """,
            (investigation_id, paper_id),
        ).fetchone()
    return _paper_row(row) if row else None


def _paper_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["authors"] = json.loads(d["authors"])
    d["topics"] = json.loads(d["topics"])
    d["frontier"] = bool(d["frontier"])
    return d


def get_graph(conn: sqlite3.Connection, investigation_id: int) -> dict:
    """Payload leve para o canvas, restrito a uma investigacao: o suficiente para
    desenhar e filtrar, sem abstracts. O detalhe completo vem de `get_paper` no clique.

    Sao arestas do grafo apenas as que ligam DOIS membros: uma citacao que sai para
    fora do recorte nao tem onde ser desenhada."""
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
            f"""
            SELECT p.id, p.title, p.year, p.cited_by_count, p.topics,
                   p.field, p.subfield,
                   COALESCE(m.status, 'unread') AS mark,
                   {_SCOPED_FIELDS}
              FROM members mb
              JOIN papers p ON p.id = mb.paper_id
              LEFT JOIN marks m ON m.paper_id = p.id
             WHERE mb.investigation_id = ?
             ORDER BY mb.depth, p.cited_by_count DESC
            """,
            (investigation_id,),
        )
    ]
    edges = [
        {"citing": r["citing_id"], "cited": r["cited_id"]}
        for r in conn.execute(
            """
            SELECT e.citing_id, e.cited_id
              FROM edges e
              JOIN members a ON a.paper_id = e.citing_id AND a.investigation_id = ?
              JOIN members b ON b.paper_id = e.cited_id  AND b.investigation_id = ?
             ORDER BY e.cited_id, e.citing_id
            """,
            (investigation_id, investigation_id),
        )
    ]
    return {"nodes": nodes, "edges": edges}


def citers_in(conn: sqlite3.Connection, investigation_id: int, paper_id: str) -> list[str]:
    """Quem, dentro desta investigacao, cita `paper_id`."""
    return [
        r["citing_id"]
        for r in conn.execute(
            """
            SELECT e.citing_id FROM edges e
              JOIN members m ON m.paper_id = e.citing_id AND m.investigation_id = ?
             WHERE e.cited_id = ?
            """,
            (investigation_id, paper_id),
        )
    ]


def set_mark(conn: sqlite3.Connection, paper_id: str, status: str, note: str = "") -> None:
    """Marca GLOBAL, de proposito — ver o cabecalho do modulo."""
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
