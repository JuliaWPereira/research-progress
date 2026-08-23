"""Cliente do OpenAlex.

O grafo se move PARA FRENTE no tempo: dado um artigo, `fetch_citers` devolve quem o
citou, via `?filter=cites:{id}`. Ordenado por contagem de citacoes, de forma que um
`top_n` pequeno traz o canone da area em vez de uma amostra arbitraria.

Limites do OpenAlex: 10 req/s e 100k/dia no polite pool (basta enviar `mailto`).
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

API = "https://api.openalex.org"
MAX_PER_PAGE = 200  # teto do OpenAlex
SEARCH_CANDIDATES = 5

_OPENALEX_ID = re.compile(r"^(?:https?://(?:api\.)?openalex\.org/(?:works/)?)?(W\d+)$", re.I)
_DOI = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/)?(10\.\d{4,9}/\S+)$", re.I)
_ARXIV = re.compile(r"^https?://arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(?:v\d+)?/?$", re.I)


class OpenAlexError(RuntimeError):
    """Falha de comunicacao com a API depois de esgotar as tentativas."""


class PaperNotFound(OpenAlexError):
    """O input nao corresponde a nenhum trabalho indexado."""


def invert_abstract(index: dict[str, list[int]] | None) -> str:
    """O OpenAlex entrega o abstract como {palavra: [posicoes]}. Reconstroi o texto.

    Uma palavra pode aparecer em varias posicoes, entao o mapa e invertido posicao a
    posicao antes de ordenar."""
    if not index:
        return ""
    words: dict[int, str] = {}
    for word, positions in index.items():
        for pos in positions:
            words[pos] = word
    return " ".join(words[pos] for pos in sorted(words))


def parse_input(raw: str) -> tuple[str, str]:
    """Classifica o que a usuaria colou.

    Retorna ("openalex", id) | ("doi", doi) | ("search", texto). Ambiguidade nao e
    resolvida aqui: qualquer coisa que nao seja um identificador vira busca, e quem
    escolhe entre os candidatos e a usuaria."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("input vazio")

    if m := _OPENALEX_ID.match(text):
        return "openalex", m.group(1).upper()
    if m := _ARXIV.match(text):
        # Preprints do arXiv costumam estar sob o DOI 10.48550/arXiv.<id>, mas nao
        # sempre: quando o artigo foi publicado, o registro fica sob o DOI do
        # periodico. Quem chama trata o 404 pedindo o titulo.
        return "doi", f"10.48550/arXiv.{m.group(1)}"
    if m := _DOI.match(text):
        return "doi", m.group(1).rstrip(".,;)")
    return "search", text


def _strip_prefix(value: str | None, prefix: str) -> str | None:
    if not value:
        return None
    return value[len(prefix):] if value.startswith(prefix) else value


def normalize_work(work: dict[str, Any], depth: int = 0) -> dict[str, Any]:
    """Achata um `work` do OpenAlex no formato que `db.upsert_paper` espera.

    Todo campo e opcional na pratica: registros incompletos sao comuns, e um nó com
    titulo faltando ainda e util no grafo."""
    primary = work.get("primary_location") or {}
    source = primary.get("source") or {}
    best_oa = work.get("best_oa_location") or {}

    # Hierarquia tematica: topico (milhares, cauda longa) < subcampo (~250) < campo
    # (~26). Guardamos os tres para a granularidade do agrupamento ser escolhivel na
    # interface: "campo" resume um grafo interdisciplinar, "subcampo" separa vertentes
    # dentro de uma area, "topico" e o detalhe.
    topics = work.get("topics") or []
    hierarchy = work.get("primary_topic") or (topics[0] if topics else {}) or {}

    authors = []
    for a in work.get("authorships") or []:
        name = ((a.get("author") or {}).get("display_name") or "").strip()
        if not name:
            continue
        institutions = a.get("institutions") or []
        affiliation = (institutions[0].get("display_name") or "") if institutions else ""
        authors.append({"name": name, "affiliation": affiliation})

    return {
        "id": _strip_prefix(work.get("id"), "https://openalex.org/"),
        "doi": _strip_prefix(work.get("doi"), "https://doi.org/"),
        "title": work.get("display_name") or work.get("title") or "(sem titulo)",
        "year": work.get("publication_year"),
        "cited_by_count": work.get("cited_by_count") or 0,
        "abstract": invert_abstract(work.get("abstract_inverted_index")),
        "venue": source.get("display_name"),
        "landing_url": primary.get("landing_page_url"),
        "oa_url": best_oa.get("pdf_url"),
        "authors": authors,
        "topics": [t["display_name"] for t in topics if t.get("display_name")],
        "field": (hierarchy.get("field") or {}).get("display_name"),
        "subfield": (hierarchy.get("subfield") or {}).get("display_name"),
        "depth": depth,
    }


class OpenAlexClient:
    """Um cliente HTTP compartilhado, com polite pool, limite de concorrencia e retry."""

    def __init__(
        self,
        mailto: str,
        client: httpx.AsyncClient | None = None,
        concurrency: int = 5,
        max_attempts: int = 4,
        retry_base_delay: float = 0.5,
    ):
        self.mailto = mailto
        self._client = client
        self._owns_client = client is None
        self._sem = asyncio.Semaphore(concurrency)
        self.max_attempts = max_attempts
        self.retry_base_delay = retry_base_delay

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0),
                follow_redirects=True,
                headers={"User-Agent": f"graph-generator (mailto:{self.mailto})"},
            )
        return self._client

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        """GET com backoff exponencial em 429/5xx. 404 nao e retentado: e resposta,
        nao falha."""
        query = {"mailto": self.mailto, **(params or {})}
        last: Exception | None = None

        for attempt in range(self.max_attempts):
            async with self._sem:
                try:
                    r = await self._http().get(f"{API}{path}", params=query)
                except httpx.HTTPError as exc:
                    last = exc
                else:
                    if r.status_code == 404:
                        raise PaperNotFound(f"nao encontrado: {path}")
                    if r.status_code == 200:
                        return r.json()
                    if r.status_code not in (429, 500, 502, 503, 504):
                        raise OpenAlexError(f"HTTP {r.status_code} em {path}: {r.text[:200]}")
                    last = OpenAlexError(f"HTTP {r.status_code} em {path}")

            if attempt < self.max_attempts - 1 and self.retry_base_delay:
                await asyncio.sleep(self.retry_base_delay * (2**attempt))

        raise OpenAlexError(f"falhou depois de {self.max_attempts} tentativas: {last}")

    async def get_work(self, kind: str, value: str) -> dict:
        path = f"/works/{value}" if kind == "openalex" else f"/works/https://doi.org/{value}"
        return await self._get(path)

    async def search_works(self, text: str, limit: int = SEARCH_CANDIDATES) -> list[dict]:
        data = await self._get(
            "/works", {"search": text, "per_page": limit, "sort": "relevance_score:desc"}
        )
        return [normalize_work(w) for w in data.get("results", [])]

    async def resolve(self, raw: str) -> dict:
        """Devolve {"paper": p, "candidates": []} quando o input identifica um artigo,
        ou {"paper": None, "candidates": [...]} quando e texto livre e a usuaria
        precisa escolher."""
        kind, value = parse_input(raw)

        if kind == "search":
            candidates = await self.search_works(value)
            if not candidates:
                raise PaperNotFound(f"nenhum artigo encontrado para {value!r}")
            return {"paper": None, "candidates": candidates}

        try:
            work = await self.get_work(kind, value)
        except PaperNotFound:
            if value.startswith("10.48550/arXiv."):
                raise PaperNotFound(
                    f"o preprint {value} nao esta indexado no OpenAlex sob esse DOI; "
                    "cole o titulo do artigo em vez do link do arXiv"
                ) from None
            raise
        return {"paper": normalize_work(work), "candidates": []}

    async def fetch_citers(
        self, paper_id: str, top_n: int, min_year: int | None, depth: int
    ) -> tuple[list[dict], int]:
        """Quem citou `paper_id`, os `top_n` mais citados primeiro.

        Devolve (artigos, total_real). O total vem de `meta.count` e e o que permite a
        interface dizer "20 de 1240 mostrados" em vez de fingir completude."""
        filters = [f"cites:{paper_id}"]
        if min_year:
            filters.append(f"from_publication_date:{int(min_year)}-01-01")

        data = await self._get(
            "/works",
            {
                "filter": ",".join(filters),
                "sort": "cited_by_count:desc",
                "per_page": max(1, min(int(top_n), MAX_PER_PAGE)),
            },
        )
        citers = [normalize_work(w, depth=depth) for w in data.get("results", [])]
        return citers, data.get("meta", {}).get("count", 0)
