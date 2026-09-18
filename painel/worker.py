import logging
import os
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from db import get_db, get_setting, set_setting
from downloader.core import download_track

logger = logging.getLogger("djdarr.worker")

_DOWNLOADS_PATH = os.environ.get("DOWNLOADS_PATH", "/downloads")
_AUDIO_FORMAT = os.environ.get("AUDIO_FORMAT", "mp3")
_THUMBS_PATH = os.environ.get("THUMBS_PATH", "/data/thumbs")
_WORKER_COUNT = max(1, min(3, int(os.environ.get("DOWNLOAD_WORKERS", "2"))))
_AUTO_APPROVE_POLL_SECONDS = 2

_queue: "queue.Queue[int]" = queue.Queue()
_enabled_event = threading.Event()
_active_lock = threading.Lock()
_active: dict[int, str] = {}  # request_id -> query, downloads em andamento agora

_started = False
_start_lock = threading.Lock()


def enqueue(request_id: int) -> None:
    """Coloca um pedido aprovado na fila. Os workers só o consomem se o
    daemon estiver ligado — enquanto desligado, os itens ficam represados."""
    _queue.put(request_id)


def is_enabled() -> bool:
    return _enabled_event.is_set()


def worker_count() -> int:
    return _WORKER_COUNT


def queue_size() -> int:
    return _queue.qsize()


def active_downloads() -> list[dict]:
    with _active_lock:
        return [{"id": rid, "query": q} for rid, q in _active.items()]


def set_enabled(enabled: bool) -> None:
    """Liga/desliga o daemon. Ao desligar, os workers terminam o download
    em andamento (se houver) e simplesmente param de puxar novos itens da
    fila — nada é cancelado no meio."""
    if enabled:
        _enabled_event.set()
    else:
        _enabled_event.clear()
    set_setting("daemon_enabled", "1" if enabled else "0")
    logger.info("Daemon de download %s.", "ativado" if enabled else "desativado")


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _auto_approve_pending() -> list[int]:
    """Aprova automaticamente todo pedido pendente (mesma transição que o
    botão "Aprovar" do painel) e devolve os ids recém-aprovados. Só é chamado
    enquanto o daemon está ligado — com o daemon desligado, pedidos continuam
    exigindo aprovação manual no painel."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id FROM requests WHERE status='pending' ORDER BY submitted_at ASC"
        ).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"""UPDATE requests
                       SET status='approved',
                           approved_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
                     WHERE status='pending' AND id IN ({placeholders})""",
                ids,
            )
    return ids


def _autopromote_loop() -> None:
    """Enquanto o daemon está ligado, promove pedidos pendentes a aprovados
    (e os enfileira) sem depender de clique no painel — internal:8001 (onde o
    pedido do fã é inserido) roda num processo separado de api:8501 (onde
    este worker vive), então o banco é o único jeito de coordenar os dois."""
    logger.info("Auto-approve loop started.")
    while True:
        _enabled_event.wait()
        try:
            for rid in _auto_approve_pending():
                enqueue(rid)
        except Exception:
            logger.exception("Erro no auto-approve do daemon.")
        time.sleep(_AUTO_APPROVE_POLL_SECONDS)


def _process(request_id: int) -> None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT query FROM requests WHERE id=?", (request_id,)
        ).fetchone()
        if not row:
            logger.warning("Worker: request %d not found in DB, skipping.", request_id)
            return
        query = row["query"]
        conn.execute(
            "UPDATE requests SET status='downloading', download_started_at=? WHERE id=?",
            (_utcnow(), request_id),
        )

    with _active_lock:
        _active[request_id] = query

    logger.info("Worker: starting download for request %d — %r", request_id, query)

    thumb_dest = str(Path(_THUMBS_PATH) / f"{request_id}.jpg")

    try:
        result = download_track(
            query=query,
            dest_folder=_DOWNLOADS_PATH,
            filename=None,  # deixa o downloader nomear como "Música - Artista"
            audio_format=_AUDIO_FORMAT,
            audio_quality="0",
            thumb_dest=thumb_dest,
        )
    except Exception as exc:
        logger.exception("Worker: unexpected error for request %d", request_id)
        result = {
            "success": False,
            "error": str(exc),
            "matched_title": "",
            "source": "",
            "path": None,
            "title": "",
            "artist": "",
            "thumbnail_path": None,
        }
    finally:
        with _active_lock:
            _active.pop(request_id, None)

    with get_db() as conn:
        if result["success"]:
            conn.execute(
                """UPDATE requests SET
                    status='ready',
                    file_path=?,
                    matched_title=?,
                    source=?,
                    title=?,
                    artist=?,
                    thumbnail_path=?,
                    download_finished_at=?
                WHERE id=?""",
                (
                    result.get("path"),
                    result.get("matched_title", ""),
                    result.get("source", ""),
                    result.get("title", ""),
                    result.get("artist", ""),
                    result.get("thumbnail_path"),
                    _utcnow(),
                    request_id,
                ),
            )
            logger.info("Worker: request %d ready — %s", request_id, result.get("path"))
        else:
            conn.execute(
                """UPDATE requests SET
                    status='failed',
                    error_message=?,
                    download_finished_at=?
                WHERE id=?""",
                (result.get("error", "Erro desconhecido"), _utcnow(), request_id),
            )
            logger.warning("Worker: request %d failed — %s", request_id, result.get("error"))


def _worker_loop(name: str) -> None:
    logger.info("Download worker '%s' started.", name)
    while True:
        _enabled_event.wait()
        try:
            request_id = _queue.get(timeout=1)
        except queue.Empty:
            continue
        try:
            _process(request_id)
        finally:
            _queue.task_done()


def start_worker() -> None:
    """Sobe o pool de workers (idempotente). O estado ligado/desligado do
    daemon é restaurado do banco — os workers ficam vivos o tempo todo,
    apenas bloqueados enquanto o daemon está desligado."""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True

    if get_setting("daemon_enabled", "0") == "1":
        _enabled_event.set()

    for i in range(_WORKER_COUNT):
        t = threading.Thread(
            target=_worker_loop, args=(f"w{i + 1}",), daemon=True,
            name=f"download-worker-{i + 1}",
        )
        t.start()

    threading.Thread(target=_autopromote_loop, daemon=True, name="auto-approve").start()
