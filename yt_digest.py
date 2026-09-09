#!/usr/bin/env python3
"""
yt_digest.py — De una URL de YouTube a un informe corto en Markdown:
    1. Esquema del contenido
    2. Tesis principal
    3. Highlights

Cuatro backends de análisis (--backend):
    claude-code   Usa el CLI `claude` que ya tienes instalado. Sin API key.
    ollama        Modelo local vía Ollama. Sin API key y sin red.
    api           API de Anthropic (necesita ANTHROPIC_API_KEY).
    none          Solo transcribe y deja el prompt listo para pegar en un chat.

Uso:
    python yt_digest.py "https://www.youtube.com/watch?v=XXXX" -o informe.md
    python yt_digest.py URL --backend ollama --ollama-model qwen3:14b
    python yt_digest.py URL --backend none -o para_pegar.md

Dependencias:
    pip install yt-dlp                # imprescindible
    pip install anthropic             # solo para --backend api
    pip install faster-whisper        # solo si el vídeo no tiene subtítulos
"""

from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

API_MODEL = "claude-sonnet-5"

# Cuánta transcripción cabe en una sola llamada, por backend. Por encima
# de ese tamaño el script pasa a map-reduce (notas por fragmento + síntesis).
# ~3,5 caracteres por token en español.
MAX_CHARS = {
    "api": 400_000,
    "claude-code": 400_000,
    "ollama": 60_000,     # asume num_ctx 32k; súbelo si tu modelo da más
}

OLLAMA_CTX = 32_768


# --------------------------------------------------------------------------
# 1. TRANSCRIPCIÓN
# --------------------------------------------------------------------------

def fetch_subtitles(url: str, langs: list[str], tmpdir: str, cookies_from: str | None):
    """Descarga metadatos + subtítulos (manuales o automáticos) en formato json3.

    Devuelve (info, cues) donde cada cue es (segundo_inicio, texto).
    La lista viene vacía si el vídeo no tiene subtítulos en esos idiomas.
    """
    from yt_dlp import YoutubeDL

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 30,
    }
    if cookies_from:
        opts["cookiesfrombrowser"] = (cookies_from,)

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    track = choose_subtitle_track(info, langs)
    if not track:
        return info, []

    try:
        req = urllib.request.Request(
            track["url"],
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        print(f"  No se pudieron descargar subtítulos ({exc.code}); se intentará transcribir audio.", file=sys.stderr)
        return info, []
    except urllib.error.URLError as exc:
        print(f"  No se pudieron descargar subtítulos ({exc.reason}); se intentará transcribir audio.", file=sys.stderr)
        return info, []

    return info, parse_json3_text(data)


def choose_subtitle_track(info: dict, langs: list[str]) -> dict | None:
    """Elige la mejor pista json3: manual primero, automática después."""
    for source in ("subtitles", "automatic_captions"):
        tracks_by_lang = info.get(source) or {}
        for lang in matching_langs(tracks_by_lang, langs):
            json3_tracks = [t for t in tracks_by_lang.get(lang, []) if t.get("ext") == "json3"]
            if json3_tracks:
                return json3_tracks[0]
    return None


def matching_langs(tracks_by_lang: dict, langs: list[str]) -> list[str]:
    matches = []
    for wanted in langs:
        for available in tracks_by_lang:
            if available == wanted or available.startswith(f"{wanted}-"):
                matches.append(available)
    return matches


def parse_json3(path: str) -> list[tuple[float, str]]:
    """Convierte el json3 de YouTube en una lista de (segundos, texto)."""
    return parse_json3_text(Path(path).read_text(encoding="utf-8"))


def parse_json3_text(raw: str) -> list[tuple[float, str]]:
    """Convierte el contenido json3 de YouTube en una lista de (segundos, texto)."""
    data = json.loads(raw)
    cues: list[tuple[float, str]] = []

    for ev in data.get("events", []):
        # aAppend marca los eventos de "rollup" de los subtítulos automáticos:
        # repiten la línea anterior y ensucian la transcripción.
        if ev.get("aAppend"):
            continue
        segs = ev.get("segs")
        if not segs:
            continue
        text = " ".join("".join(s.get("utf8", "") for s in segs).split())
        if not text or (cues and cues[-1][1] == text):
            continue
        cues.append((ev.get("tStartMs", 0) / 1000.0, text))

    return cues


def transcribe_audio(url: str, tmpdir: str, model_size: str, language: str | None,
                     cookies_from: str | None) -> list[tuple[float, str]]:
    """Fallback: descarga el audio y lo transcribe en local con faster-whisper."""
    from yt_dlp import YoutubeDL

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "format": "bestaudio/best",
        "outtmpl": str(Path(tmpdir) / "audio.%(ext)s"),
    }
    if cookies_from:
        opts["cookiesfrombrowser"] = (cookies_from,)

    with YoutubeDL(opts) as ydl:
        ydl.download([url])

    audio = glob.glob(str(Path(tmpdir) / "audio.*"))
    if not audio:
        raise RuntimeError("No se pudo descargar el audio del vídeo.")

    from faster_whisper import WhisperModel

    print(f"  Transcribiendo con Whisper ({model_size})… esto puede tardar.", file=sys.stderr)
    model = WhisperModel(model_size, device="auto", compute_type="int8")
    segments, _ = model.transcribe(audio[0], language=language, vad_filter=True)
    return [(seg.start, seg.text.strip()) for seg in segments if seg.text.strip()]


def hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def build_transcript(cues: list[tuple[float, str]], step: int = 30) -> str:
    """Agrupa los cues en bloques de ~30 s con marca de tiempo al principio.

    Las marcas son lo que luego permite que el esquema y los highlights
    apunten a un minuto concreto del vídeo.
    """
    if not cues:
        return ""

    blocks, current, block_start = [], [], cues[0][0]
    for start, text in cues:
        if start - block_start >= step and current:
            blocks.append(f"[{hms(block_start)}] " + " ".join(current))
            current, block_start = [], start
        current.append(text)
    if current:
        blocks.append(f"[{hms(block_start)}] " + " ".join(current))

    return "\n".join(blocks)


# --------------------------------------------------------------------------
# 2. PROMPTS
# --------------------------------------------------------------------------

SYSTEM = """Eres un analista que sintetiza contenido audiovisual para alguien que \
no tiene tiempo de ver el vídeo entero.

Trabajas sobre una transcripción con marcas de tiempo [hh:mm:ss]. Reglas:
- Escribe siempre en español, en Markdown, sin preámbulos ni despedidas.
- Parafrasea: no copies frases literales de la transcripción salvo una cita muy \
breve cuando la formulación exacta sea el punto.
- Cita la marca de tiempo relevante entre paréntesis cuando afirmes algo concreto.
- Si el vídeo no defiende ninguna tesis (es un tutorial, una entrevista dispersa, \
etc.), dilo claramente en lugar de inventar una.
- Distingue lo que afirma el vídeo de lo que tú deduces."""

FINAL_PROMPT = """Aquí tienes la transcripción de un vídeo de YouTube.

Título: {title}
Canal: {channel}
Duración: {duration}

<transcripcion>
{transcript}
</transcripcion>

Redacta un informe con exactamente estas tres secciones:

## 1. Esquema del contenido
Estructura jerárquica de los temas tratados, en el orden del vídeo, con la marca \
de tiempo de cada bloque. Dos niveles como máximo. Que se pueda usar como índice \
para saltar a una parte concreta. Mantén cada punto en una frase.

## 2. Tesis principal
Uno o dos párrafos: qué defiende el vídeo, sobre qué lo apoya y a qué conclusión \
llega. Si hay una tesis secundaria relevante, menciónala en una línea aparte.

## 3. Highlights
Los 2 o 3 momentos con más valor: el dato, giro o idea que justifica ver esa \
parte. Para cada uno: marca de tiempo, qué se dice y por qué importa.

Extensión total orientativa: 500-800 palabras. Prioriza densidad y claridad \
antes que exhaustividad literal."""

CHUNK_PROMPT = """Este es el fragmento {i} de {n} de la transcripción de un vídeo \
("{title}").

<fragmento>
{transcript}
</fragmento>

Toma notas estructuradas de este fragmento: temas tratados con sus marcas de \
tiempo, argumentos o afirmaciones importantes, y cualquier dato o momento que \
destaque. No resumas todavía ni saques conclusiones globales: son notas de \
trabajo para sintetizar después."""


# --------------------------------------------------------------------------
# 3. BACKENDS
# --------------------------------------------------------------------------

def run_api(user: str, max_tokens: int) -> str:
    import anthropic

    client = anthropic.Anthropic()  # lee ANTHROPIC_API_KEY del entorno
    msg = client.messages.create(
        model=API_MODEL,
        max_tokens=max_tokens,
        system=SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in msg.content if b.type == "text")


def run_claude_code(user: str, _max_tokens: int) -> str:
    """Usa el CLI `claude` en modo print. Consume tu suscripción, no la API.

    --bare evita que arrastre CLAUDE.md, skills, MCP o memoria del directorio
    actual, para que el resultado no dependa de dónde lances el script.
    Si tu versión no reconoce el flag, quítalo de la lista.
    """
    cmd = ["claude", "-p", "--bare"]
    proc = subprocess.run(
        cmd,
        input=f"{SYSTEM}\n\n---\n\n{user}",
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude falló ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout.strip()


def run_ollama(user: str, _max_tokens: int, model: str = "qwen3:14b",
               host: str = "http://localhost:11434") -> str:
    """Modelo local. Ojo con num_ctx: si no lo fijas, Ollama trunca en silencio."""
    payload = {
        "model": model,
        "stream": False,
        "options": {"num_ctx": OLLAMA_CTX, "temperature": 0.3},
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user},
        ],
    }
    req = urllib.request.Request(
        f"{host}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=1800) as resp:
        data = json.loads(resp.read())
    return data["message"]["content"].strip()


def chunk(text: str, size: int) -> list[str]:
    """Trocea por líneas para no partir un bloque con marca de tiempo por la mitad."""
    chunks, current, length = [], [], 0
    for line in text.split("\n"):
        if length + len(line) > size and current:
            chunks.append("\n".join(current))
            current, length = [], 0
        current.append(line)
        length += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def analyze(transcript: str, meta: dict, backend: str, ollama_model: str) -> str:
    if backend == "api":
        call = lambda u, t=4000: run_api(u, t)
    elif backend == "claude-code":
        call = lambda u, t=4000: run_claude_code(u, t)
    elif backend == "ollama":
        call = lambda u, t=4000: run_ollama(u, t, ollama_model)
    else:
        raise ValueError(backend)

    limit = MAX_CHARS[backend]

    if len(transcript) <= limit:
        return call(FINAL_PROMPT.format(transcript=transcript, **meta))

    pieces = chunk(transcript, limit // 2)
    print(f"  Transcripción larga: procesando en {len(pieces)} fragmentos.", file=sys.stderr)

    notes = []
    for i, piece in enumerate(pieces, 1):
        print(f"    Fragmento {i}/{len(pieces)}…", file=sys.stderr)
        notes.append(call(CHUNK_PROMPT.format(
            i=i, n=len(pieces), title=meta["title"], transcript=piece)))

    return call(FINAL_PROMPT.format(transcript="\n\n".join(notes), **meta), 5000)


# --------------------------------------------------------------------------
# 4. CLI
# --------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Esquema, tesis y highlights de un vídeo de YouTube.")
    p.add_argument("url")
    p.add_argument("-o", "--output", help="Fichero .md de salida (por defecto, stdout)")
    p.add_argument("--backend", default="claude-code",
                   choices=["claude-code", "ollama", "api", "none"])
    p.add_argument("--ollama-model", default="qwen3:14b")
    p.add_argument("--lang", default="es,en", help="Idiomas de subtítulo por preferencia")
    p.add_argument("--whisper", action="store_true", help="Forzar transcripción con Whisper")
    p.add_argument("--whisper-model", default="small", help="tiny, base, small, medium, large-v3")
    p.add_argument("--cookies-from", metavar="NAVEGADOR",
                   help="chrome, firefox… para vídeos que exigen sesión")
    p.add_argument("--save-transcript", metavar="RUTA", help="Guardar también la transcripción")
    args = p.parse_args()

    # Comprobaciones antes de gastar tiempo descargando
    if not module_available("yt_dlp"):
        print("Falta el paquete Python `yt-dlp`. Instálalo con: pip install yt-dlp", file=sys.stderr)
        return 1
    if args.backend == "api" and not os.environ.get("ANTHROPIC_API_KEY"):
        print("Falta la variable de entorno ANTHROPIC_API_KEY.", file=sys.stderr)
        return 1
    if args.backend == "api" and not module_available("anthropic"):
        print("Falta el paquete Python `anthropic`. Instálalo con: pip install anthropic", file=sys.stderr)
        return 1
    if args.backend == "claude-code" and not shutil.which("claude"):
        print("No encuentro el CLI `claude` en el PATH.", file=sys.stderr)
        return 1
    if args.whisper and not module_available("faster_whisper"):
        print("Falta el paquete Python `faster-whisper`. Instálalo con: pip install faster-whisper", file=sys.stderr)
        return 1

    langs = [l.strip() for l in args.lang.split(",") if l.strip()]

    with tempfile.TemporaryDirectory() as tmp:
        print("→ Obteniendo vídeo y subtítulos…", file=sys.stderr)
        info, cues = fetch_subtitles(args.url, langs, tmp, args.cookies_from)

        if args.whisper or not cues:
            if not module_available("faster_whisper"):
                print("El vídeo no tiene subtítulos disponibles y falta `faster-whisper` para transcribir el audio.", file=sys.stderr)
                print("Instálalo con: pip install faster-whisper", file=sys.stderr)
                return 1
            if not cues and not args.whisper:
                print("  Sin subtítulos disponibles; usando Whisper.", file=sys.stderr)
            cues = transcribe_audio(args.url, tmp, args.whisper_model,
                                    langs[0] if langs else None, args.cookies_from)

    if not cues:
        print("No se pudo obtener ninguna transcripción.", file=sys.stderr)
        return 1

    transcript = build_transcript(cues)
    meta = {
        "title": info.get("title", "—"),
        "channel": info.get("uploader", "—"),
        "duration": hms(info.get("duration") or cues[-1][0]),
    }

    if args.save_transcript:
        Path(args.save_transcript).write_text(transcript, encoding="utf-8")

    if args.backend == "none":
        # Deja el prompt completo listo para copiar y pegar en cualquier chat.
        body = SYSTEM + "\n\n---\n\n" + FINAL_PROMPT.format(transcript=transcript, **meta)
    else:
        print(f"→ Analizando con {args.backend} ({len(transcript):,} caracteres)…", file=sys.stderr)
        report = analyze(transcript, meta, args.backend, args.ollama_model)
        body = (f"# {meta['title']}\n\n"
                f"**Canal:** {meta['channel']} · **Duración:** {meta['duration']}\n"
                f"**Fuente:** {args.url}\n\n---\n\n" + report)

    if args.output:
        Path(args.output).write_text(body + "\n", encoding="utf-8")
        print(f"✓ Escrito en {args.output}", file=sys.stderr)
    else:
        print(body)

    return 0


if __name__ == "__main__":
    sys.exit(main())
