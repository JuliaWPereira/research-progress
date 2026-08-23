"""Servidor local: rotas da API e a pagina do grafo.

Rodar com:  uvicorn app.main:app --reload
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import crawl as crawl_mod
from app import db, s2
from app.models import CrawlRequest, ExpandRequest, MarkRequest, ResolveRequest
from app.openalex import OpenAlexClient, OpenAlexError, PaperNotFound

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

    @app.exception_handler(PaperNotFound)
    async def _not_found(request: Request, exc: PaperNotFound):
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(OpenAlexError)
    async def _upstream_down(request: Request, exc: OpenAlexError):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            {"detail": f"o OpenAlex nao respondeu: {exc}"}, status_code=502
        )

    # --------------------------------------------------------------------- API

    @app.post("/api/resolve")
    async def resolve(body: ResolveRequest, request: Request):
        """Traduz o que a usuaria colou em um artigo, ou em candidatos para escolher."""
        return await oa(request).resolve(body.input)

    @app.post("/api/crawl")
    async def start_crawl(body: CrawlRequest, request: Request):
        """Percorre as citacoes a partir do seminal, transmitindo progresso via SSE.

        O crawl roda como task e empurra eventos numa fila; o gerador os repassa. Sem
        isso a pagina ficaria muda por dezenas de segundos."""
        client, database = oa(request), conn(request)
        work = await client.get_work("openalex", body.seed_id)
        from app.openalex import normalize_work

        seed = normalize_work(work, depth=0)
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
                    database, client, seed, params, on_progress=queue.put_nowait
                )
            except Exception as exc:  # noqa: BLE001 - vai para a pagina como erro
                queue.put_nowait({"done": True, "error": str(exc)})
            finally:
                queue.put_nowait(None)

        task = loop.create_task(run())

        async def events():
            try:
                while (event := await queue.get()) is not None:
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
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
        try:
            return await crawl_mod.expand(
                conn(request), oa(request), body.paper_id, body.top_n, body.min_year
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.get("/api/graph")
    async def graph(request: Request):
        return db.get_graph(conn(request))

    @app.get("/api/paper/{paper_id}")
    async def paper(paper_id: str, request: Request):
        """Detalhe completo de um no.

        O abstract faltante e buscado no Semantic Scholar AQUI, e nao durante o crawl:
        o S2 aceita ~1 req/s e fazer isso para centenas de nos travaria a travessia.
        Buscando no clique, so pagamos pelo que a usuaria realmente abre — e o
        resultado fica gravado."""
        database = conn(request)
        row = db.get_paper(database, paper_id)
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


app = create_app()
