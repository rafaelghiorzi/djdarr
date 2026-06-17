import logging
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("djdarr.public")

TURNSTILE_SECRET = os.environ["TURNSTILE_SECRET_KEY"]
TURNSTILE_SITE_KEY = os.environ.get("TURNSTILE_SITE_KEY", "")
CONTAINER_B_URL = os.environ.get("CONTAINER_B_URL", "http://container_b:8001")
FAN_PAGE_ORIGIN = os.environ.get("FAN_PAGE_ORIGIN", "")
MAX_QUERY_LEN = 300
ALLOWED_URL_HOSTS = {
    "youtube.com", "www.youtube.com", "youtu.be",
    "m.youtube.com", "music.youtube.com",
    "soundcloud.com", "www.soundcloud.com",
}

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def _cf_ip(request: Request) -> str:
    return request.headers.get("CF-Connecting-IP") or (
        request.client.host if request.client else "0.0.0.0"
    )


limiter = Limiter(key_func=_cf_ip)
app = FastAPI(docs_url=None, redoc_url=None)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

if FAN_PAGE_ORIGIN:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[FAN_PAGE_ORIGIN],
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

_html_cache: str | None = None


def _get_html() -> str:
    global _html_cache
    if _html_cache is None:
        raw = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
        _html_cache = raw.replace("__TURNSTILE_SITE_KEY__", TURNSTILE_SITE_KEY)
    return _html_cache


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(_get_html())


class SubmitBody(BaseModel):
    query: str
    cf_turnstile_response: str = ""


async def _verify_turnstile(token: str, ip: str) -> bool:
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data={"secret": TURNSTILE_SECRET, "response": token, "remoteip": ip},
                timeout=8.0,
            )
            return r.json().get("success", False)
    except Exception as exc:
        logger.warning("Turnstile verification error: %s", exc)
        return False


@app.post("/submit")
@limiter.limit("5/minute")
async def submit(request: Request, body: SubmitBody):
    query = body.query.strip()

    if not query:
        raise HTTPException(400, "Digite o nome da música ou cole um link.")
    if len(query) > MAX_QUERY_LEN:
        raise HTTPException(400, "Texto muito longo.")
    if _URL_RE.match(query):
        try:
            host = urlparse(query).netloc
        except Exception:
            host = ""
        if host not in ALLOWED_URL_HOSTS:
            raise HTTPException(400, "Apenas links do YouTube e SoundCloud são aceitos.")

    ip = _cf_ip(request)
    if not await _verify_turnstile(body.cf_turnstile_response, ip):
        raise HTTPException(400, "Verificação anti-bot falhou. Tente novamente.")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{CONTAINER_B_URL}/internal/submit",
                json={"query": query, "submitter_ip": ip},
                timeout=5.0,
            )
    except httpx.RequestError as exc:
        logger.error("Failed to reach container_b: %s", exc)
        raise HTTPException(503, "Serviço temporariamente indisponível.")

    if resp.status_code == 409:
        return JSONResponse({"ok": True})  # duplicata — aceita silenciosamente
    if resp.status_code == 429:
        raise HTTPException(429, "Muitos pedidos. Tente mais tarde.")
    if not resp.is_success:
        logger.error("container_b returned %d: %s", resp.status_code, resp.text)
        raise HTTPException(502, "Erro ao registrar pedido.")

    return JSONResponse({"ok": True})
