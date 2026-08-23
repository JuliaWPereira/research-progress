"""Testes que batem na API de verdade. Fora da suite padrao.

Rodar com:  pytest -m network

Servem para pegar quebra de contrato do OpenAlex: as fixtures congelam o formato de
hoje, e este arquivo verifica se o formato de hoje ainda e o de amanha.
"""

import pytest

from app import crawl as crawl_mod
from app import db, openalex, s2

pytestmark = pytest.mark.network

MAILTO = "julia.pereira@korza.ai"
SEED_DOI = "10.1038/nature14539"  # LeCun, Bengio, Hinton — "Deep learning" (2015)
# O OpenAlex nao tem abstract para todo registro (o proprio SEED_DOI e um caso), e o
# Semantic Scholar tambem nao conhece esse DOI. Este DOI serve para verificar que o
# fallback em si continua funcionando.
S2_KNOWN_DOI = "10.1038/s41586-021-03819-2"  # AlphaFold, Nature 2021


@pytest.fixture
async def client():
    async with openalex.OpenAlexClient(mailto=MAILTO) as c:
        yield c


async def test_resolving_a_real_doi_returns_the_expected_paper(client):
    result = await client.resolve(SEED_DOI)

    paper = result["paper"]
    assert paper["title"].lower().startswith("deep learning")
    assert paper["year"] == 2015
    assert paper["cited_by_count"] > 10000
    assert any("LeCun" in a["name"] for a in paper["authors"])
    assert paper["topics"], "o OpenAlex parou de classificar este artigo em topicos"


async def test_most_citers_come_with_an_abstract_from_openalex(client):
    """A cobertura de abstract do OpenAlex e parcial (~2/3 na amostra que medimos).

    Este teste nao exige perfeicao: existe para avisar se a cobertura desabar, porque
    e o resumo que sustenta a triagem."""
    seed = (await client.resolve(SEED_DOI))["paper"]
    citers, _ = await client.fetch_citers(seed["id"], top_n=25, min_year=None, depth=1)

    with_abstract = [c for c in citers if c["abstract"]]
    assert len(with_abstract) >= len(citers) // 2, (
        f"so {len(with_abstract)}/{len(citers)} citantes vieram com abstract; "
        "a cobertura do OpenAlex caiu"
    )


async def test_fetching_real_citers_returns_them_sorted_and_with_a_true_total(client):
    seed = (await client.resolve(SEED_DOI))["paper"]

    citers, total = await client.fetch_citers(seed["id"], top_n=10, min_year=None, depth=1)

    assert total > 1000
    assert len(citers) == 10
    counts = [c["cited_by_count"] for c in citers]
    assert counts == sorted(counts, reverse=True)
    assert all(c["year"] >= seed["year"] - 1 for c in citers), "citante mais antigo que o citado"


async def test_the_year_filter_really_narrows_the_result(client):
    seed = (await client.resolve(SEED_DOI))["paper"]

    _, all_time = await client.fetch_citers(seed["id"], top_n=1, min_year=None, depth=1)
    recent, since = await client.fetch_citers(seed["id"], top_n=5, min_year=2024, depth=1)

    assert since < all_time
    assert all(c["year"] >= 2024 for c in recent)


async def test_free_text_search_finds_a_well_known_paper(client):
    result = await client.resolve("Attention is all you need")

    assert result["paper"] is None
    assert result["candidates"][0]["title"].lower() == "attention is all you need"


async def test_an_end_to_end_crawl_builds_a_connected_graph(tmp_path, client):
    conn = db.connect(tmp_path / "network.db")
    seed = (await client.resolve(SEED_DOI))["paper"]

    result = await crawl_mod.crawl(
        conn, client, seed, crawl_mod.CrawlParams(depth=2, top_n=5, max_nodes=60)
    )

    graph = db.get_graph(conn)
    assert result["nodes"] > 5
    assert len(graph["nodes"]) == result["nodes"]
    assert graph["edges"], "o crawl nao criou nenhuma aresta"

    # Todo no que nao seja o seminal precisa ter ao menos uma aresta: chegou ali
    # citando alguem.
    cited_or_citing = {e["citing"] for e in graph["edges"]} | {
        e["cited"] for e in graph["edges"]
    }
    orphans = {n["id"] for n in graph["nodes"]} - cited_or_citing
    assert not orphans, f"nos desconectados: {orphans}"

    # A profundidade e coerente: ninguem pula um nivel.
    assert {n["depth"] for n in graph["nodes"]} <= {0, 1, 2}
    conn.close()


async def test_semantic_scholar_still_answers_for_a_doi_it_knows():
    text = await s2.fetch_abstract(S2_KNOWN_DOI)

    assert text, "o fallback do Semantic Scholar parou de funcionar"
    assert len(text) > 200


async def test_semantic_scholar_returns_nothing_rather_than_the_wrong_paper():
    """O S2 nao conhece todo DOI, e isso tem de virar vazio, nao um palpite.

    Uma busca por titulo casaria com artigos homonimos e colaria o resumo errado no
    no — pior que resumo faltando, justamente na hora de decidir se o artigo importa."""
    assert await s2.fetch_abstract("10.9999/definitivamente-nao-existe") == ""
