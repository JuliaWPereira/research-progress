"""Travessia do grafo de citacoes.

BFS a partir do artigo seminal, sempre PARA FRENTE no tempo: os vizinhos de um no sao
os artigos que o citaram. O orcamento (`depth`, `top_n`, `max_nodes`) existe porque o
grafo completo nao e alcancavel — um seminal com 10 mil citacoes gera milhoes de nos
em profundidade 2. `top_n` ordenado por citacoes transforma esse corte em algo util:
traz o canone da area em vez de uma amostra arbitraria.

`total_citers` guarda quantos citantes existiam de verdade, para a interface poder
dizer "20 de 1240" e nunca fingir completude.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from app import db


class CitationSource(Protocol):
    async def fetch_citers(
        self, paper_id: str, top_n: int, min_year: int | None, depth: int
    ) -> tuple[list[dict], int]: ...


@dataclass
class CrawlParams:
    depth: int = 2
    top_n: int = 20
    min_year: int | None = None
    max_nodes: int = 500


ProgressFn = Callable[[dict[str, Any]], None]


async def crawl(
    conn,
    source: CitationSource,
    seed: dict,
    params: CrawlParams | None = None,
    on_progress: ProgressFn | None = None,
) -> dict:
    """Percorre as citacoes a partir de `seed` e grava tudo no banco.

    Devolve {"nodes", "edges", "expanded", "truncated"}. `truncated` diz se o
    orcamento `max_nodes` interrompeu a travessia — a interface precisa disso para
    nao apresentar um grafo cortado como se fosse completo."""
    params = params or CrawlParams()

    seed_id = seed["id"]
    db.upsert_paper(conn, {**seed, "depth": 0})

    seen: set[str] = {seed_id}
    queue: deque[tuple[str, int]] = deque([(seed_id, 0)])
    expanded = 0
    new_edges = 0
    truncated = False

    def report(**extra):
        if on_progress:
            on_progress(
                {
                    "nodes": len(seen),
                    "edges": new_edges,
                    "expanded": expanded,
                    "queued": len(queue),
                    "done": False,
                    **extra,
                }
            )

    while queue:
        paper_id, depth = queue.popleft()

        if depth >= params.depth:
            continue
        if len(seen) >= params.max_nodes:
            truncated = True
            break

        if db.is_expanded(conn, paper_id, params.top_n, params.min_year):
            # Ja buscado antes com cobertura igual ou maior. Reaproveita o que esta no
            # banco em vez de gastar uma requisicao.
            for citer_id in _known_citers(conn, paper_id):
                if citer_id not in seen:
                    seen.add(citer_id)
                    queue.append((citer_id, depth + 1))
            continue

        citers, total = await source.fetch_citers(
            paper_id, top_n=params.top_n, min_year=params.min_year, depth=depth + 1
        )
        expanded += 1
        db.record_expansion(conn, paper_id, params.top_n, params.min_year, total)

        for citer in citers:
            citer_id = citer["id"]
            db.upsert_paper(conn, {**citer, "depth": depth + 1})
            db.add_edge(conn, citing_id=citer_id, cited_id=paper_id)
            new_edges += 1
            if citer_id not in seen:
                seen.add(citer_id)
                queue.append((citer_id, depth + 1))
            if len(seen) >= params.max_nodes:
                truncated = True
                break

        report(title=citers[0]["title"] if citers else None, depth=depth + 1)
        if truncated:
            break

    result = {
        "nodes": len(seen),
        "edges": new_edges,
        "expanded": expanded,
        "truncated": truncated,
    }
    if on_progress:
        on_progress({**result, "queued": 0, "done": True, "title": None, "depth": params.depth})
    return result


def _known_citers(conn, paper_id: str) -> list[str]:
    return [
        r["citing_id"]
        for r in conn.execute("SELECT citing_id FROM edges WHERE cited_id = ?", (paper_id,))
    ]


async def expand(
    conn, source: CitationSource, paper_id: str, top_n: int, min_year: int | None
) -> dict:
    """Cresce UM no em UM nivel — o que o botao "expandir" no painel chama.

    A profundidade dos novos nos e a do no expandido mais um; `db.upsert_paper` mantem
    o menor valor caso um deles ja seja conhecido por um caminho mais curto."""
    parent = db.get_paper(conn, paper_id)
    if parent is None:
        raise KeyError(f"{paper_id} nao esta no grafo")

    known = set(_known_citers(conn, paper_id))
    citers, total = await source.fetch_citers(
        paper_id, top_n=top_n, min_year=min_year, depth=parent["depth"] + 1
    )
    db.record_expansion(conn, paper_id, top_n, min_year, total)

    new_nodes = 0
    for citer in citers:
        existed = db.get_paper(conn, citer["id"]) is not None
        db.upsert_paper(conn, {**citer, "depth": parent["depth"] + 1})
        db.add_edge(conn, citing_id=citer["id"], cited_id=paper_id)
        if not existed:
            new_nodes += 1

    return {
        "new_nodes": new_nodes,
        "new_edges": len([c for c in citers if c["id"] not in known]),
        "total_citers": total,
        "shown": len(citers),
    }
