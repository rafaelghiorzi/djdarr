import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from db import get_db, init_db
from worker import (
    active_downloads,
    enqueue,
    is_enabled,
    queue_size,
    set_enabled,
    start_worker,
    worker_count,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("djdarr.api")

_THUMBS_PATH = os.environ.get("THUMBS_PATH", "/data/thumbs")

app = FastAPI(title="Djdarr Painel", docs_url=None, redoc_url=None)


@app.on_event("startup")
def startup() -> None:
    init_db()
    Path(_THUMBS_PATH).mkdir(parents=True, exist_ok=True)

    approved_ids: list[int] = []
    with get_db() as conn:
        # Downloads interrompidos por reinicialização ficam como 'failed'
        conn.execute(
            """UPDATE requests
               SET status='failed',
                   error_message='Download interrompido (reinicialização do serviço)'
             WHERE status='downloading'"""
        )
        rows = conn.execute(
            "SELECT id FROM requests WHERE status='approved'"
        ).fetchall()
        approved_ids = [r[0] for r in rows]

    start_worker()
    for rid in approved_ids:
        enqueue(rid)

    logger.info(
        "Painel iniciado. Daemon %s (%d worker(s)). %d item(s) aprovado(s) reenfileirado(s).",
        "ligado" if is_enabled() else "desligado",
        worker_count(),
        len(approved_ids),
    )


# ── Página do painel (HTML estático) ─────────────────────────────────────────

_PANEL_HTML: str | None = None


def _get_panel_html() -> str:
    global _PANEL_HTML
    if _PANEL_HTML is None:
        _PANEL_HTML = (Path(__file__).parent / "static" / "panel.html").read_text(
            encoding="utf-8"
        )
    return _PANEL_HTML


@app.get("/", response_class=HTMLResponse)
def panel() -> HTMLResponse:
    return HTMLResponse(_get_panel_html())


# ── API de gestão (usada pelo painel via fetch) ──────────────────────────────

@app.get("/api/pending")
def list_pending() -> list:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM requests WHERE status='pending' ORDER BY submitted_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/approved")
def list_approved() -> list:
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM requests
                WHERE status IN ('approved','downloading','ready','failed')
             ORDER BY approved_at ASC"""
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/approve/{request_id}")
def approve(request_id: int) -> dict:
    with get_db() as conn:
        row = conn.execute(
            "SELECT status FROM requests WHERE id=?", (request_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Pedido não encontrado.")
        if row["status"] != "pending":
            raise HTTPException(400, f"Status inválido para aprovação: {row['status']}")
        conn.execute(
            """UPDATE requests
               SET status='approved',
                   approved_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
             WHERE id=?""",
            (request_id,),
        )
    enqueue(request_id)
    return {"ok": True}


@app.post("/api/reject/{request_id}")
def reject(request_id: int) -> dict:
    with get_db() as conn:
        row = conn.execute(
            "SELECT status FROM requests WHERE id=?", (request_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Pedido não encontrado.")
        if row["status"] != "pending":
            raise HTTPException(400, f"Status inválido para rejeição: {row['status']}")
        conn.execute(
            "UPDATE requests SET status='rejected' WHERE id=?", (request_id,)
        )
    return {"ok": True}


@app.post("/api/played/{request_id}")
def mark_played(request_id: int) -> dict:
    with get_db() as conn:
        row = conn.execute(
            "SELECT status FROM requests WHERE id=?", (request_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Pedido não encontrado.")
        if row["status"] not in ("ready", "failed"):
            raise HTTPException(400, f"Status inválido: {row['status']}")
        conn.execute(
            "UPDATE requests SET status='played' WHERE id=?", (request_id,)
        )
    return {"ok": True}


@app.post("/api/retry/{request_id}")
def retry(request_id: int) -> dict:
    with get_db() as conn:
        row = conn.execute(
            "SELECT status FROM requests WHERE id=?", (request_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Pedido não encontrado.")
        if row["status"] != "failed":
            raise HTTPException(400, f"Status inválido para re-tentativa: {row['status']}")
        conn.execute(
            """UPDATE requests
               SET status='approved',
                   retry_count=retry_count+1,
                   error_message=NULL,
                   download_started_at=NULL,
                   download_finished_at=NULL
             WHERE id=?""",
            (request_id,),
        )
    enqueue(request_id)
    return {"ok": True}


# ── Capa da faixa ─────────────────────────────────────────────────────────────

@app.get("/api/thumb/{request_id}")
def get_thumb(request_id: int):
    path = Path(_THUMBS_PATH) / f"{request_id}.jpg"
    if not path.is_file():
        raise HTTPException(404, "Sem capa.")
    return FileResponse(str(path), media_type="image/jpeg")


# ── Daemon de download (pool de workers) ──────────────────────────────────────

@app.get("/api/worker/status")
def worker_status() -> dict:
    return {
        "enabled": is_enabled(),
        "worker_count": worker_count(),
        "queue_size": queue_size(),
        "active": active_downloads(),
    }


@app.post("/api/worker/enable")
def worker_enable() -> dict:
    set_enabled(True)
    return {"ok": True, "enabled": True}


@app.post("/api/worker/disable")
def worker_disable() -> dict:
    set_enabled(False)
    return {"ok": True, "enabled": False}
