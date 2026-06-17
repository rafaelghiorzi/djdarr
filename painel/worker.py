import logging
import os
import threading
from datetime import datetime, timezone

from db import get_db
from downloader.core import download_track

logger = logging.getLogger("djdarr.worker")

_DOWNLOADS_PATH = os.environ.get("DOWNLOADS_PATH", "/downloads")
_AUDIO_FORMAT = os.environ.get("AUDIO_FORMAT", "wav")

_queue: list[int] = []
_lock = threading.Lock()
_event = threading.Event()


def enqueue(request_id: int) -> None:
    with _lock:
        if request_id not in _queue:
            _queue.append(request_id)
    _event.set()


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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

    logger.info("Worker: starting download for request %d — %r", request_id, query)

    try:
        result = download_track(
            query=query,
            dest_folder=_DOWNLOADS_PATH,
            filename=None,  # deixa o downloader nomear pelo título da música
            audio_format=_AUDIO_FORMAT,
            audio_quality="0",
        )
    except Exception as exc:
        logger.exception("Worker: unexpected error for request %d", request_id)
        result = {
            "success": False,
            "error": str(exc),
            "matched_title": "",
            "source": "",
            "path": None,
        }

    with get_db() as conn:
        if result["success"]:
            conn.execute(
                """UPDATE requests SET
                    status='ready',
                    file_path=?,
                    matched_title=?,
                    source=?,
                    download_finished_at=?
                WHERE id=?""",
                (
                    result.get("path"),
                    result.get("matched_title", ""),
                    result.get("source", ""),
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


def _worker_loop() -> None:
    logger.info("Download worker loop started.")
    while True:
        _event.wait()
        _event.clear()
        while True:
            with _lock:
                if not _queue:
                    break
                request_id = _queue.pop(0)
            _process(request_id)


def start_worker() -> None:
    t = threading.Thread(target=_worker_loop, daemon=True, name="download-worker")
    t.start()
