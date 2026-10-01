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


class MetaAccessError(RuntimeError):
    """Meta rechaza la petición por clave, permisos, modelo o saldo: reintentar no lo arregla."""


def call_meta(prompt, key, model, system=None):
    request = urllib.request.Request(
        "https://api.meta.ai/v1/chat/completions",
        data=json.dumps({"model": model, "messages": [
            {"role": "system", "content": system or digest.SYSTEM},
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
        if error.code in messages:
            raise MetaAccessError(messages[error.code]) from None
        raise RuntimeError(f"Meta devolvió un error ({error.code}). Inténtalo más tarde.") from None
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


def _job_view(job, verified):
    status, stage = job["status"], job["stage"]
    if status == "pending":
        state, detail = ("saved", "Transcripción guardada · sin afirmaciones") if stage == "extract" else ("pending", "En cola")
    elif status == "running":
        state, detail = "running", "Extrayendo afirmaciones…" if stage == "extract" else "Guardando la transcripción…"
    elif status == "done":
        n = verified.get(job["video_id"], 0)
        state, detail = "done", f"{n} afirmaciones verificadas"
    elif status == "failed":
        prefix = "Extracción: " if stage == "extract" else ""
        state, detail = "failed", prefix + (job["last_error"] or "No se pudo procesar.")
    else:
        state, detail = "skipped", job["notes"] or "Saltado"
    return {"id": job["id"], "title": job["title"] or job["video_id"], "state": state, "detail": detail}


def emit_queue(store):
    verified = {row["external_id"]: row["verified"] for row in store.list_sources()}
    jobs = [_job_view(job, verified) for job in store.list_jobs()]
    count = lambda state: sum(1 for job in jobs if job["state"] == state)
    summary = (f"{count('saved') + count('done')} guardados · {count('pending') + count('running')} en cola · "
               f"{count('failed')} fallidos · {count('skipped')} sin subtítulos") if jobs else ""
    emit("queue", jobs=jobs, summary=summary,
         counts={state: count(state) for state in ("saved", "done", "pending", "running", "failed", "skipped")})


def extraction_backend(options):
    """Modelo que extrae las afirmaciones: Muse Spark (clave de Meta) o el CLI de Claude Code."""
    from rezme import backends

    engine = options.get("engine") or "meta"
    if engine == "meta":
        key, model = options.get("key", "").strip(), options.get("model") or "muse-spark-1.3"
        if not key:
            raise ValueError("Introduce tu clave de Meta en los ajustes.")

        def call(system, user):
            try:
                return call_meta(user, key, model, system)
            except MetaAccessError as error:
                raise backends.BackendUnavailable(str(error)) from None
        return backends.Backend("meta", model, call)
    if engine == "claude-code":
        problem = backends.check_backend("claude-code")
        if problem:
            raise ValueError("No encuentro Claude Code en este Mac. Elige Muse Spark en los ajustes o instala el CLI `claude`.")
        return backends.make_backend("claude-code")
    raise ValueError("Motor de extracción no válido.")


def run_queue(options):
    """Cola de lotes para la app: guarda transcripciones y extrae afirmaciones, vídeo a vídeo."""
    from rezme import Store, batch

    action = options.get("action", "status")
    if action not in ("status", "run", "extract", "retry", "clear"):
        raise ValueError("Acción de cola no válida.")
    if not options.get("db"):
        raise ValueError("No se encuentra la base de datos de reZme.")
    cookies = options.get("browser") or None
    whisper = bool(options.get("whisper"))
    progress = lambda message: emit("progress", message=message)
    with Store(options["db"]) as store:
        if action == "retry":
            store.retry_jobs(status="failed")
            store.retry_jobs(status="skipped")
        elif action == "clear":
            store.clear_done_jobs()
        elif action == "extract":
            from rezme import extract
            backend = extraction_backend(options)

            def extractor(target, source_id):
                title = (target.get_source_by_id(source_id) or {}).get("title") or "Vídeo"
                return extract.extract_source(
                    target, source_id, backend,
                    progress=lambda message: progress(f"{title} · {message.strip().rstrip('…')}"))

            deps = batch.Deps()
            deps.extractor = extractor
            store.recover_running_jobs()
            emit_queue(store)
            summary = batch.run_queue(store, stage="extract", delay=0, deps=deps, out=progress,
                                      on_change=lambda: emit_queue(store))
            emit_queue(store)
            if summary.stopped:
                raise RuntimeError(f"Extracción detenida: {summary.stopped}")
            emit("done", message=batch.format_summary(summary))
            return
        elif action == "run":
            urls = batch.read_urls(options.get("urls", "").splitlines())
            if urls:
                emit("progress", message="Leyendo los vídeos de la lista…")
                # En la app, una URL con `list=` significa la lista entera.
                report = batch.add_urls(store, urls, whole_playlist=True, cookies_from=cookies)
                if report.invalid and not (report.added or report.already):
                    raise ValueError("Introduce una URL válida de YouTube (vídeo o lista).")
                message = f"{report.added} vídeos añadidos · {report.already} ya estaban"
                if report.inaccessible:
                    message += f" · {len(report.inaccessible)} no accesibles"
                emit("progress", message=message)
            if whisper:  # con Whisper activado, se recuperan los que quedaron sin subtítulos
                store.retry_jobs(status="skipped")
            store.recover_running_jobs()
            emit_queue(store)
            summary = batch.run_queue(
                store, no_whisper=not whisper, cookies_from=cookies, out=progress,
                on_change=lambda: emit_queue(store))
            emit_queue(store)
            emit("done", message=batch.format_summary(summary))
            return
        emit_queue(store)


def run_library(options):
    """Lo que hay en la base: recuentos, vídeos guardados y búsqueda de afirmaciones verificadas."""
    from rezme import Store
    from rezme.batch import ORIGIN_LABELS

    if not options.get("db"):
        raise ValueError("No se encuentra la base de datos de reZme.")
    with Store(options["db"]) as store:
        stats = store.stats()
        by_status = stats["claims_by_status"]
        sources = []
        for row in store.list_sources():
            if row["n_cues"] is None:
                continue
            frases = f"{row['n_cues']:,}".replace(",", ".")
            sources.append({
                "id": row["id"], "title": row["title"] or row["external_id"],
                "channel": row["channel"] or "", "date": row["published_at"] or "",
                "detail": f"{frases} frases · {ORIGIN_LABELS.get(row['origin'], row['origin'])}",
                "verified": row["verified"], "url": row["url"] or ""})
        # Lo que falta por extraer: vídeos con transcripción y sin afirmaciones, y llamadas estimadas.
        from rezme.chunking import chunk_transcript
        waiting = store.pending_jobs(stages=("extract",))
        calls = 0
        for job in waiting:
            source = store.get_source("youtube", job["video_id"])
            transcript = store.latest_transcript(source["id"]) if source else None
            if transcript:
                chapters = json.loads(source["chapters_json"]) if source.get("chapters_json") else None
                calls += len(chunk_transcript(transcript["cues"], chapters, source.get("duration_s")))
        emit("library", sources=sources, stats={
            "videos": len(sources), "verified": by_status.get("verified", 0),
            "ungrounded": by_status.get("ungrounded", 0), "entities": stats["entities"],
            "to_extract": len(waiting), "calls": calls})
        query = options.get("query", "").strip()
        if query:
            titles = {row["id"]: row["title"] for row in store.list_sources()}
            try:
                rows = store.search_claims(query, limit=40)
            except ValueError:
                rows = []
            emit("hits", items=[{
                "id": row["id"], "statement": row["statement"],
                "meta": " · ".join(part for part in (
                    row["type"], row["entity_name"], titles.get(row["source_id"]),
                    digest.hms(row["ts_start"]) if row["ts_start"] is not None else None) if part),
            } for row in rows])


def _terminate(*_):
    raise KeyboardInterrupt  # Cancelar desde la app: la cola deja el vídeo en curso limpio.


if __name__ == "__main__":
    import signal
    signal.signal(signal.SIGTERM, _terminate)
    try:
        options = json.load(sys.stdin)
        if options.get("mode") == "queue":
            run_queue(options)
        elif options.get("mode") == "library":
            run_library(options)
        else:
            run(options)
    except Exception as error:
        # Network/extractor exceptions may contain URLs; never include request headers or keys.
        message = str(error)
        if "Sign in" in message or "bot" in message or "429" in message:
            message = "YouTube ha bloqueado la descarga. Prueba a seleccionar tu navegador en las opciones o pega la transcripción del vídeo."
        emit("error", message=message[:1200])
        sys.exit(1)
