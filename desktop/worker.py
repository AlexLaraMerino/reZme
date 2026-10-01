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


class MetaRateLimit(RuntimeError):
    """Demasiadas peticiones (429). `retry_after` son los segundos que pide esperar Meta, si lo dice."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


META_URL = "https://api.meta.ai/v1/chat/completions"
REPORT_MAX_TOKENS = 12000
# La extracción devuelve JSON largo y algunos modelos gastan tokens en razonar antes de escribir.
EXTRACT_MAX_TOKENS = 32000
# Esperas ante un 429 cuando Meta no indica cuánto esperar, y ante errores pasajeros.
RATE_WAITS = (20, 40, 80, 160, 300)
ERROR_WAITS = (10, 30)
MAX_RETRY_AFTER = 600
MAX_PACE = 30.0
_QUOTA_HINTS = ("insufficient", "quota", "billing", "balance", "credit", "payment", "saldo")
# Pausa entre llamadas de extracción; crece cuando Meta responde 429 y se relaja si todo va bien.
_pace = {"seconds": 0.0}


def _retry_after(headers):
    try:
        return min(float(headers.get("Retry-After")), MAX_RETRY_AFTER)
    except (TypeError, ValueError, AttributeError):
        return None


def _meta_request(messages, key, model, max_tokens):
    """Una petición a Meta. Devuelve la respuesta ya decodificada o lanza un error clasificado."""
    request = urllib.request.Request(
        META_URL,
        data=json.dumps({"model": model, "messages": messages, "max_completion_tokens": max_tokens}).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=240) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        try:
            detail = error.read().decode("utf-8", "replace")[:2000].lower()
        except Exception:
            detail = ""
        finally:
            error.close()
        access = {401: "La clave API no es válida.", 403: "Tu cuenta no tiene acceso a este modelo.",
                  404: "No se encuentra el modelo. Revisa su nombre en los ajustes."}
        if error.code in access:
            raise MetaAccessError(access[error.code]) from None
        if error.code == 429:
            if any(hint in detail for hint in _QUOTA_HINTS):
                raise MetaAccessError("Meta indica que se ha agotado el saldo o la cuota de tu cuenta.") from None
            raise MetaRateLimit("Meta está limitando las peticiones.", _retry_after(error.headers)) from None
        if error.code == 400 and "token" in detail:
            raise ValueError("max_tokens") from None
        raise RuntimeError(f"Meta devolvió un error ({error.code}). Inténtalo más tarde.") from None


def call_meta(prompt, key, model, system=None, usage=None):
    """Una llamada a Meta para el informe. Si se pasa `usage` (dict), se rellena con los tokens usados."""
    messages = [{"role": "system", "content": system or digest.SYSTEM}, {"role": "user", "content": prompt}]
    try:
        data = _meta_request(messages, key, model, REPORT_MAX_TOKENS)
    except MetaRateLimit:
        raise MetaAccessError("Meta ha alcanzado el límite de uso o saldo de tu cuenta. Inténtalo más tarde.") from None
    except ValueError:
        raise RuntimeError("Meta devolvió un error (400). Inténtalo más tarde.") from None
    if usage is not None and isinstance(data.get("usage"), dict):
        usage.update(data["usage"])
    choice = data["choices"][0]
    if choice.get("finish_reason") == "length":
        raise RuntimeError("Meta alcanzó el límite de respuesta. No se ha guardado un informe incompleto; prueba el modo prompt.")
    result = choice["message"].get("content", "")
    if not isinstance(result, str) or not result.strip():
        raise RuntimeError("Meta no devolvió texto. Prueba otra vez o utiliza el modo prompt.")
    return result.strip()


def call_meta_extract(system, user, key, model, usage=None, notify=None):
    """Una llamada a Meta para la extracción, paciente con los límites de peticiones.

    - Ante un 429 espera lo que diga Meta (o cada vez más) y reintenta; además, deja una pausa
      entre llamadas que crece con cada 429 y se relaja cuando deja de haberlos.
    - Si la respuesta se corta por longitud, la devuelve tal cual: el extractor rescata las
      afirmaciones completas en lugar de perder el tramo.
    - Solo renuncia (MetaRateLimit) si Meta sigue limitando tras todas las esperas.
    """
    import time

    notify = notify or (lambda message: None)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    max_tokens = EXTRACT_MAX_TOKENS
    limited = errors = 0
    if _pace["seconds"] >= 1:
        time.sleep(_pace["seconds"])
    while True:
        try:
            data = _meta_request(messages, key, model, max_tokens)
            break
        except ValueError:  # el modelo no admite una respuesta tan larga
            if max_tokens == REPORT_MAX_TOKENS:
                raise RuntimeError("Meta devolvió un error (400). Inténtalo más tarde.") from None
            max_tokens = REPORT_MAX_TOKENS
        except MetaRateLimit as error:
            _pace["seconds"] = min(max(_pace["seconds"] * 2, 4.0), MAX_PACE)
            if limited >= len(RATE_WAITS):
                raise
            wait = error.retry_after or RATE_WAITS[limited]
            limited += 1
            notify(f"Meta limita las peticiones: espero {int(wait)} s y sigo (intento {limited} de {len(RATE_WAITS)})…")
            time.sleep(wait)
        except (MetaAccessError, KeyboardInterrupt):
            raise
        except (RuntimeError, OSError) as error:  # 5xx, red, tiempo de espera
            if errors >= len(ERROR_WAITS):
                raise RuntimeError(str(error) or "No se pudo contactar con Meta.") from None
            wait = ERROR_WAITS[errors]
            errors += 1
            notify(f"Meta no responde bien: espero {wait} s y reintento…")
            time.sleep(wait)
    if not limited:
        _pace["seconds"] = _pace["seconds"] * 0.8 if _pace["seconds"] >= 1.25 else 0.0
    if usage is not None and isinstance(data.get("usage"), dict):
        usage.update(data["usage"])
    result = data["choices"][0]["message"].get("content", "")
    if not isinstance(result, str) or not result.strip():
        raise RuntimeError("Meta no devolvió texto en este tramo.")
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


CHARS_PER_TOKEN = 3.5


def _number(options, name):
    try:
        return max(0.0, float(str(options.get(name) or "0").replace(",", ".")))
    except ValueError:
        return 0.0


def extraction_backend(options):
    """Modelo que extrae las afirmaciones, con contador de consumo y tope de gasto por tanda.

    Muse Spark (clave de Meta): el gasto se calcula con los tokens que informa la API y los
    precios de los ajustes. Claude Code: se usa el coste que informa el propio CLI.
    """
    from rezme import backends

    engine = options.get("engine") or "meta"
    usage = {}
    if engine == "meta":
        key, model = options.get("key", "").strip(), options.get("model") or "muse-spark-1.3"
        if not key:
            raise ValueError("Introduce tu clave de Meta en los ajustes.")

        def ask(system, user):
            try:
                return call_meta_extract(system, user, key, model, usage,
                                         notify=lambda message: emit("progress", message=message))
            except MetaAccessError as error:
                raise backends.BackendUnavailable(str(error)) from None
            except MetaRateLimit:
                raise backends.BackendUnavailable(
                    "Meta sigue limitando las peticiones después de varias esperas. Vuelve a lanzar la "
                    "extracción más tarde: continuará donde lo dejó.") from None
        backend = backends.Backend("meta", model, ask)
    elif engine == "claude-code":
        if backends.check_backend("claude-code"):
            raise ValueError("No encuentro Claude Code en este Mac. Elige Muse Spark en los ajustes o instala el CLI `claude`.")
        backend = backends.make_backend("claude-code")
        ask = backend.call
    else:
        raise ValueError("Motor de extracción no válido.")

    budget, price_in, price_out = (_number(options, name) for name in ("budget", "price_in", "price_out"))
    spent = {"cost": 0.0, "reached": False}

    def call(system, user):
        if budget and spent["cost"] >= budget:
            spent["reached"] = True
            raise backends.BudgetExceeded("presupuesto de la tanda alcanzado")
        usage.clear()
        reported = backend.cost_usd or 0.0
        text = ask(system, user)
        tokens_in = int(usage.get("prompt_tokens") or (len(system) + len(user)) / CHARS_PER_TOKEN)
        tokens_out = int(usage.get("completion_tokens") or len(text) / CHARS_PER_TOKEN)
        backend.input_tokens += tokens_in
        backend.output_tokens += tokens_out
        if engine == "claude-code" and backend.cost_usd is not None:
            cost = backend.cost_usd - reported           # lo informa el CLI
        else:
            cost = (tokens_in * price_in + tokens_out * price_out) / 1e6
            backend.cost_usd = reported + cost
        spent["cost"] += cost
        emit("spend", cost=round(spent["cost"], 4), tokens_in=backend.input_tokens,
             tokens_out=backend.output_tokens, budget=budget)
        return text

    backend.call = call
    return backend, spent


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
            backend, spent = extraction_backend(options)
            ids = [int(part) for part in str(options.get("ids") or "").split(",") if part.strip().isdigit()]

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
                                      on_change=lambda: emit_queue(store), only=ids or None)
            emit_queue(store)
            money = f"{spent['cost']:.2f} $"
            if spent["reached"]:
                emit("done", message=f"Tope de gasto alcanzado ({money}). Lo extraído se conserva y el resto sigue en cola.")
                return
            if summary.stopped:
                raise RuntimeError(f"Extracción detenida: {summary.stopped}")
            emit("done", message=batch.format_summary(summary) + f" · gasto de la tanda {money}")
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


def _calibration(store):
    """Caracteres por token de entrada y tokens de salida por carácter de transcripción, medidos
    en las extracciones ya hechas. Sin datos: 3,5 caracteres por token y salida ≈ transcripción."""
    chars_prompt = tokens_in = chars_text = tokens_out = runs = 0
    for row in store.db.execute("SELECT stats_json FROM extraction_runs"):
        usage = json.loads(row["stats_json"] or "{}").get("consumo") or {}
        if usage.get("entrada") and usage.get("salida") and usage.get("caracteres_transcripcion"):
            chars_prompt += usage["caracteres_prompt"]
            tokens_in += usage["entrada"]
            chars_text += usage["caracteres_transcripcion"]
            tokens_out += usage["salida"]
            runs += 1
    if not runs:
        return CHARS_PER_TOKEN, 1 / CHARS_PER_TOKEN, 0
    # Los reintentos cuentan como entrada pero no como caracteres: la estimación queda algo alta.
    return min(chars_prompt / tokens_in, CHARS_PER_TOKEN * 1.5), tokens_out / chars_text, runs


TYPE_LABELS = {
    "fact": "Hecho", "statistic": "Dato", "study_result": "Estudio", "causal_claim": "Causa y efecto",
    "forecast": "Previsión", "opinion": "Opinión", "own_calculation": "Cálculo del autor",
    "recommendation": "Recomendación", "risk": "Riesgo", "catalyst": "Catalizador",
    "methodology": "Método", "definition": "Definición"}
DIRECTION_LABELS = {"positive": "positivo", "negative": "negativo", "mixed": "mixto", "unclear": "incierto"}
BASIS_LABELS = {"stated_by_source": "lo dice el autor", "inferred_by_system": "deducido por el modelo"}


def source_detail(store, source_id):
    """Afirmaciones de un vídeo (última extracción), verificadas y no, listas para mostrar."""
    source = store.get_source_by_id(source_id)
    if source is None:
        raise ValueError("Ese vídeo ya no está en la base.")
    run = store.latest_run(source_id)
    claims = []
    for row in (store.claims_for_source(source_id, run_id=run["id"]) if run else []):
        if row["status"] not in ("verified", "ungrounded"):
            continue
        metric = ""
        if row["metric_value"] is not None:
            metric = f"{row['metric_value']:g}".replace(".", ",") + (f" {row['metric_unit']}" if row["metric_unit"] else "")
        link = ""
        if row["ts_start"] is not None and source["platform"] == "youtube":
            link = f"https://www.youtube.com/watch?v={source['external_id']}&t={int(row['ts_start'])}s"
        implications = []
        for item in store.implications_for(row["id"]):
            text = f"{item['target_label']}: {DIRECTION_LABELS.get(item['direction'], item['direction'])}"
            if item["mechanism"]:
                text += f" — {item['mechanism']}"
            implications.append(text + f" ({BASIS_LABELS.get(item['basis'], item['basis'])})")
        claims.append({
            "id": row["id"], "verified": row["status"] == "verified", "statement": row["statement"],
            "kind": TYPE_LABELS.get(row["type"], row["type"]), "entity": row["entity_name"] or "",
            "metric": metric, "time": digest.hms(row["ts_start"]) if row["ts_start"] is not None else "",
            "link": link, "quote": row["quote"] or "",
            "reasons": row["attrs"].get("grounding", {}).get("motivos", []),
            "implications": implications})
    return {"id": source_id, "title": source["title"] or source["external_id"], "url": source["url"] or "",
            "claims": claims}


def run_library(options):
    """Lo que hay en la base: recuentos, vídeos guardados, coste estimado de extraer y búsqueda."""
    from rezme import Store, prompts
    from rezme.batch import ORIGIN_LABELS
    from rezme.chunking import chunk_transcript, render

    if not options.get("db"):
        raise ValueError("No se encuentra la base de datos de reZme.")
    with Store(options["db"]) as store:
        stats = store.stats()
        by_status = stats["claims_by_status"]
        chars_per_token, out_per_char, calibrated = _calibration(store)
        overhead = len(prompts.system_prompt()) + len(prompts.load("user"))
        waiting = {job["video_id"]: job for job in store.pending_jobs(stages=("extract",))}
        sources = []
        for row in store.list_sources():
            if row["n_cues"] is None:
                continue
            frases = f"{row['n_cues']:,}".replace(",", ".")
            item = {
                "id": row["id"], "title": row["title"] or row["external_id"],
                "channel": row["channel"] or "", "date": row["published_at"] or "",
                "detail": f"{frases} frases · {ORIGIN_LABELS.get(row['origin'], row['origin'])}",
                "verified": row["verified"], "url": row["url"] or ""}
            job = waiting.get(row["external_id"])
            if job:  # falta extraer: llamadas y tokens estimados
                source = store.get_source_by_id(row["id"])
                chapters = json.loads(source["chapters_json"]) if source.get("chapters_json") else None
                chunks = chunk_transcript(store.latest_transcript(row["id"])["cues"], chapters,
                                          source.get("duration_s"))
                text = sum(len(render(chunk)) for chunk in chunks)
                item.update(job=job["id"], calls=len(chunks),
                            tokens_in=round((len(chunks) * overhead + text) / chars_per_token),
                            tokens_out=round(text * out_per_char))
            sources.append(item)
        pending = [item for item in sources if "job" in item]
        emit("library", sources=sources, stats={
            "videos": len(sources), "verified": by_status.get("verified", 0),
            "ungrounded": by_status.get("ungrounded", 0), "entities": stats["entities"],
            "to_extract": len(pending), "calls": sum(item["calls"] for item in pending),
            "calibrated": calibrated})
        if str(options.get("source") or "").isdigit():
            emit("detail", **source_detail(store, int(options["source"])))
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
