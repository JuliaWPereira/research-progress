import json
from pathlib import Path

import httpx
import pytest
import respx

from app import openalex as oa

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def client():
    return oa.OpenAlexClient(mailto="test@example.org", retry_base_delay=0)


# --------------------------------------------------------------------- abstract


def test_invert_abstract_reconstructs_word_order():
    assert oa.invert_abstract({"world": [1], "Hello": [0]}) == "Hello world"


def test_invert_abstract_handles_a_word_repeated_at_several_positions():
    assert oa.invert_abstract({"the": [0, 2], "cat": [1], "hat": [3]}) == "the cat the hat"


def test_invert_abstract_returns_empty_string_when_index_is_missing():
    assert oa.invert_abstract(None) == ""
    assert oa.invert_abstract({}) == ""


def test_invert_abstract_on_a_real_work_reads_as_prose():
    work = fixture("work_seed.json")
    text = oa.invert_abstract(work["abstract_inverted_index"])
    assert text.startswith("Despite growing interest in Open Access")
    assert "  " not in text


# ------------------------------------------------------------------ parse_input


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("W2626778328", ("openalex", "W2626778328")),
        ("  w2626778328  ", ("openalex", "W2626778328")),
        ("https://openalex.org/W2626778328", ("openalex", "W2626778328")),
        ("https://api.openalex.org/works/W2626778328", ("openalex", "W2626778328")),
        ("10.1038/nature14539", ("doi", "10.1038/nature14539")),
        ("https://doi.org/10.1038/nature14539", ("doi", "10.1038/nature14539")),
        ("http://dx.doi.org/10.1038/nature14539", ("doi", "10.1038/nature14539")),
        ("https://arxiv.org/abs/2005.14165", ("doi", "10.48550/arXiv.2005.14165")),
        ("https://arxiv.org/pdf/2005.14165v4", ("doi", "10.48550/arXiv.2005.14165")),
        ("Attention is all you need", ("search", "Attention is all you need")),
        ("https://example.org/some/paper", ("search", "https://example.org/some/paper")),
    ],
)
def test_parse_input_classifies_every_supported_form(raw, expected):
    assert oa.parse_input(raw) == expected


def test_parse_input_rejects_empty_input():
    with pytest.raises(ValueError):
        oa.parse_input("   ")


# --------------------------------------------------------------- normalize_work


def test_normalize_work_maps_a_real_work_onto_the_db_shape():
    p = oa.normalize_work(fixture("work_seed.json"), depth=0)

    assert p["id"] == "W2741809807"
    assert p["doi"] == "10.7717/peerj.4375"
    assert p["title"].startswith("The state of OA")
    assert p["year"] == 2018
    assert p["cited_by_count"] > 0
    assert p["venue"] == "PeerJ"
    assert p["landing_url"] == "https://doi.org/10.7717/peerj.4375"
    assert p["authors"][0] == {"name": "Heather Piwowar", "affiliation": "OpenAlex"}
    # O casing dos topicos do OpenAlex e inconsistente entre registros; guardamos
    # verbatim e o agrupamento na interface e case-insensitive.
    assert "Academic Publishing and Open Access" in p["topics"]
    assert "scientometrics and bibliometrics research" in p["topics"]
    assert p["abstract"].startswith("Despite growing interest")
    assert p["depth"] == 0


def test_normalize_work_keeps_all_three_levels_of_the_topic_hierarchy():
    """Os topicos do OpenAlex sao muito especificos e formam cauda longa; campo (~26)
    e subcampo (~250) sao as granularidades que agrupam um grafo inteiro em poucas
    categorias legiveis. Guardamos os tres para a granularidade ser escolhivel."""
    p = oa.normalize_work(fixture("work_seed.json"))

    assert p["field"] == "Decision Sciences"
    assert p["subfield"] == "Statistics, Probability and Uncertainty"
    assert p["topics"][0] == "scientometrics and bibliometrics research"


def test_normalize_work_falls_back_to_the_first_topic_for_the_hierarchy():
    work = {
        "id": "https://openalex.org/W1",
        "topics": [
            {
                "display_name": "T",
                "field": {"display_name": "Computer Science"},
                "subfield": {"display_name": "Artificial Intelligence"},
            }
        ],
    }
    p = oa.normalize_work(work)

    assert p["field"] == "Computer Science"
    assert p["subfield"] == "Artificial Intelligence"


def test_normalize_work_reports_no_hierarchy_when_the_work_is_unclassified():
    p = oa.normalize_work({"id": "https://openalex.org/W1"})

    assert p["field"] is None
    assert p["subfield"] is None


def test_normalize_work_survives_a_work_with_nothing_but_an_id():
    p = oa.normalize_work({"id": "https://openalex.org/W1"}, depth=2)

    assert p["id"] == "W1"
    assert p["doi"] is None
    assert p["title"] == "(sem titulo)"
    assert p["authors"] == []
    assert p["topics"] == []
    assert p["abstract"] == ""
    assert p["cited_by_count"] == 0
    assert p["depth"] == 2


def test_normalize_work_tolerates_an_authorship_without_institutions():
    work = {
        "id": "https://openalex.org/W1",
        "authorships": [{"author": {"display_name": "Solo Author"}, "institutions": []}],
    }
    assert oa.normalize_work(work)["authors"] == [{"name": "Solo Author", "affiliation": ""}]


# --------------------------------------------------------------------- resolve


@respx.mock
async def test_resolve_openalex_id_returns_a_single_paper(client):
    route = respx.get(url__startswith="https://api.openalex.org/works/W2741809807").mock(
        return_value=httpx.Response(200, json=fixture("work_seed.json"))
    )

    result = await client.resolve("W2741809807")

    assert result["paper"]["id"] == "W2741809807"
    assert result["candidates"] == []
    assert "mailto=test%40example.org" in str(route.calls[0].request.url)


@respx.mock
async def test_resolve_doi_asks_openalex_for_the_doi_url(client):
    route = respx.get(
        url__startswith="https://api.openalex.org/works/https://doi.org/10.7717/peerj.4375"
    ).mock(return_value=httpx.Response(200, json=fixture("work_seed.json")))

    result = await client.resolve("10.7717/peerj.4375")

    assert result["paper"]["doi"] == "10.7717/peerj.4375"
    assert route.called


@respx.mock
async def test_resolve_free_text_returns_candidates_for_the_user_to_pick(client):
    respx.get(url__startswith="https://api.openalex.org/works?").mock(
        return_value=httpx.Response(200, json=fixture("search_results.json"))
    )

    result = await client.resolve("Attention is all you need")

    assert result["paper"] is None
    assert len(result["candidates"]) == 5
    assert result["candidates"][0]["title"] == "Attention Is All You Need"


@respx.mock
async def test_resolve_raises_when_the_doi_is_unknown_to_openalex(client):
    respx.get(url__startswith="https://api.openalex.org/works/").mock(
        return_value=httpx.Response(404, json={"error": "Not found"})
    )

    with pytest.raises(oa.PaperNotFound):
        await client.resolve("10.9999/does-not-exist")


@respx.mock
async def test_resolve_arxiv_that_openalex_lacks_says_to_paste_the_title(client):
    respx.get(url__startswith="https://api.openalex.org/works/").mock(
        return_value=httpx.Response(404, json={"error": "Not found"})
    )

    with pytest.raises(oa.PaperNotFound, match="titulo"):
        await client.resolve("https://arxiv.org/abs/1706.03762")


@respx.mock
async def test_resolve_reports_no_match_for_a_search_with_no_results(client):
    respx.get(url__startswith="https://api.openalex.org/works?").mock(
        return_value=httpx.Response(200, json={"meta": {"count": 0}, "results": []})
    )

    with pytest.raises(oa.PaperNotFound):
        await client.resolve("zzzz nonexistent paper title zzzz")


# ----------------------------------------------------------------- fetch_citers


@respx.mock
async def test_fetch_citers_returns_papers_at_the_next_depth_and_the_true_total(client):
    route = respx.get(url__startswith="https://api.openalex.org/works?").mock(
        return_value=httpx.Response(200, json=fixture("citers_page.json"))
    )

    citers, total = await client.fetch_citers("W2741809807", top_n=5, min_year=None, depth=1)

    assert total == 1240
    assert len(citers) == 5
    assert citers[0]["id"] == "W3137875885"
    assert all(c["depth"] == 1 for c in citers)

    url = str(route.calls[0].request.url)
    assert "filter=cites%3AW2741809807" in url
    assert "sort=cited_by_count%3Adesc" in url
    assert "per_page=5" in url


@respx.mock
async def test_fetch_citers_sorts_by_citations_so_top_n_is_the_canon(client):
    respx.get(url__startswith="https://api.openalex.org/works?").mock(
        return_value=httpx.Response(200, json=fixture("citers_page.json"))
    )

    citers, _ = await client.fetch_citers("W2741809807", top_n=5, min_year=None, depth=1)

    counts = [c["cited_by_count"] for c in citers]
    assert counts == sorted(counts, reverse=True)


@respx.mock
async def test_fetch_citers_adds_a_date_filter_when_min_year_is_given(client):
    route = respx.get(url__startswith="https://api.openalex.org/works?").mock(
        return_value=httpx.Response(200, json=fixture("citers_filtered.json"))
    )

    citers, total = await client.fetch_citers("W2741809807", top_n=3, min_year=2023, depth=1)

    assert total == 525
    assert all(c["year"] >= 2023 for c in citers)
    assert "from_publication_date%3A2023-01-01" in str(route.calls[0].request.url)


@respx.mock
async def test_fetch_citers_caps_per_page_at_the_api_maximum(client):
    route = respx.get(url__startswith="https://api.openalex.org/works?").mock(
        return_value=httpx.Response(200, json=fixture("citers_page.json"))
    )

    await client.fetch_citers("W1", top_n=5000, min_year=None, depth=1)

    assert "per_page=200" in str(route.calls[0].request.url)


@respx.mock
async def test_fetch_citers_returns_empty_for_a_paper_nobody_cited(client):
    respx.get(url__startswith="https://api.openalex.org/works?").mock(
        return_value=httpx.Response(200, json={"meta": {"count": 0}, "results": []})
    )

    assert await client.fetch_citers("W1", top_n=20, min_year=None, depth=1) == ([], 0)


# ------------------------------------------------------------------ resiliencia


@respx.mock
async def test_a_rate_limited_request_is_retried_and_then_succeeds(client):
    route = respx.get(url__startswith="https://api.openalex.org/works?").mock(
        side_effect=[
            httpx.Response(429, json={"error": "slow down"}),
            httpx.Response(200, json=fixture("citers_page.json")),
        ]
    )

    _, total = await client.fetch_citers("W2741809807", top_n=5, min_year=None, depth=1)

    assert total == 1240
    assert route.call_count == 2


@respx.mock
async def test_repeated_server_errors_eventually_raise(client):
    respx.get(url__startswith="https://api.openalex.org/works?").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )

    with pytest.raises(oa.OpenAlexError):
        await client.fetch_citers("W1", top_n=5, min_year=None, depth=1)


@respx.mock
async def test_a_404_is_not_retried(client):
    route = respx.get(url__startswith="https://api.openalex.org/works/").mock(
        return_value=httpx.Response(404, json={"error": "Not found"})
    )

    with pytest.raises(oa.PaperNotFound):
        await client.resolve("W404")

    assert route.call_count == 1
