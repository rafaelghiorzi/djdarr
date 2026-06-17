"""
internal.py
===========
App FastAPI mínimo com o ÚNICO endpoint que o Container A usa para inserir
submissões: POST /internal/submit.

Roda na porta 8001 e é exposto APENAS na rede interna do docker-compose
(a porta 8001 não está no `ports:` do compose nem nas regras de ingress do
Cloudflare Tunnel). Não tem acesso ao worker — apenas insere pedidos
pendentes no SQLite.
"""

import logging
import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from db import get_db, init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("djdarr.internal")

MAX_QUEUE_SIZE = int(os.environ.get("MAX_QUEUE_SIZE", "50"))
MAX_PENDING_PER_IP = int(os.environ.get("MAX_PENDING_PER_IP", "3"))

app = FastAPI(title="Djdarr Internal", docs_url=None, redoc_url=None)


@app.on_event("startup")
def startup() -> None:
    # CREATE TABLE IF NOT EXISTS é idempotente — seguro mesmo se o painel
    # também inicializar o banco.
    init_db()


class SubmitPayload(BaseModel):
    query: str = Field(..., max_length=300)
    submitter_ip: str = Field(default="")


@app.post("/internal/submit", status_code=201)
def internal_submit(payload: SubmitPayload) -> dict:
    query = payload.query.strip()
    if not query:
        raise HTTPException(400, "Query vazia.")

    with get_db() as conn:
        total_pending = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE status='pending'"
        ).fetchone()[0]
        if total_pending >= MAX_QUEUE_SIZE:
            raise HTTPException(429, "Fila cheia.")

        ip_pending = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE status='pending' AND submitter_ip=?",
            (payload.submitter_ip,),
        ).fetchone()[0]
        if ip_pending >= MAX_PENDING_PER_IP:
            raise HTTPException(429, "Muitos pedidos deste IP.")

        # Deduplicação: mesma query já pendente/aprovada/baixando/pronta
        dup = conn.execute(
            """SELECT id FROM requests
                WHERE query=?
                  AND status IN ('pending','approved','downloading','ready')
               LIMIT 1""",
            (query,),
        ).fetchone()
        if dup:
            raise HTTPException(409, "Pedido já existe na fila.")

        conn.execute(
            "INSERT INTO requests (query, submitter_ip) VALUES (?, ?)",
            (query, payload.submitter_ip),
        )

    return {"ok": True}
