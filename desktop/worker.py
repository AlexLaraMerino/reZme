"""JSON-lines bridge for the macOS app. Secrets only arrive over stdin."""
import contextlib
import json
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import yt_digest as digest


def emit(kind, **values):
    print(json.dumps({"type": kind, **values}, ensure_ascii=False), flush=True)


def validate_url(url):
    parts = urllib.parse.urlparse(url)
    if parts.scheme not in ("https", "http") or parts.hostname not in (
        "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"
    ):
        raise ValueError("Introduce una URL válida de YouTube.")
    if parts.username or parts.password or parts.port:
        raise ValueError("La dirección de YouTube no es válida.")
    return url


def call_meta(prompt, key, model):
    request = urllib.request.Request(
        "https://api.meta.ai/v1/chat/completions",
        data=json.dumps({"model": model, "messages": [
            {"role": "system", "content": digest.SYSTEM},
            {"role": "user", "content": prompt},
        ], "max_completion_tokens": 12000}).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=240) as response:
            data = json.load(response)
    except urllib.error.HTTPError as error:
        messages = {401: "La clave API no es válida.", 403: "Tu cuenta no tiene acceso a este modelo.",
                    404: "No se encuentra el modelo. Revisa su nombre en los ajustes.",
                    429: "Meta ha alcanzado el límite de uso o saldo de tu cuenta. Inténtalo más tarde."}
        raise RuntimeError(messages.get(error.code, f"Meta devolvió un error ({error.code}). Inténtalo más tarde.")) from None
    choice = data["choices"][0]
    if choice.get("finish_reason") == "length":
        raise RuntimeError("Meta alcanzó el límite de respuesta. No se ha guardado un informe incompleto; prueba el modo prompt.")
    result = choice["message"].get("content", "")
    if not isinstance(result, str) or not result.strip():
        raise RuntimeError("Meta no devolvió texto. Prueba otra vez o utiliza el modo prompt.")
    return result.strip()


def run(options):
    mode = options.get("mode", "prompt")
    if mode not in ("prompt", "api"):
        raise ValueError("Elige uno de los dos modos disponibles.")
    if mode == "api" and not options.get("key", "").strip():
        raise ValueError("Introduce tu clave de Meta en los ajustes.")
    url = validate_url(options.get("url", "").strip())
    pasted = options.get("transcript", "").strip()
    if pasted:
        transcript = pasted
        meta = {"title": "Vídeo de YouTube", "channel": "Transcripción aportada", "duration": "—"}
    else:
        emit("progress", message="Buscando subtítulos del vídeo…")
        with tempfile.TemporaryDirectory() as temp:
            cookies = options.get("browser") or None
            with contextlib.redirect_stdout(sys.stderr):
                info, cues = digest.fetch_subtitles(url, ["es", "en"], temp, cookies)
            if not cues:
                emit("progress", message="Transcribiendo el audio en tu Mac. La primera vez se descargará el modelo; puede tardar varios minutos…")
                with contextlib.redirect_stdout(sys.stderr):
                    cues = digest.transcribe_audio(url, temp, "small", None, cookies)
            if not cues:
                raise RuntimeError("No se pudo obtener texto del vídeo. Puedes pegar su transcripción en las opciones.")
        transcript = digest.build_transcript(cues)
        meta = {"title": info.get("title") or "Vídeo de YouTube", "channel": info.get("uploader") or "—",
                "duration": digest.hms(info.get("duration") or cues[-1][0])}
    if mode == "prompt":
        body = digest.SYSTEM + "\n\nFuente: " + url + "\n\n" + digest.FINAL_PROMPT.format(transcript=transcript, **meta)
    else:
        emit("progress", message="Preparando el informe con Muse Spark…")
        if len(transcript) > 180000:
            pieces = digest.chunk(transcript, 90000)
            notes = []
            for i, piece in enumerate(pieces, 1):
                emit("progress", message=f"Analizando fragmento {i} de {len(pieces)}…")
                notes.append(call_meta(digest.CHUNK_PROMPT.format(i=i, n=len(pieces), title=meta["title"], transcript=piece), options["key"], options["model"]))
            transcript = "\n\n".join(notes)
        body = call_meta(digest.FINAL_PROMPT.format(transcript=transcript, **meta), options["key"], options["model"])
        body = f"# {meta['title']}\n\n**Canal:** {meta['channel']} · **Duración:** {meta['duration']}\n**Fuente:** {url}\n\n" + body
    emit("result", text=body, title=meta["title"])


if __name__ == "__main__":
    try:
        run(json.load(sys.stdin))
    except Exception as error:
        # Network/extractor exceptions may contain URLs; never include request headers or keys.
        message = str(error)
        if "Sign in" in message or "bot" in message or "429" in message:
            message = "YouTube ha bloqueado la descarga. Prueba a seleccionar tu navegador en las opciones o pega la transcripción del vídeo."
        emit("error", message=message[:1200])
        sys.exit(1)
