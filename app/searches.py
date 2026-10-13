"""Buscas salvas: cada busca e um arquivo SQLite proprio dentro de `root`.

Um arquivo por busca, e nao uma coluna `search_id` nas tabelas, porque `depth` e as
marcacoes sao relativos ao seminal: o mesmo artigo pode estar no nivel 1 de uma busca
e no nivel 3 de outra, e "interessa" numa investigacao nao implica "interessa" na
outra. O preco e nao compartilhar o cache de artigos entre buscas — aceitavel, um
crawl inteiro leva segundos. Apagar uma busca e apagar um arquivo.

A busca aberta fica gravada em `root/active`, para o servidor reabrir onde parou.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from app import db

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
ACTIVE_FILE = "active"
LEGACY_MARKER = ".legacy-imported"


class SearchNotFound(KeyError):
    pass


class SearchBusy(RuntimeError):
    """A busca ainda esta sendo percorrida; apagar agora deixaria o crawl escrevendo
    num arquivo removido."""


def _slug(title: str) -> str:
    text = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text[:40].rstrip("-") or "busca"


class Searches:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._conns: dict[str, sqlite3.Connection] = {}
        self.busy: set[str] = set()
        self.active = self._read_active()

    # ------------------------------------------------------------- arquivos

    def _path(self, sid: str) -> Path:
        # O id vem da URL: a regex impede que vire caminho fora de `root`.
        if not isinstance(sid, str) or not _ID.match(sid):
            raise SearchNotFound(sid)
        return self.root / f"{sid}.db"

    def exists(self, sid: str) -> bool:
        try:
            return self._path(sid).exists()
        except SearchNotFound:
            return False

    def conn(self, sid: str) -> sqlite3.Connection:
        if sid not in self._conns:
            path = self._path(sid)
            if not path.exists():
                raise SearchNotFound(sid)
            self._conns[sid] = db.connect(path)
        return self._conns[sid]

    def current(self) -> sqlite3.Connection | None:
        return self.conn(self.active) if self.active else None

    def _read_active(self) -> str | None:
        try:
            sid = (self.root / ACTIVE_FILE).read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return sid if self.exists(sid) else None

    def set_active(self, sid: str | None) -> None:
        if sid is not None and not self.exists(sid):
            raise SearchNotFound(sid)
        self.active = sid
        (self.root / ACTIVE_FILE).write_text(sid or "", encoding="utf-8")

    # ---------------------------------------------------------------- ciclo

    def create(self, seed: dict, params: dict | None = None) -> str:
        """Abre um arquivo novo para `seed` e o torna a busca ativa."""
        base = f"{datetime.now():%Y%m%d-%H%M%S}-{_slug(seed.get('title'))}"
        sid, n = base, 2
        while (self.root / f"{sid}.db").exists():
            sid, n = f"{base}-{n}", n + 1
        self._conns[sid] = db.connect(self.root / f"{sid}.db")
        db.set_meta(
            self._conns[sid],
            seed_id=seed["id"],
            seed_title=seed.get("title"),
            seed_year=seed.get("year"),
            params=params or {},
            # Microssegundos: duas buscas no mesmo segundo ainda saem na ordem certa.
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self.set_active(sid)
        return sid

    def delete(self, sid: str) -> None:
        path = self._path(sid)
        if not path.exists():
            raise SearchNotFound(sid)
        if sid in self.busy:
            raise SearchBusy(sid)
        if conn := self._conns.pop(sid, None):
            conn.close()
        for p in (path, path.with_name(path.name + "-journal")):
            p.unlink(missing_ok=True)
        if self.active == sid:
            self.set_active(None)

    def list(self) -> list[dict]:
        out = []
        for path in self.root.glob("*.db"):
            sid = path.stem
            if not _ID.match(sid):
                continue
            conn = self.conn(sid)
            meta = db.get_meta(conn)
            out.append(
                {
                    "id": sid,
                    "seed_id": meta.get("seed_id"),
                    "seed_title": meta.get("seed_title") or sid,
                    "seed_year": meta.get("seed_year"),
                    "params": meta.get("params") or {},
                    "created_at": meta.get("created_at") or "",
                    "crawling": sid in self.busy,
                    **db.summary(conn),
                }
            )
        out.sort(key=lambda s: (s["created_at"], s["id"]), reverse=True)
        return out

    def close(self) -> None:
        for conn in self._conns.values():
            conn.close()
        self._conns.clear()

    # --------------------------------------------------------------- legado

    def import_legacy(self, path: str | Path) -> str | None:
        """Traz o grafo de antes das buscas salvas (um unico graph.db) como uma busca
        da lista, para ninguem perder o que ja montou.

        Copia, nunca move: o arquivo original fica onde estava. Roda uma vez so —
        se a usuaria apagar a busca importada, ela nao volta no proximo inicio."""
        path = Path(path)
        marker = self.root / LEGACY_MARKER
        if marker.exists() or not path.exists():
            return None

        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        src.row_factory = sqlite3.Row
        try:
            seed = src.execute(
                "SELECT id, title, year FROM papers ORDER BY depth, cited_by_count DESC LIMIT 1"
            ).fetchone()
            first = src.execute("SELECT MIN(fetched_at) FROM papers").fetchone()[0]
            seeds = src.execute("SELECT COUNT(*) FROM papers WHERE depth = 0").fetchone()[0]
        except sqlite3.DatabaseError:
            seed = None
        if seed is None:
            src.close()
            marker.touch()
            return None

        created = first or db._now()
        stamp = datetime.fromisoformat(created).astimezone().strftime("%Y%m%d-%H%M%S")
        sid = f"{stamp}-{_slug(seed['title'])}"
        dst = sqlite3.connect(self.root / f"{sid}.db")
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

        db.set_meta(
            self.conn(sid),
            seed_id=seed["id"],
            # O banco antigo acumulava toda busca feita; com mais de um seminal, o
            # nome precisa dizer que e um grafo misto, nao a busca de um artigo so.
            seed_title=(
                seed["title"] if seeds <= 1
                else f"Grafo antigo: {seed['title']} + {seeds - 1} outro{'s' if seeds > 2 else ''}"
            ),
            seed_year=seed["year"],
            params={},
            created_at=created,
            imported_from=str(path),
        )
        marker.touch()
        if self.active is None:
            self.set_active(sid)
        return sid
