"""Servidor local: rotas da API e a pagina do grafo.

Rodar com:  uvicorn app.main:app --reload

O escopo de investigacao (`?investigation=` nas leituras, `investigation_id` nos
corpos) e sempre OPCIONAL: quando ausente, vale a investigacao mais recente. Isso
mantem a interface que nao conhece investigacoes funcionando sem mudanca.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import crawl as crawl_mod
from app import db, s2
from app.models import (
    CrawlRequest,
    ExpandRequest,
    InvestigationRename,
    MarkRequest,
    ResolveRequest,
)
from app.openalex import OpenAlexClient, OpenAlexError, PaperNotFound, normalize_work

STATIC = Path(__file__).resolve().parent.parent / "static"
DEFAULT_DB = Path(__file__).resolve().parent.parent / "graph.db"


def create_app() -> FastAPI:
    app = FastAPI(title="Graph Generator", docs_url="/api/docs")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.conn = db.connect(os.environ.get("GRAPH_DB") or DEFAULT_DB)
        app.state.oa = OpenAlexClient(
            mailto=os.environ.get("OPENALEX_MAILTO", "anonymous@example.org")
        )
        try:
            yield
        finally:
            await app.state.oa.aclose()
            app.state.conn.close()

    app.router.lifespan_context = lifespan

    # ------------------------------------------------------------------ helpers

    def conn(request: Request):
        return request.app.state.conn

    def oa(request: Request) -> OpenAlexClient:
        return request.app.state.oa

    def scope(database, investigation_id: int | None) -> int | None:
        """Resolve o escopo pedido. `None` explicito cai na investigacao mais recente;
        um id inexistente e 404, para nao devolver silenciosamente o grafo errado."""
        if investigation_id is not None:
            if db.get_investigation(database, investigation_id) is None:
                raise HTTPException(404, f"investigacao {investigation_id} nao existe")
            return investigation_id
        listed = db.list_investigations(database)
        return listed[0]["id"] if listed else None

    @app.exception_handler(PaperNotFound)
    async def _not_found(request: Request, exc: PaperNotFound):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(OpenAlexError)
    async def _upstream_down(request: Request, exc: OpenAlexError):
        return JSONResponse({"detail": f"o OpenAlex nao respondeu: {exc}"}, status_code=502)

    # ----------------------------------------------------------- investigacoes

    @app.get("/api/investigations")
    async def investigations(request: Request):
        return db.list_investigations(conn(request))

    @app.patch("/api/investigations/{investigation_id}")
    async def rename(investigation_id: int, body: InvestigationRename, request: Request):
        try:
            db.rename_investigation(conn(request), investigation_id, body.name)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from None
        return db.get_investigation(conn(request), investigation_id)

    @app.delete("/api/investigations/{investigation_id}")
    async def remove(investigation_id: int, request: Request):
        """Apaga a investigacao. Os artigos e as marcacoes ficam: custaram requisicoes
        e valem para as outras."""
        try:
            db.delete_investigation(conn(request), investigation_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from None
        return {"deleted": investigation_id}

    # --------------------------------------------------------------------- API

    @app.post("/api/resolve")
    async def resolve(body: ResolveRequest, request: Request):
        """Traduz o que a usuaria colou em um artigo, ou em candidatos para escolher."""
        return await oa(request).resolve(body.input)

    @app.post("/api/crawl")
    async def start_crawl(body: CrawlRequest, request: Request):
        """Percorre as citacoes a partir do seminal, transmitindo progresso via SSE.

        Sem `investigation_id`, cada busca cria a sua propria investigacao — e o que
        permite ter o mesmo seminal com e sem filtro de ano, lado a lado.

        O crawl roda como task e empurra eventos numa fila; o gerador os repassa. Sem
        isso a pagina ficaria muda por dezenas de segundos."""
        client, database = oa(request), conn(request)
        work = await client.get_work("openalex", body.seed_id)
        seed = normalize_work(work, depth=0)

        if body.investigation_id is not None:
            investigation_id = scope(database, body.investigation_id)
        else:
            investigation_id = db.create_investigation(
                database,
                name=body.name or _default_name(seed, body.min_year),
                seed_id=seed["id"],
                top_n=body.top_n,
                min_year=body.min_year,
            )

        params = crawl_mod.CrawlParams(
            depth=body.depth,
            top_n=body.top_n,
            min_year=body.min_year,
            max_nodes=body.max_nodes,
        )

        queue: asyncio.Queue[dict | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        async def run():
            try:
                await crawl_mod.crawl(
                    database, client, investigation_id, seed, params,
                    on_progress=queue.put_nowait,
                )
            except Exception as exc:  # noqa: BLE001 - vai para a pagina como erro
                queue.put_nowait({"done": True, "error": str(exc)})
            finally:
                queue.put_nowait(None)

        task = loop.create_task(run())

        async def events():
            try:
                while (event := await queue.get()) is not None:
                    payload = {**event, "investigation_id": investigation_id}
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            finally:
                if not task.done():
                    task.cancel()

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/expand")
    async def expand(body: ExpandRequest, request: Request):
        """Cresce um no em um nivel — o botao "expandir" do painel."""
        database = conn(request)
        investigation_id = scope(database, body.investigation_id)
        if investigation_id is None:
            raise HTTPException(404, "nao existe nenhuma investigacao ainda")
        try:
            return await crawl_mod.expand(
                database, oa(request), investigation_id, body.paper_id,
                body.top_n, body.min_year,
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.get("/api/graph")
    async def graph(request: Request, investigation: int | None = Query(default=None)):
        database = conn(request)
        investigation_id = scope(database, investigation)
        if investigation_id is None:
            return {"nodes": [], "edges": []}
        return db.get_graph(database, investigation_id)

    @app.get("/api/paper/{paper_id}")
    async def paper(
        paper_id: str, request: Request, investigation: int | None = Query(default=None)
    ):
        """Detalhe completo de um no.

        O abstract faltante e buscado no Semantic Scholar AQUI, e nao durante o crawl:
        o S2 aceita ~1 req/s e fazer isso para centenas de nos travaria a travessia.
        Buscando no clique, so pagamos pelo que a usuaria realmente abre — e o
        resultado fica gravado no cache global, valendo para todas as investigacoes."""
        database = conn(request)
        investigation_id = scope(database, investigation)
        row = db.get_paper(database, paper_id, investigation_id=investigation_id)
        if row is None:
            raise HTTPException(404, f"{paper_id} nao esta no grafo")

        if not row["abstract"] and row["doi"]:
            if fetched := await s2.fetch_abstract(row["doi"]):
                db.upsert_paper(database, {**row, "abstract": fetched})
                row["abstract"] = fetched

        return {
            **row,
            "abstract_available": bool(row["abstract"]),
            "openalex_url": f"https://openalex.org/{row['id']}",
            "doi_url": f"https://doi.org/{row['doi']}" if row["doi"] else None,
        }

    @app.put("/api/paper/{paper_id}/mark")
    async def mark(paper_id: str, body: MarkRequest, request: Request):
        """A marca e global: vale para o artigo, nao para a busca. Ver app/db.py."""
        database = conn(request)
        if db.get_paper(database, paper_id) is None:
            raise HTTPException(404, f"{paper_id} nao esta no grafo")
        db.set_mark(database, paper_id, body.status, body.note)
        return {"id": paper_id, "mark": body.status, "note": body.note}

    # --------------------------------------------------------------- interface

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    return app


def _default_name(seed: dict, min_year: int | None) -> str:
    """Nome legivel o suficiente para a usuaria reconhecer a busca semanas depois."""
    title = (seed.get("title") or "sem titulo").strip()
    short = title if len(title) <= 60 else title[:58] + "…"
    return f"{short} · desde {min_year}" if min_year else short


app = create_app()
