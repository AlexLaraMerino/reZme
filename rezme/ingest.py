"""Ingesta: URL de YouTube -> fuente + transcripción cruda persistida.

Reutiliza el formato de subtítulos y la transcripción de `yt_digest`. Lo nuevo
es que guarda los cues originales (con segundos), los metadatos y el origen del
texto, que prefiere los subtítulos en el idioma original del vídeo y que es
idempotente: una URL ya ingerida no vuelve a descargarse.
"""
from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from typing import Any, Callable, Sequence
from urllib.parse import parse_qs, urlparse

import yt_digest

from .store import Store

PLATFORM = "youtube"
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}

Cues = list[tuple[float, str]]


@dataclass
class IngestResult:
    source_id: int
    transcript_id: int
    video_id: str
    origin: str
    n_cues: int
    skipped_download: bool
    new_transcript: bool


def video_id_from_url(url: str) -> str | None:
    """Id de 11 caracteres de una URL de YouTube; None si no es una URL válida."""
    parts = urlparse(url.strip())
    if parts.scheme not in ("http", "https") or parts.username or parts.password:
        return None
    host = (parts.hostname or "").lower()
    candidate = ""
    if host == "youtu.be":
        candidate = parts.path.lstrip("/").split("/")[0]
    elif host in _YT_HOSTS:
        if parts.path == "/watch":
            candidate = (parse_qs(parts.query).get("v") or [""])[0]
        else:
            m = re.match(r"^/(?:shorts|embed|live|v)/([^/?#]+)", parts.path)
            candidate = m.group(1) if m else ""
    return candidate if _ID_RE.match(candidate) else None


class SubtitleDownloadError(RuntimeError):
    """El vídeo tiene subtítulos pero su descarga ha fallado. Conserva los metadatos."""

    def __init__(self, info: dict[str, Any], message: str):
        super().__init__(message)
        self.info = info


def _json3(tracks: Sequence[dict[str, Any]] | None) -> dict[str, Any] | None:
    return next((t for t in tracks or [] if t.get("ext") == "json3"), None)


def _is_translation(track: dict[str, Any]) -> bool:
    return "tlang=" in str(track.get("url") or "")


def choose_track(info: dict[str, Any],
                 langs: Sequence[str]) -> tuple[dict[str, Any], str, str] | None:
    """Mejor pista de subtítulos: (pista json3, origen, idioma), o None si no hay.

    Orden: subtítulos manuales en los idiomas pedidos; automáticos en el idioma
    original del vídeo; y solo al final las traducciones automáticas, que son
    peores y que YouTube limita con errores 429.
    """
    langs = list(langs)
    manual = info.get("subtitles") or {}
    for lang in yt_digest.matching_langs(manual, langs):
        track = _json3(manual.get(lang))
        if track:
            return track, "subtitles_manual", lang
    auto = info.get("automatic_captions") or {}
    originals = {lang: track for lang in auto
                 if (track := _json3(auto[lang])) and not _is_translation(track)}
    preferred = yt_digest.matching_langs(originals, langs)
    spoken = str(info.get("language") or "").split("-")[0]
    native = [l for l in originals if l.endswith("-orig") or (spoken and l.split("-")[0] == spoken)]
    for lang in [*preferred, *native, *originals]:
        return originals[lang], "subtitles_auto", lang.removesuffix("-orig")
    for lang in yt_digest.matching_langs(auto, langs):
        track = _json3(auto.get(lang))
        if track:
            return track, "subtitles_auto", lang
    return None


def subtitle_origin(info: dict[str, Any], langs: Sequence[str]) -> tuple[str, str | None]:
    """Origen (manual o automático) e idioma de la pista que se elige para el vídeo."""
    chosen = choose_track(info, langs)
    return (chosen[1], chosen[2]) if chosen else ("subtitles_auto", None)


def fetch_subtitles(url: str, langs: Sequence[str], tmpdir: str,
                    cookies_from: str | None) -> tuple[dict[str, Any], Cues]:
    """Metadatos y cues del vídeo. Misma firma que `yt_digest.fetch_subtitles`.

    A diferencia de aquella, elige la pista con `choose_track`, la descarga con
    la sesión de yt-dlp (cookies incluidas) y no oculta los fallos: si la pista
    existe y no se puede descargar lanza `SubtitleDownloadError`.
    """
    from yt_dlp import YoutubeDL

    opts: dict[str, Any] = {"quiet": True, "no_warnings": True, "skip_download": True,
                            "noplaylist": True, "socket_timeout": 30}
    if cookies_from:
        opts["cookiesfrombrowser"] = (cookies_from,)
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        chosen = choose_track(info, langs)
        if not chosen:
            return info, []
        try:
            raw = ydl.urlopen(chosen[0]["url"]).read().decode("utf-8")
        except Exception as exc:
            raise SubtitleDownloadError(
                info, f"hay subtítulos pero no se pudieron descargar ({exc})") from None
    return info, yt_digest.parse_json3_text(raw)


def upload_date(info: dict[str, Any]) -> str | None:
    """'20260819' -> '2026-08-19'."""
    raw = str(info.get("upload_date") or "")
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}" if re.fullmatch(r"\d{8}", raw) else None


def ingest_url(store: Store, url: str, langs: Sequence[str] = ("es", "en"), *,
               force: bool = False, whisper: bool = False, whisper_model: str = "small",
               cookies_from: str | None = None,
               fetch: Callable[..., tuple[dict, Cues]] | None = None,
               transcribe: Callable[..., Cues] | None = None) -> IngestResult:
    video_id = video_id_from_url(url)
    if not video_id:
        raise ValueError("Introduce una URL válida de YouTube.")

    existing = store.get_source(PLATFORM, video_id)
    if existing and not force and not whisper:
        transcript = store.latest_transcript(existing["id"])
        if transcript:
            return IngestResult(existing["id"], transcript["id"], video_id, transcript["origin"],
                                transcript["n_cues"], skipped_download=True, new_transcript=False)

    fetch = fetch or fetch_subtitles
    transcribe = transcribe or yt_digest.transcribe_audio
    langs = list(langs)

    with tempfile.TemporaryDirectory() as tmp:
        info, cues = fetch(url, langs, tmp, cookies_from)
        origin, language = subtitle_origin(info, langs) if cues else ("whisper", None)
        if whisper or not cues:
            cues = transcribe(url, tmp, whisper_model, langs[0] if langs else None, cookies_from)
            origin = "whisper"
            language = langs[0] if langs else None
    if not cues:
        raise RuntimeError("No se pudo obtener ninguna transcripción.")

    source_id, _ = store.add_source(
        PLATFORM, video_id, url=url, title=info.get("title"),
        channel=info.get("channel") or info.get("uploader"), channel_id=info.get("channel_id"),
        published_at=upload_date(info), duration_s=info.get("duration"),
        language=info.get("language") or language, description=info.get("description"),
        chapters=info.get("chapters") or None)
    transcript_id, created = store.save_transcript(source_id, cues, origin, language)
    return IngestResult(source_id, transcript_id, video_id, origin, len(cues),
                        skipped_download=False, new_transcript=created)
