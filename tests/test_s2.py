import httpx
import respx

from app import s2

S2 = "https://api.semanticscholar.org/graph/v1/paper/"


@respx.mock
async def test_returns_the_abstract_when_semantic_scholar_has_one():
    respx.get(url__startswith=S2).mock(
        return_value=httpx.Response(200, json={"abstract": "We present a method for..."})
    )

    assert await s2.fetch_abstract("10.1/abc") == "We present a method for..."


@respx.mock
async def test_falls_back_to_the_tldr_when_there_is_no_abstract():
    respx.get(url__startswith=S2).mock(
        return_value=httpx.Response(
            200, json={"abstract": None, "tldr": {"text": "A faster sorting algorithm."}}
        )
    )

    assert await s2.fetch_abstract("10.1/abc") == "A faster sorting algorithm."


@respx.mock
async def test_asks_for_the_doi_and_only_the_fields_it_needs():
    route = respx.get(url__startswith=S2).mock(
        return_value=httpx.Response(200, json={"abstract": "x"})
    )

    await s2.fetch_abstract("10.1038/nature14539")

    url = str(route.calls[0].request.url)
    assert "DOI:10.1038/nature14539" in httpx.URL(url).path
    assert "fields=abstract%2Ctldr" in url


@respx.mock
async def test_an_unknown_paper_yields_an_empty_abstract_not_an_error():
    respx.get(url__startswith=S2).mock(return_value=httpx.Response(404, json={}))

    assert await s2.fetch_abstract("10.9999/nope") == ""


@respx.mock
async def test_a_rate_limited_or_broken_s2_never_breaks_the_crawl():
    respx.get(url__startswith=S2).mock(return_value=httpx.Response(429, json={}))
    assert await s2.fetch_abstract("10.1/abc") == ""

    respx.get(url__startswith=S2).mock(side_effect=httpx.ConnectError("offline"))
    assert await s2.fetch_abstract("10.1/abc") == ""


async def test_a_paper_without_a_doi_is_not_looked_up_at_all():
    # Nenhum respx.mock aqui: se fizesse uma requisicao, o teste falharia por rede.
    assert await s2.fetch_abstract(None) == ""
    assert await s2.fetch_abstract("") == ""
