"""CLI de reZme fase 2:  python -m rezme {ingest,extract,claims,eval,queue,stats,search} ..."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .store import Store

DEFAULT_DB = Path(__file__).resolve().parent.parent / "rezme_data" / "rezme.db"


def db_path(arg: str | None) -> str:
    return arg or os.environ.get("REZME_DB") or str(DEFAULT_DB)


def cmd_ingest(args: argparse.Namespace) -> int:
    from .ingest import ingest_url

    langs = [l.strip() for l in args.lang.split(",") if l.strip()]
    with Store(db_path(args.db)) as store:
        try:
            res = ingest_url(store, args.url, langs, force=args.force, whisper=args.whisper,
                             whisper_model=args.whisper_model, cookies_from=args.cookies_from)
        except (ValueError, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    state = "ya estaba ingerido (sin descargar)" if res.skipped_download else (
        "transcripción nueva guardada" if res.new_transcript else "sin cambios en la transcripción")
    print(f"✓ {res.video_id}: {state} · origen={res.origin} · {res.n_cues} cues")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    with Store(db_path(args.db)) as store:
        print(json.dumps(store.stats(), indent=2, ensure_ascii=False))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    with Store(db_path(args.db)) as store:
        rows = store.search_claims(
            args.query, status=None if args.any_status else "verified", domain=args.domain,
            known_at=args.known_at, include_expired=args.include_expired, limit=args.limit)
    for r in rows:
        print(f"#{r['id']} [{r['status']}] {r['type']} · {r.get('entity_name') or '—'} · "
              f"{r['statement']}")
    if not rows:
        print("Sin resultados.", file=sys.stderr)
    return 0


def _hms(seconds: float | None) -> str:
    from .chunking import hms
    return hms(seconds) if seconds is not None else "--:--:--"


def _source_id(store: Store, ref: str) -> int | None:
    """Id de fuente a partir de un id numérico o de una URL ya ingerida."""
    from .ingest import PLATFORM, video_id_from_url

    if ref.isdigit():
        return int(ref) if store.get_source_by_id(int(ref)) else None
    video_id = video_id_from_url(ref)
    source = store.get_source(PLATFORM, video_id) if video_id else None
    return source["id"] if source else None


def cmd_extract(args: argparse.Namespace) -> int:
    from .backends import check_backend, make_backend
    from .extract import ExtractionError, extract_source
    from .ingest import ingest_url, video_id_from_url

    problem = check_backend(args.backend)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    with Store(db_path(args.db)) as store:
        source_id = _source_id(store, args.source)
        try:
            if source_id is None:
                if args.source.isdigit() or not video_id_from_url(args.source):
                    print("No existe esa fuente. Indica un id de fuente o una URL válida de YouTube.",
                          file=sys.stderr)
                    return 1
                print("→ Vídeo sin ingerir: descargando la transcripción…", file=sys.stderr)
                langs = [l.strip() for l in args.lang.split(",") if l.strip()]
                source_id = ingest_url(store, args.source, langs).source_id
            print(f"→ Extrayendo la fuente {source_id} con {args.backend}…", file=sys.stderr)
            res = extract_source(
                store, source_id, make_backend(args.backend, ollama_model=args.ollama_model),
                domain=None if args.domain == "auto" else args.domain, force=args.force,
                progress=lambda msg: print(msg, file=sys.stderr))
        except (ValueError, RuntimeError, ExtractionError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    origin = "run reutilizado" if res.reused_run else "run nuevo"
    print(f"✓ Fuente {source_id} · run {res.run_id} ({origin}, prompt {res.prompt_version}) · "
          f"{res.chunks_processed}/{res.chunks} tramos procesados, {res.chunks_skipped} ya hechos")
    print(f"  {res.claims_new} afirmaciones nuevas: {res.verified} verificadas, "
          f"{res.ungrounded} sin anclar · {res.implications} implicaciones · "
          f"{res.discarded} elementos descartados · {res.llm_calls} llamadas al modelo")
    if res.superseded:
        print(f"  {res.superseded} afirmaciones de extracciones anteriores pasan a «superseded».")
    if res.chunks_failed:
        print(f"  {res.chunks_failed} tramos fallaron; vuelve a lanzar el comando para reanudar.",
              file=sys.stderr)
        return 1
    return 0


def cmd_claims(args: argparse.Namespace) -> int:
    with Store(db_path(args.db)) as store:
        source_id = _source_id(store, args.source)
        if source_id is None:
            print("No existe esa fuente.", file=sys.stderr)
            return 1
        run_id = args.run
        if run_id is None and not args.all_runs:
            run = store.latest_run(source_id)
            run_id = run["id"] if run else None
        rows = store.claims_for_source(source_id, run_id=run_id, status=args.status)
        if args.json:
            for r in rows:
                r["implications"] = store.implications_for(r["id"])
            print(json.dumps(rows, indent=2, ensure_ascii=False))
            return 0
        for r in rows:
            metric = ""
            if r["metric_value"] is not None:
                metric = f" · {r['metric_value']:g} {r['metric_unit'] or ''}".rstrip()
            print(f"#{r['id']} [{r['status']}] {_hms(r['ts_start'])} {r['type']} · "
                  f"{r.get('entity_name') or '—'}{metric}")
            print(f"    {r['statement']}")
            if r["quote"]:
                print(f"    «{r['quote']}»")
            for reason in r["attrs"].get("grounding", {}).get("motivos", []):
                print(f"    ✗ {reason}")
            note = r["attrs"].get("entidad")
            if note:
                print(f"    ? entidad {note['nombre']!r}: {note['motivo']}")
            for i in store.implications_for(r["id"]):
                print(f"    → {i['target_label']}: {i['direction']} ({i['basis']})")
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        summary = ", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "sin afirmaciones"
        print(f"Fuente {source_id}" + (f" · run {run_id}" if run_id else "") + f": {summary}",
              file=sys.stderr)
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from .evaluate import evaluate_path
    from .schema import ValidationError

    with Store(db_path(args.db)) as store:
        try:
            reports = evaluate_path(store, args.path)
        except (ValueError, ValidationError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    if args.json:
        print(json.dumps(reports, indent=2, ensure_ascii=False))
    else:
        pct = lambda v: "   —  " if v is None else f"{v:6.1%}"
        print(f"{'vídeo':<13}{'recall':>8}{'precis.':>9}{'ground.':>9}{'cifras':>8}  detalle")
        for r in reports:
            if r["estado"] != "ok":
                print(f"{r['video_id']:<13}{'sin extraer: lanza primero `rezme extract`':>44}")
                continue
            note = "" if r["exhaustivo"] else " (precisión orientativa: fichero no exhaustivo)"
            print(f"{r['video_id']:<13}{pct(r['recall']):>8}{pct(r['precision']):>9}"
                  f"{pct(r['grounding']):>9}{pct(r['exactitud_numerica']):>8}  "
                  f"{r['emparejadas']}/{r['esperadas']} esperadas, {r['verificadas']} verificadas, "
                  f"{r['no_ancladas']} sin anclar{note}")
            if r["no_encontradas"]:
                print(f"{'':13}no encontradas: {', '.join(r['no_encontradas'])}")
            for w in r["cifras_distintas"]:
                print(f"{'':13}cifra distinta en {w['id']}: esperado {w['esperado']}, "
                      f"extraído {w['extraido']} (#{w['claim_id']})")
    if not reports:
        print("No hay ficheros de expectativas.", file=sys.stderr)
        return 1
    return 0 if any(r["estado"] == "ok" for r in reports) else 1


def cmd_queue_add(args: argparse.Namespace) -> int:
    from . import batch

    urls = list(args.urls)
    try:
        if args.file:
            urls += batch.read_urls(Path(args.file).read_text(encoding="utf-8").splitlines())
        if args.stdin:
            urls += batch.read_urls(sys.stdin)
    except OSError as exc:
        print(f"No se pudo leer el fichero: {exc.strerror or exc}", file=sys.stderr)
        return 1
    if not urls:
        print("Indica al menos una URL, o usa --file o --stdin.", file=sys.stderr)
        return 1
    with Store(db_path(args.db)) as store:
        try:
            report = batch.add_urls(store, urls, priority=args.priority,
                                    whole_playlist=args.playlist, cookies_from=args.cookies_from)
        except Exception as exc:  # yt-dlp al expandir una lista
            print(f"No se pudo leer la lista: {batch.classify_error(exc)[1]}", file=sys.stderr)
            return 1
    line = f"✓ {report.added} añadidos · {report.already} ya estaban"
    if report.queued_for_extract:
        line += f" ({report.queued_for_extract} ya ingeridos quedan en cola solo para extracción)"
    line += f" · {len(report.inaccessible)} no accesibles"
    print(line)
    for name, reason in report.inaccessible:
        print(f"  ✗ {name}: {reason}")
    for raw in report.invalid:
        print(f"  ✗ URL no válida: {raw}", file=sys.stderr)
    return 1 if report.invalid and not (report.added or report.already) else 0


def cmd_queue_run(args: argparse.Namespace) -> int:
    from . import batch

    if args.no_whisper and args.whisper_only:
        print("--no-whisper y --whisper-only son incompatibles.", file=sys.stderr)
        return 1
    deps = batch.Deps()
    if args.stage == "all":
        try:
            from .backends import check_backend
            problem = check_backend(args.backend)
        except ImportError:
            problem = None
        if problem:
            print(problem, file=sys.stderr)
            return 1
        deps.extractor = batch.load_extractor(
            args.backend, domain=None if args.domain == "auto" else args.domain,
            ollama_model=args.ollama_model)
        if deps.extractor is None:
            print("La extracción aún no está disponible: se ejecuta solo la ingesta.",
                  file=sys.stderr)
    langs = [l.strip() for l in args.lang.split(",") if l.strip()]
    with Store(db_path(args.db)) as store:
        summary = batch.run_queue(
            store, limit=args.limit, delay=args.delay, stage=args.stage,
            no_whisper=args.no_whisper, whisper_only=args.whisper_only, langs=langs,
            whisper_model=args.whisper_model, cookies_from=args.cookies_from, deps=deps,
            out=lambda msg: print(msg, flush=True))
    print(batch.format_summary(summary))
    return 130 if summary.interrupted else 0


_STATUS_LABELS = {"pending": "pendientes", "running": "en curso", "done": "hechos",
                  "failed": "fallidos", "skipped": "saltados"}


def _job_line(job: dict) -> str:
    line = (f"#{job['id']} [{job['status']}/{job['stage']}] {job['title'] or job['video_id']} "
            f"· {job['url']}")
    if job["priority"]:
        line += f" · prioridad {job['priority']}"
    if job["last_error"]:
        line += f" · {job['last_error']}"
    elif job["notes"]:
        line += f" · {job['notes']}"
    return line


def cmd_queue_status(args: argparse.Namespace) -> int:
    with Store(db_path(args.db)) as store:
        counts = store.job_counts()
        pending = store.pending_jobs(limit=5)
        failed = store.list_jobs("failed")[-5:]
    if not counts:
        print("La cola está vacía.")
        return 0
    for status, label in _STATUS_LABELS.items():
        ingest, extract = counts.get(f"{status}/ingest", 0), counts.get(f"{status}/extract", 0)
        if status == "pending":
            print(f"pendientes: {ingest} de ingesta · {extract} de extracción")
        else:
            print(f"{label}: {ingest + extract}")
    if pending:
        print("Próximos:")
        for job in pending:
            print("  " + _job_line(job))
    if failed:
        print("Últimos errores:")
        for job in failed:
            print("  " + _job_line(job))
    return 0


def cmd_queue_list(args: argparse.Namespace) -> int:
    with Store(db_path(args.db)) as store:
        jobs = store.list_jobs(args.status)
    for job in jobs:
        print(_job_line(job))
    if not jobs:
        print("Sin trabajos.", file=sys.stderr)
    return 0


def cmd_queue_retry(args: argparse.Namespace) -> int:
    chosen = [bool(args.failed), bool(args.skipped), args.id is not None]
    if sum(chosen) != 1:
        print("Indica --failed, --skipped o el ID de un trabajo.", file=sys.stderr)
        return 1
    with Store(db_path(args.db)) as store:
        if args.id is not None:
            n = store.retry_jobs(job_id=args.id)
        else:
            n = store.retry_jobs(status="failed" if args.failed else "skipped")
    print(f"✓ {n} trabajos vuelven a la cola.")
    return 0 if n or args.id is None else 1


def cmd_queue_remove(args: argparse.Namespace) -> int:
    with Store(db_path(args.db)) as store:
        removed = store.remove_job(args.id)
    if not removed:
        print(f"El trabajo {args.id} no existe.", file=sys.stderr)
        return 1
    print(f"✓ Trabajo {args.id} quitado de la cola (lo ya guardado en la base no se toca).")
    return 0


def cmd_queue_clear(args: argparse.Namespace) -> int:
    if not args.done:
        print("Indica --done: solo se pueden limpiar los trabajos terminados.", file=sys.stderr)
        return 1
    with Store(db_path(args.db)) as store:
        n = store.clear_done_jobs()
    print(f"✓ {n} trabajos terminados quitados de la cola. Transcripciones y afirmaciones intactas.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="rezme", description="Base de conocimiento de reZme (fase 2).")
    p.add_argument("--db", help="Ruta de la base SQLite (o variable REZME_DB)")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("ingest", parents=[common], help="Descarga y guarda la transcripción cruda de un vídeo")
    i.add_argument("url")
    i.add_argument("--lang", default="es,en")
    i.add_argument("--force", action="store_true", help="Volver a descargar aunque ya exista")
    i.add_argument("--whisper", action="store_true", help="Forzar transcripción con Whisper")
    i.add_argument("--whisper-model", default="small")
    i.add_argument("--cookies-from", metavar="NAVEGADOR")
    i.set_defaults(func=cmd_ingest)

    x = sub.add_parser("extract", parents=[common],
                       help="Extrae y verifica las afirmaciones de un vídeo")
    x.add_argument("source", metavar="url|source_id")
    x.add_argument("--backend", default="claude-code", choices=["claude-code", "api", "ollama"])
    x.add_argument("--ollama-model", default="qwen3:14b")
    x.add_argument("--domain", default="auto",
                   choices=["auto", "macro", "empresa", "ciencia", "cripto"],
                   help="Guía de dominio del prompt (auto = todas)")
    x.add_argument("--force", action="store_true",
                   help="Crear un run nuevo aunque ya exista uno igual")
    x.add_argument("--lang", default="es,en", help="Idiomas si hay que ingerir el vídeo")
    x.set_defaults(func=cmd_extract)

    c = sub.add_parser("claims", parents=[common],
                       help="Revisar lo extraído de una fuente, con su estado")
    c.add_argument("source", metavar="url|source_id")
    c.add_argument("--status", choices=["candidate", "verified", "ungrounded", "rejected",
                                        "superseded"])
    c.add_argument("--run", type=int, help="Run concreto (por defecto, el último)")
    c.add_argument("--all-runs", action="store_true", help="Incluir el histórico de runs")
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_claims)

    e = sub.add_parser("eval", parents=[common],
                       help="Métricas contra el conjunto de prueba (evals/golden)")
    e.add_argument("path", nargs="?", help="Fichero o carpeta de expectativas")
    e.add_argument("--json", action="store_true")
    e.set_defaults(func=cmd_eval)

    queue = sub.add_parser("queue", parents=[common], help="Cola de procesamiento por lotes")
    qsub = queue.add_subparsers(dest="queue_cmd", required=True)

    qa = qsub.add_parser("add", parents=[common], help="Encolar URLs o listas de reproducción")
    qa.add_argument("urls", nargs="*", metavar="URL")
    qa.add_argument("--file", metavar="FICHERO", help="Una URL por línea; # para comentarios")
    qa.add_argument("--stdin", action="store_true", help="Leer URLs de la entrada estándar")
    qa.add_argument("--priority", type=int, default=0, help="Mayor número = antes")
    qa.add_argument("--playlist", action="store_true",
                    help="Expandir la lista entera aunque la URL sea de un vídeo con list=")
    qa.add_argument("--cookies-from", metavar="NAVEGADOR", help="Para listas privadas")
    qa.set_defaults(func=cmd_queue_add)

    qr = qsub.add_parser("run", parents=[common], help="Procesar la cola, vídeo a vídeo")
    qr.add_argument("--limit", type=int, metavar="N", help="Máximo de vídeos en esta pasada")
    qr.add_argument("--delay", type=float, metavar="SEG",
                    help="Pausa entre vídeos (por defecto, 5-10 s al azar)")
    qr.add_argument("--stage", default="ingest", choices=["ingest", "all"],
                    help="ingest = solo transcripción; all = también extracción y verificación")
    qr.add_argument("--no-whisper", action="store_true",
                    help="No transcribir audio: los vídeos sin subtítulos quedan saltados")
    qr.add_argument("--whisper-only", action="store_true",
                    help="Procesar solo los saltados por falta de subtítulos, con Whisper")
    qr.add_argument("--whisper-model", default="small")
    qr.add_argument("--cookies-from", metavar="NAVEGADOR")
    qr.add_argument("--lang", default="es,en")
    qr.add_argument("--backend", default="claude-code", choices=["claude-code", "api", "ollama"])
    qr.add_argument("--ollama-model", default="qwen3:14b")
    qr.add_argument("--domain", default="auto",
                    choices=["auto", "macro", "empresa", "ciencia", "cripto"])
    qr.set_defaults(func=cmd_queue_run)

    qs = qsub.add_parser("status", parents=[common], help="Recuento, próximos y últimos errores")
    qs.set_defaults(func=cmd_queue_status)

    ql = qsub.add_parser("list", parents=[common], help="Listar trabajos")
    ql.add_argument("--status", choices=["pending", "running", "done", "failed", "skipped"])
    ql.set_defaults(func=cmd_queue_list)

    qt = qsub.add_parser("retry", parents=[common], help="Devolver trabajos a la cola")
    qt.add_argument("id", nargs="?", type=int, metavar="ID")
    qt.add_argument("--failed", action="store_true")
    qt.add_argument("--skipped", action="store_true")
    qt.set_defaults(func=cmd_queue_retry)

    qm = qsub.add_parser("remove", parents=[common], help="Quitar un trabajo de la cola")
    qm.add_argument("id", type=int, metavar="ID")
    qm.set_defaults(func=cmd_queue_remove)

    qc = qsub.add_parser("clear", parents=[common], help="Limpiar trabajos terminados")
    qc.add_argument("--done", action="store_true")
    qc.set_defaults(func=cmd_queue_clear)

    s = sub.add_parser("stats", parents=[common], help="Recuento de registros")
    s.set_defaults(func=cmd_stats)

    q = sub.add_parser("search", parents=[common], help="Buscar afirmaciones (solo verificadas y vigentes)")
    q.add_argument("query")
    q.add_argument("--domain")
    q.add_argument("--known-at", help="Fecha ISO: lo que se sabía ese día")
    q.add_argument("--include-expired", action="store_true")
    q.add_argument("--any-status", action="store_true", help="Incluir no verificadas (depuración)")
    q.add_argument("--limit", type=int, default=20)
    q.set_defaults(func=cmd_search)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
