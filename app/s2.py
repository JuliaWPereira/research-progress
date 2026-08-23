"""Fallback de abstract via Semantic Scholar.

O OpenAlex nao tem `abstract_inverted_index` para todos os registros. Quando falta, o
S2 costuma ter o abstract, e as vezes um TL;DR gerado, que para triagem serve bem.

Este modulo e deliberadamente tolerante: e um enfeite, nao um requisito. Qualquer
falha (404, rate limit, rede fora) devolve string vazia e o crawl segue.
"""

from __future__ import annotations

import httpx

API = "https://api.semanticscholar.org/graph/v1"


async def fetch_abstract(doi: str | None, client: httpx.AsyncClient | None = None) -> str:
    """Abstract (ou TL;DR) do artigo com este DOI. String vazia se nao der."""
    if not doi:
        return ""

    owns = client is None
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(15.0), follow_redirects=True)
    try:
        r = await http.get(f"{API}/paper/DOI:{doi}", params={"fields": "abstract,tldr"})
        if r.status_code != 200:
            return ""
        data = r.json()
    except (httpx.HTTPError, ValueError):
        return ""
    finally:
        if owns:
            await http.aclose()

    if abstract := (data.get("abstract") or "").strip():
        return abstract
    return ((data.get("tldr") or {}).get("text") or "").strip()
