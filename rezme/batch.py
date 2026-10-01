"""Cola de procesamiento por lotes: URLs o listas de reproducción -> base.

Los vídeos se procesan de uno en uno (ingesta y, si se pide, extracción), con
pausas entre vídeos, reintentos para errores transitorios y estado persistente
en la tabla `jobs`, de modo que el lote se puede interrumpir y reanudar.

Todo lo que toca la red, el reloj o el modelo se inyecta (`Deps`) para poder
probarlo sin conexión. La cola nunca guarda secretos: el navegador de las
cookies no se persiste y los mensajes de error se guardan saneados.
"""
from __future__ import annotations

import importlib
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import parse_qs, urlparse

import yt_digest

from .ingest import PLATFORM, _YT_HOSTS, ingest_url, video_id_from_url
from .schema import utc_now
from .store import Store

NO_SUBS_NOTE = "sin subtítulos (pendiente de Whisper)"
MAX_ATTEMPTS = 3
BACKOFF_S = (30.0, 120.0)        # espera antes del 2.º y del 3.er intento
DEFAULT_DELAY_S = (5.0, 10.0)    # pausa entre vídeos si no se indica --delay
DELAY_JITTER = 0.2
# Tantos vídeos seguidos rechazados por YouTube (429 / anti-bot): se para el lote.
MAX_BLOCKED_STREAK = 3
MAX_ERROR_CHARS = 300

ORIGIN_LABELS = {
    "subtitles_manual": "subtítulos manuales", "subtitles_auto": "subtítulos automáticos",
    "whisper": "Whisper", "pasted": "texto pegado",
}


class NoSubtitles(Exception):
    """El vídeo no tiene subtítulos y no se debe (o no se puede) transcribir el audio."""


class TransientError(Exception):
    """Fallo que puede resolverse reintentando."""


# --------------------------------------------------------------------------
# Errores
# --------------------------------------------------------------------------

_PERMANENT = (
    ("private video", "vídeo privado"),
    ("video is private", "vídeo privado"),
    ("has been removed", "vídeo eliminado"),
    ("removed by the uploader", "vídeo eliminado"),
    ("account associated with this video has been terminated", "cuenta eliminada"),
    ("members-only", "solo para miembros del canal"),
    ("join this channel", "solo para miembros del canal"),
    ("requires payment", "vídeo de pago"),
    ("premium", "vídeo de pago"),
    ("confirm your age", "restricción de edad (prueba con --cookies-from)"),
    ("age-restricted", "restricción de edad (prueba con --cookies-from)"),
    ("not available in your country", "no disponible en tu país"),
    ("blocked it in your country", "no disponible en tu país"),
    ("copyright", "retirado por derechos de autor"),
    ("video unavailable", "vídeo no disponible"),
    ("this video is unavailable", "vídeo no disponible"),
)
_BLOCKED = ("http error 429", "too many requests", "not a bot", "rate limit", "rate-limit")
_TRANSIENT = ("http error 5", "timed out", "timeout", "temporary failure", "connection reset",
              "connection refused", "connection aborted", "incomplete read", "remote end closed",
              "network is unreachable", "name or service not known", "nodename nor servname",
              "unable to download", "ssl")
_URL_RE = re.compile(r"(https?://[^\s?#'\"]+)[?#][^\s'\"]*")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def sanitize_error(text: str) -> str:
    """Mensaje en una línea, sin parámetros de URL (pueden llevar tokens) y acotado."""
    text = _ANSI_RE.sub("", str(text))
    text = _URL_RE.sub(r"\1", text)
    text = " ".join(text.split())
    text = re.sub(r"^(ERROR:\s*)+", "", text)
    return text[:MAX_ERROR_CHARS]


def classify_error(exc: BaseException) -> tuple[str, str]:
    """('permanent' | 'blocked' | 'transient', motivo en español)."""
    message = sanitize_error(str(exc)) or type(exc).__name__
    low = message.lower()
    if any(mark in low for mark in _BLOCKED):
        return "blocked", f"YouTube está limitando las peticiones ({message})"
    for mark, reason in _PERMANENT:
        if mark in low:
            return "permanent", reason
    if isinstance(exc, TransientError):
        return "transient", message
    if isinstance(exc, (OSError, TimeoutError)) or any(mark in low for mark in _TRANSIENT):
        return "transient", f"error de red ({message})"
    if type(exc).__name__ in ("DownloadError", "ExtractorError", "ExtractionError"):
        return "transient", message
    return "permanent", message


# --------------------------------------------------------------------------
# Entrada: URLs y listas de reproducción
# --------------------------------------------------------------------------

@dataclass
class PlaylistEntry:
    video_id: str | None
    title: str | None = None
    reason: str | None = None  # motivo si no es accesible


@dataclass
class AddReport:
    added: int = 0
    already: int = 0
    queued_for_extract: int = 0
    inaccessible: list[tuple[str, str]] = field(default_factory=list)  # (vídeo, motivo)
    invalid: list[str] = field(default_factory=list)


_UNAVAILABLE_TITLES = {"[private video]": "vídeo privado", "[deleted video]": "vídeo eliminado"}
_UNAVAILABLE = {"private": "vídeo privado", "premium_only": "vídeo de pago",
                "subscriber_only": "solo para miembros del canal",
                "needs_auth": "requiere iniciar sesión (restricción de edad o acceso)"}


def playlist_id(url: str, *, force: bool = False) -> str | None:
    """Id de lista si la URL es de una lista. Una URL de vídeo con `list=` solo cuenta con `force`."""
    parts = urlparse(url.strip())
    if parts.scheme not in ("http", "https") or (parts.hostname or "").lower() not in _YT_HOSTS:
        return None
    list_id = (parse_qs(parts.query).get("list") or [""])[0]
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,}", list_id):
        return None
    return list_id if force or parts.path.rstrip("/") == "/playlist" else None


def expand_playlist(url: str, cookies_from: str | None = None) -> list[PlaylistEntry]:
    """Vídeos de una lista, en orden, sin descargar nada (yt-dlp `extract_flat`)."""
    from yt_dlp import YoutubeDL

    opts: dict[str, Any] = {"quiet": True, "no_warnings": True, "skip_download": True,
                            "extract_flat": True, "ignoreerrors": True, "socket_timeout": 30}
    if cookies_from:
        opts["cookiesfrombrowser"] = (cookies_from,)
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise RuntimeError("No se pudo leer la lista de reproducción (¿es privada o no existe?).")
    entries = []
    for entry in info.get("entries") or []:
        if not entry:
            entries.append(PlaylistEntry(None, None, "vídeo no disponible"))
            continue
        title = entry.get("title")
        reason = (_UNAVAILABLE_TITLES.get(str(title or "").strip().lower())
                  or _UNAVAILABLE.get(entry.get("availability") or ""))
        entries.append(PlaylistEntry(entry.get("id"), title, reason))
    return entries


def read_urls(lines: Iterable[str]) -> list[str]:
    """Una URL por línea; ignora líneas vacías y comentarios con #."""
    urls = []
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#"):
            urls.append(line.split()[0])
    return urls


def extraction_complete(store: Store, source_id: int) -> bool:
    run = store.latest_run(source_id)
    chunks = (run or {}).get("stats", {}).get("tramos") or {}
    return bool(chunks) and all(c.get("estado") == "ok" for c in chunks.values())


def _enqueue(store: Store, video_id: str, playlist_url: str | None, title: str | None,
             priority: int, report: AddReport) -> None:
    url = f"https://www.youtube.com/watch?v={video_id}"
    source = store.get_source(PLATFORM, video_id)
    if source and store.latest_transcript(source["id"]):
        # Ya ingerido: no se vuelve a descargar. Si falta la extracción, queda preparado.
        report.already += 1
        if not extraction_complete(store, source["id"]):
            _, created = store.enqueue_job(video_id, url, playlist_url=playlist_url,
                                           title=source.get("title") or title,
                                           priority=priority, stage="extract")
            report.queued_for_extract += created
        return
    _, created = store.enqueue_job(video_id, url, playlist_url=playlist_url, title=title,
                                   priority=priority)
    if created:
        report.added += 1
    else:
        report.already += 1


def add_urls(store: Store, urls: Sequence[str], *, priority: int = 0, whole_playlist: bool = False,
             cookies_from: str | None = None,
             expand: Callable[..., list[PlaylistEntry]] | None = None) -> AddReport:
    """Encola vídeos sueltos y listas, sin duplicar ni lo encolado ni lo ya ingerido."""
    expand = expand or expand_playlist
    report = AddReport()
    for raw in urls:
        list_id = playlist_id(raw, force=whole_playlist)
        if list_id:
            playlist_url = f"https://www.youtube.com/playlist?list={list_id}"
            for entry in expand(playlist_url, cookies_from):
                label = entry.title or entry.video_id or "vídeo sin identificar"
                if entry.reason:
                    report.inaccessible.append((label, entry.reason))
                elif not entry.video_id or not video_id_from_url(
                        f"https://www.youtube.com/watch?v={entry.video_id}"):
                    report.inaccessible.append((label, "identificador no válido"))
                else:
                    _enqueue(store, entry.video_id, playlist_url, entry.title, priority, report)
            continue
        video_id = video_id_from_url(raw)
        if video_id:
            _enqueue(store, video_id, None, None, priority, report)
        else:
            report.invalid.append(sanitize_error(raw))
    return report


# --------------------------------------------------------------------------
# Ejecución
# --------------------------------------------------------------------------

@dataclass
class Deps:
    """Todo lo externo, inyectable en los tests."""
    fetch: Callable[..., tuple[dict, list]] | None = None
    transcribe: Callable[..., list] | None = None
    # (store, source_id) -> resultado de la extracción; None si no hay extractor.
    extractor: Callable[[Store, int], Any] | None = None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    uniform: Callable[[float, float], float] = random.uniform


@dataclass
class RunSummary:
    done: int = 0
    ingested: int = 0   # ingeridos que quedan pendientes de extracción
    failed: int = 0
    skipped: int = 0
    recovered: int = 0
    interrupted: bool = False
    blocked: bool = False
    seconds: float = 0.0
    remaining: int = 0


def load_extractor(backend_name: str, *, domain: str | None = None,
                   ollama_model: str = "qwen3:14b") -> Callable[[Store, int], Any] | None:
    """Extractor real, o None si el paquete aún no tiene `extract.py`."""
    try:
        extract = importlib.import_module(f"{__package__}.extract")
        backends = importlib.import_module(f"{__package__}.backends")
    except ImportError:
        return None
    backend = backends.make_backend(backend_name, ollama_model=ollama_model)
    return lambda store, source_id: extract.extract_source(store, source_id, backend, domain=domain)


def _n(value: int) -> str:
    return f"{value:,}".replace(",", ".")


def _elapsed(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def _process(store: Store, job: dict[str, Any], *, stage: str, langs: Sequence[str],
             no_whisper: bool, whisper: bool, whisper_model: str, cookies_from: str | None,
             deps: Deps) -> tuple[list[str], bool, bool]:
    """Ejecuta las etapas pendientes del trabajo. Devuelve (partes del mensaje, terminado, usó red)."""
    parts: list[str] = []
    network = False
    if job["stage"] == "ingest":
        base_fetch = deps.fetch or yt_digest.fetch_subtitles
        base_transcribe = deps.transcribe or yt_digest.transcribe_audio

        def fetch(url: str, lang_list: list[str], tmp: str, cookies: str | None):
            info, cues = base_fetch(url, lang_list, tmp, cookies)
            # yt_digest devuelve [] tanto si no hay subtítulos como si falló su descarga.
            if not cues and not whisper and yt_digest.choose_subtitle_track(info, lang_list):
                raise TransientError("hay subtítulos pero no se pudieron descargar")
            return info, cues

        def transcribe(*args: Any):
            if no_whisper:
                raise NoSubtitles(NO_SUBS_NOTE)
            if deps.transcribe is None and not yt_digest.module_available("faster_whisper"):
                raise NoSubtitles("sin subtítulos y falta `faster-whisper` para transcribir el audio")
            return base_transcribe(*args)

        res = ingest_url(store, job["url"], langs, whisper=whisper, whisper_model=whisper_model,
                         cookies_from=cookies_from, fetch=fetch, transcribe=transcribe)
        network = not res.skipped_download
        state = "ya ingerido" if res.skipped_download else "ingesta ok"
        parts += [state, f"{_n(res.n_cues)} cues", ORIGIN_LABELS.get(res.origin, res.origin)]
        source = store.get_source_by_id(res.source_id) or {}
        store.update_job(job["id"], stage="extract", title=source.get("title") or job["title"],
                         notes=None)
        job["stage"], job["title"] = "extract", source.get("title") or job["title"]

    if stage != "all" or deps.extractor is None:
        if stage == "all":
            parts.append("extracción pendiente (aún no disponible)")
        return parts, False, network

    source = store.get_source(PLATFORM, job["video_id"])
    if source is None:
        raise RuntimeError("no hay transcripción guardada para extraer")
    result = deps.extractor(store, source["id"])
    failed = getattr(result, "chunks_failed", 0)
    if failed:
        raise TransientError(f"{failed} tramos fallaron en la extracción")
    parts.append(f"extracción ok · {_n(result.claims_new)} afirmaciones "
                 f"({_n(result.verified)} verificadas)")
    return parts, True, True


def run_queue(store: Store, *, limit: int | None = None, delay: float | None = None,
              stage: str = "ingest", no_whisper: bool = False, whisper_only: bool = False,
              langs: Sequence[str] = ("es", "en"), whisper_model: str = "small",
              cookies_from: str | None = None, deps: Deps | None = None,
              out: Callable[[str], None] = print,
              on_change: Callable[[], None] | None = None) -> RunSummary:
    """Procesa la cola. Un fallo en un vídeo nunca detiene el lote.

    `on_change` se llama cuando un trabajo empieza o termina (para refrescar una interfaz).
    """
    changed = on_change or (lambda: None)
    if stage not in ("ingest", "all"):
        raise ValueError(f"Etapa no válida: {stage!r} (permitidas: ingest, all).")
    deps = deps or Deps()
    summary = RunSummary(recovered=store.recover_running_jobs())
    if summary.recovered:
        out(f"Recuperados {summary.recovered} trabajos que quedaron a medias; vuelven a la cola.")

    def candidates() -> list[dict[str, Any]]:
        if whisper_only:
            return store.pending_jobs(stages=("ingest",), status="skipped", notes=NO_SUBS_NOTE)
        return store.pending_jobs(stages=("ingest", "extract") if stage == "all" else ("ingest",))

    total = len(candidates())
    if limit is not None:
        total = min(total, max(0, limit))
    started = deps.clock()
    blocked_streak: list[int] = []
    seen: set[int] = set()
    position = 0
    try:
        while position < total:
            job = next((j for j in candidates() if j["id"] not in seen), None)
            if job is None:
                break
            seen.add(job["id"])
            position += 1
            tag = f"[{position}/{total}]"
            store.update_job(job["id"], status="running", started_at=utc_now(), last_error=None)
            changed()
            network = True
            tries = 0
            while True:
                tries += 1
                store.update_job(job["id"], attempts=job["attempts"] + tries)
                name = job["title"] or job["video_id"]
                try:
                    parts, finished, network = _process(
                        store, job, stage=stage, langs=langs, no_whisper=no_whisper,
                        whisper=whisper_only, whisper_model=whisper_model,
                        cookies_from=cookies_from, deps=deps)
                except KeyboardInterrupt:
                    # El trabajo en curso queda limpio: vuelve a la cola sin contar el intento.
                    store.update_job(job["id"], status="pending", started_at=None,
                                     attempts=job["attempts"] + tries - 1)
                    raise
                except NoSubtitles as exc:
                    store.update_job(job["id"], status="skipped", notes=NO_SUBS_NOTE,
                                     finished_at=utc_now())
                    summary.skipped += 1
                    blocked_streak.clear()
                    out(f"{tag} {name} · saltado · {exc}")
                    break
                except Exception as exc:
                    kind, reason = classify_error(exc)
                    if kind != "permanent" and tries < MAX_ATTEMPTS:
                        wait = BACKOFF_S[min(tries, len(BACKOFF_S)) - 1]
                        out(f"{tag} {name} · intento {tries} fallido: {reason} · "
                            f"reintento en {wait:g} s")
                        deps.sleep(wait)
                        continue
                    store.update_job(job["id"], status="failed", last_error=reason,
                                     finished_at=utc_now())
                    summary.failed += 1
                    if kind == "blocked":
                        blocked_streak.append(job["id"])
                    else:
                        blocked_streak.clear()
                    suffix = "" if kind == "permanent" else f" (tras {tries} intentos)"
                    out(f"{tag} {name} · fallido · {reason}{suffix}")
                    break
                else:
                    blocked_streak.clear()
                    name = job["title"] or job["video_id"]
                    if finished:
                        store.update_job(job["id"], status="done", finished_at=utc_now())
                        summary.done += 1
                    else:
                        store.update_job(job["id"], status="pending", started_at=None)
                        summary.ingested += 1
                    out(f"{tag} {name} · " + " · ".join(parts))
                    break

            changed()
            if len(blocked_streak) >= MAX_BLOCKED_STREAK:
                for job_id in blocked_streak:  # no fue culpa de esos vídeos
                    store.retry_jobs(job_id=job_id)
                summary.failed -= len(blocked_streak)
                summary.blocked = True
                out(f"YouTube ha rechazado {len(blocked_streak)} vídeos seguidos: se detiene el "
                    "lote y esos vídeos vuelven a la cola. Espera un rato (o usa --cookies-from) "
                    "y vuelve a lanzar `queue run`.")
                break
            if network and position < total:
                low, high = DEFAULT_DELAY_S if delay is None else (
                    delay * (1 - DELAY_JITTER), delay * (1 + DELAY_JITTER))
                pause = deps.uniform(low, high)
                if pause > 0:
                    deps.sleep(pause)
    except KeyboardInterrupt:
        summary.interrupted = True
        store.recover_running_jobs()
        out("Interrumpido: el vídeo en curso vuelve a la cola.")
    summary.seconds = deps.clock() - started
    summary.remaining = len(store.pending_jobs(
        stages=("ingest", "extract") if stage == "all" else ("ingest",)))
    return summary


def format_summary(summary: RunSummary) -> str:
    parts = [f"{summary.done} hechos"]
    if summary.ingested:
        parts.append(f"{summary.ingested} ingeridos (pendientes de extracción)")
    parts += [f"{summary.failed} fallidos", f"{summary.skipped} saltados",
              f"tiempo total {_elapsed(summary.seconds)}"]
    text = "Resumen: " + " · ".join(parts)
    if summary.remaining:
        text += f" · quedan {summary.remaining} en cola"
    return text
