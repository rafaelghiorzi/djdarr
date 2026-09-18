"""
downloader/core.py
==================
Módulo principal de busca e download de áudio.

Responsabilidades:
  - Detectar se a entrada é um link direto ou uma query de texto
  - Buscar no YouTube (via yt-dlp) e SoundCloud (via API pública do site)
  - Comparar resultados usando fuzzy matching (rapidfuzz)
  - Baixar o melhor resultado para o caminho correto
  - Logging detalhado para debugging

Nota sobre o SoundCloud:
  O `scsearch` do yt-dlp usa um endpoint interno do SoundCloud com ordenação
  diferente da busca real do site. Para replicar exatamente o que você vê em
  soundcloud.com/search, usamos a API pública /search?q=... com um client_id
  extraído dinamicamente da página, que é o mesmo mecanismo que o site usa.
"""

import os
import re
import json
import logging
from pathlib import Path
from typing import Optional

import requests
import yt_dlp
from rapidfuzz import fuzz
from yt_dlp.postprocessor.metadataparser import MetadataParserPP

try:
    from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, APIC
    from mutagen.mp3 import MP3
    _MUTAGEN_OK = True
except ImportError:  # pragma: no cover - mutagen é uma dependência obrigatória, mas degrada bem
    _MUTAGEN_OK = False

try:
    from PIL import Image
    import io as _io
    _PIL_OK = True
except ImportError:  # pragma: no cover
    _PIL_OK = False

logger = logging.getLogger("setlist.downloader")

# Cache de credenciais do SoundCloud (válido por toda a sessão do processo)
_sc_client_id: Optional[str] = None
_sc_app_version: Optional[str] = None
_sc_user_id: Optional[str] = None


# ─── Helpers ────────────────────────────────────────────────────────────────

def _is_url(text: str) -> bool:
    """Verifica se a string é uma URL válida."""
    url_pattern = re.compile(
        r"^(https?://)?"
        r"(www\.)?"
        r"(youtube\.com|youtu\.be|soundcloud\.com|open\.spotify\.com)"
        r".*$",
        re.IGNORECASE,
    )
    return bool(url_pattern.match(text.strip()))


def _sanitize_filename(name: str) -> str:
    """Remove caracteres inválidos para nomes de arquivo."""
    return re.sub(r'[<>:"/\\|?*]', "", name).strip()


def _similarity(query: str, title: str) -> float:
    """
    Retorna score de similaridade (0–100) entre query e título usando
    combinação de token_sort_ratio e partial_ratio do rapidfuzz.
    """
    q = query.lower()
    t = title.lower()
    token_score = fuzz.token_sort_ratio(q, t)
    partial_score = fuzz.partial_ratio(q, t)
    return (token_score * 0.6) + (partial_score * 0.4)


# ─── Nome limpo: "Música - Artista" ──────────────────────────────────────────

_NOISE_WORD = (
    r"(?:"
    r"official\s*(?:music\s*)?video|official\s*(?:audio|lyrics?|version)|official|"
    r"lyric\s*video|lyrics?|visualizer|audio|"
    r"full\s*(?:song|track|version|album)|"
    r"remaster(?:ed)?|hd|hq|4k|"
    r"áudio\s*oficial|clipe\s*oficial|letra"
    r")"
)
# Um grupo de colchetes/parênteses só some se TODO o conteúdo dele for feito
# de palavras de ruído (ex.: "(4K Remaster)", "(Official Video)") — assim não
# corta acidentalmente metade de um parêntese e deixa um ")" órfão.
_BRACKET_NOISE_RE = re.compile(
    rf"[\(\[]\s*(?:{_NOISE_WORD}[\s\-]*)+[\)\]]", re.IGNORECASE
)
_BARE_NOISE_RE = re.compile(rf"\b{_NOISE_WORD}\b", re.IGNORECASE)
_SPLIT_RE = re.compile(r"\s*[-–—|]\s*")


def _strip_noise(text: str) -> str:
    """Remove marcadores comuns de título de vídeo ("(Official Video)" etc.)."""
    cleaned = _BRACKET_NOISE_RE.sub("", text or "")
    cleaned = _BARE_NOISE_RE.sub("", cleaned)
    cleaned = re.sub(r"[\(\[\{]\s*[\)\]\}]", "", cleaned)  # parênteses vazios remanescentes
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip(" \t-–—|.")


def clean_track_artist(
    raw_title: str,
    uploader: Optional[str] = None,
    track: Optional[str] = None,
    artist: Optional[str] = None,
) -> tuple[str, str]:
    """
    Deriva (música, artista) limpos a partir do título bruto do YouTube/SoundCloud.

    Prioridade:
      1. Metadados explícitos (track/artist) quando a plataforma já os fornece
         (comum em uploads reconhecidos como música no YouTube).
      2. Split do título em "Artista - Música" (convenção usada tanto pelo
         YouTube quanto pela busca do SoundCloud neste projeto) → devolvido
         como (Música, Artista).
      3. Título limpo + uploader/canal como artista.
    """
    if track and artist:
        return _strip_noise(track) or "download", _strip_noise(artist)

    title = _strip_noise(raw_title)
    parts = _SPLIT_RE.split(title, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        left, right = parts[0].strip(), parts[1].strip()
        return right, left

    if uploader:
        return (title or "download"), _strip_noise(uploader)

    return (title or "download"), ""


def build_display_name(title: str, artist: str) -> str:
    """Nome de exibição/arquivo no formato 'Música - Artista'."""
    title = (title or "download").strip()
    artist = (artist or "").strip()
    name = f"{title} - {artist}" if artist else title
    return _sanitize_filename(name)


def _upsize_soundcloud_artwork(url: Optional[str]) -> Optional[str]:
    """SoundCloud serve capas em baixa resolução por padrão (-large.jpg);
    troca pelo tamanho maior disponível (-t500x500.jpg)."""
    if not url:
        return None
    return re.sub(r"-large\.(jpg|png)$", r"-t500x500.\1", url)


def _normalize_to_jpeg(data: bytes) -> Optional[bytes]:
    """
    Recodifica os bytes da capa para JPEG de verdade.

    O YouTube costuma servir thumbnails em WEBP e o SoundCloud às vezes em
    PNG, mas sempre gravamos a tag ID3 como `image/jpeg` — sem essa conversão,
    o mime declarado não bate com os bytes reais e o Explorer/Finder recusa
    mostrar a capa (o navegador é mais tolerante e disfarça o problema).
    """
    if not _PIL_OK:
        logger.warning("Pillow não disponível — capa pode não ser exibida no Explorer/Finder.")
        return data
    try:
        img = Image.open(_io.BytesIO(data))
        img = img.convert("RGB")
        buf = _io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        return buf.getvalue()
    except Exception as e:
        logger.warning(f"Falha ao converter capa para JPEG: {e}")
        return None


def _fetch_thumbnail_bytes(url: Optional[str]) -> Optional[bytes]:
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        return _normalize_to_jpeg(resp.content)
    except requests.RequestException as e:
        logger.warning(f"Falha ao baixar capa de {url}: {e}")
        return None


def _tag_mp3(path: Path, title: str, artist: str, thumbnail_bytes: Optional[bytes]) -> None:
    """Grava título/artista e capa embutida (ID3 APIC) no MP3 final."""
    if not _MUTAGEN_OK:
        logger.warning("mutagen não disponível — pulando tags/capa do MP3.")
        return
    try:
        try:
            tags = ID3(str(path))
        except ID3NoHeaderError:
            tags = ID3()

        tags.setall("TIT2", [TIT2(encoding=3, text=title)])
        if artist:
            tags.setall("TPE1", [TPE1(encoding=3, text=artist)])
        if thumbnail_bytes:
            tags.setall("APIC", [APIC(
                encoding=3, mime="image/jpeg", type=3,
                desc="Cover", data=thumbnail_bytes,
            )])
        tags.save(str(path), v2_version=3)
    except Exception as e:
        logger.warning(f"Falha ao gravar tags/capa em {path}: {e}")


# ─── SoundCloud: extração de credenciais de sessão ──────────────────────────

def _generate_sc_user_id() -> str:
    """
    Gera um user_id de sessão no mesmo formato que o SoundCloud usa:
    NNNNNN-NNNNNN-NNNNNN-NNNNNN (4 grupos de 6 dígitos separados por hífen).
    Usado como fallback caso não consigamos extrair um da página.
    """
    import random
    groups = [str(random.randint(100000, 999999)) for _ in range(4)]
    uid = "-".join(groups)
    logger.debug(f"[SOUNDCLOUD] user_id gerado localmente: {uid}")
    return uid


def _get_sc_credentials() -> tuple[Optional[str], str, str]:
    """
    Extrai client_id, app_version e user_id do SoundCloud.

    O site embute o client_id e app_version nos scripts JS da página.
    O user_id é um identificador de sessão no formato NNNNNN-NNNNNN-NNNNNN-NNNNNN
    que o browser armazena — sem ele a API retorna 400.

    Estratégia:
      1. Carrega soundcloud.com e extrai o app_version do HTML (tag <script data-version>)
      2. Varre os scripts JS para encontrar o client_id
      3. Tenta extrair user_id da página; se não achar, gera um válido localmente

    Returns:
        Tupla (client_id, app_version, user_id).
        client_id pode ser None se a extração falhar completamente.
    """
    global _sc_client_id, _sc_app_version, _sc_user_id

    if _sc_client_id and _sc_app_version and _sc_user_id:
        logger.debug(f"[SOUNDCLOUD] Usando credenciais em cache | client_id={_sc_client_id[:8]}...")
        return _sc_client_id, _sc_app_version, _sc_user_id

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
    }

    try:
        logger.debug("[SOUNDCLOUD] Carregando soundcloud.com para extrair credenciais...")
        resp = requests.get("https://soundcloud.com", headers=headers, timeout=12)
        resp.raise_for_status()
        html = resp.text

        # ── app_version: está no HTML como atributo data-sc-ab-test-version ──
        # Exemplo: <script>window.__sc_version="1778578681"</script>
        ver_match = re.search(r'window\.__sc_version\s*=\s*"(\d+)"', html)
        if ver_match:
            _sc_app_version = ver_match.group(1)
            logger.debug(f"[SOUNDCLOUD] app_version extraído do HTML: {_sc_app_version}")
        else:
            # Fallback: usa timestamp atual (o site usa um inteiro unix-like)
            import time as _time
            _sc_app_version = str(int(_time.time()))
            logger.debug(f"[SOUNDCLOUD] app_version não encontrado, usando timestamp: {_sc_app_version}")

        # ── Coleta URLs dos scripts JS ────────────────────────────────────────
        script_urls = re.findall(
            r'<script[^>]+src="(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"',
            html
        )
        logger.debug(f"[SOUNDCLOUD] {len(script_urls)} scripts JS para varrer")

        # ── Varre scripts à procura do client_id ─────────────────────────────
        # O client_id fica nos scripts mais recentes (varremos de trás pra frente)
        for script_url in reversed(script_urls):
            try:
                js = requests.get(script_url, headers=headers, timeout=8).text

                if not _sc_client_id:
                    cid = re.search(r'client_id\s*:\s*"([a-zA-Z0-9]{32})"', js)
                    if cid:
                        _sc_client_id = cid.group(1)
                        logger.info(f"[SOUNDCLOUD] client_id extraído: {_sc_client_id[:8]}...")

                # Se já temos tudo, para de varrer
                if _sc_client_id:
                    break

            except requests.RequestException:
                continue

        # ── user_id: gera localmente no formato correto ───────────────────────
        # O site usa um ID persistente do browser. Gerar um válido funciona
        # igualmente para autenticar a requisição.
        if not _sc_user_id:
            _sc_user_id = _generate_sc_user_id()

        if not _sc_client_id:
            logger.warning("[SOUNDCLOUD] Não foi possível extrair client_id dos scripts JS")
            return None, _sc_app_version, _sc_user_id

        return _sc_client_id, _sc_app_version, _sc_user_id

    except requests.RequestException as e:
        logger.error(f"[SOUNDCLOUD] Erro ao carregar soundcloud.com: {e}")
        fallback_version = str(int(__import__("time").time()))
        fallback_uid = _generate_sc_user_id()
        return None, fallback_version, fallback_uid


# ─── SoundCloud: busca via API pública ──────────────────────────────────────

def _search_soundcloud(query: str) -> Optional[dict]:
    """
    Busca no SoundCloud usando exatamente a mesma API que o site usa.

    Endpoint correto (capturado via DevTools):
      GET https://api-v2.soundcloud.com/search
          ?q=<query>
          &facet=model          ← retorna mix de tracks/sets/users
          &user_id=<session_id> ← obrigatório, 400 sem ele
          &client_id=<id>
          &limit=20
          &offset=0
          &linked_partitioning=1
          &app_version=<version>
          &app_locale=pt_BR

    O endpoint /search/tracks que usávamos antes é diferente e mais restrito
    — o site usa /search com facet=model e filtra os tipos na resposta.

    Returns:
        Dict com 'url', 'title', 'source', ou None se falhar.
    """
    client_id, app_version, user_id = _get_sc_credentials()
    if not client_id:
        logger.warning("[SOUNDCLOUD] Sem client_id, pulando busca.")
        return None

    params = {
        "q": query,
        "facet": "model",          # igual ao site — retorna tracks + sets + users misturados
        "user_id": user_id,        # obrigatório para evitar 400
        "client_id": client_id,
        "limit": 20,               # igual ao site, para ter tracks suficientes depois de filtrar
        "offset": 0,
        "linked_partitioning": 1,
        "app_version": app_version,
        "app_locale": "pt_BR",
    }

    endpoint = "https://api-v2.soundcloud.com/search"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://soundcloud.com/",
        "Origin": "https://soundcloud.com",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }

    try:
        logger.debug(f"[SOUNDCLOUD] Buscando: '{query}' | user_id={user_id[:12]}... | ver={app_version}")
        resp = requests.get(endpoint, params=params, headers=headers, timeout=12)

        # Se 401, as credenciais expiraram — limpa cache e tenta renovar uma vez
        if resp.status_code == 401:
            logger.warning("[SOUNDCLOUD] 401 — credenciais expiradas, limpando cache e renovando...")
            global _sc_client_id, _sc_app_version, _sc_user_id
            _sc_client_id = _sc_app_version = _sc_user_id = None
            client_id, app_version, user_id = _get_sc_credentials()
            if not client_id:
                return None
            params.update({"client_id": client_id, "app_version": app_version, "user_id": user_id})
            resp = requests.get(endpoint, params=params, headers=headers, timeout=12)

        resp.raise_for_status()
        data = resp.json()

        # A resposta /search com facet=model mistura tracks, sets e users.
        # Filtramos apenas kind=="track" (mesma lógica que o frontend do site aplica).
        all_items = data.get("collection", [])
        tracks = [item for item in all_items if item.get("kind") == "track"]

        if not tracks:
            logger.warning(f"[SOUNDCLOUD] Nenhuma track nos resultados para '{query}' (total itens: {len(all_items)})")
            return None

        # Resultado #1 — mesma posição que aparece no soundcloud.com/search
        first = tracks[0]
        track_url = first.get("permalink_url", "")
        title = first.get("title", "")
        artist = first.get("user", {}).get("username", "")
        full_title = f"{artist} - {title}" if artist else title
        artwork = _upsize_soundcloud_artwork(
            first.get("artwork_url") or first.get("user", {}).get("avatar_url")
        )

        logger.info(f"[SOUNDCLOUD] Resultado #1: '{full_title}' → {track_url}")

        # Loga os demais candidatos para debugging
        for i, t in enumerate(tracks[1:6], 2):
            a = t.get("user", {}).get("username", "")
            tit = t.get("title", "")
            logger.debug(f"[SOUNDCLOUD] Resultado #{i}: '{a} - {tit}'")

        return {
            "url": track_url,
            "title": full_title,
            "uploader": artist,
            "thumbnail": artwork,
            "source": "soundcloud",
        }

    except requests.HTTPError as e:
        logger.error(f"[SOUNDCLOUD] Erro HTTP {e.response.status_code} na busca: {e}")
        logger.error(f"[SOUNDCLOUD] URL requisitada: {e.response.url}")
        return None
    except (requests.RequestException, ValueError) as e:
        logger.error(f"[SOUNDCLOUD] Erro na busca: {e}")
        return None


# ─── YouTube: busca via yt-dlp ───────────────────────────────────────────────

def _search_youtube(query: str) -> Optional[dict]:
    """
    Busca o primeiro resultado no YouTube via yt-dlp (ytsearch).

    Returns:
        Dict com 'url', 'title', 'source', ou None se falhar.
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
    }

    try:
        logger.debug(f"[YOUTUBE] Buscando: '{query}'")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query}", download=False)
            entries = info.get("entries", [])
            if not entries:
                logger.warning(f"[YOUTUBE] Nenhum resultado para '{query}'")
                return None

            first = entries[0]
            thumbs = first.get("thumbnails") or []
            thumbnail = thumbs[-1]["url"] if thumbs else first.get("thumbnail")
            result = {
                "url": first.get("url") or first.get("webpage_url"),
                "title": first.get("title", ""),
                "uploader": first.get("uploader") or first.get("channel") or "",
                "thumbnail": thumbnail,
                "source": "youtube",
            }
            logger.info(f"[YOUTUBE] Encontrado: '{result['title']}' → {result['url']}")
            return result

    except yt_dlp.utils.DownloadError as e:
        logger.error(f"[YOUTUBE] Erro na busca: {e}")
        return None
    except Exception as e:
        logger.exception(f"[YOUTUBE] Erro inesperado na busca: {e}")
        return None


def _extract_info_meta(url: str) -> dict:
    """
    Extrai metadados de uma URL direta (sem baixar) via yt-dlp: título,
    uploader/canal, capa e, quando disponíveis, os campos track/artist
    (o YouTube preenche isso para uploads reconhecidos como música).
    """
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            info = ydl.extract_info(url, download=False)
        thumbs = info.get("thumbnails") or []
        thumbnail = thumbs[-1]["url"] if thumbs else info.get("thumbnail")
        return {
            "title": info.get("title") or "",
            "uploader": info.get("uploader") or info.get("channel") or "",
            "thumbnail": thumbnail,
            "track": info.get("track"),
            "artist": info.get("artist") or info.get("creator"),
        }
    except Exception as e:
        logger.warning(f"Não foi possível extrair metadados de {url}: {e}")
        return {}


def _pick_best_result(query: str, youtube_result: Optional[dict], sc_result: Optional[dict]) -> Optional[dict]:
    """
    Compara resultados do YouTube e SoundCloud via fuzzy matching e
    retorna o mais próximo da query original.
    """
    candidates = [r for r in [youtube_result, sc_result] if r is not None]

    if not candidates:
        logger.error("Nenhum resultado disponível de nenhuma fonte.")
        return None

    if len(candidates) == 1:
        logger.info(f"Apenas um resultado disponível ({candidates[0]['source']}), usando-o.")
        return candidates[0]

    scores = [(r, _similarity(query, r["title"])) for r in candidates]
    for r, score in scores:
        logger.debug(f"Score [{r['source'].upper()}] '{r['title']}': {score:.1f}")

    best, best_score = max(scores, key=lambda x: x[1])
    logger.info(f"Melhor resultado: [{best['source'].upper()}] '{best['title']}' (score={best_score:.1f})")
    return best


# ─── Download ────────────────────────────────────────────────────────────────

def _download_audio(
    url: str,
    dest_folder: str,
    filename: str,
    audio_format: str = "mp3",
    audio_quality: str = "0",
    progress_hook=None,
    tag_title: Optional[str] = None,
    tag_artist: Optional[str] = None,
    thumbnail_url: Optional[str] = None,
    thumb_dest: Optional[str] = None,
) -> tuple[Path, Optional[str]]:
    """
    Baixa o áudio de uma URL e salva no caminho especificado.

    Args:
        url: URL do vídeo/áudio
        dest_folder: Pasta de destino absoluta
        filename: Nome do arquivo sem extensão
        audio_format: Formato de saída (mp3, wav, etc.)
        audio_quality: Qualidade (0–9 para mp3)
        progress_hook: Callback opcional para progresso (Streamlit)
        tag_title/tag_artist: valores gravados nas tags ID3 (apenas mp3)
        thumbnail_url: URL da capa a embutir/salvar
        thumb_dest: caminho onde salvar uma cópia da capa (para exibição na UI)

    Returns:
        (Path do arquivo salvo, caminho da capa salva ou None)
    """
    dest = Path(dest_folder)
    dest.mkdir(parents=True, exist_ok=True)

    safe_name = _sanitize_filename(filename)
    output_template = str(dest / f"{safe_name}.%(ext)s")

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": audio_format,
                "preferredquality": audio_quality,
            }
        ],
    }

    if progress_hook:
        ydl_opts["progress_hooks"] = [progress_hook]

    try:
        logger.info(f"Baixando: {url} → {dest / safe_name}.{audio_format}")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        final_path = dest / f"{safe_name}.{audio_format}"
        logger.info(f"Download concluído: {final_path}")

        thumb_bytes = _fetch_thumbnail_bytes(thumbnail_url)
        saved_thumb_path = None
        if thumb_bytes and thumb_dest:
            try:
                Path(thumb_dest).parent.mkdir(parents=True, exist_ok=True)
                Path(thumb_dest).write_bytes(thumb_bytes)
                saved_thumb_path = thumb_dest
            except OSError as e:
                logger.warning(f"Falha ao salvar capa em {thumb_dest}: {e}")

        if audio_format == "mp3":
            _tag_mp3(final_path, tag_title or safe_name, tag_artist or "", thumb_bytes)

        return final_path, saved_thumb_path

    except yt_dlp.utils.DownloadError as e:
        logger.error(f"Erro no download: {e}")
        raise RuntimeError(f"Falha no download: {e}") from e
    except Exception as e:
        logger.exception(f"Erro inesperado no download: {e}")
        raise


# ─── Ponto de entrada público ────────────────────────────────────────────────

def download_track(
    query: str,
    dest_folder: str,
    filename: Optional[str] = None,
    audio_format: str = "mp3",
    audio_quality: str = "0",
    progress_hook=None,
    thumb_dest: Optional[str] = None,
) -> dict:
    """
    Função principal chamada pelo worker.

    Fluxo:
      1. Se query for URL → baixa diretamente
      2. Se for texto → busca no YouTube + SoundCloud → escolhe melhor → baixa

    Em ambos os casos o nome final do arquivo (e as tags ID3, se mp3) seguem
    o formato "Música - Artista", derivado via clean_track_artist().

    Returns:
        {
          "success": bool,
          "path": str | None,
          "source": str,
          "matched_title": str,   # "Música - Artista" (nome de exibição)
          "title": str,
          "artist": str,
          "thumbnail_path": str | None,
          "score": float | None,
          "error": str | None
        }
    """
    logger.info(f"=== Iniciando download | query='{query}' | destino='{dest_folder}' | arquivo='{filename}' ===")

    def _fail(error: str) -> dict:
        return {
            "success": False, "path": None, "source": "unknown",
            "matched_title": "", "title": "", "artist": "",
            "thumbnail_path": None, "score": None, "error": error,
        }

    try:
        # ── Caso 1: Link direto ──────────────────────────────────────────
        if _is_url(query):
            logger.info("Entrada detectada como URL direta.")
            meta = _extract_info_meta(query)
            clean_title, clean_artist = clean_track_artist(
                meta.get("title", ""),
                uploader=meta.get("uploader"),
                track=meta.get("track"),
                artist=meta.get("artist"),
            )
            display_name = filename or build_display_name(clean_title, clean_artist)
            path, thumb_path = _download_audio(
                url=query,
                dest_folder=dest_folder,
                filename=display_name,
                audio_format=audio_format,
                audio_quality=audio_quality,
                progress_hook=progress_hook,
                tag_title=clean_title,
                tag_artist=clean_artist,
                thumbnail_url=meta.get("thumbnail"),
                thumb_dest=thumb_dest,
            )
            return {
                "success": True,
                "path": str(path),
                "source": "direct_link",
                "matched_title": display_name,
                "title": clean_title,
                "artist": clean_artist,
                "thumbnail_path": thumb_path,
                "score": None,
                "error": None,
            }

        # ── Caso 2: Busca por texto ──────────────────────────────────────
        logger.info("Entrada detectada como texto de busca.")

        yt_result = _search_youtube(query)
        sc_result = _search_soundcloud(query)

        best = _pick_best_result(query, yt_result, sc_result)
        if not best:
            return _fail("Nenhum resultado encontrado no YouTube ou SoundCloud.")

        score = _similarity(query, best["title"])
        clean_title, clean_artist = clean_track_artist(best["title"], uploader=best.get("uploader"))
        display_name = filename or build_display_name(clean_title, clean_artist)

        path, thumb_path = _download_audio(
            url=best["url"],
            dest_folder=dest_folder,
            filename=display_name,
            audio_format=audio_format,
            audio_quality=audio_quality,
            progress_hook=progress_hook,
            tag_title=clean_title,
            tag_artist=clean_artist,
            thumbnail_url=best.get("thumbnail"),
            thumb_dest=thumb_dest,
        )

        return {
            "success": True,
            "path": str(path),
            "source": best["source"],
            "matched_title": display_name,
            "title": clean_title,
            "artist": clean_artist,
            "thumbnail_path": thumb_path,
            "score": round(score, 1),
            "error": None,
        }

    except RuntimeError as e:
        logger.error(f"Erro controlado no download_track: {e}")
        return _fail(str(e))
    except Exception as e:
        logger.exception(f"Erro inesperado no download_track: {e}")
        return _fail(f"Erro inesperado: {e}")


# ─── SoundCloud: download de playlist completa ───────────────────────────────

def is_soundcloud_playlist_url(text: str) -> bool:
    """Verifica se a string é um link de playlist/set (.../sets/...) do SoundCloud."""
    pattern = re.compile(
        r"^(https?://)?(www\.)?soundcloud\.com/[^/?#]+/sets/[^/?#]+",
        re.IGNORECASE,
    )
    return bool(pattern.match(text.strip()))


def download_playlist(
    playlist_url: str,
    dest_folder: str,
    audio_format: str = "wav",
    audio_quality: str = "0",
    progress_hook=None,
) -> dict:
    """
    Baixa todas as faixas de uma playlist (set) do SoundCloud.

    Cada faixa é salva na melhor qualidade de áudio disponível, convertida
    sem perdas para `audio_format` (recomendado: wav), com metadados
    embutidos no arquivo: título, artista, álbum (nome da playlist),
    número da faixa, data de publicação, gênero e link original.

    As faixas são salvas em uma subpasta de `dest_folder` nomeada com o
    título da playlist.

    Args:
        playlist_url: Link do set do SoundCloud (ex: .../usuario/sets/nome).
        dest_folder: Pasta de destino absoluta (categoria escolhida no app).
        audio_format: Formato de saída (recomendado: wav).
        audio_quality: Qualidade para o conversor (ignorado para wav).
        progress_hook: Callback opcional de progresso do yt-dlp.

    Returns:
        {
          "success": bool,
          "playlist_title": str,
          "playlist_folder": str | None,
          "total": int,
          "downloaded": int,
          "failed": int,
          "tracks": [
              {"index": int, "title": str | None, "uploader": str | None,
               "path": str | None, "success": bool}, ...
          ],
          "error": str | None,
        }
    """
    result = {
        "success": False,
        "playlist_title": "",
        "playlist_folder": None,
        "total": 0,
        "downloaded": 0,
        "failed": 0,
        "tracks": [],
        "error": None,
    }

    if not is_soundcloud_playlist_url(playlist_url):
        result["error"] = "O link informado não parece ser uma playlist (set) do SoundCloud."
        return result

    logger.info(f"=== Iniciando download de playlist | url='{playlist_url}' | destino='{dest_folder}' ===")

    # ── Extração rápida (sem download) para obter título e total de faixas ──
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": "in_playlist"}) as ydl:
            flat_info = ydl.extract_info(playlist_url, download=False)
    except yt_dlp.utils.DownloadError as e:
        logger.error(f"[PLAYLIST] Erro ao ler playlist: {e}")
        result["error"] = f"Falha ao ler a playlist: {e}"
        return result

    playlist_title = flat_info.get("title") or "Playlist"
    total = len(flat_info.get("entries") or [])
    result["playlist_title"] = playlist_title
    result["total"] = total

    if total == 0:
        result["error"] = "A playlist não contém faixas."
        return result

    playlist_folder = Path(dest_folder) / _sanitize_filename(playlist_title)
    playlist_folder.mkdir(parents=True, exist_ok=True)
    result["playlist_folder"] = str(playlist_folder)

    output_template = str(playlist_folder / "%(playlist_index)03d - %(uploader)s - %(title)s.%(ext)s")

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": output_template,
        "windowsfilenames": True,
        "ignoreerrors": True,
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": audio_format,
                "preferredquality": audio_quality,
            },
            {
                # Mapeia metadados da playlist (não preenchidos automaticamente
                # pelo FFmpegMetadata) para os campos de álbum/faixa/artista do álbum.
                "key": "MetadataParser",
                "actions": [
                    (MetadataParserPP.Actions.INTERPRET, "playlist_title", "%(meta_album)s"),
                    (MetadataParserPP.Actions.INTERPRET, "playlist_index", "%(meta_track)s"),
                    (MetadataParserPP.Actions.INTERPRET, "playlist_uploader", "%(meta_album_artist)s"),
                ],
            },
            {"key": "FFmpegMetadata", "add_metadata": True},
        ],
    }

    if progress_hook:
        ydl_opts["progress_hooks"] = [progress_hook]

    logger.info(f"[PLAYLIST] '{playlist_title}' | {total} faixa(s) | pasta: {playlist_folder}")

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(playlist_url, download=True)

            tracks = []
            downloaded = 0
            for idx, entry in enumerate(info.get("entries") or [], start=1):
                if entry is None:
                    logger.warning(f"[PLAYLIST] Faixa #{idx}: falhou ao extrair/baixar.")
                    tracks.append({
                        "index": idx, "title": None, "uploader": None,
                        "path": None, "success": False,
                    })
                    continue

                final_path = Path(ydl.prepare_filename(entry)).with_suffix(f".{audio_format}")
                success = final_path.exists()
                if success:
                    downloaded += 1
                else:
                    logger.warning(
                        f"[PLAYLIST] Faixa #{idx} ('{entry.get('title')}'): "
                        f"arquivo final não encontrado em {final_path}"
                    )

                tracks.append({
                    "index": idx,
                    "title": entry.get("title"),
                    "uploader": entry.get("uploader"),
                    "path": str(final_path) if success else None,
                    "success": success,
                })

    except yt_dlp.utils.DownloadError as e:
        logger.error(f"[PLAYLIST] Erro ao baixar playlist: {e}")
        result["error"] = f"Falha ao baixar playlist: {e}"
        return result
    except Exception as e:
        logger.exception(f"[PLAYLIST] Erro inesperado ao baixar playlist: {e}")
        result["error"] = f"Erro inesperado: {e}"
        return result

    failed = total - downloaded
    logger.info(f"[PLAYLIST] '{playlist_title}' concluída: {downloaded}/{total} ok, {failed} falha(s).")

    result.update({
        "success": downloaded > 0,
        "downloaded": downloaded,
        "failed": failed,
        "tracks": tracks,
    })
    if downloaded == 0:
        result["error"] = "Nenhuma faixa pôde ser baixada."
    return result
